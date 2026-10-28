"""Topluluk: yalnız aboneliği devam eden hesaplar girer; konu, yorum, akış paylaşımı, yazma sınırı, CSRF ve panelden
moderasyon."""

from __future__ import annotations

import json
from datetime import date

import pytest
from conftest import make_user
from test_admin_auth import events, post, signed_in

from peyrev.topluluk import service

ORIGIN = "https://peyrev.com"
HEADERS = {"Origin": ORIGIN, "X-Peyrev-Uye": "1"}
FLOW = {"name": "Fatura girişi", "steps": [
    {"id": "a", "action": "file.read_table", "title": "Faturaları oku", "params": {}},
    {"id": "b", "action": "control.for_each", "title": "Her fatura için", "params": {}, "children": [
        {"id": "c", "action": "system.command", "title": "Komut", "params": {}}]}]}


def send(client, path, body=None, headers=None):
    return client.post("/api/topluluk/" + path, json={} if body is None else body, headers={**HEADERS, **(headers or {})})


def sign_in(client, login, password="Gizli12345"):
    return client.post("/api/uye/giris", json={"kullanici": login, "sifre": password}, headers=HEADERS)


def member(app, login, password="Gizli12345"):
    client = app.test_client()
    response = sign_in(client, login, password)
    assert response.status_code == 200, response.get_json()
    return client


@pytest.fixture
def people(core):
    return {"ali": make_user(core, "ali@ornek.test", ad_soyad="Ali Veli", modules={"MOD_RPA": None}),
            "can": make_user(core, "can", ad_soyad="Can Er", modules={"MOD_RPA": date(2027, 1, 1)}),
            "eski": make_user(core, "eski@ornek.test", modules={"MOD_RPA": date(2026, 10, 1)}),
            "pasif": make_user(core, "pasif@ornek.test", durum="pasif", modules={"MOD_RPA": None}),
            "yonetici": make_user(core, "yonetici@peyrev.test", rol="yonetici", ad_soyad="Peyrev Ekibi")}


def test_only_subscribed_accounts_get_in(app, client, core, people):
    assert client.get("/api/topluluk/konular").status_code == 401
    wrong = sign_in(client, "ali@ornek.test", "Yanlis12345")
    assert (wrong.status_code, wrong.get_json()["kod"]) == (401, "HATALI_GIRIS")
    passive = sign_in(client, "pasif@ornek.test")
    assert (passive.status_code, passive.get_json()["kod"]) == (401, "HATALI_GIRIS")
    # An account whose license ended signs in (Hesabım) but the community stays closed.
    expired = member(app, "eski@ornek.test")
    refused = expired.get("/api/topluluk/konular")
    assert (refused.status_code, refused.get_json()["kod"]) == (403, "ABONELIK_YOK")
    ali = member(app, "ali@ornek.test")
    me = ali.get("/api/topluluk/ben").get_json()
    assert me["uye"] == {"ad": "Ali Veli", "yonetici": False} and "duyuru" in me["kategoriler"]
    cookie = ali.get_cookie("peyrev_hesap", path="/api")
    assert cookie.secure and cookie.http_only and cookie.same_site == "Strict"
    assert member(app, "yonetici@peyrev.test").get("/api/topluluk/ben").get_json()["uye"]["yonetici"] is True
    # The license ends while signed in: refused, and back once it is extended.
    with core.db.transaction() as cur:
        cur.execute("UPDATE kullanici_modulleri SET bitis = '2026-10-01' WHERE kullanici_id = %s", (people["ali"]["id"],))
    assert ali.get("/api/topluluk/konular").get_json()["kod"] == "ABONELIK_YOK"
    with core.db.transaction() as cur:
        cur.execute("UPDATE kullanici_modulleri SET bitis = NULL WHERE kullanici_id = %s", (people["ali"]["id"],))
    assert ali.get("/api/topluluk/konular").status_code == 200
    # A new password ends the session.
    with core.db.transaction() as cur:
        cur.execute("UPDATE kullanicilar SET sifre_hash = 'pbkdf2:sha256:1$x$y' WHERE id = %s", (people["ali"]["id"],))
    assert ali.get("/api/topluluk/konular").get_json()["kod"] == "OTURUM_GECERSIZ"


def test_state_changes_need_the_header_and_the_origin(app, people):
    ali = member(app, "ali@ornek.test")
    for headers in ({"X-Peyrev-Uye": None}, {"Origin": "https://evil.test"}, {"Sec-Fetch-Site": "cross-site"}):
        sent = {key: value for key, value in {**HEADERS, **headers}.items() if value is not None}
        response = ali.post("/api/topluluk/konular", json={"kategori": "soru", "baslik": "Başlık", "metin": "x"},
                            headers=sent)
        assert (response.status_code, response.get_json()["kod"]) == (403, "CSRF")


