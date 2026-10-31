"""Dijital İkiz firma adresi (<ad>.peyrev.com) panel uçları: /api/yonetim/kullanicilar/<id>/ikiz-adresi.

Dijital İkiz servisinin iç yönetim API'si yerine döngü arayüzünde gerçek bir HTTP sunucusu çalışır; panelin gönderdiği
yöntem, yol, gövde ve belirteç orada kaydedilir. Belirteç servisin yapacağı gibi yalnız açık anahtarla doğrulanır.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from dataclasses import replace
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import make_user
from test_admin_api import last_event_id, new_events, snapshot
from test_admin_auth import HEADERS, signed_in
from test_ikiz import now, verify_twin

from peyrev import users
from peyrev.admin import ikiz_adresi

AREA = "/api/yonetim/hesaplar/{}/alan"
UNREACHABLE = {"success": False, "kod": "IKIZ_ERISIM", "message": "Dijital İkiz servisine ulaşılamadı."}
NO_RIGHT = {"success": False, "kod": "IKIZ_HAKKI_YOK", "message": "Önce Dijital İkiz hakkı verin."}
INVALID = "Adres 1-40 karakter olmalı: küçük harf (a-z), rakam ve tire; tire başta ya da sonda olamaz."


class FakeTwin:
    """Dijital İkiz servisinin iç yönetim API'si: sıradaki yanıtı (durum, gövde) verir, gelen istekleri kaydeder."""

    def __init__(self):
        self.requests: list[dict] = []
        self.replies: list[tuple[int, object]] = []
        twin = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                twin.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                      "body": json.loads(self.rfile.read(length)) if length else None})
                status, body = twin.replies.pop(0) if twin.replies else (200, {"alt_alan": None, "durum": "yok",
                                                                                "mesaj": None, "adres": None})
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                if status in (301, 302, 307):
                    self.send_header("Location", "http://127.0.0.1:1/baska")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_PUT = do_DELETE = do_POST = _answer

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def area(self, name, state, message=None):
        return {"alt_alan": name, "durum": state, "mesaj": message,
                "adres": f"https://{name}.peyrev.com" if name else None}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def twin(core):
    fake = FakeTwin()
    core.settings = replace(core.settings, ikiz_internal_base=fake.base)
    yield fake
    fake.close()


@pytest.fixture
def manager(core):
    return make_user(core, "yonetici@peyrev.test", rol="yonetici", ad_soyad="Yönetici Kişi")


@pytest.fixture
def browser(app, manager):
    return signed_in(app)


@pytest.fixture
def firm(core):
    return make_user(core, "basbug@ornek.test", ad_soyad="Başbuğ Lojistik", modules={users.RPA: None, users.IKIZ: None})


def path(user, suffix=""):
    return f"/api/yonetim/kullanicilar/{user['id']}/ikiz-adresi{suffix}"


def post(browser, url, body):
    return browser.post(url, json=body, headers=HEADERS)


def test_the_panel_opens_reads_and_removes_a_firm_address_on_the_twin(core, twin, browser, manager, firm):
    since = last_event_id(core)
    twin.replies = [(200, twin.area("basbug", "bekliyor")), (200, twin.area("basbug", "hazir")),
                    (200, twin.area(None, "yok"))]
    opened = post(browser, path(firm), {"ad": " BasBug "})
    assert opened.status_code == 200, opened.get_json()
    assert opened.get_json() == {"success": True, "alan": {"alt_alan": "basbug", "durum": "bekliyor", "mesaj": None,
                                                           "adres": "https://basbug.peyrev.com"}}
    read = browser.get(path(firm))
    assert read.get_json()["alan"]["durum"] == "hazir"
    removed = post(browser, path(firm, "/kaldir"), {})
    assert removed.get_json()["alan"] == {"alt_alan": None, "durum": "yok", "mesaj": None, "adres": None}

    assert [(item["method"], item["path"], item["body"]) for item in twin.requests] == [
        ("PUT", AREA.format(firm["id"]), {"ad": "basbug", "firma": "Başbuğ Lojistik"}),
        ("GET", AREA.format(firm["id"]), None),
        ("DELETE", AREA.format(firm["id"]), None)]
    # Every call carries a fresh administrator token that the twin verifies with the public key alone.
    seen = set()
    for item in twin.requests:
        assert item["headers"]["Accept"] == "application/json"
        scheme, _, token = item["headers"]["Authorization"].partition(" ")
        assert scheme == "Bearer"
        claims = verify_twin(token, core.keys.ikiz_public_key, now(core))
        assert claims["sub"] == manager["id"] and claims["yonetici"] is True
        seen.add(claims["jti"])
    assert len(seen) == 3
    assert new_events(core, since) == [
        (manager["id"], "ikiz_adresi", f"kullanici:{firm['id']}", {"ad": "basbug"}),
        (manager["id"], "ikiz_adresi_kaldirildi", f"kullanici:{firm['id']}", {})]


