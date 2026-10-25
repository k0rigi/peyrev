"""Sunucu işleri: depodaki bir kabuk betiğini SSH ile çalıştırır; çıktıyı günlüğe değil, şifreli rapora yazar.

Depo herkese açık olduğundan Actions günlükleri de herkese açıktır. Sunucu ayrıntıları (yollar, hesaplar, yapılandırma,
kayıt sayıları) günlüğe yazılmaz: betiğin bütün çıktısı sahibinin açık anahtarıyla (.github/rapor-anahtari.pub;
X25519 + HKDF-SHA256 + AES-256-GCM) şifrelenir ve yapıt olarak yüklenir. Günlükte yalnız betiğin "GENEL: " ile başlattığı
satırlar (durum kodu gibi zararsız bilgiler) ve çıkış kodu görünür.

Ortam:
  HOST, USERNAME, PASSWORD   Secrets (SSH). Sunucu anahtarı aşağıda sabittir; başka anahtar reddedilir.
  BETIK                      Depodaki betik yolu (deploy/*.sh).
  BETIK_ORTAMI               İsteğe bağlı JSON nesnesi: betiğe ortam değişkeni olarak geçirilir (adlar büyük harf).
  DOSYALAR                   İsteğe bağlı JSON listesi [{"yerel": ..., "uzak": ...}]: betikten önce SFTP ile yüklenir;
                             uzak yol yalnız /var/tmp/peyrev-aktarim/ altında olabilir.
  RAPOR                      Şifreli rapor dosyası (varsayılan rapor.enc).
  SURE                       Saniye cinsinden üst sınır (varsayılan 1800).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path, PurePosixPath

import paramiko
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# orkestrai.net ile aynı sunucu; açık anahtar, sır değildir (Studio deposundaki yayın betiğiyle aynı).
HOST_ED25519 = "AAAAC3NzaC1lZDI1NTE5AAAAIPUVhfiU2qevA8m7fINyiSWxSOZXczcLVj/KpfRHo/+S"
TRANSFER_ROOT = PurePosixPath("/var/tmp/peyrev-aktarim")
INFO = b"peyrev-rapor-v1"
PUBLIC_PREFIX = "GENEL: "
NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,40}$")


def encrypt(data: bytes, recipient: bytes) -> bytes:
    ephemeral = X25519PrivateKey.generate()
    ephemeral_public = ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient))
    key = HKDF(hashes.SHA256(), 32, salt=None, info=INFO + ephemeral_public + recipient).derive(shared)
    nonce = os.urandom(12)
    sealed = AESGCM(key).encrypt(nonce, data, INFO)
    envelope = {"surum": 1, "epk": base64.b64encode(ephemeral_public).decode(),
                "nonce": base64.b64encode(nonce).decode(), "veri": base64.b64encode(sealed).decode()}
    return json.dumps(envelope).encode()


def command(script: str, environment: dict[str, str]) -> str:
    for name in environment:
        if not NAME.match(name):
            raise SystemExit("BETIK_ORTAMI içinde geçersiz ad.")
    prefix = "".join(f"{name}={shlex.quote(str(value))} " for name, value in environment.items())
    encoded = base64.b64encode(script.encode()).decode()
    return f"env {prefix}bash -c \"$(printf %s '{encoded}' | base64 -d)\" peyrev-betik"


def connect() -> paramiko.SSHClient:
    host = os.environ["HOST"]
    client = paramiko.SSHClient()
    client.get_host_keys().add(host, "ssh-ed25519", paramiko.Ed25519Key(data=base64.b64decode(HOST_ED25519)))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(host, username=os.environ["USERNAME"], password=os.environ["PASSWORD"], look_for_keys=False,
                   allow_agent=False, timeout=30, banner_timeout=30, auth_timeout=30)
    return client


def upload(client: paramiko.SSHClient, files: list[dict[str, str]]) -> None:
    if not files:
        return
    sftp = client.open_sftp()
    try:
        try:
            sftp.mkdir(str(TRANSFER_ROOT), 0o700)
        except OSError:
            pass
        for item in files:
            remote = PurePosixPath(item["uzak"])
            if remote.parent != TRANSFER_ROOT or ".." in remote.parts:
                raise SystemExit("Uzak dosya yolu aktarım klasörünün dışında.")
            sftp.put(item["yerel"], str(remote))
            sftp.chmod(str(remote), 0o600)
    finally:
        sftp.close()


def _echo_public(lines: list[bytes]) -> None:
    for line in lines:
        decoded = line.decode("utf-8", "replace")
        if decoded.startswith(PUBLIC_PREFIX):
            print(decoded[len(PUBLIC_PREFIX):], flush=True)


def run(client: paramiko.SSHClient, text: str, limit: float) -> tuple[int, bytes]:
    channel = client.get_transport().open_session()
    channel.set_combine_stderr(True)
    channel.exec_command(text)
    output = bytearray()
    pending = b""
    deadline = time.monotonic() + limit
    while True:
        if channel.recv_ready():
            chunk = channel.recv(65536)
            output += chunk
            *lines, pending = (pending + chunk).split(b"\n")
            _echo_public(lines)
        elif channel.exit_status_ready():
            _echo_public([pending])
            return channel.recv_exit_status(), bytes(output)
        elif time.monotonic() > deadline:
            channel.close()
            output += "\n[peyrev] Zaman sınırı aşıldı; betik durduruldu.\n".encode()
            return 124, bytes(output)
        else:
            time.sleep(0.2)


def main() -> int:
    script_path = Path(os.environ["BETIK"])
    if script_path.parent != Path("deploy") or script_path.suffix != ".sh" or not script_path.is_file():
        raise SystemExit("Betik deploy/ altında bulunamadı.")
    environment = json.loads(os.environ.get("BETIK_ORTAMI") or "{}")
    files = json.loads(os.environ.get("DOSYALAR") or "[]")
    recipient = base64.b64decode(Path(".github/rapor-anahtari.pub").read_text().strip(), validate=True)
    report = Path(os.environ.get("RAPOR", "rapor.enc"))
    started = time.time()
    try:
        client = connect()
    except Exception as error:  # noqa: BLE001 - the type is enough; details could name the host
        print(f"SSH bağlantısı kurulamadı: {type(error).__name__}")
        return 1
    try:
        upload(client, files)
        status, output = run(client, command(script_path.read_text(), environment),
                             float(os.environ.get("SURE", "1800")))
    finally:
        client.close()
    report.write_bytes(encrypt(output, recipient))
    print(f"{script_path.name}: çıkış kodu {status}, {len(output)} bayt çıktı, {time.time() - started:.0f} sn "
          f"(ayrıntılar şifreli raporda)")
    return 0 if status == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
