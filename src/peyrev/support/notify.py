"""Destek bildirimleri: yeni talep ve kullanıcı yanıtı yöneticiye Telegram ve e-postayla bildirilir.

Uygulama `core.notifier` verilmişse onu kullanır (send(konu, metin); hızlı dönmelidir). Verilmemişse ayarları
korumalı dosyadan okuyan bir gönderici kurar; gönderim arka planda yapılır, Studio'nun yanıtını geciktirmez ve
başarısızlığı talebi etkilemez. Yönetim panelindeki okunmamış rozeti bildirimden bağımsızdır.

Ayar dosyası (core.settings.support_settings_path, üretimde /etc/peyrev/destek.env) yalnız şu durumda okunur:
düzenli, tek bağlantılı, en çok 8 KB; sahibi root; izinleri en çok 0640 (root:peyrev: grup okur, grup yazamaz,
başkaları erişemez); klasörü root'a ait ve grup ya da başkaları yazamaz. Aksi halde bildirim gönderilmez ve günlüğe
yalnız neden yazılır. Belirteç, şifre veya adres hiçbir zaman günlüğe ya da çıktıya yazılmaz.

Biçim: ANAHTAR=değer satırları (# ile başlayanlar yorum, değer tırnaklıysa tırnaklar soyulur); bilinmeyen veya yinelenen
anahtar, denetim karakteri ya da 512 karakteri aşan değer bütün dosyayı geçersiz kılar. Anahtarlar SETTING_KEYS'tedir;
DESTEK_SMTP_PORT verilmezse 465 (SSL), başka bir port STARTTLS ile; DESTEK_EPOSTA_GONDEREN verilmezse SMTP kullanıcısı.
Dosyayı kurulum iş akışı GitHub Secrets'tan yazar: `python -m peyrev.support` (root; configure_from_environment).
"""

from __future__ import annotations

import contextlib
import grp
import json
import logging
import os
import re
import smtplib
import ssl
import stat
import threading
import uuid
from collections.abc import Callable, Mapping
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..config import PUBLIC_BASE, SECRETS_DIR

LOG = logging.getLogger("peyrev.support")
SETTINGS_PATH = SECRETS_DIR / "destek.env"
SETTINGS_GROUP = "peyrev"
SETTINGS_SIZE = 8192
SETTING_KEYS = ("DESTEK_TELEGRAM_TOKEN", "DESTEK_TELEGRAM_CHAT", "DESTEK_SMTP_HOST", "DESTEK_SMTP_PORT",
                "DESTEK_SMTP_USER", "DESTEK_SMTP_PASSWORD", "DESTEK_EPOSTA_GONDEREN", "DESTEK_EPOSTA_ALICI")
TELEGRAM_TOKEN = re.compile(r"\d{5,15}:[A-Za-z0-9_-]{30,64}")
TELEGRAM_CHAT = re.compile(r"-?\d{1,20}")
EMAIL = re.compile(r"[^@\s<>\"]{1,64}@[^@\s<>\"]+\.[^@\s<>\"]{2,}")
HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")
UNUSED = "Destek bildirim ayarları kullanılmadı: "


# ----- ayarlar ------------------------------------------------------------------------------------
def parse_settings(raw: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in SETTING_KEYS or key in result:
            raise ValueError("settings")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if any(ord(char) < 32 for char in value) or len(value) > 512:
            raise ValueError("settings")
        result[key] = value
    return result


def _distrust(info: os.stat_result, owner: int) -> str | None:
    """Dosya güven kuralını sağlamıyorsa nedeni (gizli bilgi içermez)."""
    if not stat.S_ISREG(info.st_mode):
        return "ayar dosyası düzenli bir dosya değil."
    if info.st_nlink != 1:
        return "ayar dosyasının birden fazla bağlantısı var."
    if info.st_size > SETTINGS_SIZE:
        return "ayar dosyası 8 KB'tan büyük."
    if info.st_uid != owner:
        return "ayar dosyasının sahibi root değil."
    if stat.S_IMODE(info.st_mode) & ~0o640:
        return "ayar dosyasının izinleri 0640'tan geniş (grup yazabiliyor ya da başkaları erişebiliyor)."
    return None


def read_settings(path: Path | str = SETTINGS_PATH, *, owner: int = 0) -> dict[str, str]:
    """Bildirim ayarları; dosya yoksa veya güvenli değilse boş (bildirim gönderilmez, neden günlüğe yazılır).

    `owner` dosyanın ve klasörün beklenen sahibidir: üretimde root (0); testler kendi kullanıcısını verir.
    """
    path = Path(path)
    parent = descriptor = None
    try:
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(parent)
        if info.st_uid not in (0, owner) or stat.S_IMODE(info.st_mode) & 0o022:
            LOG.warning(UNUSED + "ayar klasörü root'a ait değil ya da grup veya başkaları yazabiliyor.")
            return {}
        # No symbolic link, and a FIFO in its place cannot block the sender.
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        reason = _distrust(os.fstat(descriptor), owner)
        if reason:
            LOG.warning(UNUSED + reason)
            return {}
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            return parse_settings(stream.read(SETTINGS_SIZE + 1))
    except FileNotFoundError:
        LOG.info("Destek bildirim ayarları yok; talepler yalnız yönetim panelinde görünür.")
    except PermissionError:
        LOG.warning(UNUSED + "ayar dosyası okunamadı (izin yok).")
    except OSError:
        LOG.warning(UNUSED + "ayar dosyası açılamadı (sembolik bağlantı olabilir).")
    except (ValueError, UnicodeError):
        LOG.warning(UNUSED + "ayar dosyasında bilinmeyen, yinelenen ya da geçersiz bir satır var.")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)
    return {}


