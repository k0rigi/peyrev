"""Hedef: taşıma planını PostgreSQL'e tek işlemde yazar, kimlik dizilerini ilerletir ve sonucu aynı işlemde doğrular.

- prova (PROVA_DATABASE_URL): public şeması silinip yeniden kurulur, geçişler (peyrev.migrate) uygulanır, plan boş
  şemaya yazılır. Doğrulama tutmasa da yazılanlar incelemek için kalır.
- gercek (DATABASE_URL): yalnız kaynak='orkestrai' hesapları ve onların kayıtları eklenir ya da kaynakla eşitlenir
  (ON CONFLICT ... DO UPDATE, yalnız bir değer değiştiyse). Hedefte taşınmış hesap varsa (gerçek taşıma daha önce
  yapılmışsa) `again` verilmedikçe hiçbir şey yazılmaz. Peyrev'de doğan satırlara dokunulmaz: kimliği, giriş adı,
  talep numarası ya da (aynı sahipte) ofis adı hedefteki başka bir kayıtla çakışan kaynak kaydı yazılmaz ve sorun
  sayılır; Peyrev'de açılmış bekleyen başvuruyla aynı e-postalı eski bekleyen başvuru ve Peyrev'de karara bağlanmış
  (onay ya da ret) taşınmış başvuru atlanır. Okuma, yazma ve
  doğrulama tek REPEATABLE READ işlemidir: bir sorun ya da uyuşmazlık varsa işlemin tamamı geri alınır. Kimlik dizileri
  (setval geri alınamaz) yalnız doğrulama tuttuysa ilerletilir.

Yeniden çalıştırma (again): aynı kaynakla hedefte hiçbir satır değişmez. Daha önce taşınmış hesap kaynakta hakkını
yitirse de eşitlenir (hakları kalkar), kaynakta silinmişse pasif olur. Taşınmış kayıtlarda kaynaktan gelen her alan
kaynağın değerine döner (REVERTED); bu yüzden yeniden çalıştırma açıkça istenir. Hesabın alan_id'si ilk taşımadaki
değerinde kalır (Studio yerel ajan verisini onunla bölümler); Peyrev'de karara bağlanan başvurular korunur. Kaynakta
silinen kayıtlar (talep, mesaj, ofis, görev) hedeften silinmez: hangisinin Peyrev'de doğduğu ayırt edilemez.

Doğrulama yazılan her satırı hedeften geri okuyup planla karşılaştırır; planın yazmadığı bütün satırların (Peyrev'de
doğanlar) yazmadan önceki ve sonraki özetlerini karşılaştırır; şifre özetlerinin SHA-256 toplamını, ofis kimliklerini,
talep numaralarını ve her hesabın modül durumunu (users.module_state) beklenenle karşılaştırır. Sorun metinleri satır
içeriği taşımaz; PostgreSQL hatasından yalnız SQLSTATE, tablo ve kısıt adı alınır (hata metni anahtar değeri içerir).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg import IsolationLevel, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .. import migrate, users
from ..accounts.applications import PENDING
from ..agents.entitlement import MAX_OFFICES
from ..db import Database
from .plan import CONFLICT, MODULES, PEYREV_DECIDED, PEYREV_PENDING, TARGETS, Plan, build, label

PROVA, GERCEK = "prova", "gercek"
MODES = (PROVA, GERCEK)
LOCK = "peyrev-tasima"
ORKESTRAI = "orkestrai"
# Yeni talepler 1001'den başlar (0003); orkestrai'nin AUTO_INCREMENT'i de 1001'di.
TICKET_FLOOR = 1000
# Hedef tablo → anahtar kolonları ve PostgreSQL türleri. Yazma sırası TARGETS'tır (yabancı anahtarlar).
KEYS: dict[str, tuple[tuple[str, str], ...]] = {
    "kullanicilar": (("id", "bigint"),),
    "kullanici_modulleri": (("kullanici_id", "bigint"), ("modul", "text")),
    "basvurular": (("id", "text"),),
    "destek_talepleri": (("talep_no", "integer"),),
    "destek_mesajlari": (("id", "text"),),
    "destek_ekleri": (("id", "text"),),
    "ajan_ofisleri": (("id", "text"),),
    "ajan_gorevleri": (("id", "text"),),
    "ajan_onaylari": (("id", "text"),),
    "ajan_olaylari": (("id", "text"),),
}
# Hedefteki kayıtla çakışınca plandan çıkarılabilen tablolar (plan.build'in `blocked` sözlüğü).
BLOCKABLE = ("kullanicilar", "basvurular", "destek_talepleri", "ajan_ofisleri")
# Değişen satırın guncelleme damgasını yazıcı basar; diğer tablolarda damga planın bir kolonudur ya da yoktur.
STAMPED = ("kullanicilar", "kullanici_modulleri")
# Aynı kimlikli Peyrev hesabı önceden çakışma olarak ayıklanır; bu koşul yine de onu güncellemez.
GUARDS = {"kullanicilar": "kullanicilar.kaynak = 'orkestrai'"}
# Doğrulamada ekler geri okunmaz, içeriklerinin özeti karşılaştırılır.
READ_AS = {("destek_ekleri", "icerik"): "encode(sha256(icerik), 'hex')"}
# Peyrev'de doğan satırların özeti satır metninden; eklerde içerik yerine içeriğin özeti.
ROW_TEXT = {"destek_ekleri": "(t.id, t.talep_no, t.tur, t.ad, t.bicim, t.boyut, md5(t.icerik), t.olusturma)::text"}
# Geçici giriş adı: .invalid alan adı gerçek bir adreste bulunamaz.
SPARE_LOGIN = "'tasima.' || k.id || '@gecici.invalid'"


# Yeniden çalıştırmanın taşınmış kayıtlarda kaynağın değerine döndürdükleri (ret metni ve README aynı listeyi verir).
REVERTED = ("hesap durumu (Peyrev'de pasif yapılan hesap yeniden aktif olur) ve rolü, ad soyad, şifre, modül hakları ve "
            "süreleri; Peyrev'de silinen ofisler geri gelir, ofis adı ve durumu, görev ve onay durumları (sürüm "
            "geriler), talep durumu")


class TargetError(Exception):
    """Hedef hazır değil ya da kullanılamıyor. Metin adres, değer ya da satır içeriği taşımaz."""


@dataclass
class License:
    user_id: int
    module: str
    expected: tuple[str, date | None]
    actual: tuple[str, date | None] | None


@dataclass
class Sequence:
    name: str
    next_value: int
    advanced: bool


@dataclass
class Outcome:
    mode: str
    now: datetime
    again: bool = False
    database: str = ""
    plan: Plan | None = None
    # Uygun satırlardan hedefte birebir aynı olanlar ve hedef tablodaki bütün satırlar.
    matched: dict[str, int] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)
    # (kaynak toplamı, hedef toplamı, hesap sayısı)
    passwords: tuple[str, str, int] | None = None
    # (uygun, hedefte aynı kimlikle / numarayla bulunan)
    offices: tuple[int, int] = (0, 0)
    tickets: tuple[int, int] = (0, 0)
    licenses: list[License] = field(default_factory=list)
    sequences: list[Sequence] = field(default_factory=list)
    # Peyrev'de doğan (planın yazmadığı) satırları değişen tablolar; boş olmalı.
    disturbed: list[str] = field(default_factory=list)
    # Doğrulamanın bulduğu uyuşmazlıklar; çakışmalar ve kaynak sorunları plan.problems'tadır.
    problems: list[str] = field(default_factory=list)
    error: str | None = None
    verified: bool = False
    committed: bool = False

    @property
    def ok(self) -> bool:
        return (self.error is None and self.verified and self.plan is not None and not self.plan.problems
                and not self.problems)


def run(url: str, mode: str, tables: dict[str, list[dict[str, Any]]], now: datetime, *, again: bool = False) -> Outcome:
    """Planı kurar, yazar, doğrular. Hata fırlatmaz: veritabanı hatası Outcome.error'a güvenli metinle yazılır.
    `again`: gerçek kipte, hedefte taşınmış hesaplar varken de çalışır (taşınmış hesapları kaynakla yeniden eşitler)."""
    if mode not in MODES:
        raise ValueError("kip prova ya da gercek olmalı")
    outcome = Outcome(mode=mode, now=now, again=again)
    try:
        with psycopg.connect(url, autocommit=True, row_factory=dict_row) as connection:
            connection.execute("SET TIME ZONE 'UTC'")
            if not connection.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS ok",
                                      (LOCK,)).fetchone()["ok"]:
                raise TargetError("Bu veritabanında başka bir taşıma şu anda çalışıyor.")
            outcome.database = connection.execute("SELECT current_database() AS name").fetchone()["name"]
            if mode == PROVA:
                _reset(connection, url)
            # The snapshot is taken by the first statement of the transaction, after the lock is held.
            connection.isolation_level = IsolationLevel.REPEATABLE_READ
            _migrate(connection, tables, now, outcome, again=again)
    except TargetError as error:
        outcome.error = str(error)
    except psycopg.Error as error:
        outcome.error = "PostgreSQL: " + _describe(error)
    return outcome


def _reset(connection: psycopg.Connection, url: str) -> None:
    """Prova: public şeması boşaltılıp geçişlerle yeniden kurulur."""
    connection.execute("DROP SCHEMA IF EXISTS public CASCADE")
    connection.execute("CREATE SCHEMA public")
    database = Database(url, min_size=1, max_size=1)
    try:
        migrate.apply(database)
    finally:
        database.close()


def _migrate(connection: psycopg.Connection, tables: dict[str, list[dict[str, Any]]], now: datetime,
             outcome: Outcome, *, again: bool = False) -> None:
    rolled_back = False
    with connection.transaction() as transaction, connection.cursor() as cur:
        _require_schema(cur)
        if outcome.mode == GERCEK and not again:
            _require_first_run(cur)
        plan = outcome.plan = _plan(cur, tables, now)
        written = _written(plan)
        before = _others(cur, written)
        write(cur, plan)
        verify(cur, plan, outcome)
        after = _others(cur, written)
        outcome.disturbed = [table for table in TARGETS if before[table] != after[table]]
        outcome.problems += [f"{table}: Peyrev'de doğan satırlar değişti." for table in outcome.disturbed]
        outcome.verified = True
        # setval is not undone by a rollback: a real run that is going to be rolled back leaves the sequences alone.
        if outcome.ok or outcome.mode == PROVA:
            outcome.sequences = advance_sequences(cur, tables)
        _audit(cur, outcome)
        if not outcome.ok and outcome.mode == GERCEK:
            rolled_back = True
            raise psycopg.Rollback(transaction)
    outcome.committed = not rolled_back


def _require_schema(cur: psycopg.Cursor) -> None:
    cur.execute("SELECT to_regclass('schema_migrations') IS NOT NULL AS ready")
    if not cur.fetchone()["ready"]:
        raise TargetError("Hedefte şema yok (schema_migrations); önce geçişler uygulanmalı.")
    cur.execute("SELECT version FROM schema_migrations")
    applied = {row["version"] for row in cur.fetchall()}
    missing = [path.name for path in migrate.files() if path.name[:4] not in applied]
    if missing:
        raise TargetError("Hedef şeması güncel değil, uygulanmamış geçişler: " + ", ".join(missing)
                          + " (önce dağıtım yapılmalı).")


def _require_first_run(cur: psycopg.Cursor) -> None:
    cur.execute("SELECT count(*) AS n FROM kullanicilar WHERE kaynak = %s", (ORKESTRAI,))
    migrated = cur.fetchone()["n"]
    if migrated:
        raise TargetError(f"Gerçek taşıma bu veritabanında daha önce yapılmış ({migrated} taşınmış hesap); hiçbir şey "
                          "yazılmadı. Yeniden çalıştırmak taşınmış kayıtlarda Peyrev'de yapılan değişiklikleri kaynaktaki "
                          f"değerlere döndürür: {REVERTED}. Bilerek yapılıyorsa --yeniden ile (Sunucu işleri → "
                          "tasima-gercek-yeniden) çalıştırın.")


# --- Plan ve çakışmalar ------------------------------------------------------------------------------------------


def _plan(cur: psycopg.Cursor, tables: dict[str, list[dict[str, Any]]], now: datetime) -> Plan:
    """Hedefteki taşınmış hesaplarla kurulan plan; hedefle çakışan kayıtlar plandan çıkarılır. Bir kaydı çıkarmak
    başka bir kaydı (ör. aynı e-postalı daha yeni başvuruyu) plana sokabileceğinden çakışma kalmayana dek yinelenir."""
    cur.execute("SELECT id FROM kullanicilar WHERE kaynak = %s", (ORKESTRAI,))
    kept = {row["id"] for row in cur.fetchall()}
    blocked: dict[str, dict[Any, str]] = {table: {} for table in BLOCKABLE}
    problems: dict[tuple[str, Any], str] = {}
    while True:
        plan = build(tables, now, kept=kept, blocked=blocked)
        # A blocked key never re-enters the plan, so every round blocks something new and the loop ends.
        fresh = {place: found for place, found in _conflicts(cur, plan).items() if place[1] not in blocked[place[0]]}
        if not fresh:
            break
        for (table, key), (reason, problem) in fresh.items():
            blocked[table][key] = reason
            if problem:
                problems[(table, key)] = problem
    plan.problems.extend(problems.values())
    plan.problems.extend(_office_limits(cur, plan))
    return plan


def _conflicts(cur: psycopg.Cursor, plan: Plan) -> dict[tuple[str, Any], tuple[str, str | None]]:
    """Planın hedefteki başka bir kayıtla çakışan satırları: (tablo, anahtar) → (atlama nedeni, sorun metni)."""
    found: dict[tuple[str, Any], tuple[str, str | None]] = {}
    accounts = plan.tables["kullanicilar"].rows
    if accounts:
        ids = list(accounts)
        cur.execute("SELECT id, kaynak FROM kullanicilar WHERE id = ANY(%s::bigint[]) AND kaynak <> %s ORDER BY id",
                    (ids, ORKESTRAI))
        for row in cur.fetchall():
            text = (f"kullanicilar {row['id']}: hedefte aynı kimlikli, Peyrev'de açılmış bir hesap var "
                    f"(kaynak {label(row['kaynak'])}); kimlik korunamaz.")
            found[("kullanicilar", row["id"])] = (CONFLICT, text)
        # The holder is not one of the planned accounts (those get their own new login in this run).
        cur.execute("SELECT p.id, min(k.id) AS holder FROM unnest(%s::bigint[], %s::text[]) AS p(id, giris) "
                    "JOIN kullanicilar k ON lower(k.giris) = lower(p.giris) AND NOT (k.id = ANY(%s::bigint[])) "
                    "GROUP BY p.id ORDER BY p.id", (ids, [accounts[key]["giris"] for key in ids], ids))
        for row in cur.fetchall():
            text = (f"kullanicilar {row['id']}: giriş adı hedefteki {row['holder']} numaralı hesabınkiyle aynı "
                    "(büyük/küçük harf duyarsız).")
            found.setdefault(("kullanicilar", row["id"]), (CONFLICT, text))
    applications = plan.tables["basvurular"].rows
    if applications:
        # A migrated application decided in Peyrev, or linked to an account opened there, keeps Peyrev's state.
        cur.execute("SELECT b.id, b.durum, k.kaynak FROM basvurular b LEFT JOIN kullanicilar k ON k.id = b.kullanici_id "
                    "WHERE b.id = ANY(%s::text[]) ORDER BY b.id", (list(applications),))
        for row in cur.fetchall():
            decided_here = row["durum"] != PENDING and applications[row["id"]]["durum"] == PENDING
            if decided_here or (row["kaynak"] is not None and row["kaynak"] != ORKESTRAI):
                found[("basvurular", row["id"])] = (PEYREV_DECIDED, None)
    waiting = [key for key, values in applications.items() if values["durum"] == PENDING]
    if waiting:
        cur.execute("SELECT p.id FROM unnest(%s::text[], %s::text[]) AS p(id, eposta) WHERE EXISTS (SELECT 1 FROM "
                    "basvurular b WHERE b.durum = 'bekliyor' AND lower(b.eposta) = lower(p.eposta) "
                    "AND NOT (b.id = ANY(%s::text[])))",
                    (waiting, [applications[key]["eposta"] for key in waiting], list(applications)))
        for row in cur.fetchall():
            found[("basvurular", row["id"])] = (PEYREV_PENDING, None)
    tickets = plan.tables["destek_talepleri"].rows
    if tickets:
        cur.execute("SELECT talep_no, kullanici_id, olusturma FROM destek_talepleri WHERE talep_no = ANY(%s::integer[])"
                    " ORDER BY talep_no", (list(tickets),))
        for row in cur.fetchall():
            mine = tickets[row["talep_no"]]
            # Same number, same account and same creation time: the ticket a previous run wrote.
            if (row["kullanici_id"], row["olusturma"]) != (mine["kullanici_id"], mine["olusturma"]):
                text = (f"destek_talepleri {row['talep_no']}: bu numara hedefte başka bir talepte (Peyrev'de açılmış); "
                        "talep numarası korunamaz.")
                found[("destek_talepleri", row["talep_no"])] = (CONFLICT, text)
    offices = plan.tables["ajan_ofisleri"].rows
    if offices:
        ids = list(offices)
        cur.execute("SELECT p.id, min(o.id) AS other FROM unnest(%s::text[], %s::bigint[], %s::text[]) AS p(id, sahip_id,"
                    " ad) JOIN ajan_ofisleri o ON o.sahip_id = p.sahip_id AND lower(o.ad) = lower(p.ad) "
                    "AND NOT (o.id = ANY(%s::text[])) GROUP BY p.id ORDER BY p.id",
                    (ids, [offices[key]["sahip_id"] for key in ids], [offices[key]["ad"] for key in ids], ids))
        for row in cur.fetchall():
            text = (f"ajan_ofisleri {label(row['id'])}: sahibinin hedefte aynı adlı başka bir ofisi var "
                    f"({label(row['other'])}).")
            found[("ajan_ofisleri", row["id"])] = (CONFLICT, text)
    return found


def _office_limits(cur: psycopg.Cursor, plan: Plan) -> list[str]:
    """Sahip başına en çok MAX_OFFICES ofis, hedefte Peyrev'de kurulmuş ofisleriyle birlikte (yalnız kaynaktakiler
    sınırı aşıyorsa plan zaten bildirir)."""
    offices = plan.tables["ajan_ofisleri"].rows
    owners = Counter(values["sahip_id"] for values in offices.values())
    if not owners:
        return []
    cur.execute("SELECT sahip_id, count(*) AS n FROM ajan_ofisleri WHERE sahip_id = ANY(%s::bigint[]) "
                "AND NOT (id = ANY(%s::text[])) GROUP BY sahip_id ORDER BY sahip_id", (list(owners), list(offices)))
    return [f"ajan_ofisleri: {row['sahip_id']} numaralı hesabın taşımadan sonra {owners[row['sahip_id']] + row['n']} "
            f"ofisi olur (en çok {MAX_OFFICES})." for row in cur.fetchall()
            if owners[row["sahip_id"]] <= MAX_OFFICES < owners[row["sahip_id"]] + row["n"]]


# --- Yazma -------------------------------------------------------------------------------------------------------


def write(cur: psycopg.Cursor, plan: Plan) -> None:
    accounts = plan.tables["kullanicilar"].rows
    _fill_creation(cur, plan)
    _keep_areas(cur, accounts)
    _free_logins(cur, accounts)
    _upsert(cur, "kullanicilar", accounts)
    if plan.gone:
        cur.execute("UPDATE kullanicilar SET durum = 'pasif', rol = 'kullanici', guncelleme = now() "
                    "WHERE id = ANY(%s::bigint[]) AND kaynak = %s AND (durum, rol) IS DISTINCT FROM ('pasif', 'kullanici')",
                    (plan.gone, ORKESTRAI))
    _sync_modules(cur, plan)
    for table in TARGETS[2:]:
        _upsert(cur, table, plan.tables[table].rows)


def _fill_creation(cur: psycopg.Cursor, plan: Plan) -> None:
    """Kaynakta oluşturma zamanı boş hesap: hedefteki değer korunur, yeni hesaba taşıma anı yazılır."""
    accounts = plan.tables["kullanicilar"].rows
    missing = [key for key, values in accounts.items() if values["olusturma"] is None]
    if not missing:
        return
    cur.execute("SELECT id, olusturma FROM kullanicilar WHERE id = ANY(%s::bigint[])", (missing,))
    existing = {row["id"]: row["olusturma"] for row in cur.fetchall()}
    for key in missing:
        accounts[key]["olusturma"] = existing.get(key, plan.now)


def _keep_areas(cur: psycopg.Cursor, accounts: dict[Any, dict[str, Any]]) -> None:
    """Taşınmış hesabın alan_id'si değişmez (lisanstaki firma_id; Studio yerel ajan verisini onunla bölümler): kaynakta
    firması değişse de hedefteki değer yazılır, doğrulama da onu bekler."""
    if not accounts:
        return
    cur.execute("SELECT id, alan_id FROM kullanicilar WHERE id = ANY(%s::bigint[]) AND kaynak = %s",
                (list(accounts), ORKESTRAI))
    for row in cur.fetchall():
        accounts[row["id"]]["alan_id"] = row["alan_id"]


def _free_logins(cur: psycopg.Cursor, accounts: dict[Any, dict[str, Any]]) -> None:
    """Giriş adı değişecek taşınmış hesaplar önce geçici, kendine özgü bir ada alınır: iki hesap adlarını takas etse
    ya da biri ötekinin eski adını alsa da benzersizlik dizini ara adımda çakışmaz."""
    if not accounts:
        return
    ids = list(accounts)
    cur.execute(f"UPDATE kullanicilar k SET giris = {SPARE_LOGIN} FROM unnest(%s::bigint[], %s::text[]) AS p(id, giris)"
                " WHERE k.id = p.id AND k.kaynak = %s AND k.giris <> p.giris",
                (ids, [accounts[key]["giris"] for key in ids], ORKESTRAI))


def _sync_modules(cur: psycopg.Cursor, plan: Plan) -> None:
    """Taşınmış hesapların modül hakları kaynaktakinin aynısı olur: planda olmayan hakları silinir."""
    rows = plan.tables["kullanici_modulleri"].rows
    accounts = sorted(set(plan.tables["kullanicilar"].rows) | set(plan.gone))
    if accounts:
        cur.execute("SELECT m.kullanici_id, m.modul FROM kullanici_modulleri m JOIN kullanicilar k ON k.id = m.kullanici_id"
                    " WHERE m.kullanici_id = ANY(%s::bigint[]) AND k.kaynak = %s", (accounts, ORKESTRAI))
        extra = [(row["kullanici_id"], row["modul"]) for row in cur.fetchall()
                 if (row["kullanici_id"], row["modul"]) not in rows]
        if extra:
            cur.executemany("DELETE FROM kullanici_modulleri WHERE kullanici_id = %s AND modul = %s", extra)
    _upsert(cur, "kullanici_modulleri", rows)


def _upsert(cur: psycopg.Cursor, table: str, rows: dict[Any, dict[str, Any]]) -> None:
    """INSERT ... ON CONFLICT (anahtar) DO UPDATE; satır yalnız bir değeri farklıysa güncellenir (tekrar çalıştırma
    hiçbir satırı yeniden yazmaz)."""
    if not rows:
        return
    keys = [name for name, _ in KEYS[table]]
    columns = list(next(iter(rows.values())))
    updates = [sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(column), sql.Identifier(column)) for column in columns]
    if table in STAMPED:
        updates.append(sql.SQL("guncelleme = now()"))
    conditions = [sql.SQL(GUARDS[table])] if table in GUARDS else []
    conditions.append(sql.SQL("({}) IS DISTINCT FROM ({})").format(
        sql.SQL(", ").join(sql.Identifier(table, column) for column in columns),
        sql.SQL(", ").join(sql.SQL("EXCLUDED.{}").format(sql.Identifier(column)) for column in columns)))
    query = sql.SQL("INSERT INTO {table} ({names}) VALUES ({values}) ON CONFLICT ({keys}) DO UPDATE SET {updates} "
                    "WHERE {conditions}").format(
        table=sql.Identifier(table), names=sql.SQL(", ").join(map(sql.Identifier, keys + columns)),
        values=sql.SQL(", ").join(sql.Placeholder() * (len(keys) + len(columns))),
        keys=sql.SQL(", ").join(map(sql.Identifier, keys)), updates=sql.SQL(", ").join(updates),
        conditions=sql.SQL(" AND ").join(conditions))
    params = []
    for key, values in rows.items():
        parts = key if isinstance(key, tuple) else (key,)
        params.append([*parts, *(_adapt(table, column, values[column]) for column in columns)])
    cur.executemany(query, params)


def _adapt(table: str, column: str, value: Any) -> Any:
    return Jsonb(value) if (table, column) == ("ajan_olaylari", "ayrinti") else value


def advance_sequences(cur: psycopg.Cursor, tables: dict[str, list[dict[str, Any]]]) -> list[Sequence]:
    """Yeni hesap ve talepler taşınanların ve kaynaktaki (taşınmayanlar dahil) en büyük kimliğin üstünden sürer:
    sonradan hakkı açılan bir orkestrai hesabının kimliği Peyrev'de dolu olmaz. Diziler hiç geri alınmaz."""
    cur.execute("SELECT (SELECT coalesce(max(id), 0) FROM kullanicilar) AS users, "
                "(SELECT coalesce(max(alan_id), 0) FROM kullanicilar) AS areas, "
                "(SELECT coalesce(max(talep_no), 0) FROM destek_talepleri) AS tickets, "
                "pg_get_serial_sequence('destek_talepleri', 'talep_no') AS ticket_sequence")
    row = cur.fetchone()
    return [_advance(cur, "kullanici_id_seq", "kullanici_id_seq",
                     max(row["users"], _largest(tables, "SYS_Kullanicilar", "KullaniciID"))),
            _advance(cur, row["ticket_sequence"], "destek_talepleri.talep_no",
                     max(TICKET_FLOOR, row["tickets"], _largest(tables, "SYS_RpaTalepler", "TalepNo"))),
            # Taşınan alan_id'ler (eski FirmaID) dizinin başlangıcının (1000000) altında kaldıkça değişmez.
            _advance(cur, "alan_id_seq", "alan_id_seq", row["areas"])]


