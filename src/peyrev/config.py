"""Sunucu ayarları: veritabanı adresi ve sır klasörü ortamdan veya /etc/peyrev dosyalarından okunur.

Sırlar koda, günlüğe ve Git'e girmez. Üretimde kurulum iş akışı /etc/peyrev/ altına root:peyrev 0640 dosyalar yazar;
uygulama bunları yalnız okur, kendisi üretmez (dev/test kurulumunda `allow_generate=True` ile üretir).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

SECRETS_DIR = Path("/etc/peyrev")
PUBLIC_BASE = "https://peyrev.com"


def _read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE satırları; tırnaklar soyulur. Dosya yoksa boş."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass(frozen=True)
class Settings:
    database_url: str
    secrets_dir: Path = SECRETS_DIR
    public_base: str = PUBLIC_BASE
    # Yalnız geliştirme ve testte: eksik sır dosyalarını üret (üretimde kurulum iş akışı üretir).
    allow_generate: bool = False
    # Destek bildirim ayarlarının yolu (Telegram, e-posta); yoksa yalnız panelde görünür.
    support_settings_path: Path = field(default=SECRETS_DIR / "destek.env")
    # Ortak model (LLM) ayar dosyası; yoksa /llm uçları enabled:false döner.
    llm_settings_path: Path = field(default=SECRETS_DIR / "llm.env")

    @classmethod
    def from_environment(cls) -> Settings:
        secrets_dir = Path(os.environ.get("PEYREV_SECRETS_DIR", str(SECRETS_DIR)))
        url = os.environ.get("PEYREV_DATABASE_URL") or _read_env_file(secrets_dir / "db.env").get("DATABASE_URL", "")
        if not url:
            raise RuntimeError("Veritabanı adresi bulunamadı: PEYREV_DATABASE_URL ya da /etc/peyrev/db.env gerekli.")
        return cls(database_url=url, secrets_dir=secrets_dir,
                   public_base=os.environ.get("PEYREV_PUBLIC_BASE", PUBLIC_BASE),
                   allow_generate=os.environ.get("PEYREV_ALLOW_GENERATE") == "1",
                   support_settings_path=secrets_dir / "destek.env",
                   llm_settings_path=secrets_dir / "llm.env")
