"""Uygulama genelinde paylaşılan bağlam: ayarlar, veritabanı, anahtarlar, saat ve bildirim gönderici.

Uçlar `current_core()` ile alır; testler kendi saatini, veritabanını ve bildirim göndericisini verebilir.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from flask import current_app

from .clock import Clock
from .config import Settings
from .db import Database
from .keys import Keys


@dataclass
class Core:
    settings: Settings
    db: Database
    keys: Keys
    clock: Clock = field(default_factory=Clock)
    # Destek bildirimleri (Telegram, e-posta): send(subject, text). Yoksa destek modülü kendi göndericisini kurar.
    notifier: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


def current_core() -> Core:
    return current_app.extensions["peyrev"]
