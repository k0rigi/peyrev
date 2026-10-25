"""Destek bildirim ayarlarını kurar: `python -m peyrev.support [ayar dosyası]` (root olarak, kurulum iş akışı).

Ortamdaki DESTEK_* değerlerini /etc/peyrev/destek.env dosyasına root:peyrev 0640 yazar ve deneme bildirimi gönderir;
çıktı yalnız kanal durumlarıdır.
"""

import sys
from pathlib import Path

from .notify import SETTINGS_PATH, configure_from_environment

if __name__ == "__main__":  # pragma: no cover - kurulum iş akışı çalıştırır
    sys.exit(configure_from_environment(path=Path(sys.argv[1]) if len(sys.argv) > 1 else SETTINGS_PATH))
