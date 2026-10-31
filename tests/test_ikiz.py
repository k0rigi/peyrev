"""Peyrev Dijital İkiz (MOD_IKIZ): hak ve paket, imzalı giriş belirteci (ayrı Ed25519 anahtarı), Studio lisansındaki
`ikiz_erisim` ve "ikiz" iddiası, Hesabım'dan tek tıkla giriş (GET /api/uye/ikiz).

Belirteç burada ikiz servisinin yapacağı gibi yalnız açık anahtarla (ikiz_ed25519.pub) doğrulanır: biçim, imza, iddialar
ve süre. Biçim ya da anahtar değişirse servis girişleri reddeder; bu testler de düşer.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import date, timedelta

import psycopg
import pytest
from conftest import START, make_user
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from test_accounts import login, renew, studio_agents, studio_license
from test_accounts_admin import account, events, manager, pending_id, refused  # noqa: F401 - manager is a fixture
from test_accounts_applications import apply, stored
from test_topluluk import HEADERS, member

from peyrev import keys, users
from peyrev.accounts import admin, ikiz, licensing
from peyrev.keys import Keys

PART = re.compile(r"[A-Za-z0-9_-]+")
CLAIMS = ["aud", "sub", "ad", "iat", "exp", "jti"]


def _unpad(text: str) -> bytes:
    assert PART.fullmatch(text), text  # base64url without padding
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_twin(token: str, public_key: str, now: int) -> dict:
    """İkiz servisinin denetimi: <base64url(yük)>.<base64url(imza)>, imza yükün baytları üzerinde Ed25519."""
    assert isinstance(token, str) and token.count(".") == 1
    encoded, signed = token.split(".")
    body, signature = _unpad(encoded), _unpad(signed)
    assert len(signature) == 64
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True)).verify(signature, body)
    claims = json.loads(body.decode("utf-8"))
    # Compact JSON, UTF-8, not escaped to ASCII; claims in this order.
    assert body == json.dumps(claims, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert list(claims) == CLAIMS
    assert claims["aud"] == "ikiz" and type(claims["sub"]) is int and isinstance(claims["ad"], str)
    assert type(claims["iat"]) is int and claims["exp"] - claims["iat"] == 720
    assert claims["iat"] <= now < claims["exp"]
    assert re.fullmatch(r"[0-9a-f]{32}", claims["jti"])
    return claims


def now(core) -> int:
    return int(core.clock.now().timestamp())


# ----- anahtar ---------------------------------------------------------------------------------------------------
def test_the_twin_key_is_its_own_and_its_public_half_is_written_for_the_twin_service(tmp_path):
    folder = tmp_path / "sirlar"
    assert sorted(keys.generate_missing(folder)) == ["ikiz_ed25519", "jwt_secret", "lisans_ed25519"]
    assert keys.generate_missing(folder) == []  # never overwritten
    assert (folder / "ikiz_ed25519").stat().st_mode & 0o777 == 0o640
    assert len(base64.b64decode((folder / "ikiz_ed25519").read_bytes().strip(), validate=True)) == 32
    loaded = Keys(folder)
    assert loaded.ikiz_public_key != loaded.license_public_key
    assert keys.write_ikiz_public(folder) is True
    public = folder / "ikiz_ed25519.pub"
    assert public.read_text() == loaded.ikiz_public_key + "\n" and public.stat().st_mode & 0o777 == 0o644
    assert len(base64.b64decode(public.read_text().strip(), validate=True)) == 32
    assert keys.write_ikiz_public(folder) is False  # already right
    public.write_text("eski\n")
    assert keys.write_ikiz_public(folder) is True and public.read_text() == loaded.ikiz_public_key + "\n"
    # A missing twin key stops the service like the other secrets (deployment creates it first).
    (folder / "ikiz_ed25519").unlink()
    with pytest.raises(RuntimeError, match="ikiz_ed25519"):
        Keys(folder)


def test_the_token_has_the_exact_format_and_only_the_twin_key_signs_it(core):
    user = make_user(core, "ayse@ornek.test", ad_soyad="Ayşe Çınar", modules={users.IKIZ: None})
    token = ikiz.issue(core, user)
    claims = verify_twin(token, core.keys.ikiz_public_key, now(core))
    assert claims == {"aud": "ikiz", "sub": user["id"], "ad": "Ayşe Çınar", "iat": int(START.timestamp()),
                      "exp": int(START.timestamp()) + 720, "jti": claims["jti"]}
    assert "Ayşe Çınar".encode() in _unpad(token.split(".")[0])  # UTF-8, not \\u escapes
    assert ikiz.issue(core, user).split(".")[0] != token.split(".")[0]  # a new jti every time
    with pytest.raises(InvalidSignature):
        verify_twin(token, core.keys.license_public_key, now(core))
    encoded, signed = token.split(".")
    forged = base64.urlsafe_b64encode(_unpad(encoded).replace(b'"ad":"Ay', b'"ad":"Xy')).rstrip(b"=").decode()
    with pytest.raises(InvalidSignature):
        verify_twin(forged + "." + signed, core.keys.ikiz_public_key, now(core))
    # Without a full name the part of the login before "@" is shown.
    nameless = make_user(core, "depo.sefi@ornek.test", ad_soyad="", modules={users.IKIZ: None})
    assert verify_twin(ikiz.issue(core, nameless), core.keys.ikiz_public_key, now(core))["ad"] == "depo.sefi"


def test_the_right_follows_the_row_and_the_last_day_and_the_administrator_needs_none(core):
    today = core.clock.today_tr()
    cases = {"aktif@ornek.test": {users.RPA: None, users.IKIZ: today}, "biten@ornek.test": {users.IKIZ: today - timedelta(1)},
             "yok@ornek.test": {users.RPA: None, users.AGENTS: None}}
    people = {login_name: make_user(core, login_name, modules=rights) for login_name, rights in cases.items()}
    people["pasif@ornek.test"] = make_user(core, "pasif@ornek.test", durum="pasif", modules={users.IKIZ: None})
    people["yonetici@peyrev.test"] = make_user(core, "yonetici@peyrev.test", rol="yonetici")
    with core.db.transaction() as cur:
        assert {name: ikiz.allowed(cur, user, today) for name, user in people.items()} == {
            "aktif@ornek.test": True, "biten@ornek.test": False, "yok@ornek.test": False, "pasif@ornek.test": False,
            "yonetici@peyrev.test": True}
        assert users.module_state(cur, people["yonetici@peyrev.test"], users.IKIZ, today) == (users.ACTIVE, None)
        assert users.module_state(cur, people["yonetici@peyrev.test"], users.AGENTS, today) == (users.MISSING, None)


# ----- Studio lisansı --------------------------------------------------------------------------------------------
def test_the_license_carries_the_twin_token_and_claim_only_while_the_right_lasts(client, core):
    ali = make_user(core, "ali@ornek.test", ad_soyad="Ali Veli",
                    modules={users.RPA: None, users.AGENTS: None, users.IKIZ: date(2026, 10, 26)})
    response = login(client)
    claims = studio_license(client, response)
    assert claims["ikiz"] is True
    twin = verify_twin(response.get_json()["ikiz_erisim"], core.keys.ikiz_public_key, now(core))
    assert (twin["sub"], twin["ad"]) == (ali["id"], "Ali Veli")
    # The agent extension is untouched: the twin is never one of its permissions.
    assert set(studio_agents(claims)["yetkiler"]) <= {"agents.use", "agents.manage", "agents.approve"}
    # The next day the right has ended: neither the claim nor the token, the license itself goes on.
    core.clock.at = START + timedelta(days=1)
    later = renew(client, response.get_json()["yenileme"])
    assert "ikiz" not in studio_license(client, later) and "ikiz_erisim" not in later.get_json()
    # Without a row at all, too.
    make_user(core, "veli@ornek.test", modules={users.RPA: None, users.AGENTS: None})
    plain = login(client, kullanici="veli")
    assert "ikiz" not in studio_license(client, plain) and "ikiz_erisim" not in plain.get_json()
    # The owner (administrator role) opens the twin without a row.
    make_user(core, "yonetici@peyrev.test", rol="yonetici")
    owner = login(client, kullanici="yonetici")
    assert studio_license(client, owner)["ikiz"] is True
    verify_twin(owner.get_json()["ikiz_erisim"], core.keys.ikiz_public_key, now(core))


def test_a_twin_token_that_cannot_be_issued_never_costs_the_license(client, core, monkeypatch):
    make_user(core, "ali@ornek.test", modules={users.RPA: None, users.IKIZ: None})

    def broken(*_args, **_options):
        raise RuntimeError("anahtar yok")

    monkeypatch.setattr(ikiz, "issue", broken)
    response = login(client)
    assert studio_license(client, response)["ikiz"] is True
    assert "ikiz_erisim" not in response.get_json() and "destek_erisim" in response.get_json()
    assert licensing.ikiz is ikiz


# ----- Hesabım ---------------------------------------------------------------------------------------------------
def test_the_account_link_opens_the_twin_with_a_fresh_token(app, client, core):
    make_user(core, "ali@ornek.test", ad_soyad="Ali Veli", modules={users.RPA: None, users.IKIZ: date(2026, 10, 27)})
    # Without a session the browser is sent to the sign-in of Hesabım, never handed a token.
    anonymous = client.get("/api/uye/ikiz")
    assert (anonymous.status_code, anonymous.headers["Location"]) == (303, "/hesabim/?ikiz=giris")
    assert anonymous.headers["Cache-Control"] == "no-store"
    stale = app.test_client()
    stale.set_cookie("peyrev_hesap", "x" * 43, path="/api")
    gone = stale.get("/api/uye/ikiz")
    assert gone.headers["Location"] == "/hesabim/?ikiz=giris" and "peyrev_hesap=;" in gone.headers["Set-Cookie"]
    ali = member(app, "ali@ornek.test")
    account = ali.get("/api/uye/hesap").get_json()["hesap"]
    assert account["lisans"]["ikiz"] == {"durum": "aktif", "bitis": "2026-10-27", "suresiz": False}
    opened = ali.get("/api/uye/ikiz")
    assert opened.status_code == 303 and opened.headers["Cache-Control"] == "no-store"
    location = opened.headers["Location"]
    assert location.startswith("/ikiz/giris?b=")
    claims = verify_twin(location.removeprefix("/ikiz/giris?b="), core.keys.ikiz_public_key, now(core))
    assert claims["ad"] == "Ali Veli" and opened.headers["Referrer-Policy"] == "no-referrer"
    # After the last day the link explains instead.
    core.clock.at = START + timedelta(days=2)
    ended = ali.get("/api/uye/ikiz")
    assert (ended.status_code, ended.headers["Location"]) == (303, "/hesabim/?ikiz=yok")
    assert ali.get("/api/uye/hesap").get_json()["hesap"]["lisans"]["ikiz"]["durum"] == "sure_doldu"


def test_an_account_without_the_twin_is_sent_back_and_the_owner_goes_in(app, core):
    make_user(core, "veli@ornek.test", modules={users.RPA: None, users.AGENTS: None})
    veli = member(app, "veli@ornek.test")
    refused_answer = veli.get("/api/uye/ikiz")
    assert (refused_answer.status_code, refused_answer.headers["Location"]) == (303, "/hesabim/?ikiz=yok")
    assert veli.get("/api/uye/hesap").get_json()["hesap"]["lisans"]["ikiz"] == {
        "durum": "yetki_yok", "bitis": None, "suresiz": False}
    owner = make_user(core, "yonetici@peyrev.test", rol="yonetici")
    browser = member(app, "yonetici@peyrev.test")
    location = browser.get("/api/uye/ikiz").headers["Location"]
    assert verify_twin(location.split("b=", 1)[1], core.keys.ikiz_public_key, now(core))["sub"] == owner["id"]


def test_renewal_from_the_account_page_accepts_the_twin_package(app, core):
    user = make_user(core, "ali@ornek.test", modules={users.RPA: date(2026, 10, 1)})
    ali = member(app, "ali@ornek.test")
    asked = ali.post("/api/uye/uzatma", json={"paket": "ikiz"}, headers=HEADERS)
    assert asked.status_code == 200, asked.get_json()
    [event] = events(core, "uzatma_istegi")
    assert (event["hedef"], event["ayrinti"]) == (f"kullanici:{user['id']}", {"paket": "ikiz", "kaynak": "hesabim"})
    assert "Peyrev Dijital İkiz" in core.notifier.messages[-1][1]


# ----- yönetim ve başvuru ----------------------------------------------------------------------------------------
def test_the_twin_package_application_grants_all_three_rights(client, core, manager):  # noqa: F811
    assert apply(client, paket="ikiz").status_code == 201
    assert stored(core)[0]["paket"] == "ikiz"
    assert core.notifier.messages[-1][1].startswith("Ayşe Yılmaz · Peyrev Dijital İkiz\n")
    result = admin.approve_application(core, manager["id"], pending_id(core), bitis="2027-10-26")
    assert result["paket"] == "ikiz"
    _user, rights = account(core, result["kullanici_id"])
    period = {"baslangic": date(2026, 10, 26), "bitis": date(2027, 10, 26)}
    assert rights == {users.RPA: period, users.AGENTS: period, users.IKIZ: period}
    [event] = events(core, "basvuru_onaylandi")
    assert event["ayrinti"]["moduller"] == ["MOD_RPA", "MOD_AJAN", "MOD_IKIZ"]
    response = login(client, kullanici="ayse", sifre="Guclu2026")
    claims = studio_license(client, response)
    assert claims["ikiz"] is True and studio_agents(claims)["aktif"] is True and "ikiz_erisim" in response.get_json()
    # The administrator may also approve a waiting application as the twin package.
    assert apply(client, eposta="mehmet@ornek.test", paket="rpa").status_code == 201
    other = admin.approve_application(core, manager["id"], pending_id(core, "mehmet@ornek.test"), bitis="2027-10-26",
                                      paket="ikiz")
    assert set(account(core, other["kullanici_id"])[1]) == {users.RPA, users.AGENTS, users.IKIZ}


def test_the_administrator_grants_and_removes_the_twin_and_opens_twin_accounts(client, core, manager):  # noqa: F811
    ali = make_user(core, "ali@ornek.test", modules={users.RPA: None})
    given = admin.set_module(core, manager["id"], ali["id"], users.IKIZ, bitis="2027-06-30")
    assert given == {"kullanici_id": ali["id"], "modul": users.IKIZ, "baslangic": "2026-10-26", "bitis": "2027-06-30"}
    assert "ikiz_erisim" in login(client).get_json()
    assert admin.remove_module(core, manager["id"], ali["id"], users.IKIZ)["kaldirildi"] is True
    assert "ikiz_erisim" not in login(client).get_json()
    assert [(event["tur"], event["ayrinti"]["modul"]) for event in events(core)] == [
        ("modul_ayarlandi", users.IKIZ), ("modul_kaldirildi", users.IKIZ)]
    assert str(refused(admin.set_module, core, manager["id"], ali["id"], "MOD_YOK", bitis=None)) == "Modül geçersiz."
    options = {"giris": "DEPO_1", "ad_soyad": "Depo Bir", "baslangic": "2026-10-26", "bitis": "2027-10-26"}
    # The twin comes as its package: with agents, even when agents were not ticked.
    preview = admin.create_user(core, manager["id"], **options, ikiz=True, onizleme=True)
    assert preview["moduller"] == ["MOD_RPA", "MOD_AJAN", "MOD_IKIZ"]
    created = admin.create_user(core, manager["id"], **options, ikiz=True)
    assert set(account(core, created["kullanici_id"])[1]) == {users.RPA, users.AGENTS, users.IKIZ}
    plain = admin.create_user(core, manager["id"], **{**options, "giris": "DEPO_2"}, ajan=True)
    assert set(account(core, plain["kullanici_id"])[1]) == {users.RPA, users.AGENTS}
    assert str(refused(admin.create_user, core, manager["id"], **{**options, "giris": "DEPO_3"}, ikiz="evet")) == (
        "Dijital İkiz seçimi geçersiz.")
    listed = {row["giris"]: row for row in admin.list_users(core, manager["id"])["kullanicilar"]}
    assert set(listed["DEPO_1"]["moduller"]) == {users.RPA, users.AGENTS, users.IKIZ}


def test_the_database_accepts_the_twin_right_and_package_and_nothing_else(core):
    user = make_user(core, "ali@ornek.test", modules={users.IKIZ: None})
    with core.db.transaction() as cur:
        cur.execute("SELECT name FROM schema_migrations WHERE version = '0014'")
        assert cur.fetchone()["name"] == "0014_dijital_ikiz.sql"
    with pytest.raises(psycopg.errors.CheckViolation), core.db.transaction() as cur:
        cur.execute("INSERT INTO kullanici_modulleri (kullanici_id, modul) VALUES (%s, 'MOD_YOK')", (user["id"],))
    with pytest.raises(psycopg.errors.CheckViolation), core.db.transaction() as cur:
        cur.execute("INSERT INTO basvurular (id, ad_soyad, eposta, paket, aydinlatma_surumu, olusturma) "
                    "VALUES (%s, 'A', 'a@ornek.test', 'yok', '2026-10-26', now())", ("f" * 32,))