def test_the_firm_name_falls_back_to_the_login_and_an_administrator_needs_no_row(core, twin, browser):
    other = make_user(core, "depo@ornek.test", ad_soyad="", rol="yonetici")
    twin.replies = [(200, twin.area("depo-1", "bekliyor"))]
    assert post(browser, path(other), {"ad": "depo-1"}).status_code == 200
    assert twin.requests[0]["body"] == {"ad": "depo-1", "firma": "depo"}


@pytest.mark.parametrize("name", ["", "-basbug", "basbug-", "bas_bug", "başbuğ", "a" * 41, "bas.bug", "bas bug",
                                  5, None, ["basbug"]])
def test_an_invalid_name_is_refused_before_the_twin_is_asked(core, twin, browser, firm, name):
    before = snapshot(core, sessions=False)
    response = post(browser, path(firm), {"ad": name})
    assert response.status_code == 400 and response.get_json()["message"] == INVALID
    assert twin.requests == [] and snapshot(core, sessions=False) == before


@pytest.mark.parametrize("name", sorted(ikiz_adresi.RESERVED))
def test_a_reserved_name_is_refused(core, twin, browser, firm, name):
    response = post(browser, path(firm), {"ad": name.upper()})
    assert (response.status_code, response.get_json()["message"]) == (400, "Bu ad ayrılmış; başka bir ad seçin.")
    assert twin.requests == []


def test_the_longest_name_and_digits_are_accepted(twin, browser, firm):
    name = "a" + "1-" * 19 + "z"
    assert len(name) == 40
    twin.replies = [(200, twin.area(name, "bekliyor"))]
    assert post(browser, path(firm), {"ad": name}).status_code == 200
    assert twin.requests[0]["body"]["ad"] == name


def test_the_body_is_read_strictly(twin, browser, firm):
    for body in ({}, {"ad": "basbug", "firma": "Başka"}):
        response = post(browser, path(firm), body)
        assert (response.status_code, response.get_json()["message"]) == (400, "İstek alanları geçersiz.")
    assert post(browser, path(firm, "/kaldir"), {"ad": "x"}).status_code == 400
    assert twin.requests == []


@pytest.mark.parametrize("modules", [{}, {users.RPA: None}, {users.IKIZ: date(2026, 10, 25)}])
def test_without_an_active_twin_right_no_address_is_opened(core, twin, browser, modules):
    user = make_user(core, "ali@ornek.test", modules=modules)
    since = last_event_id(core)
    response = post(browser, path(user), {"ad": "ali"})
    assert (response.status_code, response.get_json()) == (409, NO_RIGHT)
    assert twin.requests == [] and new_events(core, since) == []


def test_an_expired_firm_address_can_still_be_read_and_removed(core, twin, browser):
    # Hakkı biten firmanın adresi panelden kapatılabilmeli; yalnız yeni adres açmak hak ister.
    user = make_user(core, "eski@ornek.test", modules={users.IKIZ: date(2026, 10, 25)})
    assert browser.get(path(user)).status_code == 200
    assert post(browser, path(user, "/kaldir"), {}).status_code == 200
    assert [r["method"] for r in twin.requests] == ["GET", "DELETE"]


def test_an_unknown_account_is_not_found(twin, browser):
    response = browser.get("/api/yonetim/kullanicilar/999999/ikiz-adresi")
    assert (response.status_code, response.get_json()["kod"]) == (404, "BULUNAMADI")
    assert twin.requests == []


def test_the_twins_refusals_pass_through_as_text_and_record_nothing(core, twin, browser, firm):
    since = last_event_id(core)
    twin.replies = [(409, {"hata": "KULLANILIYOR", "mesaj": "Bu ad başka bir firmada kullanılıyor."}),
                    (400, {"hata": "GECERSIZ", "mesaj": "Ad uygun değil.\x00"}),
                    (409, b"not json")]
    taken = post(browser, path(firm), {"ad": "basbug"})
    assert (taken.status_code, taken.get_json()) == (409, {"success": False, "kod": "KULLANILIYOR",
                                                           "message": "Bu ad başka bir firmada kullanılıyor."})
    invalid = post(browser, path(firm), {"ad": "basbug"})
    assert (invalid.status_code, invalid.get_json()) == (400, {"success": False, "kod": "GECERSIZ",
                                                               "message": "Ad uygun değil."})
    plain = post(browser, path(firm), {"ad": "basbug"})
    assert (plain.status_code, plain.get_json()["message"]) == (409, "Bu ad başka bir firmada kullanılıyor.")
    assert new_events(core, since) == []


