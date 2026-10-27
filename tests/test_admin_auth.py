"""Yönetim paneli kimliği: giriş, deneme sınırı, sunucu tarafı oturum, CSRF kuralı ve iki adımlı doğrulama (TOTP)."""

import hashlib
import re
from datetime import timedelta

import pytest
from conftest import START, make_user
from test_accounts import login as studio_login

from peyrev import users
from peyrev.accounts import rules
from peyrev.admin import auth, kurtarma, totp
from peyrev.common import ApiError

PASSWORD = "Gizli12345"
ORIGIN = "https://peyrev.com"  # Settings.public_base in tests
HEADERS = {"Origin": ORIGIN, "X-Peyrev-Yonetim": "1"}
WRONG_LOGIN = {"success": False, "kod": "HATALI_GIRIS", "message": "Kullanıcı adı veya şifre hatalı."}
CSRF = {"success": False, "kod": "CSRF", "message": "İstek doğrulanamadı. Sayfayı yenileyip tekrar deneyin."}
TOO_MANY = {"success": False, "kod": "COK_DENEME", "message": "Çok fazla hatalı deneme. 15 dakika sonra tekrar deneyin."}


def sign_in(client, giris="yonetici@peyrev.test", sifre=PASSWORD, kod=None, address="10.0.0.1", headers=None):
    body = {"giris": giris, "sifre": sifre, **({"kod": kod} if kod is not None else {})}
    return client.post("/api/yonetim/giris", json=body,
                       headers={**HEADERS, "X-Forwarded-For": "192.0.2.1, " + address, "User-Agent": "Deneme/1.0",
                                **(headers or {})})


def post(client, path, body=None, headers=None):
    return client.post("/api/yonetim/" + path, json={} if body is None else body, headers={**HEADERS, **(headers or {})})


def signed_in(app, giris="yonetici@peyrev.test", **options):
    """Oturumu açık yeni bir tarayıcı."""
    browser = app.test_client()
    response = sign_in(browser, giris, **options)
    assert response.status_code == 200, response.get_json()
    return browser


def cookie_of(response):
    """Set-Cookie başlığı: (değer, küçük harfli öznitelikler; değersiz öznitelik True)."""
    [header] = [value for value in response.headers.getlist("Set-Cookie") if value.startswith(auth.COOKIE + "=")]
    first, *rest = [part.strip() for part in header.split(";")]
    attributes = {}
    for part in rest:
        name, separator, value = part.partition("=")
        attributes[name.lower()] = value if separator else True
    return first.split("=", 1)[1], attributes


def panel_sessions(core, user_id=None):
    with core.db.transaction() as cur:
        cur.execute("SELECT * FROM yonetim_oturumlari" + (" WHERE kullanici_id = %s" if user_id else "")
                    + " ORDER BY olusturma, id", (user_id,) if user_id else ())
        return cur.fetchall()


def events(core, kind=None):
    with core.db.transaction() as cur:
        cur.execute("SELECT * FROM yonetim_olaylari" + (" WHERE tur = %s" if kind else "") + " ORDER BY id",
                    (kind,) if kind else ())
        return cur.fetchall()


def failed_attempts(core):
    with core.db.transaction() as cur:
        cur.execute("SELECT giris, ip FROM giris_denemeleri ORDER BY id")
        return [(row["giris"], row["ip"]) for row in cur.fetchall()]


def code_now(core, secret, offset=0):
    return totp.code_at(secret, totp.step_of(core.clock.now()) + offset)


def wrong_code(core, secret):
    """Altı haneli ama şu anda (saat kaymasıyla birlikte) geçmeyen bir kod."""
    accepted = {code_now(core, secret, offset) for offset in (-1, 0, 1)}
    return next(code for code in ("000000", "111111", "222222", "333333") if code not in accepted)


