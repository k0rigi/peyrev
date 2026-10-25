"""Hesap kuralları: alan doğrulaması, şifre özeti ve kodları, deneme sınırları.

Metinler orkestrai'deki bireysel hesap kurallarıyla aynıdır; Studio, başvuru formu ve yönetim paneli onları olduğu gibi
gösterir. Doğrulama hataları 400 GECERSIZ'dir.

Deneme sınırları iki ayrı sayaçtır (orkestrai'deki gibi), ikisinde de 15 dakikada hesap başına 10, adres başına 30 hatalı
deneme; sınıra şifre denetiminden önce bakılır, yani sınırdaki doğru şifre de 429 alır:
- giris_denemeleri: Studio lisans girişi; yazılan giriş adı (büyük/küçük harf duyarsız) ve adres sayılır.
- hesap_izleri: şifre değiştirme ve şifre kodu ("sifre"), başvurular ("basvuru").
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import date, datetime, timedelta

from psycopg import Cursor
from werkzeug.security import check_password_hash

from ..clock import TR
from ..common import ApiError

EMAIL = re.compile(r'[^@\s<>"]{1,64}@[^@\s<>"]+\.[^@\s<>"]{2,}')
PHONE = re.compile(r"\+?[0-9 ()\-]{7,32}")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
# Yönetimden açılan hesabın giriş adı: kullanıcı adı ya da e-posta, yalnız ASCII (orkestrai kullanıcı aracıyla aynı).
LOGIN = re.compile(r"[A-Za-z0-9._+\-]{3,64}(?:@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})?")
# 32 simge (I, O, 0 ve 1 yok): 12 karakterlik kod 60 bit taşır.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

ATTEMPT_WINDOW = timedelta(minutes=15)
ATTEMPTS_PER_ACCOUNT, ATTEMPTS_PER_ADDRESS = 10, 30
KEEP_LOGIN_ATTEMPTS = timedelta(days=1)
KEEP_TRACES = timedelta(days=2)
TOO_MANY_ATTEMPTS = "Çok fazla hatalı deneme. 15 dakika sonra tekrar deneyin."
AMBIGUOUS = "Bu kullanıcı adı birden fazla hesapla eşleşiyor. E-posta adresinizin tamamını yazın."


def text(value: object, label: str, minimum: int, maximum: int) -> str:
    """Kırpılmış uzunluk sınırı; boşluklar teke iner, denetim karakteri ve < > reddedilir."""
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise ApiError(f"{label} {minimum}-{maximum} karakter olmalıdır.")
    value = " ".join(value.split())
    if any(ord(char) < 32 for char in value) or "<" in value or ">" in value:
        raise ApiError(f"{label} geçersiz karakter içeriyor.")
    return value


def email(value: object) -> str:
    """Kırpılmış hali saklanır, harfler korunur (karşılaştırmalar lower() ile yapılır)."""
    if not isinstance(value, str) or len(value.strip()) > 150 or not EMAIL.fullmatch(value.strip()):
        raise ApiError("Geçerli bir e-posta adresi yazın.")
    return value.strip()


def phone(value: object) -> str | None:
    value = value or None
    if value is None:
        return None
    if not isinstance(value, str) or not PHONE.fullmatch(value.strip()):
        raise ApiError("Telefonu rakamlarla yazın (ör. +90 5xx xxx xx xx).")
    return " ".join(value.split())


def password(value: object, login: str = "") -> str:
    """8-128 karakter (kırpılmaz), en az bir harf ve bir rakam; giriş adının tamamı ya da @ öncesi olamaz."""
    if not isinstance(value, str) or not 8 <= len(value) <= 128:
        raise ApiError("Şifre 8-128 karakter olmalıdır.")
    if not re.search(r"[^\W\d_]", value) or not re.search(r"\d", value):
        raise ApiError("Şifrede en az bir harf ve bir rakam bulunmalıdır.")
    if login and value.casefold() in (login.casefold(), login.split("@", 1)[0].casefold()):
        raise ApiError("Şifre e-posta adresinizle aynı olamaz.")
    return value


def day(value: object, label: str) -> date:
    if not isinstance(value, str) or not DAY.fullmatch(value):
        raise ApiError(f"{label} YYYY-AA-GG biçiminde olmalıdır.")
    try:
        return date(*[int(part) for part in value.split("-")])
    except ValueError:
        raise ApiError(f"{label} geçersiz.") from None


def matches(password_hash: str | None, candidate: str) -> bool:
    """Werkzeug özeti: taşınan pbkdf2:sha256:260000$… ve yeni scrypt özetleri; tanınmayan biçim eşleşmez."""
    try:
        return check_password_hash(password_hash or "", candidate)
    except ValueError:
        return False


def code_digest(code: str, salt: str) -> str:
    """Kod yalnız kendi kayıt kimliğiyle tuzlanmış özet olarak saklanır; tire, boşluk ve küçük harf kabul edilir."""
    normalized = re.sub(r"[^A-Z0-9]", "", (code or "").upper())
    return hashlib.sha256(("rpa-sifre-kodu:" + salt + ":" + normalized).encode("utf-8")).hexdigest()


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))


def readable_code(code: str) -> str:
    """ABCDEFGHJKLM → ABCD-EFGH-JKLM"""
    return "-".join(code[index:index + 4] for index in range(0, 12, 4))


def stamp(value: datetime | None) -> str | None:
    """Panelde gösterilen Türkiye saati, ör. 2026-10-26 12:00."""
    return value.astimezone(TR).strftime("%Y-%m-%d %H:%M") if value is not None else None


def login_attempts_left(cur: Cursor, login: str, address: str, moment: datetime) -> None:
    cur.execute("SELECT count(*) FILTER (WHERE lower(giris) = lower(%s)) AS hesap, count(*) FILTER (WHERE ip = %s) AS adres"
                " FROM giris_denemeleri WHERE zaman > %s", (login[:150], address, moment - ATTEMPT_WINDOW))
    row = cur.fetchone()
    if row["hesap"] >= ATTEMPTS_PER_ACCOUNT or row["adres"] >= ATTEMPTS_PER_ADDRESS:
        raise ApiError(TOO_MANY_ATTEMPTS, 429, "COK_DENEME")


def login_failed(cur: Cursor, login: str, address: str, moment: datetime) -> None:
    cur.execute("INSERT INTO giris_denemeleri (giris, ip, zaman) VALUES (%s, %s, %s)", (login[:150], address[:64], moment))
    cur.execute("DELETE FROM giris_denemeleri WHERE zaman < %s", (moment - KEEP_LOGIN_ATTEMPTS,))


def trace(cur: Cursor, kind: str, key: str, moment: datetime) -> None:
    cur.execute("INSERT INTO hesap_izleri (tur, anahtar, zaman) VALUES (%s, %s, %s)", (kind, key[:170], moment))


def count(cur: Cursor, kind: str, key: str, since: datetime) -> int:
    cur.execute("SELECT count(*) AS n FROM hesap_izleri WHERE tur = %s AND anahtar = %s AND zaman > %s",
                (kind, key[:170], since))
    return cur.fetchone()["n"]


def forget_old_traces(cur: Cursor, moment: datetime) -> None:
    cur.execute("DELETE FROM hesap_izleri WHERE zaman < %s", (moment - KEEP_TRACES,))


def password_attempts_left(cur: Cursor, account: str, address: str, moment: datetime) -> None:
    """Şifre değiştirme ve şifre kodu aynı sayacı paylaşır (anahtarda giriş adı küçük harfe indirilir)."""
    since = moment - ATTEMPT_WINDOW
    if (count(cur, "sifre", "hesap:" + account.lower(), since) >= ATTEMPTS_PER_ACCOUNT
            or count(cur, "sifre", "adres:" + address, since) >= ATTEMPTS_PER_ADDRESS):
        raise ApiError(TOO_MANY_ATTEMPTS, 429, "COK_DENEME")


def password_failed(cur: Cursor, account: str, address: str, moment: datetime) -> None:
    trace(cur, "sifre", "hesap:" + account.lower(), moment)
    trace(cur, "sifre", "adres:" + address, moment)
    forget_old_traces(cur, moment)