def telegram_ready(settings: Mapping[str, str]) -> bool:
    return bool(TELEGRAM_TOKEN.fullmatch(settings.get("DESTEK_TELEGRAM_TOKEN") or "")
                and TELEGRAM_CHAT.fullmatch(settings.get("DESTEK_TELEGRAM_CHAT") or ""))


def mail_ready(settings: Mapping[str, str]) -> bool:
    return bool(HOST.fullmatch(settings.get("DESTEK_SMTP_HOST") or "") and settings.get("DESTEK_SMTP_USER")
                and settings.get("DESTEK_SMTP_PASSWORD") and EMAIL.fullmatch(settings.get("DESTEK_EPOSTA_ALICI") or ""))


# ----- kanallar -----------------------------------------------------------------------------------
def send_telegram(settings: Mapping[str, str], text: str, opener: Callable = urlopen) -> None:
    """Düz metin (biçimlendirme kipi yok): kullanıcının yazdığı hiçbir şey işaretleme olarak yorumlanmaz."""
    url = "https://api.telegram.org/bot" + settings["DESTEK_TELEGRAM_TOKEN"] + "/sendMessage"
    data = urlencode({"chat_id": settings["DESTEK_TELEGRAM_CHAT"], "text": text[:4000],
                      "disable_web_page_preview": "true"}).encode("utf-8")
    with opener(Request(url, data=data, method="POST"), timeout=15) as response:
        answer = json.loads(response.read(65536).decode("utf-8"))
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        raise OSError("telegram")


def send_mail(settings: Mapping[str, str], subject: str, text: str, smtp: Callable | None = None) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.get("DESTEK_EPOSTA_GONDEREN") or settings["DESTEK_SMTP_USER"]
    message["To"] = settings["DESTEK_EPOSTA_ALICI"]
    message.set_content(text)
    port = int(settings.get("DESTEK_SMTP_PORT") or 465)
    host = settings["DESTEK_SMTP_HOST"]
    context = ssl.create_default_context()
    if smtp is not None:
        connection = smtp(host, port)
    elif port == 465:
        connection = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
    else:
        connection = smtplib.SMTP(host, port, timeout=20)
    try:
        if smtp is None and port != 465:
            connection.starttls(context=context)
        connection.login(settings["DESTEK_SMTP_USER"], settings["DESTEK_SMTP_PASSWORD"])
        connection.send_message(message)
    finally:
        with contextlib.suppress(Exception):  # Closing must not hide the result.
            connection.quit()


class Notifier:
    """Telegram ve e-posta bildirimi arka planda gider; başarısızlık talebi etkilemez, sır günlüğe yazılmaz."""

    def __init__(self, settings: Callable[[], Mapping[str, str]], telegram: Callable = send_telegram,
                 mail: Callable = send_mail, background: bool = True):
        self.settings = settings
        self.telegram = telegram
        self.mail = mail
        self.background = background

    def send(self, subject: str, text: str) -> None:
        if self.background:
            threading.Thread(target=self._deliver, args=(subject, text), name="destek-bildirim", daemon=True).start()
        else:
            self._deliver(subject, text)

    def _deliver(self, subject: str, text: str) -> None:
        settings = self.settings() or {}
        if telegram_ready(settings):
            try:
                self.telegram(settings, subject + "\n\n" + text)
            except Exception:  # noqa: BLE001 - the token is in the address: neither it nor the error is logged.
                LOG.warning("Destek bildirimi Telegram ile gönderilemedi.")
        if mail_ready(settings):
            try:
                self.mail(settings, subject, text)
            except Exception:  # noqa: BLE001 - account details stay out of the log.
                LOG.warning("Destek bildirimi e-postayla gönderilemedi.")


def deliver(core, subject: str, text: str) -> None:
    """Yöneticiye bildirim; talep kaydedildikten sonra çağrılır, hiçbir hatası talebi ya da yanıtı etkilemez."""
    try:
        notifier = core.notifier
        if notifier is None:
            path = core.settings.support_settings_path
            notifier = Notifier(lambda: read_settings(path))
        notifier.send(subject, text)
    except Exception:  # noqa: BLE001 - a channel never decides the outcome of a ticket.
        LOG.warning("Destek bildirimi gönderilemedi.")


