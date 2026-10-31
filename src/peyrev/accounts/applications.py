"""Bireysel başvuru: POST /api/rpa/basvuru (peyrev.com/#basvuru formu, oturumsuz).

Başvuru lisans vermez: şifre yalnız özet olarak saklanır; yönetici ödemeyi görünce başvuruyu onaylar (admin.py) ve hesap,
özet aynen taşınarak açılır. Yeni başvuru, var olan hesap, bekleyen başvuru ve bal küpü aynı yanıtı alır: form hangi
adresin hesabı olduğunu belli etmez. Onaylanmayan (bekleyen ya da reddedilmiş) başvurular 90 gün sonra silinir.

Studio'daki "Lisansı uzat" bu formu açar: hesabı olan adresin gönderdiği form uzatma isteğidir. Satır açılmaz; istek
hesabın olaylarına (yonetim_olaylari, tür uzatma_istegi, istenen paketle) yazılır ve yöneticiye bildirilir. Form kimlik
doğrulamaz: isteği hesabın sahibi göndermemiş olabilir, yönetici kişiyle görüşerek uzatır.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from flask import Blueprint
from psycopg import Cursor
from psycopg.types.json import Jsonb
from werkzeug.security import generate_password_hash

from ..common import ApiError, client_ip, json_body, ok
from ..support import notify
from . import rules

PACKAGES = {"rpa": "Peyrev", "rpa_ajan": "Peyrev + Ajanlar", "ikiz": "Peyrev Dijital İkiz"}
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


def pending(cur: Cursor, login: str) -> list[dict]:
    """Giriş adına uyan, ödeme bekleyen başvurular ({"giris": e-posta, "sifre_hash": …}). Şifre bunlardan birini tutarsa
    giriş hatalı deneme sayılmaz, başvurunun beklediği söylenir; deneme sınırında hesap gibi sayılırlar (onayda başvurunun
    şifresi hesabın şifresi olur)."""
    if "@" in login:
        cur.execute("SELECT eposta AS giris, sifre_hash FROM basvurular WHERE durum = 'bekliyor' "
                    "AND lower(eposta) = lower(%s) LIMIT 5", (login,))
    else:
        cur.execute("SELECT eposta AS giris, sifre_hash FROM basvurular WHERE durum = 'bekliyor' "
                    "AND lower(split_part(eposta, '@', 1)) = lower(%s) LIMIT 5", (login,))
    return [row for row in cur.fetchall() if row["sifre_hash"]]


def forget_old(cur: Cursor, moment: datetime) -> None:
    cur.execute("DELETE FROM basvurular WHERE durum IN ('bekliyor', 'reddedildi') AND olusturma < %s",
                (moment - KEEP_APPLICATIONS,))


def _store(core, cur: Cursor, values: dict, address: str) -> tuple[str, int | None]:
    """Başvuruyu yazar: ("yeni", None) yeni satır açıldı; ("uzatma", hesap) hesabı olan adresin uzatma isteği kaydedildi;
    ("", None) yazılacak bir şey yok (aynı e-postayla bekleyen başvuru var)."""
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
    cur.execute("SELECT id FROM kullanicilar WHERE lower(giris) = lower(%s) LIMIT 1", (values["eposta"],))
    account = cur.fetchone()
    if account:
        cur.execute("INSERT INTO yonetim_olaylari (yonetici_id, tur, hedef, ayrinti, zaman) "
                    "VALUES (NULL, 'uzatma_istegi', %s, %s, %s)",
                    (f"kullanici:{account['id']}", Jsonb({"paket": values["paket"]}), moment))
        return "uzatma", account["id"]
    # A second application while one waits opens no row; the first application's password stays.
    cur.execute("INSERT INTO basvurular (id, ad_soyad, eposta, telefon, sifre_hash, paket, ip, aydinlatma_surumu, olusturma)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                (uuid.uuid4().hex, values["ad_soyad"], values["eposta"], values["telefon"],
                 digest, values["paket"], address, CONSENT_VERSION, moment))
    return ("yeni" if cur.fetchone() is not None else ""), None


def _notify(core, outcome: str, account: int | None, name: str, package: str) -> None:
    """Destek bildirimleriyle aynı kanallardan (Telegram, e-posta); gönderilemese de başvuru kalır."""
    base = core.settings.public_base
    if outcome == "yeni":
        notify.deliver(core, "Yeni Peyrev başvurusu", f"{name} · {PACKAGES[package]}\nOnay bekliyor: {base}/yonetim/")
    elif outcome == "uzatma":
        notify.deliver(core, "Lisans uzatma isteği", f"{name} · {PACKAGES[package]} (form; gönderen doğrulanmadı)\n"
                                                     f"Hesap: {base}/yonetim/#kullanici-{account}")


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
            outcome, account = _store(core, cur, values, client_ip())
        _notify(core, outcome, account, name, package)
        return ok(201, message=RESPONSE_RECEIVED)

    return blueprint