def test_topics_comments_and_a_shared_flow(app, core, people):
    ali, can = member(app, "ali@ornek.test"), member(app, "can")
    refused = send(ali, "konular", {"kategori": "duyuru", "baslik": "Duyuru deneme", "metin": "x"})
    assert (refused.status_code, refused.get_json()["kod"]) == (403, "YETKI_YOK")
    created = send(ali, "konular", {"kategori": "akis", "baslik": "Faturaları ERP'ye giren akış",
                                    "metin": "Merhaba,\r\n\r\n\r\n\r\n<b>kalın değil</b>", "akis": FLOW})
    assert created.status_code == 201
    topic = created.get_json()["konu"]
    assert topic["metin"] == "Merhaba,\n\n<b>kalın değil</b>" and topic["benim"] is True
    assert topic["yazar"] == {"ad": "Ali Veli", "yonetici": False}
    assert topic["akis"]["adim_sayisi"] == 3 and [step["dikkat"] for step in topic["akis"]["adimlar"]] == [False, False, True]
    assert core.notifier.messages[-1][0] == "Toplulukta yeni konu"
    listed = can.get("/api/topluluk/konular?kategori=akis").get_json()
    assert listed["toplam"] == 1 and listed["konular"][0]["akis"] is True and "ali@ornek.test" not in json.dumps(listed)
    assert can.get("/api/topluluk/konular?ara=ERP").get_json()["toplam"] == 1
    assert can.get("/api/topluluk/konular?ara=yok").get_json()["toplam"] == 0
    viewed = can.get(f"/api/topluluk/konular/{topic['id']}").get_json()["konu"]
    assert viewed["goruntulenme"] == 2 and viewed["benim"] is False
    reply = send(can, f"konular/{topic['id']}/yorumlar", {"metin": "Teşekkürler!"}).get_json()["yorumlar"]
    assert [(item["metin"], item["yazar"]["ad"], item["benim"]) for item in reply] == [("Teşekkürler!", "Can Er", True)]
    download = can.get(f"/api/topluluk/konular/{topic['id']}/akis")
    assert download.headers["Content-Disposition"] == 'attachment; filename="Fatura-girisi.json"'
    assert json.loads(download.data) == FLOW and download.headers["X-Content-Type-Options"] == "nosniff"
    # Someone else's topic and comment cannot be deleted; one's own can.
    assert send(can, f"konular/{topic['id']}/sil").status_code == 403
    assert send(ali, f"yorumlar/{reply[0]['id']}/sil").status_code == 403
    assert send(can, f"yorumlar/{reply[0]['id']}/sil").status_code == 200
    assert send(ali, f"konular/{topic['id']}/sil").status_code == 200
    assert can.get(f"/api/topluluk/konular/{topic['id']}").status_code == 404


@pytest.mark.parametrize(("flow", "message"), [
    ({"name": "x", "steps": []}, "Akış dosyası okunamadı"),
    ({"steps": [{"action": "core.log"}]}, "Akışın adı okunamadı"),
    ({"name": "x", "steps": [{"action": "<script>"}]}, "okunamayan bir adım"),
    ({"name": "x", "steps": [{"action": "core.log", "title": "a" * 2000}] * 600}, "en çok 1 MB"),
])
def test_a_flow_that_is_not_a_studio_export_is_refused(app, people, flow, message):
    ali = member(app, "ali@ornek.test")
    answer = send(ali, "konular", {"kategori": "akis", "baslik": "Akış paylaşımı", "metin": "x", "akis": flow})
    assert answer.status_code == 400 and message in answer.get_json()["message"]


def test_writing_is_limited_per_hour(app, people):
    ali = member(app, "ali@ornek.test")
    for number in range(service.TOPICS_PER_HOUR):
        assert send(ali, "konular", {"kategori": "soru", "baslik": f"Soru numarası {number}", "metin": "x"}).status_code == 201
    late = send(ali, "konular", {"kategori": "soru", "baslik": "Bir soru daha", "metin": "x"})
    assert (late.status_code, late.get_json()["kod"]) == (429, "COK_SIK")


def test_the_panel_moderates_and_members_stop_seeing_hidden_posts(app, core, people):
    ali, can = member(app, "ali@ornek.test"), member(app, "can")
    topic = send(ali, "konular", {"kategori": "soru", "baslik": "Bir sorum var", "metin": "Metin"}).get_json()["konu"]
    comment = send(can, f"konular/{topic['id']}/yorumlar", {"metin": "Yanıt"}).get_json()["yorumlar"][0]
    panel = signed_in(app)
    detail = panel.get(f"/api/yonetim/topluluk/{topic['id']}").get_json()
    assert detail["konu"]["yazar_giris"] == "ali@ornek.test" and detail["yorumlar"][0]["yazar_giris"] == "can"
    flags = post(panel, f"topluluk/{topic['id']}", {"sabit": True, "kilitli": True}).get_json()["konu"]
    assert flags["sabit"] and flags["kilitli"]
    assert send(can, f"konular/{topic['id']}/yorumlar", {"metin": "Bir daha"}).get_json()["kod"] == "KILITLI"
    hidden = post(panel, f"topluluk/yorum/{comment['id']}", {"gizli": True}).get_json()
    assert hidden["yorumlar"][0]["gizli"] is True and hidden["konu"]["yanit_sayisi"] == 0
    assert can.get(f"/api/topluluk/konular/{topic['id']}").get_json()["yorumlar"] == []
    post(panel, f"topluluk/{topic['id']}", {"gizli": True})
    assert can.get("/api/topluluk/konular").get_json()["toplam"] == 0
    assert can.get(f"/api/topluluk/konular/{topic['id']}").status_code == 404
    assert panel.get("/api/yonetim/topluluk").get_json()["konular"][0]["gizli"] is True
    assert post(panel, f"topluluk/yorum/{comment['id']}/sil", {}).status_code == 200
    assert post(panel, f"topluluk/{topic['id']}/sil", {}).get_json()["konu"] == {"id": topic["id"], "silindi": True}
    assert [row["tur"] for row in events(core) if row["tur"].startswith("topluluk_")] == [
        "topluluk_konu", "topluluk_yorum", "topluluk_konu", "topluluk_yorum_silindi", "topluluk_konu_silindi"]
