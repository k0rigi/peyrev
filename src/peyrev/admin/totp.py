"""İki adımlı doğrulama: TOTP (RFC 6238) ve gizli anahtarın veritabanında mühürlü saklanması.

Kod HMAC-SHA1 ile 30 saniyelik adımlardan üretilen 6 hanedir (Google Authenticator, Microsoft Authenticator, 1Password
ve benzerleriyle uyumlu); saat kayması için bir önceki ve bir sonraki adım da kabul edilir. Hesaplama yalnız standart
kütüphaneyle yapılır. Gizli anahtar geri okunabilmesi gerektiğinden özetlenemez: kullanicilar.totp_gizli kolonunda AES-GCM
ile mühürlenir. Anahtar sunucu sırrından amaca özel türetilir (Keys.derive), ek veri hesap kimliğidir; yalnız
veritabanı sızarsa ikinci adım açılmaz, mühür başka bir hesabın satırına taşınamaz.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import struct
from datetime import datetime
from urllib.parse import quote

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

STEP_SECONDS = 30
DIGITS = 6
DRIFT_STEPS = 1
SECRET_BYTES = 20  # 160 bit, RFC 4226'nın önerdiği uzunluk; base32 ile dolgusuz 32 karakter
ISSUER = "Peyrev"
PURPOSE = "peyrev-yonetim-totp"
SEALED = "v1:"
CODE = re.compile(r"\d{6}")


def new_secret() -> str:
    return base64.b32encode(os.urandom(SECRET_BYTES)).decode("ascii")


def readable(secret: str) -> str:
    """Elle yazmak için dörtlü gruplar: ABCD EFGH ..."""
    return " ".join(secret[index:index + 4] for index in range(0, len(secret), 4))


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(base64.b32decode(secret, casefold=True), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**DIGITS).zfill(DIGITS)


def step_of(moment: datetime) -> int:
    return int(moment.timestamp()) // STEP_SECONDS


def matching_step(secret: str, code: object, moment: datetime, *, after: int | None = None) -> int | None:
    """Kodun geçtiği adım ya da None. `after` verilirse yalnız ondan sonraki adımlar sayılır: kullanılmış kod geçmez.
    Boşluk ve tire yok sayılır (uygulamalar kodu "123 456" diye gösterebilir)."""
    if not isinstance(code, str) or len(code) > 16:
        return None
    code = re.sub(r"[\s-]", "", code)
    if not CODE.fullmatch(code):
        return None
    current = step_of(moment)
    for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
        if (after is None or step > after) and hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def link(secret: str, account: str) -> str:
    """Doğrulama uygulamasına eklemek için otpauth bağlantısı (telefonda dokununca uygulama açılır)."""
    label = quote(f"{ISSUER}:{account}", safe=":@")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={ISSUER}&algorithm=SHA1&digits={DIGITS}"
            f"&period={STEP_SECONDS}")


def seal(keys, user_id: int, secret: str) -> str:
    nonce = os.urandom(12)
    sealed = AESGCM(keys.derive(PURPOSE)).encrypt(nonce, secret.encode("ascii"), f"kullanici:{user_id}".encode())
    return SEALED + base64.urlsafe_b64encode(nonce + sealed).decode("ascii")


def unseal(keys, user_id: int, value: str | None) -> str | None:
    """Mühürlü gizli anahtar; açılamazsa (bozuk değer, başka hesabın mührü, değişmiş sunucu sırrı) None."""
    if not isinstance(value, str) or not value.startswith(SEALED):
        return None
    try:
        raw = base64.urlsafe_b64decode(value[len(SEALED):].encode("ascii"))
        secret = AESGCM(keys.derive(PURPOSE)).decrypt(raw[:12], raw[12:], f"kullanici:{user_id}".encode())
        return secret.decode("ascii")
    except (ValueError, InvalidTag, UnicodeError):
        return None