def enable_two_step(core, browser):
    """Hesabım'dan iki adımlı doğrulamayı açar; düz gizli anahtarı döndürür."""
    started = post(browser, "iki-adim/baslat", {"sifre": PASSWORD})
    assert started.status_code == 200, started.get_json()
    secret = started.get_json()["gizli"].replace(" ", "")
    confirmed = post(browser, "iki-adim/onayla", {"kod": code_now(core, secret)})
    assert confirmed.status_code == 200, confirmed.get_json()
    core.clock.advance(seconds=30)  # the confirming code is spent; the next step's code is fresh
    return secret


@pytest.fixture
def manager(core):
    return make_user(core, "yonetici@peyrev.test", rol="yonetici", ad_soyad="Yönetici Kişi")


def test_an_administrator_signs_in_to_a_server_side_session(client, core, manager):
    response = sign_in(client)
    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "yonetici": {"kullanici_id": manager["id"], "giris": "yonetici@peyrev.test", "ad_soyad": "Yönetici Kişi",
                     "iki_adim": False},
        "oturum": {"acilis": "2026-10-26 12:00", "bitis": "2026-10-27 00:00", "bosta_dakika": 120}}
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
    token, attributes = cookie_of(response)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
    assert attributes == {"max-age": "43200", "secure": True, "httponly": True, "path": "/", "samesite": "Strict",
                          "expires": attributes["expires"]}
    # Only the SHA-256 digest of the token is kept; the browser alone holds the token.
    [session] = panel_sessions(core)
    assert session["belirtec_ozeti"] == hashlib.sha256(token.encode()).hexdigest() and token not in str(session)
    assert (session["kullanici_id"], session["ip"], session["tarayici"]) == (manager["id"], "10.0.0.1", "Deneme/1.0")
    assert (session["olusturma"], session["son_gorulme"], session["bitis"]) == (START, START, START + timedelta(hours=12))
    me = client.get("/api/yonetim/ben")
    assert me.status_code == 200
    assert me.get_json()["oturumlar"] == [{"id": session["id"], "bu_oturum": True, "acilis": "2026-10-26 12:00",
                                           "son_islem": "2026-10-26 12:00", "bitis": "2026-10-27 00:00",
                                           "ip": "10.0.0.1", "tarayici": "Deneme/1.0"}]
    [event] = events(core)
    assert (event["yonetici_id"], event["tur"], event["hedef"], event["ayrinti"]) == (
        manager["id"], "yonetim_girisi", f"kullanici:{manager['id']}", {"ip": "10.0.0.1", "iki_adim": False})


def test_the_login_name_follows_the_studio_rule(app, core, manager):
    assert sign_in(app.test_client(), "YONETICI").status_code == 200  # the part before "@", any case
    assert sign_in(app.test_client(), "Yonetici@Peyrev.Test").status_code == 200


def test_wrong_passwords_passive_accounts_and_users_without_the_role_are_refused(client, core, manager):
    make_user(core, "eski@peyrev.test", rol="yonetici", durum="pasif")
    make_user(core, "calisan@peyrev.test", modules={users.RPA: None})
    for giris, sifre in (("yonetici@peyrev.test", "Yanlis12345"), ("yok@peyrev.test", PASSWORD),
                         ("eski@peyrev.test", PASSWORD)):
        response = sign_in(client, giris, sifre)
        # A passive account looks like a wrong password: the answer never tells which accounts exist.
        assert (response.status_code, response.get_json()) == (401, WRONG_LOGIN)
        assert "Set-Cookie" not in response.headers
    staff = sign_in(client, "calisan@peyrev.test")
    assert (staff.status_code, staff.get_json()) == (403, {
        "success": False, "kod": "YETKI_YOK", "message": "Bu panel yalnız platform yöneticisine açıktır."})
    for body in ({"giris": "yonetici@peyrev.test"}, {"giris": "", "sifre": PASSWORD}, {"giris": 1, "sifre": PASSWORD},
                 {"giris": "yonetici@peyrev.test", "sifre": PASSWORD, "kod": 123456},
                 {"giris": "yonetici@peyrev.test", "sifre": PASSWORD, "fazla": True}):
        assert client.post("/api/yonetim/giris", json=body, headers=HEADERS).status_code == 400
    assert panel_sessions(core) == [] and events(core) == []
    assert failed_attempts(core) == [("yonetici@peyrev.test", "10.0.0.1"), ("yok@peyrev.test", "10.0.0.1"),
                                     ("eski@peyrev.test", "10.0.0.1")]
    assert client.get("/api/yonetim/ben").status_code == 401


