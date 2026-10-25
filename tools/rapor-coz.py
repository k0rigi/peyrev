"""Sunucu işleri yapıtındaki şifreli raporu indirir ve çözer (yalnız özel anahtarın sahibi açabilir).

Kullanım: python tools/rapor-coz.py <run_id> [--anahtar credentials/peyrev-rapor-x25519.pem] [--cikti raporlar/]
GitHub belirteci GITHUB_TOKEN ortam değişkeninden ya da `git credential fill` ile alınır; ekrana yazılmaz.
Rapor dosyaları 0600 izinle yazılır.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import subprocess
import urllib.request
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

INFO = b"peyrev-rapor-v1"
REPO = "k0rigi/peyrev"


def token() -> str:
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    answer = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n", text=True,
                            capture_output=True, check=True).stdout
    return next(line.split("=", 1)[1] for line in answer.splitlines() if line.startswith("password="))


def get(url: str, secret: str) -> bytes:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {secret}",
                                                   "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def decrypt(blob: bytes, key_path: Path) -> bytes:
    envelope = json.loads(blob)
    key = serialization.load_pem_private_key(key_path.read_bytes(), None)
    mine = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ephemeral = base64.b64decode(envelope["epk"])
    shared = key.exchange(X25519PublicKey.from_public_bytes(ephemeral))
    aes = HKDF(hashes.SHA256(), 32, salt=None, info=INFO + ephemeral + mine).derive(shared)
    return AESGCM(aes).decrypt(base64.b64decode(envelope["nonce"]), base64.b64decode(envelope["veri"]), INFO)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--anahtar", type=Path, default=Path("credentials/peyrev-rapor-x25519.pem"))
    parser.add_argument("--cikti", type=Path, default=Path("raporlar"))
    args = parser.parse_args()
    secret = token()
    listing = json.loads(get(f"https://api.github.com/repos/{REPO}/actions/runs/{args.run_id}/artifacts", secret))
    args.cikti.mkdir(mode=0o700, parents=True, exist_ok=True)
    for artifact in listing["artifacts"]:
        with zipfile.ZipFile(io.BytesIO(get(artifact["archive_download_url"], secret))) as archive:
            for name in archive.namelist():
                target = args.cikti / f"{args.run_id}-{artifact['name']}.txt"
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(decrypt(archive.read(name), args.anahtar))
                print(target)


if __name__ == "__main__":
    main()
