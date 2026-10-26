"""Hesap kuralları: alan doğrulaması, şifre özeti ve kodları, deneme sınırları.

Metinler sabittir; Studio, başvuru formu ve yönetim paneli onları olduğu gibi
gösterir. Doğrulama hataları 400 GECERSIZ'dir.

Deneme sınırları iki ayrı sayaçtır, ikisinde de 15 dakikada hesap başına 10, adres başına 30 hatalı
deneme; sınıra şifre denetiminden önce bakılır, yani sınırdaki doğru şifre de 429 alır:
- giris_denemeleri: Studio lisans girişi ve yönetim paneli.
- hesap_izleri: şifre değiştirme ve şifre kodu ("sifre"), başvurular ("basvuru").
Hesap sayacı yazılan ada ve eşleşen her hesap (Studio girişinde bekleyen başvuru da) için o kaydın asıl adıyla ya da
@ öncesiyle yazılmış hatalı denemelerin toplamına bakar; tek hesaba inen hatalı deneme asıl adla yazılır, adlar veritabanında küçük harfe indirilir
(find_by_login gibi). Kısa ad, e-postanın tamamı ve büyük/küçük harf yazımları arasında geçmek yeni deneme hakkı vermez.
"""

from __future__ import annotations

import functools
import hashlib
import re
import secrets
from collections.abc import Iterable
from datetime import date, datetime, timedelta

from psycopg import Cursor
from werkzeug.security import check_password_hash, generate_password_hash

from ..clock import TR
from ..common import ApiError

EMAIL = re.compile(r'[^@\s<>"]{1,64}@[^@\s<>"]+\.[^@\s<>"]{2,}')
PHONE = re.compile(r"\+?[0-9 ()\-]{7,32}")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
# Yönetimden açılan hesabın giriş adı: kullanıcı adı ya da e-posta, yalnız ASCII.
LOGIN = re.compile(r"[A-Za-z0-9._+\-]{3,64}(?:@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})?")
# 32 simge (I, O, 0 ve 1 yok): 12 karakterlik kod 60 bit taşır.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

ATTEMPT_WINDOW = timedelta(minutes=15)
ATTEMPTS_PER_ACCOUNT, ATTEMPTS_PER_ADDRESS = 10, 30
KEEP_LOGIN_ATTEMPTS = timedelta(days=1)
KEEP_TRACES = timedelta(days=2)
TOO_MANY_ATTEMPTS = "Çok fazla hatalı deneme. 15 dakika sonra tekrar deneyin."
AMBIGUOUS = "Bu kullanıcı adı birden fazla hesapla eşleşiyor. E-posta adresinizin tamamını yazın."
# Eski sistemden taşınan özetlerin biçimi (Werkzeug 2.0); Peyrev'in kendi özetleri Werkzeug'un varsayılanıdır.
MIGRATED_METHOD = "pbkdf2:sha256:260000"
# Saklanmış özetlerin bütün biçimleri; yalnız eklenir. Werkzeug'un varsayılanı değişirse yenisi buraya eklenir (test
# uyarır): eski biçimdeki hesaplar da sahte özetle eşitlenmeye devam eder.
STORED_METHODS = ("scrypt:32768:8:1", MIGRATED_METHOD)


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


@functools.cache
def _decoys() -> dict[str, str]:
    """Özet biçimi → sahte özet: önce Werkzeug'un bugünkü varsayılanı, sonra saklanmış her biçim."""
    made = [generate_password_hash(secrets.token_urlsafe(16))]
    made += [generate_password_hash(secrets.token_urlsafe(16), method=method) for method in STORED_METHODS]
    return {value.split("$", 1)[0]: value for value in made}


def hash_methods() -> tuple[str, ...]:
    """Denetim süresi eşitlenen özet biçimleri; ilki yeni özetlerinki."""
    return tuple(_decoys())