def _largest(tables: dict[str, list[dict[str, Any]]], table: str, column: str) -> int:
    return max((row[column] for row in tables.get(table) or [] if type(row[column]) is int), default=0)


def _advance(cur: psycopg.Cursor, sequence: str, name: str, floor: int) -> Sequence:
    # The sequence name is a constant or comes from pg_get_serial_sequence (already quoted).
    cur.execute(sql.SQL("SELECT last_value, is_called FROM {}").format(sql.SQL(sequence)))
    row = cur.fetchone()
    used = row["last_value"] if row["is_called"] else row["last_value"] - 1
    if floor > used:
        cur.execute("SELECT setval(%s::regclass, %s, true)", (sequence, floor))
        return Sequence(name, floor + 1, True)
    return Sequence(name, used + 1, False)


def _audit(cur: psycopg.Cursor, outcome: Outcome) -> None:
    """yonetim_olaylari'na sayılar (gerçek kipte başarısızsa işlemle birlikte geri alınır)."""
    plan = outcome.plan
    counts = {table: {"kaynak": rows.source, "uygun": len(rows.rows), "hedef": outcome.matched.get(table, 0),
                      "atlanan": sum(rows.skipped.values())} for table, rows in plan.tables.items()}
    cur.execute("INSERT INTO yonetim_olaylari (yonetici_id, tur, hedef, ayrinti, zaman) VALUES (NULL, 'tasima', %s, %s, %s)",
                (outcome.mode, Jsonb({"sonuc": "basarili" if outcome.ok else "basarisiz", "yeniden": outcome.again,
                                      "tablolar": counts, "pasif_yapilan": len(plan.gone)}), plan.now))


