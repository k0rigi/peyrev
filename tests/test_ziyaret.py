"""Çerezsiz ziyaret sayımı (POST /api/ziyaret) ve paneldeki özet: yalnız aynı kökenden gelen tarayıcı istekleri sayılır,
kişiyi tanımlayan hiçbir şey saklanmaz, yol ve kaynak sınırlıdır, günlük satır sınırı ve saklama süresi işler."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from conftest import make_user
from test_admin_auth import signed_in

from peyrev import ziyaret

BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36",
           "Origin": "https://peyrev.com", "Sec-Fetch-Site": "same-origin", "Content-Type": "text/plain;charset=UTF-8"}


def visit(client, yol="/", kaynak="", **headers):
    body = json.dumps({"yol": yol, "kaynak": kaynak})
    return client.post("/api/ziyaret", data=body.encode(), headers={**BROWSER, **headers})


def counts(core):
    with core.db.transaction() as cur:
        cur.execute("SELECT gun::text, yol, kaynak, sayi FROM ziyaret_sayilari ORDER BY gun, yol, kaynak")
        return [tuple(row.values()) for row in cur.fetchall()]


def test_only_a_count_is_kept_per_day_page_and_referring_site(client, core):
    with core.db.transaction() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'ziyaret_sayilari'")
        assert {row["column_name"] for row in cur.fetchall()} == {"gun", "yol", "kaynak", "sayi"}
    for _ in range(2):
        response = visit(client, "/egitim/")
        assert response.status_code == 204 and response.data == b""
        assert "Set-Cookie" not in response.headers
    visit(client, "/", "www.Google.com")
    visit(client, "/", "peyrev.com")
    visit(client, "/", "www.peyrev.com")
    assert counts(core) == [("2026-10-26", "/", "", 2), ("2026-10-26", "/", "google.com", 1),
                            ("2026-10-26", "/egitim/", "", 2)]


@pytest.mark.parametrize("headers", [
    {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"},
    {"User-Agent": "Mozilla/5.0 HeadlessChrome/130.0"},
    {"User-Agent": "curl/8.4.0"},
    {"User-Agent": "python-requests/2.32"},
    {"User-Agent": ""},
    {"Origin": "https://evil.test"},
    {"Origin": "https://peyrev.com, https://evil.test"},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_bots_and_other_sites_are_not_counted(client, core, headers):
    assert visit(client, **headers).status_code == 204
    assert counts(core) == []


@pytest.mark.parametrize(("yol", "kaynak"), [
    ("/index.html", ""), ("egitim/", ""), ("/egitim", ""), ("/Egitim/", ""), ("/a/b/c/d/", ""), ("/../", ""),
    ("/" + "a" * 41 + "/", ""), ("/?q=1", ""), ("", ""),
])
def test_only_site_shaped_paths_are_counted(client, core, yol, kaynak):
    assert visit(client, yol, kaynak).status_code == 204
    assert counts(core) == []


@pytest.mark.parametrize("kaynak", ["evil test", "<script>", "a" * 120 + ".com", "localhost", "-x.com", "x.com:8080"])
def test_an_invalid_referring_site_is_counted_as_direct(client, core, kaynak):
    visit(client, "/", kaynak)
    assert counts(core) == [("2026-10-26", "/", "", 1)]


def test_a_malformed_or_large_body_is_ignored(client, core):
    for body in (b"{", b"[]", b'{"yol": 5}', b'{"yol": "/", "x": "' + b"a" * 2000 + b'"}', "\xff".encode("latin-1")):
        assert client.post("/api/ziyaret", data=body, headers=BROWSER).status_code == 204
    assert counts(core) == []


def test_a_day_opens_a_limited_number_of_rows_and_old_days_are_deleted(client, core, monkeypatch):
    monkeypatch.setattr(ziyaret, "DAILY_ROWS", 2)
    for path in ("/", "/egitim/", "/topluluk/", "/"):
        visit(client, path)
    assert counts(core) == [("2026-10-26", "/", "", 2), ("2026-10-26", "/egitim/", "", 1)]
    core.clock.advance(days=401)
    visit(client, "/topluluk/")
    assert [row[0] for row in counts(core)] == [(core.clock.today_tr()).isoformat()]


@pytest.fixture
def admin(app, core):
    make_user(core, "yonetici@peyrev.test", rol="yonetici")
    return signed_in(app)


def test_the_panel_summarises_days_pages_and_referring_sites(admin, client, core):
    core.clock.advance(days=-2)
    visit(client, "/", "google.com")
    core.clock.advance(days=2)
    for path in ("/", "/egitim/", "/egitim/", "/egitim/"):
        visit(client, path)
    data = admin.get("/api/yonetim/ziyaretler?gun=7").get_json()
    today = core.clock.today_tr()
    assert data["gun_sayisi"] == 7 and data["toplam"] == 5 and data["bugun"] == 4
    assert [item["gun"] for item in data["gunluk"]] == [(today - timedelta(days=6 - n)).isoformat() for n in range(7)]
    assert [item["sayi"] for item in data["gunluk"]] == [0, 0, 0, 0, 1, 0, 4]
    assert data["sayfalar"] == [{"yol": "/egitim/", "sayi": 3}, {"yol": "/", "sayi": 2}]
    assert data["kaynaklar"] == [{"kaynak": "google.com", "sayi": 1}]
    assert admin.get("/api/yonetim/ziyaretler").get_json()["gun_sayisi"] == 30
    assert admin.get("/api/yonetim/ziyaretler?gun=365").status_code == 400
