"""Destek bildirimleri: ayar dosyasının güven kuralları (sahibi root, izinler en çok 0640), iki kanal, arka planda
gönderim, gizli bilgilerin günlüğe ve çıktıya girmemesi, kurulum iş akışının dosyayı yazması."""

import io
import json
import logging
import os
import smtplib
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from conftest import DEVICE, make_user, open_session

from peyrev import tokens, users
from peyrev.support import notify

ME = os.geteuid()
TOKEN = "12345678:" + "A" * 35
SETTINGS = ("DESTEK_TELEGRAM_TOKEN=" + TOKEN + "\nDESTEK_TELEGRAM_CHAT=4242\n"
            "DESTEK_SMTP_HOST=smtp.gmail.com\nDESTEK_SMTP_USER=destek@ornek.test\n"
            'DESTEK_SMTP_PASSWORD="uygulama sifresi"\nDESTEK_EPOSTA_ALICI=sahip@ornek.test\n')


def private(tmp_path, text=SETTINGS, mode=0o640):
    """Üretimdeki /etc/peyrev/destek.env gibi (root:peyrev 0640, klasör 0750); sahibi burada testi çalıştıran."""
    folder = tmp_path / "peyrev"
    folder.mkdir(exist_ok=True)
    os.chmod(folder, 0o750)
    path = folder / "destek.env"
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    os.chmod(path, mode)
    return path


def test_settings_are_read_only_from_a_private_file(tmp_path, caplog):
    path = private(tmp_path)
    settings = notify.read_settings(path, owner=ME)
    assert settings["DESTEK_SMTP_PASSWORD"] == "uygulama sifresi" and settings["DESTEK_TELEGRAM_CHAT"] == "4242"
    assert notify.telegram_ready(settings) and notify.mail_ready(settings)
    for mode in (0o600, 0o440, 0o400):
        os.chmod(path, mode)
        assert notify.read_settings(path, owner=ME) == settings, oct(mode)
    # Readable by everyone, writable by the group or executable: not used, and only the reason is logged.
    for mode in (0o644, 0o604, 0o660, 0o670, 0o740):
        os.chmod(path, mode)
        caplog.clear()
        assert notify.read_settings(path, owner=ME) == {}, oct(mode)
        assert "izinleri 0640'tan geniş" in caplog.text
    assert "uygulama sifresi" not in caplog.text and TOKEN not in caplog.text


def test_the_file_and_its_folder_must_belong_to_root(tmp_path, caplog):
    path = private(tmp_path)
    assert notify._distrust(os.stat(path), ME) is None
    if ME != 0:
        assert notify._distrust(os.stat(path), 0) == "ayar dosyasının sahibi root değil."
        assert notify.read_settings(path) == {}  # the production rule: root only
        assert "root'a ait değil" in caplog.text
    os.chmod(path.parent, 0o770)
    caplog.clear()
    assert notify.read_settings(path, owner=ME) == {} and "yazabiliyor" in caplog.text


def test_links_special_and_large_files_are_refused(tmp_path, caplog):
    path = private(tmp_path)
    link = path.with_name("baglanti.env")
    link.symlink_to(path)
    assert notify.read_settings(link, owner=ME) == {} and "açılamadı" in caplog.text
    second = path.with_name("ikinci.env")
    os.link(path, second)
    assert notify.read_settings(path, owner=ME) == {} and "birden fazla bağlantısı" in caplog.text
    second.unlink()
    pipe = path.with_name("boru.env")
    os.mkfifo(pipe, 0o640)
    started = time.monotonic()
    assert notify.read_settings(pipe, owner=ME) == {} and "düzenli bir dosya değil" in caplog.text
    assert time.monotonic() - started < 2  # a FIFO does not make the sender wait for a writer
    big = private(tmp_path, "# yorum\n" * 1200)
    assert notify.read_settings(big, owner=ME) == {} and "8 KB" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="peyrev"):
        assert notify.read_settings(tmp_path / "yok" / "destek.env", owner=ME) == {}
    assert "ayarları yok" in caplog.text


@pytest.mark.parametrize("text", [
    "BASKA_ANAHTAR=1\n", "DESTEK_TELEGRAM_CHAT=1\nDESTEK_TELEGRAM_CHAT=2\n", "DESTEK_TELEGRAM_CHAT\n",
    "DESTEK_SMTP_PASSWORD=" + "x" * 513 + "\n", "DESTEK_SMTP_PASSWORD=a\x01b\n", b"DESTEK_SMTP_USER=\xff\n",
])
def test_an_unknown_repeated_or_odd_line_voids_the_whole_file(tmp_path, caplog, text):
    path = private(tmp_path, SETTINGS + text if isinstance(text, str) else SETTINGS.encode() + text)
    assert notify.read_settings(path, owner=ME) == {}
    assert "geçersiz bir satır" in caplog.text and "uygulama sifresi" not in caplog.text


