"""Yönetim paneli için hesap işlemleri (uç değil, servis işlevleri; panel /api/yonetim/* uçlarından çağırır).

- Her işlev kendi veritabanı işlemini açar ve işlemin içinde `admin_id`'nin hâlâ aktif bir yönetici olduğunu
  veritabanından yeniden okur (yönetici güvencesi): değilse 403 YETKI_YOK.
- Girdiler panelin aldığı JSON değerleridir (tarih "YYYY-AA-GG", NULL bitiş = süresiz). Doğrulama hataları ApiError'dır;
  metinler Studio'nun bildiği metinlerdir ve panel bunları olduğu gibi döndürebilir.
- Her değişiklik yonetim_olaylari'na yazılır (yonetici_id, tur, hedef, ayrinti); şifre, özet ve kod yazılmaz.

Olay türleri: basvuru_onaylandi, basvuru_reddedildi, kullanici_acildi, modul_ayarlandi, modul_kaldirildi,
sifre_kodu_uretildi, oturumlar_kapatildi, durum_degisti. Hedef: "kullanici:<id>" ya da "basvuru:<id>".
"""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import date, datetime, timedelta
from typing import Any

import psycopg
from psycopg import Cursor
from psycopg.types.json import Jsonb
from werkzeug.security import generate_password_hash

from .. import users
from ..common import ApiError
from ..db import advisory_lock
from . import applications, rules, sessions

CODE_HOURS = 24            # yönetimden üretilen şifre yenileme kodu
INITIAL_CODE_DAYS = 7      # yönetimden açılan hesabın ilk şifre kodu
MODULES = (users.RPA, users.AGENTS, users.IKIZ)
# Başvuru paketlerinin verdiği haklar: Dijital İkiz paketi üçünü birlikte verir.
PACKAGE_MODULES = {"rpa": (users.RPA,), "rpa_ajan": (users.RPA, users.AGENTS),
                   "ikiz": (users.RPA, users.AGENTS, users.IKIZ)}
APPLICATION_ID = re.compile(r"[0-9a-f]{32}")
SESSION_ID = re.compile(r"[0-9a-f]{32}")


def _admin(cur: Cursor, admin_id: Any) -> dict[str, Any]:
    if type(admin_id) is not int or admin_id < 1:
        raise ApiError("Yönetici oturumu geçersiz.", 401, "OTURUM_GECERSIZ")
    admin = users.get(cur, admin_id)
    if admin is None or admin["durum"] != "aktif" or admin["rol"] != "yonetici":
        raise ApiError("Bu işlem yalnız platform yöneticisine açıktır.", 403, "YETKI_YOK")
    return admin


def _account(cur: Cursor, user_id: Any) -> dict[str, Any]:
    """Hesap satırı kilitli okunur: aynı hesabın eş zamanlı yönetim işlemleri sırayla işlenir."""
    user = users.get(cur, user_id, lock=True) if type(user_id) is int and user_id >= 1 else None
    if user is None:
        raise ApiError("Hesap bulunamadı.", 404, "BULUNAMADI")
    return user


def _event(core, cur: Cursor, admin_id: int, kind: str, target: str, **details: Any) -> None:
    cur.execute("INSERT INTO yonetim_olaylari (yonetici_id, tur, hedef, ayrinti, zaman) VALUES (%s, %s, %s, %s, %s)",
                (admin_id, kind, target, Jsonb(details), core.clock.now()))


def _note(value: Any) -> str | None:
    note = value or None
    if note is not None and (not isinstance(note, str) or len(note) > 300):
        raise ApiError("Not en fazla 300 karakter olabilir.")
    return note


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _write_module(cur: Cursor, user_id: int, module: str, starts_on: date | None, ends_on: date | None,
                  moment: datetime, today: date) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """(önceki değerler ya da None, yazılan değerler). Başlangıç verilmezse yeni satırda bugün, var olanda eskisi kalır."""
    cur.execute("SELECT baslangic, bitis FROM kullanici_modulleri WHERE kullanici_id = %s AND modul = %s FOR UPDATE",
                (user_id, module))
    before = cur.fetchone()
    if before is None:
        starts_on = starts_on or today
        cur.execute("INSERT INTO kullanici_modulleri (kullanici_id, modul, baslangic, bitis, guncelleme) "
                    "VALUES (%s, %s, %s, %s, %s)", (user_id, module, starts_on, ends_on, moment))
    else:
        starts_on = starts_on or before["baslangic"]
        cur.execute("UPDATE kullanici_modulleri SET baslangic = %s, bitis = %s, guncelleme = %s "
                    "WHERE kullanici_id = %s AND modul = %s", (starts_on, ends_on, moment, user_id, module))
    after = {"baslangic": _iso(starts_on), "bitis": _iso(ends_on)}
    return (None if before is None else {"baslangic": _iso(before["baslangic"]), "bitis": _iso(before["bitis"])}), after


