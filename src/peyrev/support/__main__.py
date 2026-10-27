"""Destek bildirim ayarlarını kurar: `python -m peyrev.support [--koru] [ayar dosyası]` (root olarak, iş akışları).

Ortamdaki DESTEK_* değerlerini /etc/peyrev/destek.env dosyasına root:peyrev 0640 yazar ve deneme bildirimi gönderir;
çıktı yalnız kanal durumlarıdır. --koru ile ortamda olmayan anahtarlar mevcut dosyadan korunur.
"""

import sys
from pathlib import Path

from .notify import SETTINGS_PATH, configure_from_environment

if __name__ == "__main__":  # pragma: no cover - kurulum iş akışı çalıştırır
    arguments = sys.argv[1:]
    keep = "--koru" in arguments
    paths = [argument for argument in arguments if argument != "--koru"]
    sys.exit(configure_from_environment(path=Path(paths[0]) if paths else SETTINGS_PATH, keep=keep))