def test_a_failing_status_shows_its_message(twin, browser, firm):
    twin.replies = [(200, twin.area("basbug", "hata", "Sertifika alınamadı."))]
    assert browser.get(path(firm)).get_json()["alan"] == {
        "alt_alan": "basbug", "durum": "hata", "mesaj": "Sertifika alınamadı.", "adres": "https://basbug.peyrev.com"}


@pytest.mark.parametrize("reply", [
    (500, {"hata": "SUNUCU"}), (401, {"hata": "YETKI"}), (403, {"hata": "YETKI"}), (404, {}), (302, {}),
    (200, b"{"), (200, {"durum": "bilinmiyor"}), (200, {"alt_alan": "<b>", "durum": "hazir"}), (200, [1]),
    (200, b" " * (ikiz_adresi.RESPONSE_LIMIT + 1)),
])
def test_a_broken_twin_answer_is_a_bad_gateway(core, twin, browser, firm, reply):
    since = last_event_id(core)
    twin.replies = [reply]
    response = post(browser, path(firm), {"ad": "basbug"})
    assert response.status_code == 502 and response.get_json()["kod"] == "IKIZ_ERISIM"
    assert new_events(core, since) == []
    if reply[0] in (401, 403):
        assert response.get_json()["message"] == "Dijital İkiz servisi yönetici kimliğini kabul etmedi."
    # A redirect is never followed.
    assert len(twin.requests) == 1


def test_an_unreachable_twin_is_a_bad_gateway_and_the_token_is_never_logged(core, browser, firm, caplog, monkeypatch):
    with socket.socket() as probe:  # a port nobody listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    core.settings = replace(core.settings, ikiz_internal_base=f"http://127.0.0.1:{port}")
    issued = []
    original = ikiz_adresi.twin_tokens.issue
    monkeypatch.setattr(ikiz_adresi.twin_tokens, "issue", lambda *args: issued.append(original(*args)) or issued[-1])
    caplog.set_level(logging.DEBUG)
    since = last_event_id(core)
    for method, url, body in (("GET", path(firm), None), ("POST", path(firm), {"ad": "basbug"}),
                              ("POST", path(firm, "/kaldir"), {})):
        response = browser.get(url) if method == "GET" else post(browser, url, body)
        assert (response.status_code, response.get_json()) == (502, UNREACHABLE), url
    assert len(issued) == 3 and new_events(core, since) == []
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "Dijital İkiz servisine ulaşılamadı" in logged
    for token in issued:
        assert token not in logged and token.split(".")[0] not in logged


@pytest.mark.parametrize("base", ["https://127.0.0.1:5102", "http://ikiz.peyrev.com:5102", "http://10.0.0.5:5102",
                                  "http://127.0.0.1", "http://user:pw@127.0.0.1:5102", "http://127.0.0.1:5102/yol"])
def test_only_a_loopback_http_address_is_used(core, twin, browser, firm, base):
    core.settings = replace(core.settings, ikiz_internal_base=base)
    response = browser.get(path(firm))
    assert (response.status_code, response.get_json()) == (502, UNREACHABLE)
    assert twin.requests == []


def test_the_default_address_is_the_local_twin(monkeypatch, tmp_path):
    from peyrev.config import Settings

    monkeypatch.setenv("PEYREV_DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("PEYREV_IKIZ_IC_ADRES", raising=False)
    assert Settings.from_environment().ikiz_internal_base == "http://127.0.0.1:5102"
    monkeypatch.setenv("PEYREV_IKIZ_IC_ADRES", "http://127.0.0.1:6000")
    assert Settings.from_environment().ikiz_internal_base == "http://127.0.0.1:6000"


def test_a_plain_user_or_a_missing_session_reaches_nothing(app, core, twin, firm):
    for client in (app.test_client(),):
        assert client.get(path(firm)).status_code == 401
        assert client.post(path(firm), json={"ad": "basbug"}, headers=HEADERS).status_code == 401
    # The CSRF rule holds for the state-changing calls even with a session.
    make_user(core, "yonetici@peyrev.test", rol="yonetici")
    browser = signed_in(app)
    for url, body in ((path(firm), {"ad": "basbug"}), (path(firm, "/kaldir"), {})):
        response = browser.post(url, json=body, headers={"Origin": HEADERS["Origin"]})
        assert (response.status_code, response.get_json()["kod"]) == (403, "CSRF")
    assert twin.requests == []