# --- Doğrulama ---------------------------------------------------------------------------------------------------


def verify(cur: psycopg.Cursor, plan: Plan, outcome: Outcome) -> None:
    for table in TARGETS:
        rows = plan.tables[table].rows
        found = _read_back(cur, table, rows)
        matched = 0
        for key, values in rows.items():
            actual = found.get(key)
            if actual is None:
                outcome.problems.append(f"{table} {label(key)}: hedefte yok.")
                continue
            differ = [column for column, value in values.items() if not _same(table, column, value, actual[column])]
            if differ:
                outcome.problems.append(f"{table} {label(key)}: hedefteki {', '.join(differ)} kaynakla uyuşmuyor.")
            else:
                matched += 1
        outcome.matched[table] = matched
        if matched != len(rows):
            outcome.problems.append(f"{table}: uygun {len(rows)} satırın {matched} tanesi hedefte birebir aynı.")
        cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table)))
        outcome.totals[table] = cur.fetchone()["n"]
        if table == "ajan_ofisleri":
            outcome.offices = (len(rows), len(found))
        elif table == "destek_talepleri":
            outcome.tickets = (len(rows), len(found))
    _verify_accounts(cur, plan, outcome)


def _read_back(cur: psycopg.Cursor, table: str, rows: dict[Any, dict[str, Any]]) -> dict[Any, dict[str, Any]]:
    if not rows:
        return {}
    keys = KEYS[table]
    columns = list(next(iter(rows.values())))
    fields = [sql.Identifier(name) for name, _ in keys]
    fields += [sql.SQL(READ_AS[(table, column)] + " AS {}").format(sql.Identifier(column)) if (table, column) in READ_AS
               else sql.Identifier(column) for column in columns]
    arrays = sql.SQL(", ").join(sql.SQL("%s::" + kind + "[]") for _, kind in keys)  # kinds are constants
    if len(keys) == 1:
        where = sql.SQL("{} = ANY({})").format(sql.Identifier(keys[0][0]), arrays)
        params = [list(rows)]
    else:
        where = sql.SQL("({}) IN (SELECT * FROM unnest({}))").format(
            sql.SQL(", ").join(sql.Identifier(name) for name, _ in keys), arrays)
        params = [list(part) for part in zip(*rows, strict=True)]
    cur.execute(sql.SQL("SELECT {} FROM {} WHERE {}").format(sql.SQL(", ").join(fields), sql.Identifier(table), where),
                params)
    found = {}
    for row in cur.fetchall():
        key = tuple(row[name] for name, _ in keys)
        found[key if len(keys) > 1 else key[0]] = row
    return found


