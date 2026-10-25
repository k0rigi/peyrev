"""Kullanıcı ve modül hakkı kuralları (firma yok): bütün modüller bu yardımcıları kullanır.

- Giriş adı: "@" içeriyorsa tam `giris` eşleşmesi; içermiyorsa `giris` ya da "@" öncesi (hepsi büyük/küçük harf duyarsız).
- Lisans: kullanıcının modül satırı olmalı ve `bitis` (dahil) bugünden (Europe/Istanbul) önce olmamalı; `bitis` NULL = süresiz.
  Yönetici rolü MOD_RPA için satırsız da aktiftir (orkestrai'deki Admin kuralı); MOD_AJAN her zaman satır ister.
- `alan_id`: lisans ve ajan yanıtlarında `firma_id` olarak gönderilir; Studio yerel ajan verisini bununla bölümler.
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from psycopg import Cursor

RPA, AGENTS = "MOD_RPA", "MOD_AJAN"
ACTIVE, EXPIRED, MISSING = "aktif", "sure_doldu", "yetki_yok"
MAX_MATCHES = 5


def fingerprint(password_hash: str | None) -> str:
    """Şifre değişince bütün Studio belirteçlerini geçersiz kılan kısa iz (özetin kendisi dışarı çıkmaz)."""
    return hashlib.sha256((password_hash or "").encode("utf-8")).hexdigest()[:16]


def find_by_login(cur: Cursor, login: str, *, active_only: bool = True) -> list[dict[str, Any]]:
    login = (login or "").strip()
    if not login:
        return []
    active = " AND durum = 'aktif'" if active_only else ""
    if "@" in login:
        cur.execute("SELECT * FROM kullanicilar WHERE lower(giris) = lower(%s)" + active + " ORDER BY id LIMIT %s",
                    (login, MAX_MATCHES))
    else:
        cur.execute("SELECT * FROM kullanicilar WHERE (lower(giris) = lower(%s) OR lower(split_part(giris, '@', 1)) = lower(%s))"
                    + active + " ORDER BY id LIMIT %s", (login, login, MAX_MATCHES))
    return list(cur.fetchall())


def get(cur: Cursor, user_id: int, *, lock: bool = False) -> dict[str, Any] | None:
    cur.execute("SELECT * FROM kullanicilar WHERE id = %s" + (" FOR UPDATE" if lock else ""), (user_id,))
    return cur.fetchone()


def module_state(cur: Cursor, user: dict[str, Any], module: str, today: date) -> tuple[str, date | None]:
    """(aktif | sure_doldu | yetki_yok, bitiş günü ya da None)."""
    if module == RPA and user.get("rol") == "yonetici":
        return ACTIVE, None
    cur.execute("SELECT bitis FROM kullanici_modulleri WHERE kullanici_id = %s AND modul = %s", (user["id"], module))
    row = cur.fetchone()
    if row is None:
        return MISSING, None
    end = row["bitis"]
    if end is not None and end < today:
        return EXPIRED, end
    return ACTIVE, end


def modules(cur: Cursor, user_id: int) -> dict[str, dict[str, Any]]:
    cur.execute("SELECT modul, baslangic, bitis FROM kullanici_modulleri WHERE kullanici_id = %s", (user_id,))
    return {row["modul"]: {"baslangic": row["baslangic"], "bitis": row["bitis"]} for row in cur.fetchall()}
