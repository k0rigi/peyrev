"""Bireysel başvuru: POST /api/rpa/basvuru (peyrev.com/#basvuru formu, oturumsuz).

Başvuru lisans vermez: şifre yalnız özet olarak saklanır; yönetici ödemeyi görünce başvuruyu onaylar (admin.py) ve hesap,
özet aynen taşınarak açılır. Yeni başvuru, var olan hesap, bekleyen başvuru ve bal küpü aynı yanıtı alır: form hangi
adresin hesabı olduğunu belli etmez. Onaylanmayan (bekleyen ya da reddedilmiş) başvurular 90 gün sonra silinir.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from flask import Blueprint
from psycopg import Cursor
from werkzeug.security import generate_password_hash

from ..common import ApiError, client_ip, json_body, ok
from ..support import notify
from . import rules

PACKAGES = {"rpa": "Peyrev", "rpa_ajan": "Peyrev + Ajanlar"}
# Formdaki bilgilendirme metninin sürümü; metin değişince yeni tarih yazılır.
CONSENT_VERSION = "2026-10-26"
PENDING, APPROVED, REJECTED = "bekliyor", "onaylandi", "reddedildi"
# Adres başına saatte ve günde, hizmetin tamamında günde.
SIGNUPS_PER_HOUR, SIGNUPS_PER_DAY, SIGNUPS_TOTAL_PER_DAY = 5, 20, 300
KEEP_APPLICATIONS = timedelta(days=90)
RESPONSE_RECEIVED = ("Başvurunuz alındı. Fiyat ve ödeme bilgisini başvurudaki e-posta veya telefonla size iletiyoruz. "
                     "Ödemeniz onaylandığında hesabınız açılır; Peyrev'e bu e-posta adresi ve belirlediğiniz şifreyle "
                     "girersiniz. Bu e-postayla daha önce hesap açıldıysa mevcut şifrenizle giriş yapabilirsiniz.")
PENDING_MESSAGE = ("Başvurunuz alındı; ödemeniz onaylandığında hesabınız açılır ve bu e-posta ile şifrenizle "
                   "giriş yapabilirsiniz.")


def pending_hashes(cur: Cursor, login: str) -> list[str]:
    """Giriş adına uyan, ödeme bekleyen başvuruların şifre özetleri. Şifre bunlardan birini tutarsa giriş hatalı deneme
    sayılmaz, başvurunun beklediği söylenir."""
    if "@" in login:
        cur.execute("SELECT sifre_hash FROM basvurular WHERE durum = 'bekliyor' AND lower(eposta) = lower(%s) LIMIT 5",
                    (login,))
    else:
        cur.execute("SELECT sifre_hash FROM basvurular WHERE durum = 'bekliyor' "
                    "AND lower(split_part(eposta, '@', 1)) = lower(%s) LIMIT 5", (login,))
    return [row["sifre_hash"] for row in cur.fetchall() if row["sifre_hash"]]


def forget_old(cur: Cursor, moment: datetime) -> None:
    cur.execute("DELETE FROM basvurular WHERE durum IN ('bekliyor', 'reddedildi') AND olusturma < %s",
                (moment - KEEP_APPLICATIONS,))


def _store(core, cur: Cursor, values: dict, address: str) -> bool:
    """Başvuruyu yazar; yeni satır açıldıysa doğru."""
    moment, key = core.clock.now(), "adres:" + address
    if (rules.count(cur, "basvuru", key, moment - timedelta(hours=1)) >= SIGNUPS_PER_HOUR
            or rules.count(cur, "basvuru", key, moment - timedelta(days=1)) >= SIGNUPS_PER_DAY):
        raise ApiError("Bu bağlantıdan çok fazla başvuru yapıldı. Daha sonra tekrar deneyin.", 429, "COK_DENEME")
    cur.execute("SELECT count(*) AS n FROM hesap_izleri WHERE tur = 'basvuru' AND zaman > %s", (moment - timedelta(days=1),))
    if cur.fetchone()["n"] >= SIGNUPS_TOTAL_PER_DAY:
        raise ApiError("Başvurular şu anda alınamıyor. Lütfen daha sonra tekrar deneyin.", 503, "YOGUNLUK")
    # Counted after the fields are valid and before the account check: an address that has an account counts too.
    rules.trace(cur, "basvuru", key, moment)
    rules.forget_old_traces(cur, moment)
    forget_old(cur, moment)
    # Hashed before the account check: an address that has an account is answered as slowly as a new one.
    digest = generate_password_hash(values["sifre"])
    cur.execute("SELECT 1 FROM kullanicilar WHERE lower(giris) = lower(%s) LIMIT 1", (values["eposta"],))
    if cur.fetchone():
        return False
    # A second application while one waits opens no row; the first application's password stays.
    cur.execute("INSERT INTO basvurular (id, ad_soyad, eposta, telefon, sifre_hash, paket, ip, aydinlatma_surumu, olusturma)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                (uuid.uuid4().hex, values["ad_soyad"], values["eposta"], values["telefon"],
                 digest, values["paket"], address, CONSENT_VERSION, moment))
    return cur.fetchone() is not None


def _notify(core, name: str, package: str) -> None:
    """Destek bildirimleriyle aynı kanallardan (Telegram, e-posta); gönderilemese de başvuru kalır."""
    notify.deliver(core, "Yeni Peyrev başvurusu",
                   f"{name} · {PACKAGES[package]}\nOnay bekliyor: {core.settings.public_base}/yonetim/")


def create_blueprint(core) -> Blueprint:
    blueprint = Blueprint("basvuru", __name__)

    @blueprint.post("/api/rpa/basvuru")
    def apply():
        data = json_body({"ad_soyad", "eposta", "telefon", "sifre", "paket", "onay", "web_sitesi"},
                         {"ad_soyad", "eposta", "sifre", "paket", "onay"})
        if data.get("web_sitesi"):
            return ok(201, message=RESPONSE_RECEIVED)  # a filled hidden field is a bot: same answer, nothing stored
        name = rules.text(data["ad_soyad"], "Ad soyad", 3, 120)
        address = rules.email(data["eposta"])
        phone = rules.phone(data.get("telefon"))
        password = rules.password(data["sifre"], address)
        package = data["paket"]
        if not isinstance(package, str) or package not in PACKAGES:
            raise ApiError("Paket seçin.")
        if data["onay"] is not True:
            raise ApiError("Başvuru için bilgilendirme metnini onaylamanız gerekir.")
        values = {"ad_soyad": name, "eposta": address, "telefon": phone, "paket": package, "sifre": password}
        with core.db.transaction() as cur:
            created = _store(core, cur, values, client_ip())
        if created:
            _notify(core, name, package)
        return ok(201, message=RESPONSE_RECEIVED)

    return blueprint
