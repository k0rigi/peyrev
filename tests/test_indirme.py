"""Kişiye özel indirme bağlantıları: panelden oluşturma ve e-postayla gönderme, ödeme onayında kendiliğinden gönderim,
bağlantı sayfası, güncel kurulum dosyasına yönlendirme, iptal, süre, pasif hesap ve sayım."""

from __future__ import annotations

import base64
import json
import re

import pytest
from conftest import make_user
from test_accounts_applications import apply
from test_admin_api import pending
from test_admin_auth import events, post, signed_in

from peyrev import indirme

BASE = "https://peyrev.com/indir/releases/1.2.1/"
ASSETS = {"windows-x64": {"url": BASE + "Peyrev-Setup-1.2.1-Windows-x64.exe"},
          "macos-arm64": {"url": BASE + "Peyrev-1.2.1-macOS-arm64.dmg"}}
MAIL = {"DESTEK_SMTP_HOST": "peyrev.com", "DESTEK_SMTP_USER": "info@peyrev.com", "DESTEK_SMTP_PASSWORD": "x",
        "DESTEK_EPOSTA_ALICI": "info@peyrev.com", "DESTEK_EPOSTA_GONDEREN": "info@peyrev.com"}


def manifest(assets=None, version="1.2.1"):
    payload = base64.b64encode(json.dumps({"version": version, "assets": assets or ASSETS}).encode()).decode()
    return json.dumps({"payload": payload, "signature": "x"}).encode()


@pytest.fixture
def mailbox(core):
    sent = []
    core.extras.update(eposta_ayarlari=MAIL, indirme_bildirimi=lambda _core: manifest(),
                       eposta_gonderici=lambda settings, subject, text: sent.append((settings, subject, text)))
    return sent


@pytest.fixture
def admin(app, core):
    make_user(core, "yonetici@peyrev.test", rol="yonetici")
    return signed_in(app)


def token_of(link):
    return re.fullmatch(r"https://peyrev\.com/api/indir/([A-Za-z0-9_-]{43})", link).group(1)


def test_a_link_is_mailed_opens_a_page_and_leads_to_the_current_installer(admin, client, core, mailbox):
    user = make_user(core, "ali@ornek.test", ad_soyad="Ali Veli", modules={"MOD_RPA": None})
    answer = post(admin, f"kullanicilar/{user['id']}/indirme", {"eposta": True}).get_json()["indirme"]
    assert (answer["eposta"], answer["alici"], answer["gecerlilik"]) == ("gonderildi", "ali@ornek.test", "2026-11-25 12:00")
    [(settings, subject, text)] = mailbox
    assert settings["DESTEK_EPOSTA_ALICI"] == "ali@ornek.test" and settings["DESTEK_EPOSTA_GONDEREN"] == "Peyrev <info@peyrev.com>"
    assert subject == "Peyrev indirme bağlantınız" and answer["baglanti"] in text and "Kullanıcı adı: ali@ornek.test" in text
    assert "İndirme bağlantınız (25.11.2026 tarihine kadar geçerli):" in text
    token = token_of(answer["baglanti"])
    page = client.get(f"/api/indir/{token}")
    assert page.status_code == 200 and "Güncel sürüm 1.2.1" in page.get_data(as_text=True)
    assert f'href="/api/indir/{token}/windows"' in page.get_data(as_text=True)
    assert page.headers["Cache-Control"] == "no-store" and page.headers["Referrer-Policy"] == "no-referrer"
    assert "script-src" not in page.headers["Content-Security-Policy"] and "default-src 'none'" in page.headers[
        "Content-Security-Policy"]
    for platform, asset in (("windows", "windows-x64"), ("mac", "macos-arm64")):
        response = client.get(f"/api/indir/{token}/{platform}")
        assert (response.status_code, response.headers["Location"]) == (302, ASSETS[asset]["url"])
    detail = admin.get(f"/api/yonetim/kullanicilar/{user['id']}").get_json()["indirme"]
    assert detail["gecerli"] is True and detail["kullanim"] == 2
    # Only the digest is stored and the log never carries the link.
    with core.db.transaction() as cur:
        cur.execute("SELECT ozet FROM indirme_baglantilari")
        assert cur.fetchone()["ozet"] == indirme.digest(token)
    [event] = events(core, "indirme_baglantisi")
    assert event["ayrinti"]["eposta"] == "gonderildi" and token not in json.dumps(event["ayrinti"])