def _issue_code(cur: Cursor, admin_id: int, user_id: int, moment: datetime, lifetime: timedelta) -> tuple[str, datetime]:
    """(okunur kod, son geçerlilik anı). Yeni kod önceki kullanılmamış kodları geçersiz kılar; düz kod saklanmaz."""
    code, code_id = rules.new_code(), uuid.uuid4().hex
    cur.execute("UPDATE sifre_kodlari SET kullanildi = %s WHERE kullanici_id = %s AND kullanildi IS NULL",
                (moment, user_id))
    expires = moment + lifetime
    cur.execute("INSERT INTO sifre_kodlari (id, kullanici_id, kod_ozeti, olusturan_id, olusturma, gecerlilik) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (code_id, user_id, rules.code_digest(code, code_id), admin_id, moment, expires))
    return rules.readable_code(code), expires


def list_applications(core, admin_id: int, *, durum: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Başvurular, yeniden eskiye. `durum` yoksa bekleyen ve reddedilenler; varsa yalnız o
    durum ('bekliyor' | 'onaylandi' | 'reddedildi'). Önce 90 günü dolan onaylanmamış başvurular silinir."""
    if durum is not None and durum not in (applications.PENDING, applications.APPROVED, applications.REJECTED):
        raise ApiError("Başvuru durumu geçersiz.")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ApiError("Sayfa geçersiz.")
    states = [durum] if durum else [applications.PENDING, applications.REJECTED]
    with core.db.transaction() as cur:
        _admin(cur, admin_id)
        applications.forget_old(cur, core.clock.now())
        cur.execute("SELECT id, ad_soyad, eposta, telefon, paket, durum, olusturma, karar_zamani, yonetici_notu, "
                    "kullanici_id FROM basvurular WHERE durum = ANY(%s) ORDER BY olusturma DESC, id LIMIT %s",
                    (states, limit))
        rows = cur.fetchall()
    return [{"id": row["id"], "ad_soyad": row["ad_soyad"], "eposta": row["eposta"], "telefon": row["telefon"],
             "paket": row["paket"], "durum": row["durum"], "olusturma": rules.stamp(row["olusturma"]),
             "karar": rules.stamp(row["karar_zamani"]), "not": row["yonetici_notu"], "kullanici_id": row["kullanici_id"]}
            for row in rows]


def approve_application(core, admin_id: int, application_id: Any, *, bitis: Any, baslangic: Any = None,
                        paket: Any = None, note: Any = None) -> dict[str, Any]:
    """Ödemesi görülen başvurudan hesap açar (kaynak 'basvuru', rol 'kullanici'): MOD_RPA, paket 'rpa_ajan' ise MOD_AJAN,
    paket 'ikiz' ise MOD_AJAN ve MOD_IKIZ satırları [baslangic, bitis] ile yazılır (PACKAGE_MODULES), başvurudaki şifre
    özeti hesaba taşınır (yönetici şifreyi görmez).
    `baslangic` yoksa bugün; `paket` yoksa başvurunun paketi. Ajan ofisi açılmaz: kişi Studio'da kendisi kurar."""
    if not isinstance(application_id, str) or not APPLICATION_ID.fullmatch(application_id):
        raise ApiError("Başvuru bulunamadı.", 404, "BULUNAMADI")
    with core.db.transaction() as cur:
        _admin(cur, admin_id)
        moment, today = core.clock.now(), core.clock.today_tr()
        starts_on = rules.day(baslangic, "Başlangıç") if baslangic else today
        ends_on = rules.day(bitis, "Bitiş")
        if starts_on > ends_on or ends_on < today:
            raise ApiError("Bitiş bugünden ve başlangıçtan önce olamaz.")
        note = _note(note)
        cur.execute("SELECT * FROM basvurular WHERE id = %s FOR UPDATE", (application_id,))
        application = cur.fetchone()
        if application is None:
            raise ApiError("Başvuru bulunamadı.", 404, "BULUNAMADI")
        if application["durum"] != applications.PENDING:
            raise ApiError("Bu başvuru daha önce sonuçlandırılmış.", 409, "SONUCLANDI")
        package = paket or application["paket"]
        if not isinstance(package, str) or package not in applications.PACKAGES:
            raise ApiError("Paket geçersiz.")
        taken = ApiError("Bu e-postayla bir hesap zaten var; başvuruyu reddedip kişiyle görüşün.", 409, "HESAP_VAR")
        cur.execute("SELECT 1 FROM kullanicilar WHERE lower(giris) = lower(%s) LIMIT 1", (application["eposta"],))
        if cur.fetchone():
            raise taken
        try:
            cur.execute("INSERT INTO kullanicilar (giris, ad_soyad, telefon, sifre_hash, rol, durum, kaynak, olusturma, "
                        "guncelleme) VALUES (%s, %s, %s, %s, 'kullanici', 'aktif', 'basvuru', %s, %s) RETURNING id",
                        (application["eposta"], application["ad_soyad"], application["telefon"],
                         application["sifre_hash"], moment, moment))
        except psycopg.errors.UniqueViolation:
            raise taken from None  # opened meanwhile by another request
        user_id = cur.fetchone()["id"]
        modules = list(PACKAGE_MODULES[package])
        for module in modules:
            _write_module(cur, user_id, module, starts_on, ends_on, moment, today)
        # The password now lives only on the account; the application keeps the audit trail.
        cur.execute("UPDATE basvurular SET durum = %s, paket = %s, karar_zamani = %s, karar_veren_id = %s, "
                    "kullanici_id = %s, sifre_hash = '', yonetici_notu = %s WHERE id = %s",
                    (applications.APPROVED, package, moment, admin_id, user_id, note, application_id))
        _event(core, cur, admin_id, "basvuru_onaylandi", f"kullanici:{user_id}", basvuru_id=application_id,
               giris=application["eposta"], paket=package, moduller=modules, baslangic=starts_on.isoformat(),
               bitis=ends_on.isoformat(), **({"not": note} if note else {}))
    return {"kullanici_id": user_id, "eposta": application["eposta"], "paket": package, "bitis": ends_on.isoformat()}


def reject_application(core, admin_id: int, application_id: Any, *, note: Any = None) -> dict[str, Any]:
    """Bekleyen başvuruyu reddeder (gerekçe `note`); kayıtlı şifre özeti silinir, aynı e-posta yeniden başvurabilir."""
    if not isinstance(application_id, str) or not APPLICATION_ID.fullmatch(application_id):
        raise ApiError("Başvuru bulunamadı.", 404, "BULUNAMADI")
    with core.db.transaction() as cur:
        _admin(cur, admin_id)
        note = _note(note)
        cur.execute("UPDATE basvurular SET durum = %s, karar_zamani = %s, karar_veren_id = %s, sifre_hash = '', "
                    "yonetici_notu = %s WHERE id = %s AND durum = %s",
                    (applications.REJECTED, core.clock.now(), admin_id, note, application_id, applications.PENDING))
        if cur.rowcount != 1:
            raise ApiError("Bekleyen başvuru bulunamadı.", 404, "BULUNAMADI")
        _event(core, cur, admin_id, "basvuru_reddedildi", f"basvuru:{application_id}", **({"not": note} if note else {}))
    return {"id": application_id, "durum": applications.REJECTED}


def list_users(core, admin_id: int, *, ara: Any = None, durum: Any = None, limit: int = 100,
               offset: int = 0) -> dict[str, Any]:
    """Hesaplar, yeniden eskiye; `ara` giriş adında ve ad soyadda geçen metin, `durum` 'aktif' | 'pasif'.
    Her satırda modül hakları (bitis None = süresiz) ve `rpa`: Studio lisans durumu (aktif | sure_doldu | yetki_yok;
    yönetici rolü satırsız da aktiftir)."""
    if ara is not None and (not isinstance(ara, str) or len(ara) > 150):
        raise ApiError("Arama metni en fazla 150 karakter olabilir.")
    if durum is not None and durum not in ("aktif", "pasif"):
        raise ApiError("Hesap durumu geçersiz.")
    if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
        raise ApiError("Sayfa geçersiz.")
    where, values = [], []
    if ara and ara.strip():
        # Lowered by PostgreSQL on both sides (Python lowers "İ" differently); LIKE wildcards are escaped.
        pattern = "%" + re.sub(r"([\\%_])", r"\\\1", ara.strip()) + "%"
        where.append("(lower(giris) LIKE lower(%s) OR lower(ad_soyad) LIKE lower(%s))")
        values += [pattern, pattern]
    if durum:
        where.append("durum = %s")
        values.append(durum)
    condition = (" WHERE " + " AND ".join(where)) if where else ""
    with core.db.transaction() as cur:
        _admin(cur, admin_id)
        today = core.clock.today_tr()
        cur.execute("SELECT count(*) AS n FROM kullanicilar" + condition, values)
        total = cur.fetchone()["n"]
        cur.execute("SELECT k.*, EXISTS (SELECT 1 FROM oturumlar o WHERE o.kullanici_id = k.id AND o.durum = 'aktif') "
                    "AS oturum_acik FROM kullanicilar k" + condition + " ORDER BY k.id DESC LIMIT %s OFFSET %s",
                    values + [limit, offset])
        result = []
        for row in cur.fetchall():
            rights = users.modules(cur, row["id"])
            result.append({
                "kullanici_id": row["id"], "giris": row["giris"], "ad_soyad": row["ad_soyad"], "telefon": row["telefon"],
                "rol": row["rol"], "durum": row["durum"], "kaynak": row["kaynak"], "acilis": rules.stamp(row["olusturma"]),
                "oturum_acik": row["oturum_acik"], "rpa": users.module_state(cur, row, users.RPA, today)[0],
                "ofis_siniri": row["ofis_siniri"],
                "moduller": {module: {"baslangic": _iso(value["baslangic"]), "bitis": _iso(value["bitis"])}
                             for module, value in sorted(rights.items())},
            })
    return {"kullanicilar": result, "toplam": total}


def create_user(core, admin_id: int, *, giris: Any, ad_soyad: Any, baslangic: Any, bitis: Any, ajan: Any = False,
                ikiz: Any = False, telefon: Any = None, sifre: Any = None, onizleme: bool = False) -> dict[str, Any]:
    """Yönetimden hesap açar: kaynak 'yonetim', MOD_RPA (ajan ise MOD_AJAN; ikiz ise Dijital İkiz paketi gibi MOD_AJAN
    ve MOD_IKIZ) [baslangic, bitis]. `sifre` verilirse hesap
    o şifreyle açılır ve kod üretilmez; verilmezse şifre kimsenin bilmediği rastgele bir değerdir ve 7 gün geçerli ilk
    şifre kodu döner (kişi Studio'da "Şifre kodum var" ile kendi şifresini belirler; kod None değilse gösterilir).
    `onizleme` doğruysa hiçbir şey yazılmaz, yalnız denetim sonucu döner."""
    if not isinstance(giris, str) or not rules.LOGIN.fullmatch(giris):
        raise ApiError("Kullanıcı adı 3-64 karakter (harf, rakam, . _ + -) ya da geçerli bir e-posta olmalıdır.")
    password = None if sifre is None else rules.password(sifre, giris)
    name = rules.text(ad_soyad, "Ad soyad", 2, 120)
    phone = rules.phone(telefon)
    starts_on, ends_on = rules.day(baslangic, "Başlangıç"), rules.day(bitis, "Bitiş")
    if starts_on > ends_on:
        raise ApiError("Başlangıç bitişten sonra olamaz.")
    if type(ajan) is not bool:
        raise ApiError("Ajanlar seçimi geçersiz.")
    if type(ikiz) is not bool:
        raise ApiError("Dijital İkiz seçimi geçersiz.")
    modules = [users.RPA] + ([users.AGENTS] if ajan or ikiz else []) + ([users.IKIZ] if ikiz else [])
    password_hash = None if onizleme else generate_password_hash(password or secrets.token_urlsafe(48))
    taken = ApiError("Bu kullanıcı adıyla bir hesap zaten var; başka bir ad seçin.", 409, "HESAP_VAR")
    with core.db.transaction() as cur:
        _admin(cur, admin_id)
        moment, today = core.clock.now(), core.clock.today_tr()
        if not starts_on <= today <= ends_on:
            raise ApiError("Lisans dönemi bugünü kapsamalıdır.")
        # A user name also signs in as the part before "@" of any address, so neither may be taken; openings of the
        # same short name are taken in turn (the unique index guards only the full login).
        short = giris.split("@", 1)[0].lower()
        advisory_lock(cur, f"peyrev-giris-{short}")
        if "@" in giris:
            cur.execute("SELECT 1 FROM kullanicilar WHERE lower(giris) = lower(%s) LIMIT 1", (giris,))
        else:
            cur.execute("SELECT 1 FROM kullanicilar WHERE lower(giris) = lower(%s) "
                        "OR lower(split_part(giris, '@', 1)) = lower(%s) LIMIT 1", (giris, giris))
        if cur.fetchone():
            raise taken
        summary = {"giris": giris, "ad_soyad": name, "moduller": modules, "baslangic": starts_on.isoformat(),
                   "bitis": ends_on.isoformat()}
        if onizleme:
            return {"durum": "hazir", **summary}
        try:
            cur.execute("INSERT INTO kullanicilar (giris, ad_soyad, telefon, sifre_hash, rol, durum, kaynak, olusturma, "
                        "guncelleme) VALUES (%s, %s, %s, %s, 'kullanici', 'aktif', 'yonetim', %s, %s) RETURNING id",
                        (giris, name, phone, password_hash, moment, moment))
        except psycopg.errors.UniqueViolation:
            raise taken from None
        user_id = cur.fetchone()["id"]
        for module in modules:
            _write_module(cur, user_id, module, starts_on, ends_on, moment, today)
        if password is not None:
            # The event says only that the administrator chose the password, never the password itself.
            _event(core, cur, admin_id, "kullanici_acildi", f"kullanici:{user_id}", **summary, sifre="yonetici_belirledi")
            return {"kullanici_id": user_id, "giris": giris, "kod": None, "gecerlilik": None}
        code, expires = _issue_code(cur, admin_id, user_id, moment, timedelta(days=INITIAL_CODE_DAYS))
        _event(core, cur, admin_id, "kullanici_acildi", f"kullanici:{user_id}", **summary,
               kod_gecerlilik=expires.isoformat())
    return {"kullanici_id": user_id, "giris": giris, "kod": code, "gecerlilik": rules.stamp(expires)}


def set_module(core, admin_id: int, user_id: int, modul: Any, *, bitis: Any, baslangic: Any = None) -> dict[str, Any]:
    """Modül hakkını verir ya da günceller. `bitis` None = süresiz (bitiş günü dahil); bugünden önce olamaz, erişimi
    kapatmak için bugün seçilir ya da remove_module çağrılır. `baslangic` bilgi amaçlıdır (lisans ona bakmaz)."""
    if modul not in MODULES:
        raise ApiError("Modül geçersiz.")
    with core.db.transaction() as cur:
        _admin(cur, admin_id)
        moment, today = core.clock.now(), core.clock.today_tr()
        ends_on = rules.day(bitis, "Bitiş") if bitis is not None else None
        if ends_on is not None and ends_on < today:
            raise ApiError("Bitiş bugünden önce olamaz; erişimi kapatmak için bugünü seçin.")
        starts_on = rules.day(baslangic, "Başlangıç") if baslangic is not None else None
        if starts_on is not None and ends_on is not None and starts_on > ends_on:
            raise ApiError("Başlangıç bitişten sonra olamaz.")
        _account(cur, user_id)
        before, after = _write_module(cur, user_id, modul, starts_on, ends_on, moment, today)
        _event(core, cur, admin_id, "modul_ayarlandi", f"kullanici:{user_id}", modul=modul, **after, onceki=before)
    return {"kullanici_id": user_id, "modul": modul, **after}


def remove_module(core, admin_id: int, user_id: int, modul: Any) -> dict[str, Any]:
    """Modül hakkını kaldırır. Açık Studio oturumu kapanmaz: bir sonraki yenilemede 403 YETKI_YOK alır."""
    if modul not in MODULES:
        raise ApiError("Modül geçersiz.")
    with core.db.transaction() as cur:
        _admin(cur, admin_id)
        _account(cur, user_id)
        cur.execute("DELETE FROM kullanici_modulleri WHERE kullanici_id = %s AND modul = %s RETURNING baslangic, bitis",
                    (user_id, modul))
        before = cur.fetchone()
        if before is not None:
            _event(core, cur, admin_id, "modul_kaldirildi", f"kullanici:{user_id}", modul=modul,
                   onceki={"baslangic": _iso(before["baslangic"]), "bitis": _iso(before["bitis"])})
    return {"kullanici_id": user_id, "modul": modul, "kaldirildi": before is not None}


def issue_password_code(core, admin_id: int, user_id: int, *, hours: int = CODE_HOURS) -> dict[str, Any]:
    """Tek kullanımlık şifre kodu: {"kod": "ABCD-EFGH-JKLM", "gecerlilik": "2026-10-27 12:00"} (Türkiye saati).
    Düz kod yalnız bu dönüşte görünür, veritabanında özeti durur; önceki kullanılmamış kodlar geçersizleşir.
    Pasif hesaba da üretilir ama hesap aktif olana kadar kullanılamaz."""
    if type(hours) is not int or not 1 <= hours <= 24 * INITIAL_CODE_DAYS:
        raise ApiError("Kod süresi geçersiz.")
    with core.db.transaction() as cur:
        _admin(cur, admin_id)
        _account(cur, user_id)
        code, expires = _issue_code(cur, admin_id, user_id, core.clock.now(), timedelta(hours=hours))
        _event(core, cur, admin_id, "sifre_kodu_uretildi", f"kullanici:{user_id}", gecerlilik=expires.isoformat())
    return {"kod": code, "gecerlilik": rules.stamp(expires)}


def close_sessions(core, admin_id: int, user_id: int, *, session_id: Any = None) -> dict[str, Any]:
    """Hesabın aktif Studio oturumlarını (ya da yalnız `session_id`'yi) YONETICI nedeniyle kapatır; Studio bir sonraki
    yenilemede (en geç 10 dk) "Oturumunuz yönetici tarafından kapatıldı" ile giriş ekranına döner."""
    if session_id is not None and (not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id)):
        raise ApiError("Oturum bulunamadı.", 404, "BULUNAMADI")
    with core.db.transaction() as cur:
        _admin(cur, admin_id)
        _account(cur, user_id)
        closed = sessions.close_all(cur, user_id, "YONETICI", core.clock.now(), session_id)
        if closed:
            _event(core, cur, admin_id, "oturumlar_kapatildi", f"kullanici:{user_id}", adet=closed,
                   **({"oturum_id": session_id} if session_id else {}))
    return {"kullanici_id": user_id, "kapatilan": closed}


def set_status(core, admin_id: int, user_id: int, durum: Any) -> dict[str, Any]:
    """Hesabı 'aktif' ya da 'pasif' yapar. Pasif hesap giriş yapamaz ve aktif oturumları YONETICI ile kapanır."""
    if durum not in ("aktif", "pasif"):
        raise ApiError("Hesap durumu geçersiz.")
    with core.db.transaction() as cur:
        _admin(cur, admin_id)
        if durum == "pasif" and user_id == admin_id:
            raise ApiError("Kendi hesabınızı pasif yapamazsınız.")
        user = _account(cur, user_id)
        moment = core.clock.now()
        if user["durum"] != durum:
            cur.execute("UPDATE kullanicilar SET durum = %s, guncelleme = %s WHERE id = %s", (durum, moment, user_id))
        closed = sessions.close_all(cur, user_id, "YONETICI", moment) if durum == "pasif" else 0
        if user["durum"] != durum or closed:
            _event(core, cur, admin_id, "durum_degisti", f"kullanici:{user_id}", onceki=user["durum"], durum=durum,
                   kapatilan_oturum=closed)
    return {"kullanici_id": user_id, "durum": durum, "kapatilan": closed}