def test_failed_sign_ins_are_limited_per_account_and_shared_with_studio(app, client, core, manager):
    # Switching between the short name and the whole address gives no new attempts.
    for index in range(10):
        assert sign_in(client, "yonetici" if index % 2 else "yonetici@peyrev.test", "Yanlis12345").status_code == 401
    for address in ("10.0.0.1", "10.0.0.2"):
        refused = sign_in(client, address=address)
        assert (refused.status_code, refused.get_json()) == (429, TOO_MANY)  # even the right password
    assert studio_login(client, kullanici="yonetici@peyrev.test").get_json()["kod"] == "COK_DENEME"
    assert panel_sessions(core) == []
    core.clock.advance(minutes=15, seconds=1)
    assert sign_in(client).status_code == 200


def test_an_unknown_name_checks_as_many_hashes_as_a_wrong_password(client, core, manager, monkeypatch):
    checked = []
    original = rules.matches

    def spy(password_hash, candidate):
        checked.append((password_hash or "").split("$", 1)[0])
        return original(password_hash, candidate)

    monkeypatch.setattr(rules, "matches", spy)
    for name in ("yok@peyrev.test", "yonetici"):
        checked.clear()
        assert sign_in(client, name, "Yanlis12345").status_code == 401
        assert sorted(checked) == sorted(rules.hash_methods()), name


def test_failed_sign_ins_are_limited_per_address(app, client, core, manager):
    for index in range(30):
        assert sign_in(client, f"deneme{index}@peyrev.test", "Yanlis12345", address="10.0.0.9").status_code == 401
    assert sign_in(client, address="10.0.0.9").get_json() == TOO_MANY
    assert sign_in(app.test_client(), address="10.0.0.10").status_code == 200


def test_sessions_end_after_two_idle_hours_or_twelve_hours_in_all(app, core, manager):
    idle = signed_in(app)
    core.clock.advance(hours=1, minutes=59, seconds=59)
    assert idle.get("/api/yonetim/ben").status_code == 200  # activity moves the idle limit
    core.clock.advance(hours=2)
    response = idle.get("/api/yonetim/ben")
    assert (response.status_code, response.get_json()) == (401, {
        "success": False, "kod": "OTURUM_GECERSIZ", "message": "Oturumunuzun süresi doldu. Yeniden giriş yapın."})
    value, attributes = cookie_of(response)  # the browser forgets the cookie
    assert value == "" and attributes["max-age"] == "0"
    assert panel_sessions(core) == []

    busy = signed_in(app)
    for _ in range(7):
        core.clock.advance(hours=1, minutes=30)
        assert busy.get("/api/yonetim/ben").status_code == 200
    core.clock.advance(hours=1, minutes=30)  # twelve hours after signing in, however busy
    assert busy.get("/api/yonetim/ben").get_json()["kod"] == "OTURUM_GECERSIZ"
    assert panel_sessions(core) == []


def test_signing_out_another_sign_in_and_closing_other_sessions(app, core, manager):
    first, second = signed_in(app), signed_in(app)
    core.clock.advance(seconds=1)
    assert sign_in(first).status_code == 200  # signing in again in the same browser replaces its session
    assert len(panel_sessions(core)) == 2
    listed = first.get("/api/yonetim/ben").get_json()["oturumlar"]
    assert [item["bu_oturum"] for item in listed] == [True, False]
    closed = post(first, "oturumlar/digerlerini-kapat")
    assert closed.get_json() == {"success": True, "kapatilan": 1}
    assert second.get("/api/yonetim/ben").get_json()["kod"] == "OTURUM_GECERSIZ"
    out = post(first, "cikis")
    assert out.status_code == 200 and cookie_of(out)[0] == ""
    assert first.get("/api/yonetim/ben").get_json()["kod"] == "OTURUM_GEREKLI"
    assert panel_sessions(core) == []
    assert [event["tur"] for event in events(core)] == ["yonetim_girisi", "yonetim_girisi", "yonetim_girisi",
                                                       "yonetim_oturumlari_kapatildi", "yonetim_cikisi"]
    assert events(core, "yonetim_oturumlari_kapatildi")[0]["ayrinti"] == {"adet": 1}