def test_a_new_link_cancels_the_old_one_and_links_end(admin, client, core, mailbox):
    user = make_user(core, "ali@ornek.test", modules={"MOD_RPA": None})
    first = token_of(post(admin, f"kullanicilar/{user['id']}/indirme", {"eposta": False}).get_json()["indirme"]["baglanti"])
    assert mailbox == []
    second = token_of(post(admin, f"kullanicilar/{user['id']}/indirme", {"eposta": False}).get_json()["indirme"]["baglanti"])
    assert client.get(f"/api/indir/{first}").status_code == 404
    assert client.get(f"/api/indir/{first}/windows").status_code == 404
    assert client.get(f"/api/indir/{second}").status_code == 200
    for bad in ("kisa", "a" * 43 + "!", "a" * 43):
        assert client.get(f"/api/indir/{bad}").status_code == 404
    assert client.get(f"/api/indir/{second}/linux").status_code == 404
    core.clock.advance(days=indirme.DAYS, seconds=1)
    assert client.get(f"/api/indir/{second}").status_code == 404


def test_a_passive_account_gets_no_link_and_its_link_stops(admin, client, core, mailbox):
    user = make_user(core, "ali@ornek.test", modules={"MOD_RPA": None})
    token = token_of(post(admin, f"kullanicilar/{user['id']}/indirme", {"eposta": False}).get_json()["indirme"]["baglanti"])
    assert post(admin, f"kullanicilar/{user['id']}/durum", {"durum": "pasif"}).status_code == 200
    assert client.get(f"/api/indir/{token}/mac").status_code == 404
    refused = post(admin, f"kullanicilar/{user['id']}/indirme", {"eposta": True})
    assert (refused.status_code, refused.get_json()["kod"]) == (409, "HESAP_PASIF")


def test_mail_states_and_a_foreign_installer_address(admin, client, core, mailbox):
    named = make_user(core, "kisa_ad", modules={"MOD_RPA": None})
    assert post(admin, f"kullanicilar/{named['id']}/indirme", {"eposta": True}).get_json()["indirme"]["eposta"] == "adres_yok"
    core.extras["eposta_ayarlari"] = {}
    core.extras.pop("eposta_gonderici")
    user = make_user(core, "ali@ornek.test", modules={"MOD_RPA": None})
    answer = post(admin, f"kullanicilar/{user['id']}/indirme", {"eposta": True}).get_json()["indirme"]
    assert answer["eposta"] == "ayar_yok" and answer["baglanti"]
    assert post(admin, f"kullanicilar/{user['id']}/indirme", {"eposta": "evet"}).status_code == 400
    core.extras["indirme_bildirimi"] = lambda _core: manifest({**ASSETS, "windows-x64": {"url": "https://evil.test/x.exe"}})
    token = token_of(answer["baglanti"])
    assert client.get(f"/api/indir/{token}/windows").status_code == 503


def test_approving_a_paid_application_mails_the_welcome_and_the_link(admin, app, core, mailbox):
    assert apply(app.test_client(), ad_soyad="Can Yıldız", eposta="can@ornek.test").status_code == 201
    result = post(admin, f"basvurular/{pending(core, 'can@ornek.test')}/onayla", {"bitis": "2027-10-26"}).get_json()
    assert result["indirme"]["eposta"] == "gonderildi"
    [(settings, _subject, text)] = mailbox
    assert settings["DESTEK_EPOSTA_ALICI"] == "can@ornek.test"
    assert text.startswith("Merhaba Can Yıldız,\n\nÖdemeniz onaylandı ve Peyrev hesabınız açıldı.")
    assert "Lisansınız 26.10.2027 tarihine kadar geçerlidir." in text and "Şifre: başvuruda belirlediğiniz şifre" in text
