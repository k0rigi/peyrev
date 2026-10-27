"""Yönetici erişimini kurtarma (Sunucu işleri → yonetici-sifre): panele girebilen tek hesabın şifresini unutan sahip için.

Panelde başka yönetici yokken şifre panelden yenilenemez; bu iş sunucuda, uygulamanın kendi kurallarıyla çalışır:
giriş adı ve ad soyad `service.update_details` ile değişir, yeni şifre burada üretilir, hesabın açık şifre kodları
harcanır, panel oturumları kapanır ve olay yonetim_olaylari'na yazılır (şifre yazılmaz). Birden fazla etkin yönetici
varsa iş reddedilir: o durumda diğer yönetici panelden şifre kodu üretir.
"""

from __future__ import annotations

import secrets
import string
from typing import Any

from werkzeug.security import generate_password_hash

from ..accounts import rules
from ..common import ApiError
from . import auth, events, service

ALPHABET = string.ascii_letters + string.digits


def _new_password(login: str) -> str:
    while True:
        password = "".join(secrets.choice(ALPHABET) for _ in range(20))
        try:
            return rules.password(password, login)
        except ApiError:  # no digit or no letter: draw again
            continue


def recover(core, giris: str, ad_soyad: str) -> dict[str, Any]:
    """Tek etkin yöneticinin giriş adını ve adını değiştirir, yeni şifre verir: {"kullanici_id", "giris", "sifre"}."""
    with core.db.transaction() as cur:
        cur.execute("SELECT id, telefon FROM kullanicilar WHERE rol = 'yonetici' AND durum = 'aktif' ORDER BY id")
        admins = cur.fetchall()
    if len(admins) != 1:
        raise ApiError(f"Tek etkin yönetici bekleniyordu, {len(admins)} bulundu; şifre kodu panelden üretilmelidir.")
    admin_id = admins[0]["id"]
    login = service.update_details(core, admin_id, admin_id, ad_soyad=ad_soyad, telefon=admins[0]["telefon"],
                                   giris=giris)["giris"]
    password = _new_password(login)
    with core.db.transaction() as cur:
        moment = core.clock.now()
        cur.execute("UPDATE kullanicilar SET sifre_hash = %s, guncelleme = %s WHERE id = %s",
                    (generate_password_hash(password), moment, admin_id))
        cur.execute("UPDATE sifre_kodlari SET kullanildi = %s WHERE kullanici_id = %s AND kullanildi IS NULL",
                    (moment, admin_id))
        closed = auth.end_sessions(cur, admin_id)
        events.record(core, cur, admin_id, "sifre_sunucuda_yenilendi", f"kullanici:{admin_id}",
                      kapatilan_panel_oturumu=closed)
    return {"kullanici_id": admin_id, "giris": login, "sifre": password}
