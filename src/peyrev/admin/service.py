"""Panelin birinci dalga modüllerinde karşılığı olmayan işlemleri: özet, hesap ayrıntısı, ad/telefon ve rol değişikliği,
başka bir yöneticinin iki adımlı doğrulamasını sıfırlama.

Hesap açma, modül hakkı, şifre kodu, Studio oturumu, hesap durumu ve başvurular peyrev.accounts.admin'de; destek
talepleri peyrev.support'ta; ofis durumu peyrev.agents.admin'de; ortak model peyrev.llm.admin'dedir. Panel onları
çağırır, kuralları burada yinelenmez. Buradaki her işlev de yöneticiyi kendi işleminde yeniden okur (etkin 'yonetici'
rolü; değilse 403 YETKI_YOK) ve her değişikliği yonetim_olaylari'na yazar.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg import Cursor
from werkzeug.security import generate_password_hash

from .. import indirme, users
from ..accounts import applications, rules
from ..agents import admin as agents_admin
from ..common import ApiError
from ..db import advisory_lock
from ..support import unread_count
from . import auth, events

ROLES = ("kullanici", "yonetici")
ENDING_WITHIN = timedelta(days=30)
ENDING_LIST = 50
SESSION_LIST = 20


def require_admin(cur: Cursor, admin_id: Any) -> dict[str, Any]:
    found = users.get(cur, admin_id) if type(admin_id) is int and admin_id >= 1 else None
    if found is None or found["durum"] != "aktif" or found["rol"] != "yonetici":
        raise ApiError("Bu işlem yalnız platform yöneticisine açıktır.", 403, "YETKI_YOK")
    return found


def _account(cur: Cursor, user_id: Any) -> dict[str, Any]:
    user = users.get(cur, user_id, lock=True) if type(user_id) is int and user_id >= 1 else None
    if user is None:
        raise ApiError("Hesap bulunamadı.", 404, "BULUNAMADI")
    return user


def _module_rows(rights: dict[str, dict[str, Any]], today) -> dict[str, dict[str, Any]]:
    """Modül satırları ve satırın kendi durumu (yönetici rolünün satırsız hakkı ayrıca `rpa` alanındadır)."""
    return {module: {"baslangic": _iso(value["baslangic"]), "bitis": _iso(value["bitis"]),
                     "durum": users.ACTIVE if value["bitis"] is None or value["bitis"] >= today else users.EXPIRED}
            for module, value in sorted(rights.items())}


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def summary(core, cur: Cursor, admin_id: int) -> dict[str, Any]:
    """Özet sayıları: aktif hesap, Peyrev'i kullanabilen hesap, 30 gün içinde bitecek hak, bekleyen başvuru, okunmamış
    destek talebi; bitecek hakların ilk 50'si bitiş sırasıyla."""
    require_admin(cur, admin_id)
    moment, today = core.clock.now(), core.clock.today_tr()
    until = today + ENDING_WITHIN
    cur.execute("SELECT count(*) FILTER (WHERE durum = 'aktif') AS aktif, count(*) FILTER (WHERE durum = 'aktif' AND "
                "(rol = 'yonetici' OR EXISTS (SELECT 1 FROM kullanici_modulleri m WHERE m.kullanici_id = k.id AND "
                "m.modul = %s AND (m.bitis IS NULL OR m.bitis >= %s)))) AS lisansli FROM kullanicilar k",
                (users.RPA, today))
    accounts = cur.fetchone()
    ending = ("FROM kullanici_modulleri m JOIN kullanicilar k ON k.id = m.kullanici_id "
              "WHERE k.durum = 'aktif' AND m.bitis BETWEEN %s AND %s")
    cur.execute("SELECT count(DISTINCT m.kullanici_id) AS n " + ending, (today, until))
    ending_count = cur.fetchone()["n"]
    cur.execute("SELECT m.kullanici_id, m.modul, m.bitis, k.giris, k.ad_soyad " + ending
                + " ORDER BY m.bitis, k.giris, m.modul LIMIT %s", (today, until, ENDING_LIST))
    soon = [{"kullanici_id": row["kullanici_id"], "giris": row["giris"], "ad_soyad": row["ad_soyad"],
             "modul": row["modul"], "bitis": row["bitis"].isoformat(), "kalan_gun": (row["bitis"] - today).days}
            for row in cur.fetchall()]
    # Applications older than the keeping period are deleted when the list is opened; they are not counted here.
    cur.execute("SELECT count(*) AS n FROM basvurular WHERE durum = %s AND olusturma >= %s",
                (applications.PENDING, moment - applications.KEEP_APPLICATIONS))
    pending = cur.fetchone()["n"]
    return {"aktif_kullanici": accounts["aktif"], "lisansli_kullanici": accounts["lisansli"],
            "yakinda_bitecek": ending_count, "bitecekler": soon, "bekleyen_basvuru": pending,
            "okunmamis_destek": unread_count(core, cur, admin_id)}


