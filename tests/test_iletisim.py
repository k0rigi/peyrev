"""İletişim formu (POST /api/iletisim) ve paneldeki mesajlar: kayıt, bildirim (mesajın kendisi bildirime girmez), bal küpü,
alan kuralları, adres ve hizmet sınırları, saklama süresi; panelde listeleme, okundu işareti ve silme (olay kaydıyla)."""

from __future__ import annotations

import pytest
from conftest import make_user
from test_admin_auth import events, post, signed_in

from peyrev import iletisim

BODY = {"ad": "  Ali   Veli ", "eposta": "ali@ornek.test", "konu": "satis",
        "mesaj": "Merhaba,\n\n\n\nFiyat   hakkında <bir> sorum var."}


def send(client, ip="203.0.113.7", **changes):
    return client.post("/api/iletisim", json={**BODY, **changes}, headers={"X-Forwarded-For": ip})


def stored(core):
    with core.db.transaction() as cur:
        cur.execute("SELECT ad, eposta, konu, mesaj, ip, okundu FROM iletisim_mesajlari ORDER BY id")
        return cur.fetchall()


def test_a_message_is_stored_and_announced_without_its_text(client, core):
    response = send(client)
    assert response.status_code == 201
    assert response.get_json() == {"success": True, "message": iletisim.RECEIVED}
    assert stored(core) == [{"ad": "Ali Veli", "eposta": "ali@ornek.test", "konu": "satis",
                             "mesaj": "Merhaba,\n\nFiyat hakkında <bir> sorum var.", "ip": "203.0.113.7",
                             "okundu": False}]
    [(subject, text)] = core.notifier.messages
    assert subject == "İletişim formu: Satış ve fiyat"
    assert "Ali Veli · ali@ornek.test" in text and "/yonetim/#iletisim" in text and "Fiyat" not in text


def test_a_filled_trap_gets_the_same_answer_and_nothing_is_stored(client, core):
    response = send(client, web_sitesi="https://spam.test")
    assert (response.status_code, response.get_json()["message"]) == (201, iletisim.RECEIVED)
    assert stored(core) == [] and core.notifier.messages == []


@pytest.mark.parametrize(("changes", "message"), [
    ({"ad": "A"}, "Ad 2-120 karakter olmalıdır."),
    ({"ad": "<b>Ali</b>"}, "Ad geçersiz karakter içeriyor."),
    ({"eposta": "ali"}, "Geçerli bir e-posta adresi yazın."),
    ({"konu": "kampanya"}, "Konu seçin."),
    ({"konu": None}, "Konu seçin."),
    ({"mesaj": "Kısa"}, "Mesaj 10-3000 karakter olmalıdır."),
    ({"mesaj": "x" * 3001}, "Mesaj 10-3000 karakter olmalıdır."),
    ({"mesaj": "Merhaba\x00 nasılsınız"}, "Mesaj geçersiz karakter içeriyor."),
    ({"mesaj": 12345678901}, "Mesaj 10-3000 karakter olmalıdır."),
])
def test_invalid_messages_are_refused(client, core, changes, message):
    response = send(client, **changes)
    assert (response.status_code, response.get_json()["message"]) == (400, message)
    assert stored(core) == []


def test_unknown_or_missing_fields_are_refused(client, core):
    assert client.post("/api/iletisim", json={**BODY, "telefon": "1"}).status_code == 400
    assert client.post("/api/iletisim", json={"ad": "Ali Veli", "eposta": "ali@ornek.test"}).status_code == 400
    assert stored(core) == []


def test_an_address_may_send_three_messages_an_hour_and_ten_a_day(client, core):
    for _ in range(iletisim.PER_HOUR):
        assert send(client).status_code == 201
    refused = send(client)
    assert (refused.status_code, refused.get_json()["kod"]) == (429, "COK_DENEME")
    assert send(client, ip="198.51.100.9").status_code == 201  # another address is not affected
    core.clock.advance(hours=1, seconds=1)
    assert send(client).status_code == 201
    assert len(stored(core)) == iletisim.PER_HOUR + 2


def test_the_whole_service_stops_at_the_daily_total(client, core, monkeypatch):
    monkeypatch.setattr(iletisim, "TOTAL_PER_DAY", 2)
    assert send(client, ip="203.0.113.1").status_code == 201
    assert send(client, ip="203.0.113.2").status_code == 201
    response = send(client, ip="203.0.113.3")
    assert (response.status_code, response.get_json()["kod"]) == (503, "YOGUNLUK")


def test_messages_older_than_a_year_are_deleted(client, core):
    assert send(client).status_code == 201
    core.clock.advance(days=366)
    assert send(client, ad="Ayşe Kaya").status_code == 201
    assert [row["ad"] for row in stored(core)] == ["Ayşe Kaya"]


@pytest.fixture
def admin(app, core):
    make_user(core, "yonetici@peyrev.test", rol="yonetici")
    return signed_in(app)


def test_the_panel_lists_marks_and_deletes_messages(admin, client, core):
    send(client)
    core.clock.advance(minutes=5)
    send(client, ad="Ayşe Kaya", konu="teknik", ip="198.51.100.9")
    data = admin.get("/api/yonetim/iletisim").get_json()
    assert data["okunmamis"] == 2 and data["konular"] == iletisim.TOPICS
    newest, oldest = data["mesajlar"]
    assert (newest["ad"], newest["konu_adi"], newest["ip"], newest["okundu"]) == ("Ayşe Kaya", "Teknik soru",
                                                                                   "198.51.100.9", False)
    assert admin.get("/api/yonetim/ozet").get_json()["okunmamis_iletisim"] == 2
    marked = post(admin, f"iletisim/{oldest['id']}", {"okundu": True}).get_json()
    assert marked["mesaj"]["okundu"] is True and marked["okunmamis"] == 1
    assert post(admin, f"iletisim/{oldest['id']}", {"okundu": "evet"}).status_code == 400
    deleted = post(admin, f"iletisim/{newest['id']}/sil", {}).get_json()
    assert (deleted["silindi"], deleted["okunmamis"]) == (True, 0)
    assert post(admin, f"iletisim/{newest['id']}/sil", {}).status_code == 404
    assert post(admin, f"iletisim/{newest['id']}", {"okundu": False}).status_code == 404
    [event] = events(core, "iletisim_silindi")
    assert event["hedef"] == f"iletisim:{newest['id']}" and event["ayrinti"] == {"konu": "teknik", "tarih": newest["zaman"]}
    assert [row["ad"] for row in stored(core)] == ["Ali Veli"]