def _same(table: str, column: str, expected: Any, actual: Any) -> bool:
    if (table, column) in READ_AS:
        return hashlib.sha256(expected).hexdigest() == actual
    return expected == actual


def _verify_accounts(cur: psycopg.Cursor, plan: Plan, outcome: Outcome) -> None:
    """Şifre özetleri toplamı, her hesabın beklenen modül durumu, silinmiş hesapların pasifliği, fazla haklar."""
    accounts = sorted(plan.tables["kullanicilar"].rows)
    everyone = sorted(set(accounts) | set(plan.gone))
    found: dict[int, dict[str, Any]] = {}
    if everyone:
        cur.execute("SELECT *, encode(sha256(convert_to(sifre_hash, 'UTF8')), 'hex') AS sifre_ozeti FROM kullanicilar "
                    "WHERE id = ANY(%s::bigint[])", (everyone,))
        found = {row["id"]: row for row in cur.fetchall()}
    source_total = password_total(plan.passwords)
    target_total = password_total({key: found[key]["sifre_ozeti"] if key in found else "-" for key in plan.passwords})
    outcome.passwords = (source_total, target_total, len(plan.passwords))
    if source_total != target_total:
        outcome.problems.append("kullanicilar: şifre özetlerinin SHA-256 toplamı kaynakla uyuşmuyor.")
    for user_id in accounts:
        user = found.get(user_id)
        for module in MODULES:
            expected = plan.licenses[user_id][module]
            actual = users.module_state(cur, user, module, plan.today) if user is not None else None
            outcome.licenses.append(License(user_id, module, expected, actual))
            if actual != expected:
                outcome.problems.append(f"kullanici_modulleri {user_id}: {module} beklenen {state_text(expected)}, "
                                        f"hedefte {state_text(actual)}.")
    for user_id in plan.gone:
        user = found.get(user_id)
        if user is None or (user["durum"], user["rol"]) != ("pasif", "kullanici"):
            outcome.problems.append(f"kullanicilar {user_id}: kaynakta silinmiş hesap hedefte pasif değil.")
    if everyone:
        cur.execute("SELECT kullanici_id, modul FROM kullanici_modulleri WHERE kullanici_id = ANY(%s::bigint[]) "
                    "ORDER BY kullanici_id, modul", (everyone,))
        planned = plan.tables["kullanici_modulleri"].rows
        outcome.problems += [f"kullanici_modulleri {row['kullanici_id']}/{label(row['modul'])}: hedefte kaynakta "
                             "olmayan bir hak var." for row in cur.fetchall()
                             if (row["kullanici_id"], row["modul"]) not in planned]


