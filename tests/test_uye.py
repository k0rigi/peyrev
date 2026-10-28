"""Hesabım (/api/uye/*): lisansı biten hesap da girer; hesap ve lisans durumu, kişisel indirme bağlantısı (abonelik
sürerken, günde sınırlı) ve lisans uzatma isteği (bildirilir, olay kaydına yazılır, günde bir)."""

from __future__ import annotations

from datetime import date

from conftest import make_user
from test_admin_auth import events
from test_indirme import manifest
from test_topluluk import HEADERS, member, sign_in

from peyrev import uye


def send(client, path, body=None):
    return client.post("/api/uye/" + path, json={} if body is None else body, headers=HEADERS)


def test_the_account_page_shows_the_license_and_gives_a_download_link(app, client, core):
    core.extras["indirme_bildirimi"] = lambda _core: manifest()
    make_user(core, "ali@ornek.test", ad_soyad="Ali Veli", modules={"MOD_RPA": date(2027, 1, 31), "MOD_AJAN": None})
    assert client.get("/api/uye/hesap").get_json()["kod"] == "OTURUM_GEREKLI"
    ali = member(app, "ali@ornek.test")
    account = ali.get("/api/uye/hesap").get_json()["hesap"]
    assert (account["giris"], account["ad"], account["abone"]) == ("ali@ornek.test", "Ali Veli", True)
    assert account["lisans"]["peyrev"] == {"durum": "aktif", "bitis": "2027-01-31", "suresiz": False}
    assert account["lisans"]["ajanlar"]["suresiz"] is True and account["indirme"] is None
    link = send(ali, "indirme").get_json()["indirme"]["baglanti"]
    assert link.startswith("https://peyrev.com/api/indir/")
    assert client.get(link.removeprefix("https://peyrev.com")).status_code == 200
    assert ali.get("/api/uye/hesap").get_json()["hesap"]["indirme"]["gecerli"] is True
    for _ in range(uye.LINKS_PER_DAY - 1):
        assert send(ali, "indirme").status_code == 200
    late = send(ali, "indirme")
    assert (late.status_code, late.get_json()["kod"]) == (429, "COK_SIK")


def test_an_expired_account_signs_in_gets_no_link_and_asks_for_renewal(app, core):
    user = make_user(core, "eski@ornek.test", modules={"MOD_RPA": date(2026, 10, 1)})
    client = app.test_client()
    signed = sign_in(client, "eski@ornek.test").get_json()
    assert signed["uye"]["abone"] is False
    assert client.get("/api/uye/hesap").get_json()["hesap"]["lisans"]["peyrev"]["durum"] == "sure_doldu"
    refused = send(client, "indirme")
    assert (refused.status_code, refused.get_json()["kod"]) == (403, "ABONELIK_YOK")
    assert send(client, "uzatma", {"paket": "yok"}).status_code == 400
    asked = send(client, "uzatma", {"paket": "rpa_ajan"})
    assert asked.status_code == 200 and core.notifier.messages[-1][0] == "Lisans uzatma isteği"
    [event] = events(core, "uzatma_istegi")
    assert (event["hedef"], event["ayrinti"]) == (f"kullanici:{user['id']}", {"paket": "rpa_ajan", "kaynak": "hesabim"})
    again = send(client, "uzatma", {"paket": "rpa"})
    assert (again.status_code, again.get_json()["kod"]) == (429, "COK_SIK")
    assert send(client, "cikis").status_code == 200
    assert client.get("/api/uye/hesap").status_code == 401


def test_account_requests_need_the_header_and_the_origin(app, core):
    make_user(core, "ali@ornek.test", modules={"MOD_RPA": None})
    ali = member(app, "ali@ornek.test")
    response = ali.post("/api/uye/uzatma", json={"paket": "rpa"}, headers={"Origin": "https://evil.test", "X-Peyrev-Uye": "1"})
    assert (response.status_code, response.get_json()["kod"]) == (403, "CSRF")