def user_detail(core, admin_id: int, user_id: int) -> dict[str, Any]:
    """Hesap, modül hakları, açık Studio oturumları, ajan ofisleri, destek talebi sayıları ve onaylı başvuru."""
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        user = users.get(cur, user_id)
        if user is None:
            raise ApiError("Hesap bulunamadı.", 404, "BULUNAMADI")
        today = core.clock.today_tr()
        account = {"kullanici_id": user["id"], "giris": user["giris"], "ad_soyad": user["ad_soyad"],
                   "telefon": user["telefon"], "rol": user["rol"], "durum": user["durum"], "kaynak": user["kaynak"],
                   "acilis": rules.stamp(user["olusturma"]), "guncelleme": rules.stamp(user["guncelleme"]),
                   "iki_adim": bool(user["totp_gizli"]), "rpa": users.module_state(cur, user, users.RPA, today)[0],
                   "moduller": _module_rows(users.modules(cur, user_id), today)}
        cur.execute("SELECT id, cihaz, surum, ip, olusturma, son_gorulme FROM oturumlar WHERE kullanici_id = %s "
                    "AND durum = 'aktif' ORDER BY son_gorulme DESC, id LIMIT %s", (user_id, SESSION_LIST))
        studio = [{"id": row["id"], "cihaz": row["cihaz"][:8], "surum": row["surum"], "ip": row["ip"],
                   "acilis": rules.stamp(row["olusturma"]), "son_gorulme": rules.stamp(row["son_gorulme"])}
                  for row in cur.fetchall()]
        cur.execute("SELECT count(*) AS toplam, count(*) FILTER (WHERE durum NOT IN ('yayinlandi', 'kapandi')) AS acik "
                    "FROM destek_talepleri WHERE kullanici_id = %s", (user_id,))
        tickets = cur.fetchone()
        cur.execute("SELECT id, paket, olusturma, karar_zamani FROM basvurular WHERE kullanici_id = %s", (user_id,))
        application = cur.fetchone()
        download = indirme.status(cur, user_id, core.clock.now())
    offices = agents_admin.user_offices(core, user_id)
    for office in offices["ofisler"]:
        for key in ("olusturma", "guncelleme"):
            office[key] = rules.stamp(datetime.fromisoformat(office[key])) if office[key] else None
    return {"kullanici": account, "oturumlar": studio,
            "ofisler": {"sinir": offices["ofis_siniri"], "liste": offices["ofisler"]},
            "destek": {"toplam": tickets["toplam"], "acik": tickets["acik"]}, "indirme": download,
            "basvuru": None if application is None else {
                "id": application["id"], "paket": application["paket"], "olusturma": rules.stamp(application["olusturma"]),
                "karar": rules.stamp(application["karar_zamani"])}}


def update_details(core, admin_id: int, user_id: int, *, ad_soyad: Any, telefon: Any, giris: Any = None) -> dict[str, Any]:
    """Ad soyad, telefon ve isteğe bağlı giriş adı (kurallar yönetimden hesap açmayla aynı: 2-120 karakter, telefon
    isteğe bağlı; giriş adı 3-64 karakter ya da e-posta ve başka bir hesabın adı veya kısa adı olamaz). Giriş adı
    Studio'da ve panelde yeni adla girilir; şifre, haklar ve açık oturumlar değişmez."""
    name = rules.text(ad_soyad, "Ad soyad", 2, 120)
    phone = rules.phone(telefon)
    if giris is not None and (not isinstance(giris, str) or not rules.LOGIN.fullmatch(giris)):
        raise ApiError("Kullanıcı adı 3-64 karakter (harf, rakam, . _ + -) ya da geçerli bir e-posta olmalıdır.")
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        user = _account(cur, user_id)
        login = user["giris"] if giris is None else giris
        changed = [field for field, value in (("giris", login), ("ad_soyad", name), ("telefon", phone))
                   if user[field] != value]
        if "giris" in changed:
            # Same rule as opening an account: a short name also signs in as the part before "@" of an address.
            short = login.split("@", 1)[0].lower()
            advisory_lock(cur, f"peyrev-giris-{short}")
            if "@" in login:
                cur.execute("SELECT 1 FROM kullanicilar WHERE lower(giris) = lower(%s) AND id <> %s LIMIT 1",
                            (login, user_id))
            else:
                cur.execute("SELECT 1 FROM kullanicilar WHERE (lower(giris) = lower(%s) "
                            "OR lower(split_part(giris, '@', 1)) = lower(%s)) AND id <> %s LIMIT 1", (login, login, user_id))
            if cur.fetchone():
                raise ApiError("Bu kullanıcı adıyla bir hesap zaten var; başka bir ad seçin.", 409, "HESAP_VAR")
        if changed:
            try:
                cur.execute("UPDATE kullanicilar SET giris = %s, ad_soyad = %s, telefon = %s, guncelleme = %s "
                            "WHERE id = %s", (login, name, phone, core.clock.now(), user_id))
            except psycopg.errors.UniqueViolation:
                raise ApiError("Bu kullanıcı adıyla bir hesap zaten var; başka bir ad seçin.", 409, "HESAP_VAR") from None
            # Only the names of the fields: the event log keeps no personal values.
            events.record(core, cur, admin_id, "bilgiler_degisti", f"kullanici:{user_id}", alanlar=changed)
    return {"kullanici_id": user_id, "giris": login, "ad_soyad": name, "telefon": phone, "degisti": bool(changed)}