def password_total(digests: dict[int, str]) -> str:
    """Hesap kimliği ve şifre özetinin SHA-256'sı satırlarının, kimlik sırasıyla SHA-256'sı."""
    text = "".join(f"{key}:{digests[key]}\n" for key in sorted(digests))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def state_text(state: tuple[str, date | None] | None) -> str:
    if state is None:
        return "hesap yok"
    status, end = state
    if end is not None:
        return f"{status}, bitiş {end.isoformat()}"
    return "aktif, süresiz" if status == users.ACTIVE else status


def _written(plan: Plan) -> dict[str, list[Any]]:
    """Tablo başına taşımanın yazabileceği anahtarlar (kullanici_modulleri'nde hesap kimliği); geri kalan her satır
    Peyrev'indir ve taşıma boyunca değişmemelidir."""
    accounts = sorted(set(plan.tables["kullanicilar"].rows) | set(plan.gone))
    keys: dict[str, list[Any]] = {table: list(plan.tables[table].rows) for table in TARGETS}
    keys["kullanicilar"] = keys["kullanici_modulleri"] = accounts
    return keys


def _others(cur: psycopg.Cursor, written: dict[str, list[Any]]) -> dict[str, str]:
    """Planın yazmadığı satırların sayısı ve satır metinlerinin özeti, tablo başına."""
    digests = {}
    for table in TARGETS:
        column, kind = ("kullanici_id", "bigint") if table == "kullanici_modulleri" else KEYS[table][0]
        text = sql.SQL(ROW_TEXT.get(table, "t::text"))
        cur.execute(sql.SQL("SELECT count(*) AS n, encode(sha256(convert_to(coalesce(string_agg(x, E'\\n' ORDER BY x), "
                            "''), 'UTF8')), 'hex') AS ozet FROM (SELECT {text} AS x FROM {table} t "
                            "WHERE NOT (t.{column} = ANY(%s::" + kind + "[]))) AS satirlar").format(
            text=text, table=sql.Identifier(table), column=sql.Identifier(column)), (written[table],))
        row = cur.fetchone()
        digests[table] = f"{row['n']}:{row['ozet']}"
    return digests


def _describe(error: psycopg.Error) -> str:
    """SQLSTATE, tablo ve kısıt adı; hata metni ve DETAIL (anahtar değerleri içerir) yazılmaz."""
    if not error.sqlstate:
        return "veritabanına bağlanılamadı ya da bağlantı koptu"
    parts = [f"{type(error).__name__} (SQLSTATE {error.sqlstate})"]
    if error.diag.table_name:
        parts.append(f"tablo {error.diag.table_name}")
    if error.diag.constraint_name:
        parts.append(f"kısıt {error.diag.constraint_name}")
    return ", ".join(parts)