def test_an_administrator_keeps_at_most_ten_sessions(app, core, manager):
    browsers = []
    for _ in range(11):
        browsers.append(signed_in(app))
        core.clock.advance(seconds=1)
    assert len(panel_sessions(core, manager["id"])) == 10
    assert browsers[0].get("/api/yonetim/ben").status_code == 401  # the oldest one closed
    assert all(browser.get("/api/yonetim/ben").status_code == 200 for browser in browsers[1:])


def test_a_new_password_a_passive_account_or_a_lost_role_ends_the_session(app, client, core, manager):
    second = make_user(core, "ikinci@peyrev.test", rol="yonetici")
    browser = signed_in(app)
    changed = client.post("/api/rpa/hesap/sifre", json={"kullanici": "yonetici@peyrev.test", "sifre": PASSWORD,
                                                       "yeni_sifre": "Yeni2026sifre"})
    assert changed.status_code == 200
    assert browser.get("/api/yonetim/ben").get_json() == {
        "success": False, "kod": "OTURUM_GECERSIZ", "message": "Oturumunuz kapandı. Yeniden giriş yapın."}
    assert panel_sessions(core, manager["id"]) == []

    other = signed_in(app, "ikinci@peyrev.test")
    browser = signed_in(app, sifre="Yeni2026sifre")
    assert post(browser, f"kullanicilar/{second['id']}/durum", {"durum": "pasif"}).status_code == 200
    assert panel_sessions(core, second["id"]) == []
    assert other.get("/api/yonetim/ben").status_code == 401
    # Restoring the account does not bring the closed session back.
    assert post(browser, f"kullanicilar/{second['id']}/durum", {"durum": "aktif"}).status_code == 200
    assert other.get("/api/yonetim/ben").status_code == 401

    other = signed_in(app, "ikinci@peyrev.test")
    assert post(browser, f"kullanicilar/{second['id']}/rol", {"rol": "kullanici"}).status_code == 200
    assert other.get("/api/yonetim/ben").status_code == 401
    # A role taken away directly in the database ends the session on its next request too.
    third = make_user(core, "ucuncu@peyrev.test", rol="yonetici")
    late = signed_in(app, "ucuncu@peyrev.test")
    with core.db.transaction() as cur:
        cur.execute("UPDATE kullanicilar SET rol = 'kullanici' WHERE id = %s", (third["id"],))
    assert late.get("/api/yonetim/ozet").get_json()["kod"] == "OTURUM_GECERSIZ"
    assert panel_sessions(core, third["id"]) == []