def equalize(checked: Iterable[str | None], candidate: str) -> None:
    """Eşleşmeyen girişin süresi hesabın varlığını ve özetinin biçimini belli etmesin: bu denemede denetlenmemiş her
    özet biçimi için bir sahte özet denetlenir. Bilinmeyen ad, tek hesaba ya da tek bekleyen başvuruya uyan ad aynı
    sayıda özet denetler; birden çok hesaba (ya da hesap ve başvuruya) uyan kısa ad her fazla eşleşme için bir tane daha."""
    seen = {(value or "").split("$", 1)[0] for value in checked}
    for method, decoy in _decoys().items():
        if method not in seen:
            matches(decoy, candidate)


def _identity_groups(login: str, identities: Iterable[dict]) -> tuple[list[int], list[str]]:
    """Tek sorguluk sayım için (grup, ad) çiftleri: 0. grup yazılan ad, sonra her kimlik (hesap ya da bekleyen başvuru,
    {"giris": …}) için asıl adı ve @ öncesi (saklandıkları gibi kırpılmış)."""
    groups = [[login[:150]]] + [list(dict.fromkeys([item["giris"][:150], item["giris"].split("@", 1)[0][:150]]))
                                for item in identities]
    pairs = [(index, name) for index, names in enumerate(groups) for name in names]
    return [index for index, _ in pairs], [name for _, name in pairs]


def failed_name(login: str, accounts: list[dict]) -> str:
    """Hatalı denemenin yazıldığı ad: tek hesaba inen ad o hesabın asıl giriş adıdır."""
    return accounts[0]["giris"] if len(accounts) == 1 else login


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


def login_attempts_left(cur: Cursor, login: str, address: str, moment: datetime, accounts: Iterable[dict] = ()) -> None:
    """`accounts`: yazılan adla eşleşen hesaplar (find_by_login) ve bekleyen başvurular; her biri için kendi toplamına
    bakılır. Sayım tek sorgudur: yanıt süresi eşleşen kayıt sayısına bağlı değildir."""
    since = moment - ATTEMPT_WINDOW
    groups, names = _identity_groups(login, accounts)
    cur.execute("SELECT (SELECT coalesce(max(n), 0) FROM (SELECT count(d.zaman) AS n FROM unnest(%s::int[], %s::text[]) "
                "AS a(grp, name) LEFT JOIN giris_denemeleri d ON d.zaman > %s AND lower(d.giris) = lower(a.name) "
                "GROUP BY a.grp) AS per_group) AS hesap, "
                "(SELECT count(*) FROM giris_denemeleri WHERE zaman > %s AND ip = %s) AS adres",
                (groups, names, since, since, address))
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


def password_attempts_left(cur: Cursor, login: str, address: str, moment: datetime,
                           accounts: Iterable[dict] = ()) -> None:
    """Şifre değiştirme ve şifre kodu aynı sayacı paylaşır; ad kuralı ve tek sorgu girişteki gibidir."""
    since = moment - ATTEMPT_WINDOW
    groups, names = _identity_groups(login, accounts)
    cur.execute("SELECT (SELECT coalesce(max(n), 0) FROM (SELECT count(h.zaman) AS n FROM unnest(%s::int[], %s::text[]) "
                "AS a(grp, name) LEFT JOIN hesap_izleri h ON h.tur = 'sifre' AND h.zaman > %s "
                "AND h.anahtar = 'hesap:' || lower(a.name) GROUP BY a.grp) AS per_group) AS hesap, "
                "(SELECT count(*) FROM hesap_izleri WHERE tur = 'sifre' AND zaman > %s AND anahtar = %s) AS adres",
                (groups, names, since, since, ("adres:" + address)[:170]))
    row = cur.fetchone()
    if row["hesap"] >= ATTEMPTS_PER_ACCOUNT or row["adres"] >= ATTEMPTS_PER_ADDRESS:
        raise ApiError(TOO_MANY_ATTEMPTS, 429, "COK_DENEME")


def password_failed(cur: Cursor, account: str, address: str, moment: datetime) -> None:
    # Lowered by the database like the lookups ('İ' becomes 'i' there; Python's lower() gives 'i̇').
    cur.execute("INSERT INTO hesap_izleri (tur, anahtar, zaman) VALUES ('sifre', 'hesap:' || lower(%s), %s)",
                (account[:150], moment))
    trace(cur, "sifre", "adres:" + address, moment)
    forget_old_traces(cur, moment)