def set_password(core, admin_id: int, user_id: int, sifre: Any) -> dict[str, Any]:
    """Başka bir hesabın şifresini yönetici belirler (kural Studio'yla aynı). Açık şifre kodları harcanır, hesabın panel
    oturumları kapanır; şifre izi değiştiği için Studio, ajan ve destek belirteçleri bir sonraki kullanımda geçersizdir.
    Kendi şifresini yönetici Hesabım'dan, mevcut şifresiyle değiştirir."""
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        if user_id == admin_id:
            raise ApiError("Kendi şifrenizi Hesabım bölümünden değiştirin.")
        user = _account(cur, user_id)
        password = rules.password(sifre, user["giris"])
        moment = core.clock.now()
        cur.execute("UPDATE kullanicilar SET sifre_hash = %s, guncelleme = %s WHERE id = %s",
                    (generate_password_hash(password), moment, user_id))
        cur.execute("UPDATE sifre_kodlari SET kullanildi = %s WHERE kullanici_id = %s AND kullanildi IS NULL",
                    (moment, user_id))
        closed = auth.end_sessions(cur, user_id)
        events.record(core, cur, admin_id, "sifre_belirlendi", f"kullanici:{user_id}", kapatilan_panel_oturumu=closed)
    return {"kullanici_id": user_id, "kapatilan_panel_oturumu": closed}


def set_role(core, admin_id: int, user_id: int, rol: Any) -> dict[str, Any]:
    """'kullanici' ya da 'yonetici'. Yönetici kendi yetkisini kaldıramaz; yetkisi kalkan hesabın panel oturumları kapanır.
    Studio lisansı rolden etkilenir: yönetici MOD_RPA satırı olmadan da Studio'yu kullanır, kullanıcı satır ister."""
    if not isinstance(rol, str) or rol not in ROLES:
        raise ApiError("Rol geçersiz.")
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        if user_id == admin_id and rol != "yonetici":
            raise ApiError("Kendi yönetici yetkinizi kaldıramazsınız.")
        user = _account(cur, user_id)
        closed = 0
        if user["rol"] != rol:
            cur.execute("UPDATE kullanicilar SET rol = %s, guncelleme = %s WHERE id = %s",
                        (rol, core.clock.now(), user_id))
            if rol != "yonetici":
                closed = auth.end_sessions(cur, user_id)
            events.record(core, cur, admin_id, "rol_degisti", f"kullanici:{user_id}", onceki=user["rol"], rol=rol,
                          kapatilan_panel_oturumu=closed)
    return {"kullanici_id": user_id, "rol": rol, "degisti": user["rol"] != rol}


def end_panel_sessions(core, admin_id: int, user_id: int) -> int:
    """Pasifleşen hesabın panel oturumları (hesap durumu accounts.admin.set_status ile değişir); kapanan sayısı."""
    with core.db.transaction() as cur:
        closed = auth.end_sessions(cur, user_id)
        if closed:
            events.record(core, cur, admin_id, "yonetim_oturumlari_kapatildi", f"kullanici:{user_id}", adet=closed)
    return closed


def reset_two_factor(core, admin_id: int, user_id: int) -> dict[str, Any]:
    """Başka bir yöneticinin iki adımlı doğrulamasını kapatır (kodunu kaybeden yönetici için); oturumları kapanır,
    kişi panele şifresiyle girip Hesabım'dan yeniden açar."""
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        if user_id == admin_id:
            raise ApiError("Kendi iki adımlı doğrulamanızı Hesabım bölümünden kapatın.")
        user = _account(cur, user_id)
        if not user["totp_gizli"]:
            raise ApiError("Bu hesapta iki adımlı doğrulama kapalı.", 409, "IKI_ADIM_KAPALI")
        cur.execute("UPDATE kullanicilar SET totp_gizli = NULL, guncelleme = %s WHERE id = %s",
                    (core.clock.now(), user_id))
        closed = auth.end_sessions(cur, user_id)
        events.record(core, cur, admin_id, "iki_adim_sifirlandi", f"kullanici:{user_id}", kapatilan_panel_oturumu=closed)
    return {"kullanici_id": user_id, "iki_adim": False, "kapatilan": closed}


def event_page(core, admin_id: int, *, page: int, kind: str | None, target: str | None) -> dict[str, Any]:
    with core.db.transaction() as cur:
        require_admin(cur, admin_id)
        return events.listing(cur, page=page, kind=kind, target=target)