@pytest.mark.parametrize("headers", [
    {"X-Peyrev-Yonetim": None},
    {"X-Peyrev-Yonetim": "0"},
    {"Origin": None},
    {"Origin": "https://evil.test"},
    {"Origin": "null"},
    {"Origin": "http://peyrev.com"},
    {"Origin": "https://peyrev.com.evil.test"},
    {"Origin": None, "Referer": "https://evil.test/yonetim/"},
    {"Origin": None, "Referer": "https://peyrev.com.evil.test/yonetim/"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
])
def test_state_changing_requests_need_the_panel_header_and_origin(app, client, core, manager, headers):
    browser = signed_in(app)
    sent = {key: value for key, value in {**HEADERS, **headers}.items() if value is not None}
    for path, body in (("giris", {"giris": "yonetici@peyrev.test", "sifre": "Yanlis12345"}), ("cikis", {}),
                       (f"kullanicilar/{manager['id']}/bilgiler", {"ad_soyad": "Başka Ad"})):
        for actor in (client, browser):
            response = actor.post("/api/yonetim/" + path, json=body, headers=sent)
            assert (response.status_code, response.get_json()) == (403, CSRF)
    # Nothing happened: no failed attempt, the session is still open and the name is unchanged.
    assert failed_attempts(core) == [] and len(panel_sessions(core)) == 1
    assert browser.get("/api/yonetim/ben").get_json()["yonetici"]["ad_soyad"] == "Yönetici Kişi"


def test_the_referer_stands_in_for_a_missing_origin_and_reads_need_no_header(app, client, core, manager):
    referer = {"Origin": None, "Referer": ORIGIN + "/yonetim/#ozet", "Sec-Fetch-Site": "same-origin"}
    sent = {key: value for key, value in {**HEADERS, **referer}.items() if value is not None}
    assert client.post("/api/yonetim/giris", json={"giris": "yonetici@peyrev.test", "sifre": PASSWORD},
                       headers=sent).status_code == 200
    assert client.get("/api/yonetim/ben").status_code == 200  # no header, no origin: a read changes nothing
    assert client.post("/api/yonetim/cikis", json={}, headers=sent).status_code == 200


def test_totp_codes_follow_rfc_6238():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890" (RFC 6238, SHA-1)
    for seconds, code in ((59, "287082"), (1111111109, "081804"), (1111111111, "050471"), (1234567890, "005924"),
                          (2000000000, "279037"), (20000000000, "353130")):
        assert totp.code_at(secret, seconds // 30) == code
    assert len(totp.new_secret()) == 32 and totp.new_secret() != totp.new_secret()


def test_two_step_verification_is_set_up_with_the_password_and_confirmed_with_a_code(app, core, manager):
    browser, other = signed_in(app), signed_in(app)
    wrong = post(browser, "iki-adim/baslat", {"sifre": "Yanlis12345"})
    assert (wrong.status_code, wrong.get_json()["kod"]) == (403, "SIFRE_HATALI")  # not the session: no 401
    started = post(browser, "iki-adim/baslat", {"sifre": PASSWORD})
    assert started.status_code == 200
    data = started.get_json()
    secret = data["gizli"].replace(" ", "")
    assert re.fullmatch(r"(?:[A-Z2-7]{4} ){7}[A-Z2-7]{4}", data["gizli"]) and data["gecerlilik"] == "2026-10-26 12:10"
    assert data["baglanti"] == (f"otpauth://totp/Peyrev:yonetici@peyrev.test?secret={secret}&issuer=Peyrev"
                                "&algorithm=SHA1&digits=6&period=30")
    # Until it is confirmed the key waits sealed in this session; the account is unchanged.
    [pending] = [row for row in panel_sessions(core) if row["totp_bekleyen"]]
    assert secret not in pending["totp_bekleyen"] and pending["totp_bekleyen_bitis"] == START + timedelta(minutes=10)
    assert post(other, "iki-adim/onayla", {"kod": code_now(core, secret)}).get_json()["kod"] == "KURULUM_YOK"
    assert post(browser, "iki-adim/onayla", {"kod": wrong_code(core, secret)}).get_json()["kod"] == "KOD_HATALI"
    confirmed = post(browser, "iki-adim/onayla", {"kod": code_now(core, secret)})
    assert confirmed.get_json() == {"success": True, "iki_adim": True, "kapatilan": 1}
    assert other.get("/api/yonetim/ben").status_code == 401  # sessions opened with the password alone end
    assert browser.get("/api/yonetim/ben").get_json()["yonetici"]["iki_adim"] is True
    with core.db.transaction() as cur:
        stored = users.get(cur, manager["id"])["totp_gizli"]
    assert stored.startswith("v1:") and secret not in stored
    assert totp.unseal(core.keys, manager["id"], stored) == secret
    assert totp.unseal(core.keys, manager["id"] + 1, stored) is None  # a seal does not move to another account
    assert post(browser, "iki-adim/baslat", {"sifre": PASSWORD}).get_json()["kod"] == "IKI_ADIM_ACIK"
    assert [event["tur"] for event in events(core, "iki_adim_acildi")] == ["iki_adim_acildi"]
    assert events(core, "iki_adim_acildi")[0]["ayrinti"] == {"kapatilan_panel_oturumu": 1}
    assert secret not in str([event["ayrinti"] for event in events(core)])


def test_an_unconfirmed_setup_expires_after_ten_minutes(app, core, manager):
    browser = signed_in(app)
    secret = post(browser, "iki-adim/baslat", {"sifre": PASSWORD}).get_json()["gizli"].replace(" ", "")
    core.clock.advance(minutes=10)
    assert post(browser, "iki-adim/onayla", {"kod": code_now(core, secret)}).get_json()["kod"] == "KURULUM_YOK"
    with core.db.transaction() as cur:
        assert users.get(cur, manager["id"])["totp_gizli"] is None


def test_sign_in_asks_for_the_code_once_two_step_verification_is_on(app, client, core, manager):
    secret = enable_two_step(core, signed_in(app))
    missing = sign_in(client)
    assert (missing.status_code, missing.get_json()) == (401, {
        "success": False, "kod": "KOD_GEREKLI", "message": "İki adımlı doğrulama kodunu yazın."})
    assert "Set-Cookie" not in missing.headers and failed_attempts(core) == []
    wrong = sign_in(client, kod="12345")
    assert (wrong.status_code, wrong.get_json()["kod"]) == (401, "KOD_HATALI")
    assert failed_attempts(core) == [("yonetici@peyrev.test", "10.0.0.1")]  # a wrong code is a failed attempt
    code = code_now(core, secret)
    assert sign_in(client, kod=code[:3] + " " + code[3:]).status_code == 200  # spaces as apps show them
    # A code passes once: the same code again, or an older one, is refused.
    assert sign_in(app.test_client(), kod=code).get_json()["kod"] == "KOD_HATALI"
    assert sign_in(app.test_client(), kod=code_now(core, secret, -1)).get_json()["kod"] == "KOD_HATALI"
    assert sign_in(app.test_client(), kod=code_now(core, secret, 1)).status_code == 200  # clock drift of one step
    core.clock.advance(seconds=90)
    assert sign_in(app.test_client(), kod=code_now(core, secret, -2)).get_json()["kod"] == "KOD_HATALI"
    assert sign_in(app.test_client(), kod=code_now(core, secret)).status_code == 200
    assert events(core, "yonetim_girisi")[-1]["ayrinti"] == {"ip": "10.0.0.1", "iki_adim": True}


def test_wrong_codes_count_towards_the_sign_in_limit(app, client, core, manager):
    secret = enable_two_step(core, signed_in(app))
    for _ in range(10):
        assert sign_in(client, kod=wrong_code(core, secret)).status_code == 401
    assert sign_in(client, kod=code_now(core, secret)).get_json() == TOO_MANY


def test_turning_two_step_verification_off_needs_the_password_and_a_code(app, client, core, manager):
    browser = signed_in(app)
    secret = enable_two_step(core, browser)
    assert post(browser, "iki-adim/kapat", {"sifre": "Yanlis12345", "kod": code_now(core, secret)}
                ).get_json()["kod"] == "SIFRE_HATALI"
    assert post(browser, "iki-adim/kapat", {"sifre": PASSWORD, "kod": ""}).get_json()["kod"] == "KOD_GEREKLI"
    wrong = post(browser, "iki-adim/kapat", {"sifre": PASSWORD, "kod": "abcdef"})
    assert (wrong.status_code, wrong.get_json()["kod"]) == (403, "KOD_HATALI")
    stopped = post(browser, "iki-adim/kapat", {"sifre": PASSWORD, "kod": code_now(core, secret)})
    assert stopped.get_json() == {"success": True, "iki_adim": False}
    assert post(browser, "iki-adim/kapat", {"sifre": PASSWORD, "kod": "123456"}).get_json()["kod"] == "IKI_ADIM_KAPALI"
    assert sign_in(client).status_code == 200  # the password alone again
    assert [event["tur"] for event in events(core) if event["tur"].startswith("iki_adim")] == [
        "iki_adim_acildi", "iki_adim_kapatildi"]


def test_another_administrator_resets_a_lost_second_step(app, core, manager):
    second = make_user(core, "ikinci@peyrev.test", rol="yonetici")
    lost = signed_in(app, "ikinci@peyrev.test")
    enable_two_step(core, lost)
    browser = signed_in(app)
    assert post(browser, f"kullanicilar/{manager['id']}/iki-adim-sifirla").get_json()["message"] == (
        "Kendi iki adımlı doğrulamanızı Hesabım bölümünden kapatın.")
    reset = post(browser, f"kullanicilar/{second['id']}/iki-adim-sifirla")
    assert reset.get_json() == {"success": True, "kullanici": {"kullanici_id": second["id"], "iki_adim": False,
                                                                "kapatilan": 1}}
    assert lost.get("/api/yonetim/ben").status_code == 401
    assert sign_in(app.test_client(), "ikinci@peyrev.test").status_code == 200
    again = post(browser, f"kullanicilar/{second['id']}/iki-adim-sifirla")
    assert (again.status_code, again.get_json()["kod"]) == (409, "IKI_ADIM_KAPALI")
    [event] = events(core, "iki_adim_sifirlandi")
    assert (event["yonetici_id"], event["hedef"], event["ayrinti"]) == (
        manager["id"], f"kullanici:{second['id']}", {"kapatilan_panel_oturumu": 1})


def test_an_unreadable_secret_never_lets_anyone_in(app, client, core, manager):
    with core.db.transaction() as cur:
        cur.execute("UPDATE kullanicilar SET totp_gizli = 'v1:bozuk' WHERE id = %s", (manager["id"],))
    response = sign_in(client, kod="123456")
    assert (response.status_code, response.get_json()["kod"]) == (409, "IKI_ADIM_OKUNAMADI")
    assert panel_sessions(core) == []


def test_the_only_administrator_gets_a_new_name_and_password_from_the_server_job(app, core, manager):
    old = signed_in(app)
    with core.db.transaction() as cur:
        cur.execute("INSERT INTO sifre_kodlari (id, kullanici_id, kod_ozeti, olusturma, gecerlilik) "
                    "VALUES (%s, %s, %s, %s, %s)", ("0" * 32, manager["id"], "0" * 64, START, START + timedelta(days=1)))
    make_user(core, "kullanici@peyrev.test")
    make_user(core, "eski@peyrev.test", rol="yonetici", durum="pasif")
    result = kurtarma.recover(core, "admin@peyrev.test", "Peyrev Yönetici")
    assert (result["kullanici_id"], result["giris"]) == (manager["id"], "admin@peyrev.test")
    assert re.fullmatch(r"[A-Za-z0-9]{20}", result["sifre"]) and rules.password(result["sifre"])
    assert old.get("/api/yonetim/ben").status_code == 401
    assert sign_in(app.test_client(), "yonetici@peyrev.test").status_code == 401
    assert sign_in(app.test_client(), "admin@peyrev.test").status_code == 401
    me = signed_in(app, "admin@peyrev.test", sifre=result["sifre"]).get("/api/yonetim/ben").get_json()
    assert me["yonetici"]["ad_soyad"] == "Peyrev Yönetici"
    with core.db.transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM sifre_kodlari WHERE kullanici_id = %s AND kullanildi IS NULL",
                    (manager["id"],))
        assert cur.fetchone()["n"] == 0
    [event] = events(core, "sifre_sunucuda_yenilendi")
    assert (event["yonetici_id"], event["hedef"], event["ayrinti"]) == (
        manager["id"], f"kullanici:{manager['id']}", {"kapatilan_panel_oturumu": 1})
    assert result["sifre"] not in str([row["ayrinti"] for row in events(core)])


def test_the_server_job_refuses_when_another_administrator_can_issue_a_code(core, manager):
    make_user(core, "ikinci@peyrev.test", rol="yonetici")
    with pytest.raises(ApiError, match="2 bulundu"):
        kurtarma.recover(core, "admin@peyrev.test", "Peyrev Yönetici")
    assert events(core) == []
