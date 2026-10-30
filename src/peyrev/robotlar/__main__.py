"""Robot sessizlik denetimi: `python -m peyrev.robotlar` (hizmet kullanıcısı olarak, düzenli aralıkla).

Son 24 saatte bildirmiş, kapanmamış ve 15 dakikadır susan robotları sahibine destek bildirim kanalıyla bir kez bildirir.
Aynı denetim Studio bildirimlerinin arasında da yapılır; bu giriş noktası hiçbir robot bildirmezken de denetler.
Çıktı yalnız uyarılan robot sayısıdır.
"""

import sys

from ..clock import Clock
from ..config import Settings
from ..core import Core
from ..db import Database
from ..keys import Keys
from ..support.notify import Notifier, read_settings
from .service import check_silence


def main() -> int:
    settings = Settings.from_environment()
    database = Database(settings.database_url, max_size=1)
    try:
        path = settings.support_settings_path
        # In the foreground: a background thread would end with the process before the message leaves.
        core = Core(settings=settings, db=database, keys=Keys(settings.secrets_dir, settings.allow_generate),
                    clock=Clock(), notifier=Notifier(lambda: read_settings(path), background=False))
        print(f"uyarilan_robot={check_silence(core)}")
    finally:
        database.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - zamanlanmış görev çalıştırır
    sys.exit(main())
