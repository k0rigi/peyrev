"""Sunucu sırları: JWT anahtarı, lisans imza anahtarı ve Dijital İkiz giriş anahtarı (ikisi Ed25519).

Dosya biçimi: base64 metin (jwt_secret = 48 rastgele bayt, lisans_ed25519 ve ikiz_ed25519 = 32 baytlık Ed25519 tohumu).
Özel anahtarlar sunucudan çıkmaz; Studio yalnız `GET /api/rpa/lisans/anahtar` ile lisansın açık anahtarını alır.
Dijital İkiz servisi (peyrev.com/ikiz/) yalnız ikiz_ed25519.pub'ı bilir: 32 baytlık ham açık anahtarın standart base64'ü,
herkese açık (0644). Lisans anahtarından ayrıdır: ikiz anahtarı değişse de kurulu Studio'lar etkilenmez.
Kurulum: `python -m peyrev.keys /etc/peyrev` eksik dosyaları 0640 izinle üretir, eksik ya da tutmayan açık anahtar
dosyasını yeniden yazar (sahipliği kurulum betiği verir).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

JWT_FILE = "jwt_secret"
LICENSE_FILE = "lisans_ed25519"
IKIZ_FILE = "ikiz_ed25519"
IKIZ_PUBLIC_FILE = "ikiz_ed25519.pub"
SIZES = {JWT_FILE: 48, LICENSE_FILE: 32, IKIZ_FILE: 32}


def _create(path: Path, size: int, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(base64.b64encode(os.urandom(size)) + b"\n")
    os.chmod(path, mode)


def _read(path: Path, size: int, allow_generate: bool) -> bytes:
    if not path.exists():
        if not allow_generate:
            raise RuntimeError(f"Sır dosyası bulunamadı: {path.name} (kurulum iş akışını çalıştırın).")
        try:
            _create(path, size, 0o600)
        except FileExistsError:
            pass  # Another worker created it first.
    value = base64.b64decode(path.read_bytes().strip(), validate=True)
    if len(value) != size:
        raise RuntimeError(f"Sır dosyası geçersiz: {path.name}")
    return value


class Keys:
    def __init__(self, secrets_dir: Path, allow_generate: bool = False):
        self.jwt_secret = _read(Path(secrets_dir) / JWT_FILE, SIZES[JWT_FILE], allow_generate)
        seed = _read(Path(secrets_dir) / LICENSE_FILE, SIZES[LICENSE_FILE], allow_generate)
        self._license = Ed25519PrivateKey.from_private_bytes(seed)
        self._ikiz = Ed25519PrivateKey.from_private_bytes(
            _read(Path(secrets_dir) / IKIZ_FILE, SIZES[IKIZ_FILE], allow_generate))

    def derive(self, purpose: str) -> bytes:
        """Amaca özel anahtar: belirteç türleri birbirinin yerine geçemez."""
        return hmac.new(self.jwt_secret, purpose.encode("ascii"), hashlib.sha256).digest()

    def sign(self, payload: bytes) -> bytes:
        return self._license.sign(payload)

    def ikiz_sign(self, payload: bytes) -> bytes:
        """Dijital İkiz giriş belirtecinin imzası (lisans anahtarıyla değil, kendi anahtarıyla)."""
        return self._ikiz.sign(payload)

    @property
    def license_public_key(self) -> str:
        return _public(self._license)

    @property
    def ikiz_public_key(self) -> str:
        return _public(self._ikiz)


def _public(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def write_ikiz_public(secrets_dir: Path) -> bool:
    """ikiz_ed25519.pub'ı özel anahtardan (0644) yazar; dosya zaten doğruysa dokunmaz. Yazdıysa True."""
    seed = _read(Path(secrets_dir) / IKIZ_FILE, SIZES[IKIZ_FILE], False)
    text = _public(Ed25519PrivateKey.from_private_bytes(seed)) + "\n"
    path = Path(secrets_dir) / IKIZ_PUBLIC_FILE
    if path.is_file() and path.read_text(encoding="ascii", errors="replace") == text:
        return False
    temporary = path.with_name(IKIZ_PUBLIC_FILE + ".yeni")
    temporary.write_text(text, encoding="ascii")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)
    return True


def generate_missing(secrets_dir: Path) -> list[str]:
    created = []
    for name, size in SIZES.items():
        path = Path(secrets_dir) / name
        if not path.exists():
            _create(path, size)
            created.append(name)
    return created


if __name__ == "__main__":  # pragma: no cover - kurulum iş akışı çalıştırır
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "/etc/peyrev")
    made = generate_missing(target)
    if write_ikiz_public(target):
        made.append(IKIZ_PUBLIC_FILE)
    print("Üretilen sır dosyaları: " + (", ".join(made) if made else "yok (hepsi vardı)"))