def test_comments_blank_lines_and_quotes(tmp_path):
    path = private(tmp_path, "# Peyrev destek bildirimleri\n\n  DESTEK_TELEGRAM_CHAT='4242'  \n"
                             'DESTEK_SMTP_PASSWORD="a=b # c"\nDESTEK_SMTP_PORT=587\n')
    assert notify.read_settings(path, owner=ME) == {"DESTEK_TELEGRAM_CHAT": "4242", "DESTEK_SMTP_PASSWORD": "a=b # c",
                                                    "DESTEK_SMTP_PORT": "587"}


def test_both_channels_are_used_only_when_ready_and_trusted(tmp_path):
    path = private(tmp_path)
    delivered = []
    notifier = notify.Notifier(lambda: notify.read_settings(path, owner=ME), background=False,
                               telegram=lambda values, text: delivered.append(("telegram", text)),
                               mail=lambda values, subject, text: delivered.append(("eposta", subject, text)))
    notifier.send("Konu", "Metin")
    assert delivered == [("telegram", "Konu\n\nMetin"), ("eposta", "Konu", "Metin")]
    delivered.clear()
    os.chmod(path, 0o644)  # readable by everyone: nothing is sent
    notifier.send("Konu", "Metin")
    assert delivered == []
    private(tmp_path, "DESTEK_TELEGRAM_TOKEN=" + TOKEN + "\nDESTEK_TELEGRAM_CHAT=1\n")
    notifier.send("Konu", "Metin")
    assert delivered == [("telegram", "Konu\n\nMetin")]
    delivered.clear()
    private(tmp_path, "DESTEK_TELEGRAM_TOKEN=kisa:x\nDESTEK_TELEGRAM_CHAT=1\nDESTEK_SMTP_HOST=smtp.ornek.test\n")
    notifier.send("Konu", "Metin")
    assert delivered == []  # a malformed token and half a mail account are no channel


def test_a_failed_channel_never_stops_the_other_or_logs_secrets(caplog):
    settings = {"DESTEK_TELEGRAM_TOKEN": "12345678:" + "B" * 35, "DESTEK_TELEGRAM_CHAT": "1",
                "DESTEK_SMTP_HOST": "smtp.ornek.test", "DESTEK_SMTP_USER": "u@ornek.test",
                "DESTEK_SMTP_PASSWORD": "gizli-sifre", "DESTEK_EPOSTA_ALICI": "sahip@ornek.test"}

    def failing(*_args):
        raise OSError("bot" + settings["DESTEK_TELEGRAM_TOKEN"] + " " + settings["DESTEK_SMTP_PASSWORD"])

    notify.Notifier(lambda: settings, telegram=failing, mail=failing, background=False).send("Konu", "Metin")
    assert "Telegram ile gönderilemedi" in caplog.text and "e-postayla gönderilemedi" in caplog.text
    assert "B" * 35 not in caplog.text and "gizli-sifre" not in caplog.text


def test_sending_happens_in_the_background():
    release, done = threading.Event(), threading.Event()

    def slow(_values, _text):
        release.wait(5)
        done.set()

    started = time.monotonic()
    notify.Notifier(lambda: {"DESTEK_TELEGRAM_TOKEN": TOKEN, "DESTEK_TELEGRAM_CHAT": "1"}, telegram=slow).send("K", "M")
    assert time.monotonic() - started < 1 and not done.is_set()
    release.set()
    assert done.wait(5)


def test_telegram_gets_plain_text_and_a_refusal_is_an_error():
    seen = []

    def opener(request, timeout):
        seen.append((request.full_url, request.get_method(), parse_qs(request.data.decode()), timeout))
        return io.BytesIO(b'{"ok": true, "result": {}}')

    notify.send_telegram({"DESTEK_TELEGRAM_TOKEN": TOKEN, "DESTEK_TELEGRAM_CHAT": "42"}, "x" * 5000, opener=opener)
    [(url, method, data, timeout)] = seen
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage" and method == "POST" and timeout == 15
    # No parse_mode: what a person typed is never read as markup.
    assert data == {"chat_id": ["42"], "text": ["x" * 4000], "disable_web_page_preview": ["true"]}
    with pytest.raises(OSError, match="telegram"):
        notify.send_telegram({"DESTEK_TELEGRAM_TOKEN": TOKEN, "DESTEK_TELEGRAM_CHAT": "42"}, "x",
                             opener=lambda request, timeout: io.BytesIO(b'{"ok": false}'))


