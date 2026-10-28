"""Müşteriye e-posta (ör. indirme bağlantısı): destek bildirimleriyle aynı SMTP hesabından (/etc/peyrev/destek.env,
info@peyrev.com) gönderilir; gönderen "Peyrev <info@peyrev.com>", yanıtlar aynı kutuya döner.

Sonuç bir durumdur, hata metni değil: "gonderildi", "ayar_yok" (SMTP ayarı yok) ya da "gonderilemedi". Gönderim
istekte yapılır (en çok 20 saniye); başarısızlık çağıranın işini geri almaz, panel bağlantıyı kopyalanabilir gösterir.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .common import LOG
from .support import notify

SENT, NOT_CONFIGURED, FAILED = "gonderildi", "ayar_yok", "gonderilemedi"


def send(core, to: str, subject: str, text: str) -> str:
    # Tests (and a future provider) put their own settings and sender in core.extras.
    settings: dict[str, Any] = core.extras.get("eposta_ayarlari") or notify.read_settings(core.settings.support_settings_path)
    sender: Callable = core.extras.get("eposta_gonderici", notify.send_mail)
    if not notify.mail_ready(settings):
        return NOT_CONFIGURED
    address = settings.get("DESTEK_EPOSTA_GONDEREN") or settings["DESTEK_SMTP_USER"]
    sender_name = address if "<" in address else f"Peyrev <{address}>"
    try:
        sender({**settings, "DESTEK_EPOSTA_GONDEREN": sender_name, "DESTEK_EPOSTA_ALICI": to}, subject, text)
    except Exception as error:  # noqa: BLE001 - reported as a state; the address and the text stay out of the log.
        LOG.warning("Müşteri e-postası gönderilemedi: %s", type(error).__name__)
        return FAILED
    return SENT
