"""orkestrai → Peyrev taşıması (peyrev.tasima): dönüşüm kuralları, prova ve gerçek kip, tekrar çalıştırma, Peyrev'de
doğan satırların korunması, çakışma ve uyuşmazlıkta geri alma.

Kaynak sahtedir (orkestrai_source.Orkestrai: sorguların kolonları, gerçek tabloların türleri); hedef gerçek
PostgreSQL'dir: gerçek kip conftest'in veritabanına (her test öncesi boşaltılır), prova ayrı bir veritabanına yazar.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime

import psycopg
import pytest
from conftest import START, make_user
from orkestrai_source import PRECISE, Orkestrai, hex_id, password_hash
from psycopg.rows import dict_row
from test_accounts import login, studio_agents, studio_license

from peyrev import migrate, users
from peyrev.clock import TR
from peyrev.db import Database
from peyrev.tasima import plan as rules
from peyrev.tasima import report, target

RPA, AGENTS = users.RPA, users.AGENTS
JANUARY = date(2026, 1, 1)
ORDER = {"kullanicilar": "id", "kullanici_modulleri": "kullanici_id, modul", "basvurular": "id",
         "destek_talepleri": "talep_no", "destek_mesajlari": "id", "destek_ekleri": "id", "ajan_ofisleri": "id",
         "ajan_gorevleri": "id", "ajan_onaylari": "id", "ajan_olaylari": "id"}
OFFICE_A, OFFICE_B, OFFICE_C, OFFICE_E = (hex_id("ofis", name) for name in "ABCE")
TASK = hex_id("gorev", 1)
GONE_APPROVAL = hex_id("onay", "silinmis")


@pytest.fixture
def source():
    return Orkestrai()


def transfer(core, source, mode=target.GERCEK, url=None, now=START):
    return target.run(url or core.db.url, mode, source.read(), now)


def rows(where, name, condition="TRUE", params=()):
    """Satırlar ve satır sürümü (xmin): yeniden yazılan bir satırın sürümü değişir."""
    query = f"SELECT xmin::text AS surum, * FROM {name} WHERE {condition} ORDER BY {ORDER[name]}"
    if isinstance(where, str):
        with psycopg.connect(where, row_factory=dict_row) as connection:
            return connection.execute(query, params).fetchall()
    with where.transaction() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def keyed(found, key="id"):
    return {row[key]: row for row in found}


def snapshot(where):
    return {name: rows(where, name) for name in ORDER}


def content(found):
    """Satır sürümü ve yazıcının bastığı guncelleme damgası olmadan."""
    return {name: [{key: value for key, value in row.items()
                    if key != "surum" and not (name in target.STAMPED and key == "guncelleme")} for row in table]
            for name, table in found.items()}


def execute(db, query, params=()):
    with db.transaction() as cur:
        cur.execute(query, params)
        return cur.fetchall() if cur.description else None


def tr(*parts):
    return datetime(*parts, tzinfo=TR)


def company(source):
    """Kaynak-inceleme raporundaki yapı: platform firması (Admin) ve RPA ile ajan kullanan bir firma. 10 hesaptan 6'sı
    taşınır: Admin ve RPA ya da ajan satırı olan 5 hesap (MOD_RPA 5 hesap, 2'si süresiz; MOD_AJAN 4 hesap)."""
    source.firm(1, modules={})
    source.firm(2, modules={RPA: date(2026, 12, 31), AGENTS: date(2026, 12, 31)})
    source.add("SYS_AjanOrganizasyonlari", FirmaID=2)
    source.user(1, 1, "platform@ornek.test", rol="Admin")
    source.user(2, 2, "mudur@firma.test", rol="Yonetici", modules={RPA: None, AGENTS: date(2026, 12, 31)})
    source.user(3, 2, "ayse@firma.test", modules={RPA: None, AGENTS: date(2026, 11, 30)})
    source.user(4, 2, "mehmet@firma.test", modules={RPA: date(2026, 11, 15), AGENTS: date(2026, 12, 31)})
    source.user(5, 2, "zeynep@firma.test", modules={RPA: date(2026, 6, 30)})
    source.user(6, 2, "ali@firma.test", modules={RPA: date(2027, 3, 31), AGENTS: date(2027, 3, 31)})
    for user_id in (7, 8, 9):
        source.user(user_id, 2, f"personel{user_id}@firma.test")
    source.user(10, 2, "sofor@firma.test", rol="Sofor")


def agents(source):
    """Dört ofis ve dört üyelik (biri yetkili olmayan bir üyenin), bir görev ve 16 olay."""
    source.office(OFFICE_A, 2, "Merkez Ofis", Olusturma=datetime(2026, 1, 10, 10, 0),
                  members={2: datetime(2026, 2, 1, 8, 0), 4: datetime(2026, 1, 15, 9, 0)})
    source.office(OFFICE_B, 2, "Depo", members={2: datetime(2026, 2, 3, 8, 0)})
    source.office(OFFICE_C, 3, "Satış", members={3: datetime(2026, 2, 4, 8, 0)})
    source.office(OFFICE_E, 6, "Saha", Durum="Pasif")
    source.add("SYS_AjanEkipGorevleri", ID=TASK, OfisID=OFFICE_A, OlusturanID=4, AtananID=4, IstekID=hex_id("istek", 1),
               Durum="running", Baslama=PRECISE)
    for number, (office, owner) in enumerate(((OFFICE_A, 2), (OFFICE_B, 2), (OFFICE_C, 3), (OFFICE_E, 6))):
        source.event(hex_id("olay", "ofis", number), "office", office, owner, "office_created", {"surum": 1})
    for number, olay in enumerate(("task_created", "task_started", "task_note")):
        source.event(hex_id("olay", "gorev", number), "task", TASK, 4, olay, {"durum": "running", "sira": number})
    for number, olay in enumerate(("approval_requested", "approval_rejected")):
        source.event(hex_id("olay", "onay", number), "approval", GONE_APPROVAL, 4, olay)
    for number in range(4):
        source.event(hex_id("olay", "lisans", number), "license", hex_id("lisans", number), None, "license_granted",
                     Aktor="system")
    for number in range(3):
        source.event(hex_id("olay", "hesap", number), "account", hex_id("hesap", number), None, "account_created",
                     Aktor="system")


def leftovers(source):
    """Taşınmayan tablolar: 110 oturum (10'u açık), 2 şifre kodu (biri açık), 8 hesap izi."""
    for number in range(110):
        source.add("SYS_RpaOturumlari", KullaniciID=2 + number % 5, Durum="Aktif" if number < 10 else "Kapandi")
    source.add("SYS_RpaSifreKodlari", KullaniciID=3, Gecerlilik=datetime(2026, 10, 27, 9, 0), Kullanildi=None)
    source.add("SYS_RpaSifreKodlari", KullaniciID=4, Gecerlilik=datetime(2026, 10, 27, 9, 0),
               Kullanildi=datetime(2026, 10, 26, 9, 0))
    for number in range(8):
        source.add("SYS_RpaHesapIzleri", Tur="sifre" if number % 2 else "basvuru")


def support(source):
    picture = bytes(range(256)) * 3 + b"\x00\xff"
    source.add("SYS_RpaTalepler", TalepNo=1001, KullaniciID=3, Durum="yanitlandi", SonYazan="yonetici",
               YoneticiGordu=datetime(2026, 3, 3, 9, 0))
    source.add("SYS_RpaTalepMesajlari", MesajID=hex_id("mesaj", 1), TalepNo=1001, YazanID=3)
    source.add("SYS_RpaTalepMesajlari", MesajID=hex_id("mesaj", 2), TalepNo=1001, YazanID=1, Yazan="yonetici",
               Olusturma=datetime(2026, 3, 3, 9, 0))
    source.add("SYS_RpaTalepEkleri", EkID=hex_id("ek", 1), TalepNo=1001, Boyut=len(picture), Icerik=bytearray(picture))
    source.add("SYS_RpaBasvurulari", BasvuruID=hex_id("basvuru", 1), Eposta="aday@ornek.test",
               SifreHash=password_hash("Aday12345"))
    return picture


# ----- prova ---------------------------------------------------------------------------------------------------------


def test_a_prova_builds_a_fresh_schema_and_reports_every_table(prova_url, source):
    company(source)
    agents(source)
    leftovers(source)
    with psycopg.connect(prova_url, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS public CASCADE")
        connection.execute("CREATE SCHEMA public")
        connection.execute("CREATE TABLE eski_deneme (id integer)")
    outcome = target.run(prova_url, target.PROVA, source.read(), START)
    text = report.render(outcome)
    assert outcome.ok and outcome.committed and outcome.database == "peyrev_tasima_prova", text
    with psycopg.connect(prova_url, row_factory=dict_row) as connection:
        assert connection.execute("SELECT to_regclass('eski_deneme') AS t").fetchone()["t"] is None
        applied = [row["version"] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY 1")]
    assert applied == [path.name[:4] for path in migrate.files()]
    lines = text.splitlines()
    for line in (
            "Peyrev taşıması (orkestrai → Peyrev): kip prova, hedef veritabanı peyrev_tasima_prova",
            "Taşıma anı 2026-10-26 12:00:00 (Türkiye saati); lisans günü 2026-10-26",
            "  kullanicilar ← SYS_Kullanicilar: 10 (6) / 6 / 4 (RPA ya da Ajan satırı yok ve Admin değil: 4)",
            "  kullanici_modulleri ← SYS_KullaniciModulleri: 9 (9) / 9 / 0",
            "  ajan_ofisleri ← SYS_AjanOfisleri: 4 (4) / 4 / 0",
            "  ajan_gorevleri ← SYS_AjanEkipGorevleri: 1 (1) / 1 / 0",
            "  ajan_onaylari ← SYS_AjanEkipOnaylari: 0 (0) / 0 / 0",
            ("  ajan_olaylari ← SYS_AjanEkipOlaylari: 16 (7) / 7 / 9 ('license' olayı (taşınmaz): 4; 'account' olayı "
            "(taşınmaz): 3; sahibi bulunamadı (silinmiş ofis ya da kayıt): 2)"),
            "  Ofis kimlikleri: uygun 4 ofisin 4 tanesi hedefte aynı kimlikle",
            "  Talep numaraları: uygun 0 talebin 0 tanesi hedefte aynı numarayla",
            "  Peyrev'de doğan satırlar: değişmedi",
            ("  Kimlik dizileri: kullanici_id_seq sonraki 11 (ilerletildi); destek_talepleri.talep_no sonraki 1001; "
            "alan_id_seq sonraki 1000000"),
            "  1: MOD_RPA aktif, süresiz → aktif, süresiz; MOD_AJAN yetki_yok → yetki_yok",
            "  5: MOD_RPA sure_doldu, bitiş 2026-06-30 → sure_doldu, bitiş 2026-06-30; MOD_AJAN yetki_yok → yetki_yok",
            ("  - SYS_AjanOfisUyeleri: 4 üyelik; sahiplik 3, taşınmayan diğer üyelik 1 (hesaplar: 4); Peyrev'de ofissiz "
            "başlayan taşınmış hesaplar: 4."),
            "  - SYS_RpaOturumlari: 110 oturum (aktif 10) taşınmaz; Studio'da yeniden giriş gerekir.",
            ("  - SYS_RpaSifreKodlari: 2 kod taşınmaz; açık kodu olan taşınan hesaplar: 3 (Peyrev yönetim panelinden "
            "yeni kod üretin)."),
            "  - SYS_RpaHesapIzleri: 8 iz taşınmaz.",
            "Sorunlar: yok",
            "SONUÇ: başarılı (prova); bütün tablolarda sayılar ve denetimler uyuşuyor, veri yazıldı."):
        assert line in lines, line
    source_total, target_total, count = outcome.passwords
    assert source_total == target_total and count == 6
    digest = hashlib.sha256(password_hash("Eski12345").encode()).hexdigest()
    assert source_total == target.password_total(dict.fromkeys(range(1, 7), digest))
    # Every prova starts from an empty schema again and writes the same rows.
    first = snapshot(prova_url)
    assert target.run(prova_url, target.PROVA, source.read(), START).ok
    assert content(snapshot(prova_url)) == content(first)


# ----- kurallar --------------------------------------------------------------------------------------------------


def test_roles_and_module_rights_follow_orkestrai(core, source):
    company(source)
    outcome = transfer(core, source)
    assert outcome.ok and outcome.committed, report.render(outcome)
    accounts = keyed(rows(core.db, "kullanicilar"))
    assert {key: (row["giris"], row["rol"], row["durum"], row["alan_id"], row["kaynak"])
            for key, row in accounts.items()} == {
        1: ("platform@ornek.test", "yonetici", "aktif", 1, "orkestrai"),
        # The firm's manager (Yonetici) gets an ordinary account like the staff.
        2: ("mudur@firma.test", "kullanici", "aktif", 2, "orkestrai"),
        3: ("ayse@firma.test", "kullanici", "aktif", 2, "orkestrai"),
        4: ("mehmet@firma.test", "kullanici", "aktif", 2, "orkestrai"),
        5: ("zeynep@firma.test", "kullanici", "aktif", 2, "orkestrai"),
        6: ("ali@firma.test", "kullanici", "aktif", 2, "orkestrai")}
    assert {row["sifre_hash"] for row in accounts.values()} == {password_hash("Eski12345")}
    modules = {(row["kullanici_id"], row["modul"]): (row["baslangic"], row["bitis"])
               for row in rows(core.db, "kullanici_modulleri")}
    assert modules == {
        (2, RPA): (JANUARY, date(2026, 12, 31)), (2, AGENTS): (JANUARY, date(2026, 12, 31)),
        (3, RPA): (JANUARY, date(2026, 12, 31)), (3, AGENTS): (JANUARY, date(2026, 11, 30)),
        (4, RPA): (JANUARY, date(2026, 11, 15)), (4, AGENTS): (JANUARY, date(2026, 12, 31)),
        (5, RPA): (JANUARY, date(2026, 6, 30)),
        (6, RPA): (JANUARY, date(2026, 12, 31)), (6, AGENTS): (JANUARY, date(2026, 12, 31))}
    states = {(item.user_id, item.module): item.actual for item in outcome.licenses}
    assert states[(1, RPA)] == (users.ACTIVE, None) and states[(1, AGENTS)] == (users.MISSING, None)  # Admin: no row
    assert states[(5, RPA)] == (users.EXPIRED, date(2026, 6, 30)) and states[(6, AGENTS)] == (users.ACTIVE,
                                                                                               date(2026, 12, 31))
    assert len(outcome.licenses) == 12 and all(item.actual == item.expected for item in outcome.licenses)
    assert "Roller: Admin → yonetici: 1; firma yöneticisi (Yonetici) → kullanici: 2." in outcome.plan.notes
    assert outcome.plan.tables["kullanicilar"].skipped == {rules.NO_RIGHTS: 4}


def test_migrated_accounts_sign_in_to_studio_with_their_old_password(client, core, source):
    company(source)
    source.find("SYS_Kullanicilar", KullaniciID=3)["SifreHash"] = password_hash("Ayse.Eski-2025")
    assert transfer(core, source).ok
    # Copied byte for byte: the Werkzeug hash orkestrai stored is the one Peyrev checks.
    assert keyed(rows(core.db, "kullanicilar"))[3]["sifre_hash"] == password_hash("Ayse.Eski-2025")
    claims = studio_license(client, login(client, kullanici="ayse", sifre="Ayse.Eski-2025"))
    assert (claims["kullanici_id"], claims["firma_id"], claims["bitis"], claims["hesap_turu"]) == (3, 2, "2026-12-31",
                                                                                                   "bireysel")
    team = studio_agents(claims)
    assert (team["kullanici_id"], team["firma_id"], team["onayci_id"]) == (3, 2, 3)
    assert login(client, kullanici="zeynep@firma.test", sifre="Eski12345").get_json()["kod"] == "SURE_DOLDU"
    admin = studio_license(client, login(client, kullanici="platform@ornek.test", sifre="Eski12345"))
    assert (admin["kullanici_id"], admin["firma_id"], admin["bitis"]) == (1, 1, None)


def test_a_module_ends_on_the_earlier_of_the_account_and_firm_dates(core, source):
    source.firm(11, modules={RPA: date(2026, 12, 31)})
    source.firm(12, modules={RPA: None}, start=date(2025, 5, 1))
    source.firm(13, modules={RPA: date(2026, 10, 25)})              # yesterday in Türkiye
    source.firm(14, modules={RPA: date(2026, 10, 26)}, start=None)  # today: the last day counts
    source.user(111, 11, "a@ornek.test", modules={RPA: None})
    source.user(112, 11, "b@ornek.test", modules={RPA: date(2026, 11, 30)})
    source.user(113, 11, "c@ornek.test", modules={RPA: date(2027, 6, 30)})
    source.user(121, 12, "d@ornek.test", modules={RPA: date(2027, 1, 31)})
    source.user(122, 12, "e@ornek.test", modules={RPA: None})
    source.user(131, 13, "f@ornek.test", modules={RPA: None})
    source.user(141, 14, "g@ornek.test", modules={RPA: None})
    outcome = transfer(core, source)
    assert outcome.ok, report.render(outcome)
    found = {row["kullanici_id"]: (row["baslangic"], row["bitis"]) for row in rows(core.db, "kullanici_modulleri")}
    assert found == {111: (JANUARY, date(2026, 12, 31)), 112: (JANUARY, date(2026, 11, 30)),
                     113: (JANUARY, date(2026, 12, 31)), 121: (date(2025, 5, 1), date(2027, 1, 31)),
                     122: (date(2025, 5, 1), None), 131: (JANUARY, date(2026, 10, 25)), 141: (None, date(2026, 10, 26))}
    expected = {item.user_id: item.expected for item in outcome.licenses if item.module == RPA}
    assert expected[122] == (users.ACTIVE, None)
    assert expected[131] == (users.EXPIRED, date(2026, 10, 25)) and expected[141] == (users.ACTIVE, date(2026, 10, 26))
    assert all(item.actual == item.expected for item in outcome.licenses)


def test_rights_need_an_open_firm_that_has_the_same_module(core, source):
    source.firm(21, modules={RPA: None})                 # the firm has no agent row
    source.firm(22, durum="Pasif")                       # passive firm
    source.firm(23)                                      # agents registration closed
    source.add("SYS_AjanOrganizasyonlari", FirmaID=23, Durum="Pasif")
    for user_id, firm_id in ((211, 21), (221, 22), (231, 23)):
        source.user(user_id, firm_id, f"k{user_id}@ornek.test", modules={RPA: None, AGENTS: None})
    outcome = transfer(core, source)
    assert outcome.ok, report.render(outcome)
    # The accounts move (they have rows); only the rights their firm would not give are left behind.
    assert [row["id"] for row in rows(core.db, "kullanicilar")] == [211, 221, 231]
    assert [(row["kullanici_id"], row["modul"]) for row in rows(core.db, "kullanici_modulleri")] == [(211, RPA),
                                                                                                    (231, RPA)]
    assert outcome.plan.tables["kullanici_modulleri"].skipped == {rules.NO_FIRM_ROW: 1, rules.FIRM_CLOSED: 2,
                                                                   rules.AGENTS_CLOSED: 1}
    assert {(item.user_id, item.module): item.expected[0] for item in outcome.licenses} == {
        (211, RPA): users.ACTIVE, (211, AGENTS): users.MISSING, (221, RPA): users.MISSING, (221, AGENTS): users.MISSING,
        (231, RPA): users.ACTIVE, (231, AGENTS): users.MISSING}
    assert "SYS_Firmalar: 3 firma, pasif: 22; modül hakları taşınmayan hesaplar: 221." in outcome.plan.notes
    assert "SYS_AjanOrganizasyonlari: 1 kayıt (Peyrev'de tablo yok); ajan kaydı kapalı firmalar: 23." in outcome.plan.notes


def test_turkish_wall_clock_times_become_instants(core, source):
    source.firm(2)
    source.user(2, 2, "kis@ornek.test", modules={RPA: None}, OlusturmaTarihi=datetime(2015, 1, 15, 12, 0))  # UTC+2
    source.user(3, 2, "yaz@ornek.test", modules={RPA: None}, OlusturmaTarihi=datetime(2026, 6, 1, 12, 0))   # UTC+3
    source.user(4, 2, "bos@ornek.test", modules={RPA: None}, OlusturmaTarihi=None)
    source.add("SYS_AjanEkipGorevleri", ID=TASK, OlusturanID=3, AtananID=3, IstekID=hex_id("istek", 1))
    assert transfer(core, source).ok
    created = {row["id"]: row["olusturma"] for row in rows(core.db, "kullanicilar")}
    assert created[2] == datetime(2015, 1, 15, 10, 0, tzinfo=UTC) and created[3] == datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    # No creation time in the source: the moment of the migration, kept by later runs.
    assert created[4] == START
    assert rows(core.db, "ajan_gorevleri")[0]["olusturma"] == datetime(2026, 3, 2, 6, 30, 15, 123456, tzinfo=UTC)
    later = transfer(core, source, now=datetime(2026, 10, 27, 9, 0, tzinfo=UTC))
    assert later.ok and keyed(rows(core.db, "kullanicilar"))[4]["olusturma"] == START


def test_applications_keep_only_the_waiting_password(core, source):
    company(source)
    waiting, newer, approved, rejected, orphan = (hex_id("basvuru", name)
                                                  for name in ("bekleyen", "yeni", "onay", "ret", "sahipsiz"))
    decided = {"KararZamani": datetime(2026, 9, 2, 10, 0), "KararVerenID": 1}
    source.add("SYS_RpaBasvurulari", BasvuruID=waiting, Eposta="Aday@Ornek.test", SifreHash=password_hash("Aday12345"),
               Olusturma=datetime(2026, 9, 1, 10, 0), Telefon="05550000000")
    source.add("SYS_RpaBasvurulari", BasvuruID=newer, Eposta="aday@ornek.test", SifreHash=password_hash("Baska12345"),
               Olusturma=datetime(2026, 9, 5, 10, 0))
    source.add("SYS_RpaBasvurulari", BasvuruID=approved, Eposta="ayse@firma.test", SifreHash=password_hash("x"),
               Durum="onaylandi", KullaniciID=3, Paket="rpa_ajan", **decided)
    source.add("SYS_RpaBasvurulari", BasvuruID=rejected, Eposta="ret@ornek.test", SifreHash=password_hash("y"),
               Durum="reddedildi", YoneticiNotu="Ödeme gelmedi", **decided)
    source.add("SYS_RpaBasvurulari", BasvuruID=orphan, Eposta="personel8@firma.test", SifreHash=password_hash("z"),
               Durum="onaylandi", KullaniciID=8, **decided)
    outcome = transfer(core, source)
    assert outcome.ok, report.render(outcome)
    found = keyed(rows(core.db, "basvurular"))
    assert sorted(found) == sorted([waiting, approved, rejected, orphan])
    assert (found[waiting]["sifre_hash"], found[waiting]["eposta"], found[waiting]["durum"]) == (
        password_hash("Aday12345"), "Aday@Ornek.test", "bekliyor")
    assert [found[key]["sifre_hash"] for key in (approved, rejected, orphan)] == ["", "", ""]
    assert (found[approved]["kullanici_id"], found[approved]["karar_veren_id"], found[orphan]["kullanici_id"]) == (3, 1,
                                                                                                                 None)
    assert found[approved]["karar_zamani"] == tr(2026, 9, 2, 10, 0)
    assert outcome.plan.tables["basvurular"].skipped == {rules.OLDER_PENDING: 1}
    assert "basvurular: hesabı taşınmayan 1 başvurunun kullanici_id'si boş yazıldı." in outcome.plan.notes


def test_tickets_keep_their_numbers_messages_and_attachment_bytes(core, source):
    company(source)
    picture = support(source)
    flow = json.dumps({"adimlar": [{"tur": "pencere.odakla"}]}).encode()
    source.add("SYS_RpaTalepEkleri", EkID=hex_id("ek", 2), TalepNo=1001, Tur="akis", Ad="akis.json",
               Bicim="application/json", Boyut=len(flow), Icerik=flow)
    source.add("SYS_RpaTalepler", TalepNo=1004, KullaniciID=8)            # account not migrated
    source.add("SYS_RpaTalepMesajlari", MesajID=hex_id("mesaj", 3), TalepNo=1004, YazanID=8)
    source.add("SYS_RpaTalepler", TalepNo=1003, KullaniciID=4, Tur="adim_onerisi", Durum="yayinlandi",
               YayinSurumu="1.0.0", AdimTuru="pencere.odakla")
    outcome = transfer(core, source)
    assert outcome.ok, report.render(outcome)
    tickets = keyed(rows(core.db, "destek_talepleri"), "talep_no")
    assert sorted(tickets) == [1001, 1003] and (tickets[1001]["kullanici_id"], tickets[1003]["yayin_surumu"]) == (3, "1.0.0")
    assert tickets[1001]["yonetici_gordu"] == datetime(2026, 3, 3, 6, 0, tzinfo=UTC)
    files = keyed(rows(core.db, "destek_ekleri"))
    assert files[hex_id("ek", 1)]["icerik"] == picture and files[hex_id("ek", 1)]["boyut"] == 770
    assert files[hex_id("ek", 2)]["icerik"] == flow
    assert sorted(keyed(rows(core.db, "destek_mesajlari"))) == sorted([hex_id("mesaj", 1), hex_id("mesaj", 2)])
    assert outcome.plan.tables["destek_talepleri"].skipped == {rules.NO_ACCOUNT: 1}
    assert outcome.plan.tables["destek_mesajlari"].skipped == {rules.NO_TICKET: 1}
    assert outcome.tickets == (2, 2)
    # New tickets continue above every orkestrai number, also above the ticket that was not migrated.
    created = execute(core.db, "INSERT INTO destek_talepleri (kullanici_id, tur, baslik, surum, platform) "
                               "VALUES (3, 'soru', 'Yeni', '1.0.0', 'macOS') RETURNING talep_no")
    assert created[0]["talep_no"] == 1005


def test_an_office_belongs_to_its_manager_alone(core, source):
    company(source)
    agents(source)
    source.office(hex_id("ofis", "X"), 8, "Eski Ofis", members={8: datetime(2026, 1, 5, 8, 0),
                                                              3: datetime(2026, 1, 6, 8, 0)})
    outcome = transfer(core, source)
    assert outcome.ok, report.render(outcome)
    found = keyed(rows(core.db, "ajan_ofisleri"))
    assert sorted(found) == sorted([OFFICE_A, OFFICE_B, OFFICE_C, OFFICE_E])
    office = found[OFFICE_A]
    # The owner's own joining time keeps the order Studio showed; the office's own time is the fallback.
    assert (office["sahip_id"], office["ad"], office["durum"], office["olusturma"], office["guncelleme"]) == (
        2, "Merkez Ofis", "aktif", tr(2026, 2, 1, 8, 0), tr(2026, 2, 1, 8, 0))
    assert (found[OFFICE_E]["sahip_id"], found[OFFICE_E]["durum"], found[OFFICE_E]["olusturma"]) == (
        6, "pasif", tr(2026, 3, 2, 9, 30))
    assert 4 not in {row["sahip_id"] for row in found.values()}  # a member of someone else's office starts without one
    assert outcome.plan.tables["ajan_ofisleri"].skipped == {rules.NO_OWNER: 1}
    assert ("SYS_AjanOfisUyeleri: 6 üyelik; sahiplik 4, taşınmayan diğer üyelik 2 (hesaplar: 3, 4); Peyrev'de ofissiz "
            "başlayan taşınmış hesaplar: 4.") in outcome.plan.notes
    assert outcome.offices == (4, 4)


def test_tasks_approvals_and_events_follow_the_office_owner(core, source):
    company(source)
    agents(source)
    deleted = hex_id("ofis", "silinmis")
    own, orphaned, ownerless = (hex_id("gorev", name) for name in ("kendi", "silinmis", "sahipsiz"))
    source.add("SYS_AjanEkipGorevleri", ID=own, OfisID=None, OlusturanID=3, AtananID=3, IstekID=hex_id("istek", 2))
    source.add("SYS_AjanEkipGorevleri", ID=orphaned, OfisID=deleted, OlusturanID=4, AtananID=6,
               IstekID=hex_id("istek", 3))
    source.add("SYS_AjanEkipGorevleri", ID=ownerless, OfisID=None, OlusturanID=8, AtananID=8, IstekID=hex_id("istek", 4))
    approvals = {}
    for number, (state, office, asker, approver) in enumerate((
            ("pending", OFFICE_A, 4, 2), ("approved", OFFICE_A, 4, 2), ("rejected", OFFICE_A, 4, 2),
            ("consumed", None, 3, 3))):
        approvals[state] = hex_id("onay", state)
        source.add("SYS_AjanEkipOnaylari", ID=approvals[state], OfisID=office, IsteyenID=asker, OnayciID=approver,
                   OturumID=hex_id("oturum", number), IstekID=hex_id("onay-istek", number),
                   CalismaID=hex_id("calisma", number), AkisID=hex_id("akis", number), Durum=state,
                   KararNedeni="user" if state in rules.KEPT_APPROVALS else None)
    details = {"neden": "kapatildi", "liste": [1, 2.5, None, True], "metin": "çğıöşü"}
    source.event(hex_id("olay", "x1"), "approval", approvals["pending"], 4, "approval_requested")
    source.event(hex_id("olay", "x2"), "office", deleted, 3, "office_created")
    source.event(hex_id("olay", "x3"), "office", deleted, 3, "office_deleted", details)
    source.event(hex_id("olay", "x4"), "task", hex_id("gorev", "bilinmiyor"), 4, "task_created")
    outcome = transfer(core, source)
    assert outcome.ok, report.render(outcome)
    assert {row["id"]: (row["sahip_id"], row["olusturan_id"], row["atanan_id"])
            for row in rows(core.db, "ajan_gorevleri")} == {
        TASK: (2, 4, 4),         # in an office: the office's manager owns it, the old colleague stays assigned
        own: (3, 3, 3),          # no office: its creator
        orphaned: (4, 4, 6)}     # deleted office: its creator
    assert outcome.plan.tables["ajan_gorevleri"].skipped == {rules.NO_OWNER: 1}
    assert {row["id"]: (row["sahip_id"], row["durum"], row["isteyen_id"], row["onayci_id"])
            for row in rows(core.db, "ajan_onaylari")} == {approvals["rejected"]: (2, "rejected", 4, 2),
                                                          approvals["consumed"]: (3, "consumed", 3, 3)}
    assert outcome.plan.tables["ajan_onaylari"].skipped == {rules.OPEN_APPROVAL: 2}
    events = keyed(rows(core.db, "ajan_olaylari"))
    assert events[hex_id("olay", "x1")]["sahip_id"] == 2   # the approval stays behind, its history follows the office
    assert (events[hex_id("olay", "x2")]["sahip_id"], events[hex_id("olay", "x3")]["sahip_id"]) == (3, 3)  # founder
    assert events[hex_id("olay", "x3")]["ayrinti"] == details
    assert hex_id("olay", "x4") not in events
    assert {row["sahip_id"] for row in events.values() if row["kayit_id"] == TASK} == {2}
    assert outcome.plan.tables["ajan_olaylari"].skipped[rules.OWNER_UNKNOWN] == 3


# ----- gerçek kip: tekrar çalıştırma, Peyrev satırları, çakışma ve geri alma ------------------------------------------


def test_running_the_real_migration_again_rewrites_no_row(core, source):
    company(source)
    agents(source)
    leftovers(source)
    support(source)
    first = transfer(core, source)
    assert first.ok and first.committed, report.render(first)
    before = snapshot(core.db)
    second = transfer(core, source)
    assert second.ok and second.committed, report.render(second)
    assert snapshot(core.db) == before          # row versions included: nothing was rewritten
    assert [item.advanced for item in second.sequences] == [False, False, False]
    audit = execute(core.db, "SELECT hedef, ayrinti FROM yonetim_olaylari WHERE tur = 'tasima' ORDER BY id")
    assert [(row["hedef"], row["ayrinti"]["sonuc"]) for row in audit] == [("gercek", "basarili")] * 2
    assert audit[0]["ayrinti"]["tablolar"]["kullanicilar"] == {"kaynak": 10, "uygun": 6, "hedef": 6, "atlanan": 4}


def test_a_later_real_run_brings_migrated_accounts_in_line_with_the_source(core, source):
    company(source)
    agents(source)
    assert transfer(core, source).ok
    # Meanwhile in orkestrai: two accounts swap logins, one gets a new password and date, one loses its rights, one is
    # deleted and a new one gets RPA.
    source.find("SYS_Kullanicilar", KullaniciID=3)["Eposta"] = "mehmet@firma.test"
    source.find("SYS_Kullanicilar", KullaniciID=4)["Eposta"] = "ayse@firma.test"
    source.find("SYS_Kullanicilar", KullaniciID=4)["SifreHash"] = password_hash("Yeni.Sifre-1")
    source.find("SYS_KullaniciModulleri", KullaniciID=4, ModulKodu=RPA)["BitisTarihi"] = date(2026, 12, 15)
    for user_id, module in ((6, RPA), (6, AGENTS), (5, RPA)):
        source.drop("SYS_KullaniciModulleri", KullaniciID=user_id, ModulKodu=module)
    source.drop("SYS_Kullanicilar", KullaniciID=5)
    source.user(11, 2, "yeni@firma.test", modules={RPA: None})
    outcome = transfer(core, source)
    assert outcome.ok, report.render(outcome)
    accounts = keyed(rows(core.db, "kullanicilar"))
    assert (accounts[3]["giris"], accounts[4]["giris"]) == ("mehmet@firma.test", "ayse@firma.test")
    assert accounts[4]["sifre_hash"] == password_hash("Yeni.Sifre-1")
    assert (accounts[5]["durum"], accounts[5]["rol"], accounts[5]["kaynak"]) == ("pasif", "kullanici", "orkestrai")
    assert (accounts[6]["durum"], accounts[11]["kaynak"]) == ("aktif", "orkestrai")
    modules = {(row["kullanici_id"], row["modul"]): row["bitis"] for row in rows(core.db, "kullanici_modulleri")}
    # The new account's own row is open-ended; its firm's end date applies.
    assert modules[(4, RPA)] == date(2026, 12, 15) and modules[(11, RPA)] == date(2026, 12, 31)
    assert not {user_id for user_id, _ in modules} & {5, 6}
    assert outcome.plan.gone == [5]
    assert ("kullanicilar: daha önce taşınmış, kaynakta artık RPA ya da Ajan hakkı olmayan hesaplar: 6 (hesap kaynakla "
            "eşitlendi, modül hakları kaldırıldı).") in outcome.plan.notes
    # Records stay with their accounts: the office of the account without rights is still its own.
    assert keyed(rows(core.db, "ajan_ofisleri"))[OFFICE_E]["sahip_id"] == 6


def test_rows_born_in_peyrev_are_never_touched(core, source):
    company(source)
    agents(source)
    make_user(core, "yeni.uye@peyrev.test", user_id=500, modules={RPA: date(2027, 1, 31)})
    office = hex_id("peyrev", "ofis")
    with core.db.transaction() as cur:
        cur.execute("UPDATE kullanicilar SET kaynak = 'basvuru' WHERE id = 500")
        # Same office name as an orkestrai office of another owner: no conflict.
        cur.execute("INSERT INTO ajan_ofisleri (id, sahip_id, ad) VALUES (%s, 500, 'Merkez Ofis')", (office,))
        cur.execute("INSERT INTO ajan_olaylari (id, sahip_id, kayit_turu, kayit_id, kullanici_id, aktor, olay, olusturma)"
                    " VALUES (%s, 500, 'office', %s, 500, 'human', 'office_created', now())", (hex_id("peyrev", "olay"),
                                                                                            office))
        cur.execute("INSERT INTO destek_talepleri (talep_no, kullanici_id, tur, baslik, surum, platform) "
                    "VALUES (1500, 500, 'soru', 'Soru', '1.0.0', 'Windows')")
        cur.execute("INSERT INTO basvurular (id, ad_soyad, eposta, sifre_hash, paket, aydinlatma_surumu, olusturma) "
                    "VALUES (%s, 'Aday', 'aday@ornek.test', 'ozet', 'rpa', '2026-10-26', now())",
                    (hex_id("peyrev", "basvuru"),))
    # An older waiting application in orkestrai with the same address gives way to the one made in Peyrev.
    source.add("SYS_RpaBasvurulari", BasvuruID=hex_id("basvuru", "eski"), Eposta="ADAY@ornek.test",
               SifreHash=password_hash("Aday12345"))
    before = snapshot(core.db)
    outcome = transfer(core, source)
    assert outcome.ok and outcome.disturbed == [], report.render(outcome)
    after = snapshot(core.db)
    for name, found in before.items():
        assert all(row in after[name] for row in found), name
    assert [row["id"] for row in after["kullanicilar"]] == [1, 2, 3, 4, 5, 6, 500]
    assert outcome.plan.tables["basvurular"].skipped == {rules.PEYREV_PENDING: 1}
    assert hex_id("basvuru", "eski") not in keyed(after["basvurular"])
    # Peyrev's own accounts keep coming after its highest id.
    assert outcome.sequences[0] == target.Sequence("kullanici_id_seq", 501, True)


def test_records_that_collide_with_peyrev_rows_stop_the_real_migration(core, source):
    company(source)
    source.add("SYS_RpaTalepler", TalepNo=1001, KullaniciID=2)
    make_user(core, "baska@peyrev.test", user_id=3)          # a Peyrev account holding orkestrai id 3
    make_user(core, "MEHMET@firma.test", user_id=600)        # and one holding account 4's login
    execute(core.db, "INSERT INTO destek_talepleri (talep_no, kullanici_id, tur, baslik, surum, platform) "
                     "VALUES (1001, 600, 'soru', 'Soru', '1.0.0', 'macOS')")
    before = snapshot(core.db)
    outcome = transfer(core, source)
    assert outcome.verified and not outcome.ok and not outcome.committed
    assert outcome.plan.problems == [
        "kullanicilar 3: hedefte aynı kimlikli, Peyrev'de açılmış bir hesap var (kaynak yonetim); kimlik korunamaz.",
        "kullanicilar 4: giriş adı hedefteki 600 numaralı hesabınkiyle aynı (büyük/küçük harf duyarsız).",
        "destek_talepleri 1001: bu numara hedefte başka bir talepte (Peyrev'de açılmış); talep numarası korunamaz."]
    assert outcome.plan.tables["kullanicilar"].skipped[rules.CONFLICT] == 2
    assert snapshot(core.db) == before          # rolled back: not even the other accounts were written
    # The Peyrev accounts were given their ids; the failed run did not move the sequence either.
    assert execute(core.db, "SELECT is_called FROM kullanici_id_seq")[0]["is_called"] is False
    text = report.render(outcome)
    assert "SONUÇ: BAŞARISIZ (gercek, 3 sorun); gerçek veritabanında hiçbir değişiklik kalmadı (işlem geri alındı)." in text
    assert "  Kimlik dizileri: dokunulmadı (doğrulama tutmadı)" in text
    assert "mehmet" not in text.lower() and "baska@" not in text


def test_an_office_named_like_a_peyrev_office_of_its_owner_stops_the_run(core, source, monkeypatch):
    company(source)
    agents(source)
    assert transfer(core, source).ok
    peyrev_office, orkestrai_office = hex_id("peyrev", "arsiv"), hex_id("ofis", "arsiv")
    execute(core.db, "INSERT INTO ajan_ofisleri (id, sahip_id, ad) VALUES (%s, 2, 'Arsiv')", (peyrev_office,))
    source.office(orkestrai_office, 2, "ARSIV")
    before = snapshot(core.db)
    outcome = transfer(core, source)
    assert not outcome.ok and not outcome.committed and snapshot(core.db) == before
    assert outcome.plan.problems == [(f"ajan_ofisleri {orkestrai_office}: sahibinin hedefte aynı adlı başka bir ofisi var "
                                     f"({peyrev_office}).")]
    # The limit per owner counts the offices opened in Peyrev too.
    source.drop("SYS_AjanOfisleri", OfisID=orkestrai_office)
    monkeypatch.setattr(target, "MAX_OFFICES", 2)
    limited = transfer(core, source)
    assert limited.plan.problems == ["ajan_ofisleri: 2 numaralı hesabın taşımadan sonra 3 ofisi olur (en çok 2)."]
    assert not limited.committed


def test_a_mismatch_found_after_writing_rolls_the_real_migration_back(core, source):
    company(source)
    with core.db.transaction() as cur:
        cur.execute("CREATE FUNCTION tasima_bozucu() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                    "IF NEW.id = 3 THEN NEW.ad_soyad := 'Değişmiş'; END IF; RETURN NEW; END $$")
        cur.execute("CREATE TRIGGER tasima_bozucu BEFORE INSERT ON kullanicilar FOR EACH ROW "
                    "EXECUTE FUNCTION tasima_bozucu()")
    try:
        outcome = transfer(core, source)
    finally:
        execute(core.db, "DROP TRIGGER tasima_bozucu ON kullanicilar")
        execute(core.db, "DROP FUNCTION tasima_bozucu()")
    assert outcome.verified and not outcome.ok and not outcome.committed
    assert outcome.problems == ["kullanicilar 3: hedefteki ad_soyad kaynakla uyuşmuyor.",
                                "kullanicilar: uygun 6 satırın 5 tanesi hedefte birebir aynı."]
    assert rows(core.db, "kullanicilar") == [] and rows(core.db, "kullanici_modulleri") == []
    assert execute(core.db, "SELECT count(*) AS n FROM yonetim_olaylari")[0]["n"] == 0
    # setval cannot be rolled back, so a failing real run never calls it.
    assert execute(core.db, "SELECT is_called FROM kullanici_id_seq")[0]["is_called"] is False
    assert outcome.sequences == []


def test_a_source_row_peyrev_cannot_hold_fails_the_run_but_a_prova_keeps_the_rest(core, source, prova_url):
    company(source)
    source.add("SYS_RpaTalepler", TalepNo=1001, KullaniciID=3, Durum="bilinmiyor")
    source.find("SYS_Kullanicilar", KullaniciID=6)["AdSoyad"] = "A" * 121
    real = transfer(core, source)
    assert not real.ok and not real.committed and rows(core.db, "kullanicilar") == []
    assert real.plan.problems == ["kullanicilar 6: ad soyad en çok 120 karakter olabilir",
                                  "destek_talepleri 1001: tür, durum ya da son yazan geçersiz"]
    prova = target.run(prova_url, target.PROVA, source.read(), START)
    assert not prova.ok and prova.committed
    assert [row["id"] for row in rows(prova_url, "kullanicilar")] == [1, 2, 3, 4, 5]
    text = report.render(prova)
    assert "SONUÇ: BAŞARISIZ (prova, 2 sorun); yazılanlar incelemek için prova veritabanında bırakıldı." in text
    assert "A" * 121 not in text


def test_a_database_error_is_reported_by_code_and_constraint_only(core, source):
    company(source)
    agents(source)
    # A task made in Peyrev with the same creator and request id as the orkestrai task.
    make_user(core, "peyrev@peyrev.test", user_id=700)
    execute(core.db, "INSERT INTO ajan_gorevleri (id, sahip_id, olusturan_id, atanan_id, istek_id, istek_ozeti, baslik, "
                     "talimat, oncelik, olusturma, guncelleme) VALUES (%s, 700, 4, 4, %s, 'o', 'Görev', '', 'normal', "
                     "now(), now())", (hex_id("peyrev", "gorev"), hex_id("istek", 1)))
    outcome = transfer(core, source)
    assert outcome.error == ("PostgreSQL: UniqueViolation (SQLSTATE 23505), tablo ajan_gorevleri, kısıt "
                             "ajan_gorevleri_istek_uq")
    assert not outcome.ok and not outcome.verified and not outcome.committed
    assert [row["id"] for row in rows(core.db, "kullanicilar")] == [700]
    text = report.render(outcome)
    assert "Hata: PostgreSQL: UniqueViolation" in text and hex_id("istek", 1) not in text
    assert "SONUÇ: BAŞARISIZ (gercek, hata); gerçek veritabanında hiçbir değişiklik kalmadı (işlem geri alındı)." in text


def test_new_ids_continue_above_every_orkestrai_id_and_never_go_back(core, source):
    source.firm(2)
    source.user(3, 2, "uc@ornek.test", modules={RPA: None})
    source.user(40, 2, "kirk@ornek.test")                   # no rights: not migrated, but its id stays reserved
    source.add("SYS_RpaTalepler", TalepNo=1001, KullaniciID=3)
    source.add("SYS_RpaTalepler", TalepNo=1010, KullaniciID=40)
    outcome = transfer(core, source)
    assert outcome.ok, report.render(outcome)
    assert outcome.sequences == [target.Sequence("kullanici_id_seq", 41, True),
                                 target.Sequence("destek_talepleri.talep_no", 1011, True),
                                 target.Sequence("alan_id_seq", 1000000, False)]
    fresh = make_user(core, "yeni@peyrev.test")
    assert (fresh["id"], fresh["alan_id"]) == (41, 1000000)
    execute(core.db, "SELECT setval('kullanici_id_seq', 90)")
    again = transfer(core, source)
    assert again.ok and again.sequences[0] == target.Sequence("kullanici_id_seq", 91, False)


def test_only_one_migration_runs_at_a_time(core, source):
    company(source)
    with psycopg.connect(core.db.url, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (target.LOCK,))
        outcome = transfer(core, source)
    assert outcome.error == "Bu veritabanında başka bir taşıma şu anda çalışıyor." and outcome.plan is None
    assert rows(core.db, "kullanicilar") == []
    assert "SONUÇ: BAŞARISIZ (gercek, hata)" in report.render(outcome)


def test_the_real_target_needs_every_schema_migration(prova_url, source):
    company(source)
    with psycopg.connect(prova_url, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS public CASCADE")
        connection.execute("CREATE SCHEMA public")
    outcome = target.run(prova_url, target.GERCEK, source.read(), START)
    assert outcome.error == "Hedefte şema yok (schema_migrations); önce geçişler uygulanmalı."
    database = Database(prova_url, max_size=1)
    try:
        migrate.apply(database)
    finally:
        database.close()
    last = migrate.files()[-1].name
    with psycopg.connect(prova_url, autocommit=True) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = %s", (last[:4],))
    outcome = target.run(prova_url, target.GERCEK, source.read(), START)
    assert outcome.error == f"Hedef şeması güncel değil, uygulanmamış geçişler: {last} (önce dağıtım yapılmalı)."
    assert rows(prova_url, "kullanicilar") == []