def test_mail_logs_in_and_sends_to_the_owner():
    calls = []

    class Smtp:
        def __init__(self, host, port):
            calls.append(("bağlan", host, port))

        def login(self, user, _password):
            calls.append(("giriş", user))

        def send_message(self, message):
            calls.append(("gönder", message["From"], message["To"], message["Subject"], message.get_content()))

        def quit(self):
            calls.append(("kapat",))
            raise OSError("closing")  # a failing close does not hide the result

    account = {"DESTEK_SMTP_HOST": "smtp.ornek.test", "DESTEK_SMTP_USER": "u@ornek.test", "DESTEK_SMTP_PASSWORD": "x",
               "DESTEK_EPOSTA_ALICI": "sahip@ornek.test"}
    notify.send_mail(account, "Konu", "Metin", smtp=Smtp)
    assert calls == [("bağlan", "smtp.ornek.test", 465), ("giriş", "u@ornek.test"),
                     ("gönder", "u@ornek.test", "sahip@ornek.test", "Konu", "Metin\n"), ("kapat",)]
    calls.clear()
    notify.send_mail({**account, "DESTEK_SMTP_PORT": "587", "DESTEK_EPOSTA_GONDEREN": "Peyrev <destek@peyrev.com>"},
                     "Konu", "Metin", smtp=Smtp)
    assert calls[0] == ("bağlan", "smtp.ornek.test", 587) and calls[2][1] == "Peyrev <destek@peyrev.com>"


def test_the_setup_job_writes_a_private_file_finds_the_chat_and_reports_without_secrets(tmp_path, capsys):
    path = tmp_path / "etc" / "destek.env"
    token = "12345678:" + "C" * 35
    sent = []
    code = notify.configure_from_environment(
        {"DESTEK_TELEGRAM_TOKEN": token, "DESTEK_SMTP_HOST": "smtp.gmail.com", "DESTEK_SMTP_USER": "g@ornek.test",
         "DESTEK_SMTP_PASSWORD": ' "gizli sifre" ', "DESTEK_EPOSTA_ALICI": "sahip@ornek.test"},
        path=path, discover=lambda _token: "987654",
        notifier={"telegram": lambda values, text: sent.append((values["DESTEK_TELEGRAM_CHAT"], text)),
                  "eposta": lambda values, subject, text: sent.append((values["DESTEK_SMTP_PASSWORD"], subject))})
    assert code == 0
    trial = ("Peyrev destek bildirimi denemesi\n\nDestek talepleri bu kanala bildirilecek. "
             "Talepler: https://peyrev.com/yonetim/#destek")
    assert sent == [("987654", trial), ('"gizli sifre"', "Peyrev destek bildirimi denemesi")]  # values come back exactly
    assert oct(path.stat().st_mode & 0o777) == "0o640" and oct(path.parent.stat().st_mode & 0o777) == "0o750"
    assert path.read_text(encoding="utf-8").startswith("# Peyrev destek bildirimleri; iş akışı yazar")
    assert [item.name for item in path.parent.iterdir()] == ["destek.env"]  # no temporary file is left
    output = capsys.readouterr().out
    assert json.loads(output) == {"telegram": "gonderildi", "eposta": "gonderildi"}
    assert token not in output and "gizli" not in output and "sahip@" not in output
    # Without a chat to find, Telegram is reported, not guessed; a failed search is the same.
    def unreachable(value):
        raise OSError("getUpdates " + value)

    for discover, state in ((lambda _token: None, "sohbet_bulunamadi"), (unreachable, "sohbet_aranamadi:OSError")):
        code = notify.configure_from_environment({"DESTEK_TELEGRAM_TOKEN": token}, path=path, discover=discover,
                                                 notifier={})
        output = capsys.readouterr().out
        assert code == 1 and json.loads(output)["telegram"] == state and token not in output
    # A failed send names the error type and code, never its text (a Telegram address carries the token).
    def refused(*_args):
        raise smtplib.SMTPAuthenticationError(535, b"bad " + token.encode())

    def conflict(*_args):
        raise urllib.error.HTTPError("https://api.telegram.org/bot" + token + "/sendMessage", 409, "Conflict", None, None)

    code = notify.configure_from_environment(
        {"DESTEK_TELEGRAM_TOKEN": token, "DESTEK_TELEGRAM_CHAT": "1", "DESTEK_SMTP_HOST": "h", "DESTEK_SMTP_USER": "u",
         "DESTEK_SMTP_PASSWORD": "p", "DESTEK_EPOSTA_ALICI": "a@ornek.test"}, path=path,
        notifier={"telegram": conflict, "eposta": refused})
    output = capsys.readouterr().out
    assert code == 1 and json.loads(output) == {"telegram": "gonderilemedi:HTTPError/409",
                                                "eposta": "gonderilemedi:SMTPAuthenticationError/535"}
    assert token not in output
    code = notify.configure_from_environment({"DESTEK_TELEGRAM_TOKEN": "bozuk", "DESTEK_SMTP_HOST": "smtp.gmail.com"},
                                             path=path, notifier={})
    assert code == 1 and json.loads(capsys.readouterr().out) == {"telegram": "gecersiz_belirtec", "eposta": "eksik_ayar"}
    with pytest.raises(SystemExit):
        notify.configure_from_environment({"DESTEK_SMTP_USER": "a\nb"}, path=path, notifier={})