# ----- bildirim ayarlarını kurma (yalnız kurulum iş akışı çağırır) ---------------------------------
def _discover_chat(token: str, opener: Callable = urlopen) -> str | None:
    """Bota en son özelden yazan sohbet: sahibi bota /start yazdıktan sonra iş akışı çalıştırılır."""
    url = "https://api.telegram.org/bot" + token + "/getUpdates"
    with opener(Request(url, method="GET"), timeout=15) as response:
        answer = json.loads(response.read(1024 * 1024).decode("utf-8"))
    if not isinstance(answer, dict) or answer.get("ok") is not True or not isinstance(answer.get("result"), list):
        raise OSError("telegram")
    for update in reversed(answer["result"]):
        message = update.get("message") if isinstance(update, dict) else None
        chat = message.get("chat") if isinstance(message, dict) else None
        if isinstance(chat, dict) and chat.get("type") == "private" and isinstance(chat.get("id"), int):
            return str(chat["id"])
    return None


def configure_from_environment(environment: Mapping[str, str] | None = None, path: Path | str = SETTINGS_PATH, *,
                               notifier: Mapping[str, Callable] | None = None, discover: Callable = _discover_chat,
                               base: str = PUBLIC_BASE) -> int:
    """GitHub Secrets'tan gelen DESTEK_* değerlerini korumalı dosyaya yazar ve deneme bildirimi gönderir.

    Root olarak çalışınca dosya root:peyrev 0640 olur (hizmet grup olarak okur). Çıktı yalnız kanal durumlarıdır;
    belirteç, şifre veya adres yazılmaz. Dönüş 0: ayarlanan her kanal deneme bildirimini iletti.
    """
    environment = os.environ if environment is None else environment
    values = {key: (environment.get(key) or "").strip() for key in SETTING_KEYS}
    report = {"telegram": "ayarlanmadi", "eposta": "ayarlanmadi"}
    if values["DESTEK_TELEGRAM_TOKEN"] and not TELEGRAM_TOKEN.fullmatch(values["DESTEK_TELEGRAM_TOKEN"]):
        report["telegram"] = "gecersiz_belirtec"
        values["DESTEK_TELEGRAM_TOKEN"] = ""
    if values["DESTEK_TELEGRAM_TOKEN"] and not values["DESTEK_TELEGRAM_CHAT"]:
        try:
            values["DESTEK_TELEGRAM_CHAT"] = discover(values["DESTEK_TELEGRAM_TOKEN"]) or ""
        except Exception:  # noqa: BLE001 - the token stays out of the output.
            values["DESTEK_TELEGRAM_CHAT"] = ""
        if not values["DESTEK_TELEGRAM_CHAT"]:
            report["telegram"] = "sohbet_bulunamadi"
    for value in values.values():
        if any(ord(char) < 32 for char in value) or len(value) > 512:
            raise SystemExit("Ayar değerleri geçersiz.")
    lines = ["# Peyrev destek bildirimleri; iş akışı yazar, elle düzenlemeyin."]
    # Always quoted: the reader strips exactly one pair, so a value that itself starts and ends with a quote survives.
    lines += [f'{key}="{value}"' for key, value in values.items() if value]
    path = Path(path)
    group = grp.getgrnam(SETTINGS_GROUP).gr_gid if os.geteuid() == 0 else None
    if not path.parent.is_dir():
        path.parent.mkdir(mode=0o750, parents=True)
        if group is not None:
            os.chown(path.parent, 0, group)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")
            output.flush()
            os.fsync(output.fileno())
        if group is not None:
            os.chown(temporary, 0, group)
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    settings = read_settings(path, owner=os.geteuid())
    subject = "Peyrev destek bildirimi denemesi"
    text = "Destek talepleri bu kanala bildirilecek. Talepler: " + base.rstrip("/") + "/yonetim/#destek"
    channels = notifier if notifier is not None else {"telegram": send_telegram, "eposta": send_mail}
    if telegram_ready(settings):
        try:
            channels["telegram"](settings, subject + "\n\n" + text)
            report["telegram"] = "gonderildi"
        except Exception:  # noqa: BLE001 - reported as a state, without the error text.
            report["telegram"] = "gonderilemedi"
    if mail_ready(settings):
        try:
            channels["eposta"](settings, subject, text)
            report["eposta"] = "gonderildi"
        except Exception:  # noqa: BLE001 - reported as a state, without the error text.
            report["eposta"] = "gonderilemedi"
    elif any(values[key] for key in ("DESTEK_SMTP_HOST", "DESTEK_SMTP_USER", "DESTEK_EPOSTA_ALICI")):
        report["eposta"] = "eksik_ayar"
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    configured = [state for state in report.values() if state != "ayarlanmadi"]
    return 0 if configured and all(state == "gonderildi" for state in configured) else 1