def test_keep_adds_telegram_without_losing_the_mail_settings(tmp_path, capsys):
    path = tmp_path / "etc" / "destek.env"
    mail = {"DESTEK_SMTP_HOST": "peyrev.com", "DESTEK_SMTP_USER": "info@peyrev.com", "DESTEK_SMTP_PASSWORD": "Kutu123",
            "DESTEK_EPOSTA_ALICI": "info@peyrev.com"}
    notify.configure_from_environment(mail, path=path, notifier={})
    capsys.readouterr()
    token = "12345678:" + "D" * 35
    sent = []
    code = notify.configure_from_environment(
        {"DESTEK_TELEGRAM_TOKEN": token}, path=path, keep=True, discover=lambda _token: "555",
        notifier={"telegram": lambda values, text: sent.append("telegram"),
                  "eposta": lambda values, subject, text: sent.append(values["DESTEK_SMTP_PASSWORD"])})
    assert code == 0 and sent == ["telegram", "Kutu123"]
    settings = notify.read_settings(path, owner=os.geteuid())
    assert settings == {**mail, "DESTEK_TELEGRAM_TOKEN": token, "DESTEK_TELEGRAM_CHAT": "555"}
    # A new bot does not inherit the old chat: it is found again.
    other = "87654321:" + "E" * 35
    notify.configure_from_environment({"DESTEK_TELEGRAM_TOKEN": other}, path=path, keep=True,
                                      discover=lambda _token: "777", notifier={})
    assert notify.read_settings(path, owner=os.geteuid())["DESTEK_TELEGRAM_CHAT"] == "777"
    # Without keep the file is replaced as before.
    notify.configure_from_environment({"DESTEK_TELEGRAM_TOKEN": other, "DESTEK_TELEGRAM_CHAT": "777"}, path=path,
                                      notifier={})
    assert "DESTEK_SMTP_PASSWORD" not in notify.read_settings(path, owner=os.geteuid())


def test_the_setup_command_runs_as_a_module(tmp_path):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("DESTEK_")}
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    target = tmp_path / "ayar" / "destek.env"
    finished = subprocess.run([sys.executable, "-m", "peyrev.support", str(target)], env=environment,
                              capture_output=True, text=True, timeout=60, check=False)
    assert finished.returncode == 1 and finished.stderr == ""  # nothing configured, nothing to send
    assert json.loads(finished.stdout) == {"eposta": "ayarlanmadi", "telegram": "ayarlanmadi"}
    assert oct(target.stat().st_mode & 0o777) == "0o640"


@pytest.mark.skipif(ME == 0, reason="root olarak test klasörü güvenilir sayılır ve gerçek gönderim denenir")
def test_without_a_notifier_the_settings_file_decides_and_the_request_does_not_wait(core, client, caplog):
    core.notifier = None
    user = make_user(core, "ayse@firma.test", modules={users.RPA: None})
    headers = {"Authorization": "Bearer " + tokens.issue(core, "destek", user, DEVICE, open_session(core, user))}
    path = core.settings.support_settings_path  # in the test folder, not owned by root
    path.write_text("DESTEK_TELEGRAM_TOKEN=" + TOKEN + "\nDESTEK_TELEGRAM_CHAT=1\n", encoding="utf-8")
    os.chmod(path, 0o640)
    response = client.post("/api/rpa/destek/talepler", headers=headers, json={
        "tur": "soru", "baslik": "Soru", "metin": "Nasıl?", "surum": "1.0.0", "platform": "macOS"})
    assert response.status_code == 201
    for thread in threading.enumerate():
        if thread.name == "destek-bildirim":
            thread.join(5)
    assert "Destek bildirim ayarları kullanılmadı" in caplog.text and "root" in caplog.text
    assert TOKEN not in caplog.text
