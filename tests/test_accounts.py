"""Studio lisansı ve oturumları: /api/rpa/lisans/* ve oturum kuralları (orkestrai .testler'in PostgreSQL karşılığı).

Lisans zarfı Studio 1.0.0'ın kurallarıyla doğrulanır (studio_license, studio_agents): tam iki anahtar, sıkı base64,
/anahtar ucundaki açık anahtarla Ed25519, ürün ve modül, cihaz ve isteğin rastgele değeri. Diğer hesap testleri
buradaki yardımcıları kullanır.
"""

import base64
import json
import re
import secrets
import threading
from datetime import UTC, date, datetime, timedelta

import jwt
import pytest
from conftest import DEVICE, START, make_user
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from werkzeug.security import generate_password_hash

from peyrev import tokens, users
from peyrev.accounts import licensing, rules, sessions
from peyrev.agents import entitlement
from peyrev.common import ApiError
from peyrev.db import advisory_lock

PASSWORD = "Gizli12345"
OTHER, THIRD = "b" * 32, "c" * 32
APP1, APP2 = "1" * 32, "2" * 32  # a running Studio's own random identity ("ornek")
SESSION_EXPIRED = "Oturumun süresi doldu. Yeniden giriş yapın."
TOO_MANY = "Çok fazla hatalı deneme. 15 dakika sonra tekrar deneyin."


def login(client, kullanici="ali@ornek.test", sifre=PASSWORD, cihaz=DEVICE, surum="1.0.0", address="10.0.0.1", **extra):
    body = {"kullanici": kullanici, "sifre": sifre, "cihaz": cihaz, "surum": surum, "protokol": 2,
            "nonce": secrets.token_hex(16), **extra}
    # The proxy appends the real address to X-Forwarded-For; only the last item counts.
    return client.post("/api/rpa/lisans/giris", json=body, headers={"X-Forwarded-For": "192.0.2.1, " + address})


def renew(client, token, cihaz=DEVICE, surum="1.0.0", **extra):
    body = {"token": token, "cihaz": cihaz, "surum": surum, "protokol": 2, "nonce": secrets.token_hex(16), **extra}
    return client.post("/api/rpa/lisans/yenile", json=body)


def token_claims(core, response):
    return jwt.decode(response.get_json()["yenileme"], core.keys.derive(licensing.REFRESH_AUDIENCE),
                      algorithms=["HS256"], audience=licensing.REFRESH_AUDIENCE, options={"verify_exp": False})


def sessions_of(core, user):
    with core.db.transaction() as cur:
        cur.execute("SELECT * FROM oturumlar WHERE kullanici_id = %s ORDER BY olusturma, cihaz", (user["id"],))
        return cur.fetchall()


def studio_license(client, response, device=DEVICE, nonce=None):
    """Studio 1.0.0 licensing.verify_license ve _request ile aynı denetimler; imzalı iddiaları döndürür."""
    body = response.get_json()
    assert response.status_code == 200 and body["success"] is True, body
    assert re.fullmatch(r"[A-Za-z0-9_\-.]{20,4096}", body["yenileme"])
    for name in ("ajan_erisim", "destek_erisim"):
        assert name not in body or re.fullmatch(r"[A-Za-z0-9_.-]{20,8192}", body[name])
    envelope = body["lisans"]
    assert isinstance(envelope, dict) and set(envelope) == {"payload", "signature"}
    assert len(envelope["payload"]) <= 64 * 1024
    payload = base64.b64decode(envelope["payload"], validate=True)
    signature = base64.b64decode(envelope["signature"], validate=True)
    assert len(signature) == 64
    key = client.get("/api/rpa/lisans/anahtar").get_json()["anahtar"]
    Ed25519PublicKey.from_public_bytes(base64.b64decode(key, validate=True)).verify(signature, payload)
    claims = json.loads(payload.decode("utf-8"))
    assert (claims["surum"], claims["urun"], claims["modul"]) == (1, "rpa-orkestrai", "MOD_RPA")
    assert isinstance(claims["kullanici"], str) and type(claims["kullanici_id"]) is int and type(claims["firma_id"]) is int
    for name in ("verildi", "gecerlilik"):
        assert isinstance(claims[name], str) and len(claims[name]) <= 40
        assert datetime.fromisoformat(claims[name]).tzinfo is not None
    assert type(claims["tolerans"]) is int and type(claims["aralik"]) is int
    assert claims["bitis"] is None or date.fromisoformat(claims["bitis"])
    assert claims["hesap_turu"] in ("bireysel", "kurumsal")
    assert claims["cihaz"] == device
    assert nonce is None or claims["nonce"] == nonce
    return claims


def studio_agents(claims):
    """Studio'nun ajan uzantısı denetimi (_agent_claims); geçerli ve aktif değilse {} (ajanlar kapalı, lisans sürer)."""
    value = claims.get("ajan")
    if not isinstance(value, dict) or value.get("surum") != 1 or value.get("aktif") is not True:
        return {}
    assert all(type(value[name]) is int and value[name] >= 1 for name in ("kullanici_id", "firma_id", "onayci_id"))
    assert (value["kullanici_id"], value["firma_id"]) == (claims["kullanici_id"], claims["firma_id"])
    assert isinstance(value["oturum_id"], str) and 1 <= len(value["oturum_id"]) <= 200
    permissions = value["yetkiler"]
    assert isinstance(permissions, list) and len(permissions) <= 10
    assert set(permissions) <= {"agents.use", "agents.manage", "agents.approve"}
    if value["kullanici_id"] != value["onayci_id"]:
        assert not {"agents.manage", "agents.approve"} & set(permissions)
    assert value.get("bitis") is None or date.fromisoformat(value["bitis"])
    return value


# ----- uçlar ---------------------------------------------------------------------------------------------------------

def test_key_endpoint_names_the_public_key_the_protocol_and_the_minimum_version(client, core):
    answer = client.get("/api/rpa/lisans/anahtar")
    assert answer.status_code == 200 and answer.headers["Cache-Control"] == "no-store"
    assert answer.get_json() == {"success": True, "anahtar": core.keys.license_public_key, "protokol": 2,
                                 "asgari_surum": "1.0.0"}
    assert len(base64.b64decode(core.keys.license_public_key, validate=True)) == 32


def test_sign_in_gives_a_fresh_signed_license_that_studio_accepts(client, core):
    user = make_user(core, "ali@ornek.test", modules={users.RPA: date(2027, 9, 27)}, alan_id=2, ad_soyad="Ali Veli")
    assert login(client, sifre="Yanlis12345").get_json()["kod"] == "HATALI_GIRIS"
    nonce = "ab" * 16
    response = login(client, kullanici="ALI", nonce=nonce, ornek=APP1)
    claims = studio_license(client, response, nonce=nonce)
    # The registered login name is signed, not what was typed; the workspace id stands in for the firm.
    assert (claims["kullanici_id"], claims["kullanici"], claims["ad_soyad"]) == (user["id"], "ali@ornek.test", "Ali Veli")
    assert (claims["firma_id"], claims["firma_adi"], claims["hesap_turu"]) == (2, "", "bireysel")
    assert (claims["bitis"], claims["tolerans"], claims["aralik"]) == ("2027-09-27", 3600, 600)
    assert claims["verildi"] == "2026-10-26T12:00:00+03:00"
    issued, valid = (datetime.fromisoformat(claims[name]) for name in ("verildi", "gecerlilik"))
    assert valid - issued == timedelta(hours=1)
    session = token_claims(core, response)
    assert session["sira"] == 0 and re.fullmatch(r"[0-9a-f]{32}", session["oturum"]) and session["cihaz"] == DEVICE
    assert session["exp"] == int((START + timedelta(days=30)).timestamp())
    row = sessions_of(core, user)[0]
    assert (row["id"], row["durum"], row["cihaz"], row["surum"], row["ip"], row["ornek"]) == (
        session["oturum"], "aktif", DEVICE, "1.0.0", "10.0.0.1", APP1)
    # The signature covers exactly the payload bytes.
    envelope = response.get_json()["lisans"]
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(core.keys.license_public_key))
    with pytest.raises(InvalidSignature):
        key.verify(base64.b64decode(envelope["signature"]), base64.b64decode(envelope["payload"]) + b" ")
    # The support token belongs to this session and works at once.
    with core.db.transaction() as cur:
        found, support = tokens.verify(core, cur, "destek", "Bearer " + response.get_json()["destek_erisim"])
    assert found["id"] == user["id"] and support["oturum"] == session["oturum"]


def test_old_studio_versions_protocols_and_missing_random_values_are_refused(client, core):
    make_user(core, "ali@ornek.test", modules={users.RPA: None})
    old = {"kullanici": "ali", "sifre": PASSWORD, "cihaz": DEVICE, "surum": "0.9.53"}  # no protocol, no nonce
    for response in (client.post("/api/rpa/lisans/giris", json=old), login(client, surum="0.9.53"),
                     login(client, surum=None), login(client, surum="1.0"), login(client, protokol=1),
                     login(client, protokol="2"), login(client, protokol=True), login(client, nonce=None),
                     login(client, nonce="AB" * 16), login(client, nonce="ab" * 16 + "\n"), login(client, nonce="a" * 31)):
        body = response.get_json()
        assert response.status_code == 403 and (body["kod"], body["neden"]) == ("YETKI_YOK", "ESKI_SURUM")
        assert body["message"] == ("Bu Peyrev sürümü artık desteklenmiyor. Güncel sürümü https://peyrev.com/indir/ "
                                   "adresinden kurun.")
    with core.db.transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM oturumlar")
        assert cur.fetchone()["n"] == 0  # refused requests open no session
    # The password is checked first: an old Studio with a wrong password hears HATALI_GIRIS.
    assert login(client, sifre="Yanlis12345", surum="0.9.53").get_json()["kod"] == "HATALI_GIRIS"
    # The version refusal comes before the license refusal.
    make_user(core, "lisanssiz@ornek.test")
    assert login(client, kullanici="lisanssiz@ornek.test", surum="0.9.53").get_json()["neden"] == "ESKI_SURUM"
    assert login(client, kullanici="lisanssiz@ornek.test").get_json()["kod"] == "YETKI_YOK"
    assert login(client, surum="1.12.3-beta").status_code == 200 and login(client, surum="2.0.0").status_code == 200


def test_sign_in_error_catalog(client, core):
    make_user(core, "ali@ornek.test", modules={users.RPA: None})
    make_user(core, "pasif@ornek.test", durum="pasif", modules={users.RPA: None})
    base = {"cihaz": DEVICE, "surum": "1.0.0", "protokol": 2, "nonce": "ab" * 16}
    missing, invalid = "Kullanıcı adı ve şifre gereklidir.", "Geçersiz giriş isteği."
    cases = [({"kullanici": " ", "sifre": PASSWORD}, missing), ({"sifre": PASSWORD}, missing),
             ({"kullanici": "ali"}, missing), ({"kullanici": "ali", "sifre": ""}, missing),
             ({"kullanici": "ali", "sifre": 12345678}, missing),
             ({"kullanici": "a" * 151, "sifre": PASSWORD}, invalid), ({"kullanici": "ali", "sifre": "x" * 257}, invalid),
             ({"kullanici": "ali", "sifre": PASSWORD, "cihaz": "kisa"}, invalid),
             ({"kullanici": "ali", "sifre": PASSWORD, "cihaz": "A" * 32}, invalid),
             ({"kullanici": "ali", "sifre": PASSWORD, "cihaz": DEVICE + "\n"}, invalid)]
    for changes, message in cases:
        response = client.post("/api/rpa/lisans/giris", json={**base, **changes})
        assert response.status_code == 400 and response.get_json() == {"success": False, "kod": "EKSIK", "message": message}
    # A body that is not a JSON object counts as empty, unknown fields are ignored (a newer Studio may add some).
    for raw in ("bozuk", "[1, 2]", '{"kullanici": "ali", "kullanici": "ali"}'):
        response = client.post("/api/rpa/lisans/giris", data=raw, content_type="application/json")
        assert response.status_code == 400 and response.get_json()["message"] == missing
    assert login(client, yeni_alan={"ne": "olursa"}).status_code == 200
    too_big = client.post("/api/rpa/lisans/giris", data="x" * 9000, content_type="application/json")
    assert too_big.status_code == 413 and too_big.get_json()["kod"] == "GOVDE_SINIRI"
    for name in ("yok@ornek.test", "pasif@ornek.test"):
        wrong = login(client, kullanici=name)
        assert wrong.status_code == 401 and wrong.get_json() == {
            "success": False, "kod": "HATALI_GIRIS", "message": "Kullanıcı adı veya şifre hatalı."}


def test_failed_sign_ins_are_limited_per_login_name_and_per_address(client, core):
    make_user(core, "ali@ornek.test", modules={users.RPA: None})
    for index in range(10):
        response = login(client, kullanici="ALI" if index % 2 else "ali", sifre="Yanlis12345", address=f"10.0.1.{index}")
        assert response.status_code == 401
    blocked = login(client, kullanici="Ali")  # the right password from a fresh address: the name is at its limit
    assert blocked.status_code == 429 and blocked.get_json() == {"success": False, "kod": "COK_DENEME", "message": TOO_MANY}
    # The failures were written with the account's own name: the whole address gives no new attempts either.
    assert login(client, kullanici="ali@ornek.test").status_code == 429
    with core.db.transaction() as cur:
        cur.execute("SELECT DISTINCT giris FROM giris_denemeleri")
        assert [row["giris"] for row in cur.fetchall()] == ["ali@ornek.test"]
    core.clock.advance(minutes=15, seconds=1)
    assert login(client, kullanici="ali").status_code == 200
    for index in range(30):
        login(client, kullanici=f"yok{index}", sifre="Yanlis12345", address="10.9.9.9")
    assert login(client, address="10.9.9.9").status_code == 429
    assert login(client, address="10.9.9.8").status_code == 200
    # A successful sign-in does not reset the counters; attempts older than a day go at the next failure.
    core.clock.advance(days=1, minutes=1)
    login(client, kullanici="yeni", sifre="Yanlis12345")
    with core.db.transaction() as cur:
        cur.execute("SELECT giris FROM giris_denemeleri")
        assert [row["giris"] for row in cur.fetchall()] == ["yeni"]


def test_ambiguous_names_and_waiting_applications_are_not_counted_as_failures(client, core):
    make_user(core, "ali@a.test", modules={users.RPA: None})
    make_user(core, "ali@b.test", modules={users.RPA: None})
    ambiguous = login(client, kullanici="ali")
    assert ambiguous.status_code == 409 and ambiguous.get_json() == {
        "success": False, "kod": "BELIRSIZ",
        "message": "Bu kullanıcı adı birden fazla hesapla eşleşiyor. E-posta adresinizin tamamını yazın."}
    assert studio_license(client, login(client, kullanici="ALI@B.TEST"))["kullanici"] == "ali@b.test"
    # A different password tells two accounts with the same user name apart.
    make_user(core, "veli@a.test", password="Baska12345", modules={users.RPA: None})
    make_user(core, "veli@b.test", modules={users.RPA: None})
    assert studio_license(client, login(client, kullanici="veli"))["kullanici"] == "veli@b.test"
    application = {"ad_soyad": "Ayşe Yılmaz", "eposta": "ayse@ornek.test", "sifre": "Guclu2026", "paket": "rpa",
                   "onay": True}
    assert client.post("/api/rpa/basvuru", json=application).status_code == 201
    for name in ("ayse@ornek.test", "AYSE"):
        waiting = login(client, kullanici=name, sifre="Guclu2026")
        assert waiting.status_code == 403 and waiting.get_json() == {
            "success": False, "kod": "BASVURU_BEKLIYOR",
            "message": "Başvurunuz alındı; ödemeniz onaylandığında hesabınız açılır ve bu e-posta ile şifrenizle "
                       "giriş yapabilirsiniz."}
    assert login(client, kullanici="ayse", sifre="Yanlis2026").get_json()["kod"] == "HATALI_GIRIS"
    with core.db.transaction() as cur:
        cur.execute("SELECT giris FROM giris_denemeleri")
        assert [row["giris"] for row in cur.fetchall()] == ["ayse"]


def test_license_follows_the_module_row_and_the_last_day_in_turkish_time(client, core):
    user = make_user(core, "ali@ornek.test", modules={users.RPA: date(2026, 10, 26)})
    first = login(client)
    claims = studio_license(client, first)
    assert (claims["bitis"], claims["gecerlilik"]) == ("2026-10-26", "2026-10-26T13:00:00+03:00")
    core.clock.at = datetime(2026, 10, 26, 20, 30, tzinfo=UTC)  # 23:30 in Türkiye
    late = renew(client, first.get_json()["yenileme"])
    assert studio_license(client, late)["gecerlilik"] == "2026-10-26T23:59:59+03:00"  # never past the last day
    core.clock.at = datetime(2026, 10, 26, 20, 59, 59, tzinfo=UTC)  # 23:59:59: still the last day
    token = renew(client, late.get_json()["yenileme"]).get_json()["yenileme"]
    core.clock.at = datetime(2026, 10, 26, 21, 0, tzinfo=UTC)  # 00:00 on 27 October in Türkiye
    expired = renew(client, token)
    assert expired.status_code == 403 and expired.get_json() == {
        "success": False, "kod": "SURE_DOLDU", "message": "Peyrev kullanım süreniz 26.10.2026 tarihinde dolmuştur.",
        "bitis": "2026-10-26"}
    assert login(client).get_json()["kod"] == "SURE_DOLDU"
    with core.db.transaction() as cur:
        cur.execute("UPDATE kullanici_modulleri SET bitis = NULL WHERE kullanici_id = %s", (user["id"],))
    assert studio_license(client, renew(client, token))["bitis"] is None  # open-ended
    with core.db.transaction() as cur:
        cur.execute("DELETE FROM kullanici_modulleri WHERE kullanici_id = %s", (user["id"],))
    refused = login(client)
    assert refused.status_code == 403 and refused.get_json() == {
        "success": False, "kod": "YETKI_YOK", "message": "Bu kullanıcı için Peyrev lisansı tanımlı değil."}
    # MOD_AJAN alone is no Studio license.
    make_user(core, "veli@ornek.test", modules={users.AGENTS: None})
    assert login(client, kullanici="veli").get_json()["kod"] == "YETKI_YOK"


def test_administrator_role_runs_studio_without_a_module_row(client, core):
    make_user(core, "yonetici@peyrev.test", rol="yonetici")
    claims = studio_license(client, login(client, kullanici="yonetici"))
    assert claims["bitis"] is None
    assert datetime.fromisoformat(claims["gecerlilik"]) - datetime.fromisoformat(claims["verildi"]) == timedelta(hours=1)
    assert studio_agents(claims) == {}  # agents always need their module rows


def test_every_renewal_moves_the_number_and_a_lost_answer_can_be_asked_again(client, core):
    user = make_user(core, "ali@ornek.test", modules={users.RPA: None})
    first = login(client, nonce="01" * 16)
    second = renew(client, first.get_json()["yenileme"], nonce="02" * 16)
    studio_license(client, second, nonce="02" * 16)
    assert token_claims(core, second)["sira"] == 1
    assert token_claims(core, second)["oturum"] == token_claims(core, first)["oturum"]
    # The answer was lost: the earlier token is accepted again and the current number comes back.
    again = renew(client, first.get_json()["yenileme"])
    assert again.status_code == 200 and token_claims(core, again)["sira"] == 1
    assert sessions_of(core, user)[0]["tekrar"] == 1  # a request without "ornek" counts
    third = renew(client, second.get_json()["yenileme"], ornek="0f" * 16)
    assert token_claims(core, third)["sira"] == 2 and sessions_of(core, user)[0]["ornek"] == "0f" * 16
    for _ in range(4):  # the Studio that moved the number on repeats its lost request: not counted
        assert token_claims(core, renew(client, second.get_json()["yenileme"], ornek="0f" * 16))["sira"] == 2
    assert sessions_of(core, user)[0]["tekrar"] == 1
    assert token_claims(core, renew(client, second.get_json()["yenileme"], ornek="0e" * 16))["sira"] == 2
    assert (sessions_of(core, user)[0]["tekrar"], sessions_of(core, user)[0]["durum"]) == (2, "aktif")
    copy = renew(client, second.get_json()["yenileme"], ornek="0d" * 16)
    assert copy.status_code == 401 and copy.get_json() == {
        "success": False, "kod": "OTURUM_GECERSIZ", "neden": "KOPYA",
        "message": "Bu oturum aynı anda birden fazla yerde kullanıldığı için kapatıldı. Yeniden giriş yapın."}
    # The closing stays although the request was refused.
    assert (sessions_of(core, user)[0]["durum"], sessions_of(core, user)[0]["kapanma_nedeni"]) == ("kapandi", "KOPYA")
    # Sign-in writes the Studio's identity; an invalid one is left out.
    core.clock.advance(seconds=1)
    login(client, ornek="0a" * 16)
    assert sessions_of(core, user)[-1]["ornek"] == "0a" * 16
    core.clock.advance(seconds=1)
    assert login(client, ornek="<bozuk>").status_code == 200 and sessions_of(core, user)[-1]["ornek"] is None
    # Another device or a broken token cannot renew.
    token = login(client).get_json()["yenileme"]
    assert renew(client, token, cihaz=OTHER).get_json() == {"success": False, "kod": "OTURUM_GECERSIZ",
                                                           "message": SESSION_EXPIRED}
    for broken in ("bozuk", "", None, 42, token + "x"):
        assert renew(client, broken).get_json()["kod"] == "OTURUM_GECERSIZ"
    forged = jwt.encode({**token_claims(core, renew(client, token)), "sira": 99}, "baska-anahtar" * 4, algorithm="HS256")
    assert renew(client, forged).status_code == 401


def test_signing_in_on_another_computer_closes_the_earlier_session(client, core):
    ali = make_user(core, "ali@ornek.test", modules={users.RPA: None})
    make_user(core, "veli@ornek.test", modules={users.RPA: None})
    first = login(client, cihaz=DEVICE).get_json()
    core.clock.advance(minutes=1)
    second = login(client, cihaz=OTHER).get_json()["yenileme"]
    closed = renew(client, first["yenileme"], cihaz=DEVICE)
    assert closed.status_code == 401 and closed.get_json() == {
        "success": False, "kod": "OTURUM_GECERSIZ", "neden": "BASKA_CIHAZ",
        "message": "Bu hesap başka bir bilgisayarda açıldığı için bu bilgisayardaki oturum kapatıldı. "
                   "Devam etmek için yeniden giriş yapın."}
    # The closed session's support token stops working at once.
    with core.db.transaction() as cur, pytest.raises(ApiError) as error:
        tokens.verify(core, cur, "destek", "Bearer " + first["destek_erisim"])
    assert (error.value.status, error.value.kod) == (401, "OTURUM_GECERSIZ")
    assert renew(client, second, cihaz=OTHER).status_code == 200
    assert [(row["cihaz"], row["durum"], row["kapanma_nedeni"]) for row in sessions_of(core, ali)] == [
        (DEVICE, "kapandi", "BASKA_CIHAZ"), (OTHER, "aktif", None)]
    # Signing in again on the same computer closes that computer's earlier session.
    core.clock.advance(minutes=1)
    third = login(client, cihaz=OTHER).get_json()["yenileme"]
    again = renew(client, second, cihaz=OTHER).get_json()
    assert (again["kod"], again["neden"], again["message"]) == ("OTURUM_GECERSIZ", "YENI_GIRIS", SESSION_EXPIRED)
    assert renew(client, third, cihaz=OTHER).status_code == 200
    # Another account's sign-in on the same computer leaves this account alone.
    login(client, kullanici="veli", cihaz=OTHER)
    assert [row["durum"] for row in sessions_of(core, ali)] == ["kapandi", "kapandi", "aktif"]


def test_concurrent_sign_ins_of_one_account_leave_a_single_session(app, core):
    user = make_user(core, "ali@ornek.test", modules={users.RPA: None})
    results = []
    with core.db.transaction() as cur:
        # A sign-in of the same account is in progress elsewhere and holds the account's sign-in lock.
        advisory_lock(cur, f"peyrev-oturum-{user['id']}")
        sessions.start(cur, user, OTHER, "1.0.0", "", core.clock.now())
        worker = threading.Thread(target=lambda: results.append(login(app.test_client()).status_code))
        worker.start()
        worker.join(timeout=0.5)
        assert worker.is_alive()  # waits instead of opening a second session beside the uncommitted one
    worker.join(timeout=10)
    assert results == [200]
    assert {row["cihaz"]: (row["durum"], row["kapanma_nedeni"]) for row in sessions_of(core, user)} == {
        OTHER: ("kapandi", "BASKA_CIHAZ"), DEVICE: ("aktif", None)}


def test_housekeeping_never_blocks_a_sign_in(client, core):
    user = make_user(core, "ali@ornek.test", modules={users.RPA: None})
    with core.db.transaction() as cur:
        old, _ = sessions.start(cur, user, OTHER, "1.0.0", "", START - timedelta(days=200))
        sessions.close(cur, old, "CIKIS", START - timedelta(days=200))
        # Another module's table that still refers to the old session (without ON DELETE CASCADE / SET NULL).
        cur.execute("CREATE TABLE deneme_oturum_bagi (oturum_id TEXT REFERENCES oturumlar (id))")
        cur.execute("INSERT INTO deneme_oturum_bagi VALUES (%s)", (old,))
    try:
        assert studio_license(client, login(client))
        assert old in {row["id"] for row in sessions_of(core, user)}
    finally:
        with core.db.transaction() as cur:
            cur.execute("DROP TABLE deneme_oturum_bagi")
    login(client)
    assert old not in {row["id"] for row in sessions_of(core, user)}


def test_a_removed_license_keeps_the_session_and_a_restored_one_resumes_it(client, core):
    user = make_user(core, "ali@ornek.test", modules={users.RPA: None})
    token = login(client).get_json()["yenileme"]
    with core.db.transaction() as cur:
        cur.execute("DELETE FROM kullanici_modulleri WHERE kullanici_id = %s", (user["id"],))
    refused = renew(client, token)
    assert refused.status_code == 403 and refused.get_json()["kod"] == "YETKI_YOK"
    assert renew(client, token, surum="0.9.53").get_json()["neden"] == "ESKI_SURUM"
    assert [(row["durum"], row["sira"]) for row in sessions_of(core, user)] == [("aktif", 0)]
    with core.db.transaction() as cur:
        cur.execute("INSERT INTO kullanici_modulleri (kullanici_id, modul, bitis) VALUES (%s, 'MOD_RPA', %s)",
                    (user["id"], date(2020, 1, 31)))
    expired = renew(client, token)
    assert (expired.status_code, expired.get_json()["kod"], expired.get_json()["bitis"]) == (403, "SURE_DOLDU", "2020-01-31")
    with core.db.transaction() as cur:
        cur.execute("UPDATE kullanici_modulleri SET bitis = NULL WHERE kullanici_id = %s", (user["id"],))
    assert token_claims(core, renew(client, token))["sira"] == 1


def test_a_new_password_a_passive_account_and_signing_out_end_the_session(client, core):
    ali = make_user(core, "ali@ornek.test", modules={users.RPA: None})
    veli = make_user(core, "veli@ornek.test", modules={users.RPA: None})
    token = login(client).get_json()["yenileme"]
    with core.db.transaction() as cur:
        cur.execute("UPDATE kullanicilar SET sifre_hash = %s WHERE id = %s", (generate_password_hash("Yeni12345"), ali["id"]))
    assert renew(client, token).get_json() == {"success": False, "kod": "OTURUM_GECERSIZ", "message": SESSION_EXPIRED}
    token = login(client, sifre="Yeni12345").get_json()["yenileme"]
    with core.db.transaction() as cur:
        cur.execute("UPDATE kullanicilar SET durum = 'pasif' WHERE id = %s", (ali["id"],))
    assert renew(client, token).get_json() == {"success": False, "kod": "OTURUM_GECERSIZ", "message": SESSION_EXPIRED}
    assert login(client, sifre="Yeni12345").get_json()["kod"] == "HATALI_GIRIS"
    # Signing out closes only this computer's session.
    token = login(client, kullanici="veli").get_json()["yenileme"]
    assert client.post("/api/rpa/lisans/cikis", json={"token": token, "cihaz": OTHER}).get_json() == {"success": True}
    assert sessions_of(core, veli)[0]["durum"] == "aktif"
    assert client.post("/api/rpa/lisans/cikis", json={"token": token, "cihaz": DEVICE}).status_code == 200
    assert (sessions_of(core, veli)[0]["durum"], sessions_of(core, veli)[0]["kapanma_nedeni"]) == ("kapandi", "CIKIS")
    closed = renew(client, token).get_json()
    assert (closed["kod"], closed["neden"], closed["message"]) == ("OTURUM_GECERSIZ", "CIKIS", SESSION_EXPIRED)
    for body in ({"token": "bozuk"}, {}, [], {"token": token}):  # always a success, even with nothing to close
        assert client.post("/api/rpa/lisans/cikis", json=body).get_json() == {"success": True}


def test_the_refresh_token_lives_thirty_days_from_its_last_answer(client, core):
    user = make_user(core, "ali@ornek.test", modules={users.RPA: None})
    token = login(client).get_json()["yenileme"]
    core.clock.advance(days=30, seconds=1)
    assert renew(client, token).get_json() == {"success": False, "kod": "OTURUM_GECERSIZ", "message": SESSION_EXPIRED}
    assert client.post("/api/rpa/lisans/cikis", json={"token": token, "cihaz": DEVICE}).get_json() == {"success": True}
    assert sessions_of(core, user)[0]["durum"] == "aktif"  # an expired token closes nothing
    # A running Studio renews every few minutes, so its session never runs out.
    token = login(client).get_json()["yenileme"]
    for _ in range(3):
        core.clock.advance(days=29)
        token = renew(client, token).get_json()["yenileme"]


def test_an_answer_that_cannot_be_signed_rolls_the_session_back(client, core, monkeypatch):
    user = make_user(core, "ali@ornek.test", modules={users.RPA: None})
    token = login(client).get_json()["yenileme"]

    def broken(_body):
        raise RuntimeError("GIZLI-ANAHTAR")

    monkeypatch.setattr(core.keys, "sign", broken)
    failed = renew(client, token)
    assert failed.status_code == 503 and failed.get_json()["kod"] == "SUNUCU"
    assert "GIZLI" not in failed.get_data(as_text=True)
    assert login(client).status_code == 503
    assert [(row["durum"], row["sira"]) for row in sessions_of(core, user)] == [("aktif", 0)]  # nothing half done
    monkeypatch.undo()
    assert token_claims(core, renew(client, token))["sira"] == 1


def test_agent_access_comes_with_both_modules_and_support_access_with_every_answer(client, core):
    ali = make_user(core, "ali@ornek.test", modules={users.RPA: None, users.AGENTS: date(2027, 1, 31)}, alan_id=5)
    response = login(client)
    claims = studio_license(client, response)
    agents = studio_agents(claims)
    assert agents["oturum_id"] == token_claims(core, response)["oturum"] and "agents.use" in agents["yetkiler"]
    with core.db.transaction() as cur:
        found, token = tokens.verify(core, cur, "ajan", "Bearer " + response.get_json()["ajan_erisim"])
    assert found["id"] == ali["id"] and token["firma_id"] == 5
    make_user(core, "veli@ornek.test", modules={users.RPA: None})
    only_rpa = login(client, kullanici="veli")
    assert studio_agents(studio_license(client, only_rpa)) == {}
    assert "ajan_erisim" not in only_rpa.get_json() and "destek_erisim" in only_rpa.get_json()


def test_a_failing_agent_extension_never_costs_the_license(client, core, monkeypatch):
    make_user(core, "ali@ornek.test", modules={users.RPA: None, users.AGENTS: None}, alan_id=3)

    def broken(_core, cur, _user, _session_id, **_options):
        cur.execute("SELECT * FROM yok_boyle_bir_tablo")  # would abort the sign-in without a savepoint

    monkeypatch.setattr(entitlement, "license_extension", broken)
    response = login(client)
    claims = studio_license(client, response)
    assert claims["ajan"] == {"surum": 1, "aktif": False, "firma_id": 3, "kullanici_id": claims["kullanici_id"],
                              "onayci_id": None, "oturum_id": token_claims(core, response)["oturum"], "bitis": None,
                              "yetkiler": []}
    assert "ajan_erisim" not in response.get_json() and "destek_erisim" in response.get_json()
    assert token_claims(core, renew(client, response.get_json()["yenileme"]))["sira"] == 1


def test_a_token_that_cannot_be_issued_never_costs_the_license(client, core, monkeypatch):
    make_user(core, "ali@ornek.test", modules={users.RPA: None, users.AGENTS: None})

    def broken(*_args, **_options):
        raise RuntimeError("anahtar yok")

    monkeypatch.setattr(tokens, "issue", broken)
    response = login(client)
    studio_license(client, response)
    assert "ajan_erisim" not in response.get_json() and "destek_erisim" not in response.get_json()


def test_a_failed_sign_in_checks_one_hash_of_each_kind_whatever_the_name(client, core, monkeypatch):
    """Bilinmeyen ad, yanlış şifre (yeni ya da taşınmış özet) ve bekleyen başvuru aynı işi yapar: yanıt süresi hesabın
    varlığını ya da özetinin biçimini belli etmez."""
    make_user(core, "yeni@ornek.test", modules={users.RPA: None})
    old = make_user(core, "eski@ornek.test", modules={users.RPA: None})
    with core.db.transaction() as cur:
        cur.execute("UPDATE kullanicilar SET sifre_hash = %s WHERE id = %s",
                    (generate_password_hash(PASSWORD, method="pbkdf2:sha256:260000"), old["id"]))
    application = {"ad_soyad": "Ayşe Yılmaz", "eposta": "ayse@ornek.test", "sifre": "Guclu2026", "paket": "rpa",
                   "onay": True}
    assert client.post("/api/rpa/basvuru", json=application).status_code == 201
    checked = []
    original = rules.matches

    def spy(password_hash, candidate):
        checked.append((password_hash or "").split("$", 1)[0])
        return original(password_hash, candidate)

    monkeypatch.setattr(rules, "matches", spy)
    for name in ("yok@ornek.test", "yok", "yeni", "eski@ornek.test", "ayse"):
        checked.clear()
        assert login(client, kullanici=name, sifre="Yanlis12345").get_json()["kod"] == "HATALI_GIRIS", name
        assert sorted(checked) == sorted(rules.HASH_METHODS), name
    # The right password needs nothing more.
    checked.clear()
    assert login(client, kullanici="yeni").status_code == 200 and checked == ["scrypt:32768:8:1"]


def test_a_migrated_werkzeug_hash_signs_in_and_stays_as_it_is(client, core):
    legacy = generate_password_hash("Eski12345", method="pbkdf2:sha256:260000", salt_length=16)
    assert re.fullmatch(r"pbkdf2:sha256:260000\$[A-Za-z0-9]{16}\$[0-9a-f]{64}", legacy)  # orkestrai's Werkzeug 2.0
    make_user(core, "BASBUG_1", modules={users.RPA: None}, alan_id=2, user_id=63)
    with core.db.transaction() as cur:
        cur.execute("UPDATE kullanicilar SET sifre_hash = %s WHERE id = 63", (legacy,))
    claims = studio_license(client, login(client, kullanici="basbug_1", sifre="Eski12345"))
    assert (claims["kullanici_id"], claims["kullanici"], claims["firma_id"]) == (63, "BASBUG_1", 2)
    with core.db.transaction() as cur:
        assert users.get(cur, 63)["sifre_hash"] == legacy
        cur.execute("UPDATE kullanicilar SET sifre_hash = 'md5$tuz$ozet' WHERE id = 63")
    # An unknown hash format never matches and never fails the request.
    assert login(client, kullanici="basbug_1", sifre="Eski12345").get_json()["kod"] == "HATALI_GIRIS"


def test_the_module_serves_exactly_the_studio_and_application_endpoints(app):
    served = {(rule.rule, method) for rule in app.url_map.iter_rules() if rule.endpoint.startswith("accounts.")
              for method in rule.methods - {"HEAD", "OPTIONS"}}
    assert served == {("/api/rpa/lisans/anahtar", "GET"), ("/api/rpa/lisans/giris", "POST"),
                      ("/api/rpa/lisans/yenile", "POST"), ("/api/rpa/lisans/cikis", "POST"),
                      ("/api/rpa/hesap/sifre", "POST"), ("/api/rpa/hesap/sifre-yenile", "POST"),
                      ("/api/rpa/basvuru", "POST")}
    assert app.test_client().get("/api/rpa/lisans/giris").get_json()["kod"] == "YONTEM"


def test_version_numbers_and_random_values():
    assert licensing.version_tuple("1.0.0") == (1, 0, 0) and licensing.version_tuple("1.12.3-beta") == (1, 12, 3)
    assert licensing.version_tuple("0.9.53") < licensing.MINIMUM_VERSION <= licensing.version_tuple("1.0.1")
    for broken in (None, "", "abc", "1.2", {"a": 1}, "v1.0.0"):
        assert licensing.version_tuple(broken) == (0, 0, 0)
    assert licensing.valid_nonce("0123456789abcdef" * 2) == "0123456789abcdef" * 2
    assert licensing.valid_nonce("f" * 64) == "f" * 64
    for broken in (None, 5, "kisa", "G" * 32, "a" * 65, "a" * 31, "a" * 32 + "\n"):
        assert licensing.valid_nonce(broken) is None


# ----- oturum kuralları (sessions) -----------------------------------------------------------------------------------

def run(core, function, *args, **options):
    with core.db.transaction() as cur:
        return function(cur, *args, **options)


def test_a_second_computer_closes_the_first_session(core):
    ali, veli = make_user(core, "ali@x.test"), make_user(core, "veli@x.test")
    first, number = run(core, sessions.start, ali, DEVICE, "1.0.0", "1.1.1.1", START)
    assert number == 0 and re.fullmatch(r"[0-9a-f]{32}", first)
    second, _ = run(core, sessions.start, ali, OTHER, "1.0.0", "2.2.2.2", START + timedelta(minutes=5))
    assert {row["id"]: (row["durum"], row["kapanma_nedeni"]) for row in sessions_of(core, ali)} == {
        first: ("kapandi", "BASKA_CIHAZ"), second: ("aktif", None)}
    later = START + timedelta(minutes=10)
    assert run(core, sessions.renew, first, 0, ali["id"], DEVICE, "1.0.0", "", later) == ("kapali", "BASKA_CIHAZ")
    assert run(core, sessions.renew, second, 0, ali["id"], OTHER, "1.0.0", "", later) == ("tamam", 1)
    theirs, _ = run(core, sessions.start, veli, THIRD, "1.0.0", "", START)
    run(core, sessions.start, ali, DEVICE, "1.0.0", "", START + timedelta(minutes=20))
    assert [(row["id"], row["durum"]) for row in sessions_of(core, veli)] == [(theirs, "aktif")]


def test_signing_in_again_on_the_same_computer_and_a_two_computer_limit(core):
    ali = make_user(core, "ali@x.test")
    first, _ = run(core, sessions.start, ali, DEVICE, "", "", START, limit=2)
    second, _ = run(core, sessions.start, ali, OTHER, "", "", START + timedelta(minutes=1), limit=2)
    assert [row["durum"] for row in sessions_of(core, ali)] == ["aktif", "aktif"]
    renewed, _ = run(core, sessions.start, ali, DEVICE, "", "", START + timedelta(minutes=2), limit=2)
    assert {row["id"]: (row["durum"], row["kapanma_nedeni"]) for row in sessions_of(core, ali)} == {
        first: ("kapandi", "YENI_GIRIS"), second: ("aktif", None), renewed: ("aktif", None)}
    # A third computer closes the session seen longest ago.
    run(core, sessions.renew, renewed, 0, ali["id"], DEVICE, "", "", START + timedelta(minutes=3))
    run(core, sessions.start, ali, THIRD, "", "", START + timedelta(minutes=4), limit=2)
    assert {row["cihaz"]: row["durum"] for row in sessions_of(core, ali) if row["id"] != first} == {
        DEVICE: "aktif", OTHER: "kapandi", THIRD: "aktif"}


def test_each_renewal_moves_the_number_and_repeats_are_counted_only_from_another_copy(core):
    ali = make_user(core, "ali@x.test")
    session, _ = run(core, sessions.start, ali, DEVICE, "1.0.0", "", START, instance=APP1)
    moment = START
    for expected in (1, 2, 3):
        moment += timedelta(minutes=10)
        assert run(core, sessions.renew, session, expected - 1, ali["id"], DEVICE, "1.0.1", "9.9.9.9", moment,
                   instance=APP1) == ("tamam", expected)
    row = sessions_of(core, ali)[0]
    assert (row["sira"], row["surum"], row["ip"], row["tekrar"], row["ornek"]) == (3, "1.0.1", "9.9.9.9", 0, APP1)
    # The same Studio asks again with the earlier number however late: the current number, not counted.
    for delay in (timedelta(seconds=30), timedelta(minutes=50), timedelta(hours=9), timedelta(days=3)):
        assert run(core, sessions.renew, session, 2, ali["id"], DEVICE, "1.0.1", "", moment + delay,
                   instance=APP1) == ("tamam", 3)
    assert sessions_of(core, ali)[0]["tekrar"] == 0
    # Closed and opened again before the answer arrived (another identity): accepted but counted.
    moment += timedelta(days=4)
    assert run(core, sessions.renew, session, 2, ali["id"], DEVICE, "", "", moment, instance=APP2) == ("tamam", 3)
    assert (sessions_of(core, ali)[0]["tekrar"], sessions_of(core, ali)[0]["ornek"]) == (1, APP2)
    assert run(core, sessions.renew, session, 2, ali["id"], DEVICE, "", "", moment, instance=APP2) == ("tamam", 3)
    assert sessions_of(core, ali)[0]["tekrar"] == 1
    # Two clean renewals in a row reset the counter: one-off connection problems never add up.
    assert run(core, sessions.renew, session, 3, ali["id"], DEVICE, "", "", moment, instance=APP2) == ("tamam", 4)
    assert sessions_of(core, ali)[0]["tekrar"] == 1
    assert run(core, sessions.renew, session, 4, ali["id"], DEVICE, "", "", moment, instance=APP2) == ("tamam", 5)
    assert sessions_of(core, ali)[0]["tekrar"] == 0


def test_an_old_number_or_two_running_copies_close_the_session(core):
    ali = make_user(core, "ali@x.test")
    session, _ = run(core, sessions.start, ali, DEVICE, "", "", START, instance=APP1)
    for number in (0, 1):
        assert run(core, sessions.renew, session, number, ali["id"], DEVICE, "", "", START, instance=APP1)[0] == "tamam"
    assert run(core, sessions.renew, session, 0, ali["id"], DEVICE, "", "", START, instance=APP1) == ("kapali", "KOPYA")
    assert run(core, sessions.renew, session, 2, ali["id"], DEVICE, "", "", START, instance=APP1) == ("kapali", "KOPYA")
    # Two copies at once: one always stays behind the number the other moved on; the third round closes the session.
    for ahead, behind in ((APP1, APP2), (None, None)):  # None: a request without "ornek"
        session, _ = run(core, sessions.start, ali, DEVICE, "", "", START, instance=ahead)
        result = None
        for number in range(5):
            assert run(core, sessions.renew, session, number, ali["id"], DEVICE, "", "", START,
                       instance=ahead) == ("tamam", number + 1)
            result = run(core, sessions.renew, session, number, ali["id"], DEVICE, "", "", START, instance=behind)
            if result[0] == "kapali":
                break
        assert result == ("kapali", "KOPYA") and number == 2
    # Two copies taking turns are caught the same way.
    session, _ = run(core, sessions.start, ali, DEVICE, "", "", START, instance=APP1)
    outcomes = []
    for number in range(6):
        ahead, behind = (APP1, APP2) if number % 2 == 0 else (APP2, APP1)
        outcomes.append(run(core, sessions.renew, session, number, ali["id"], DEVICE, "", "", START, instance=ahead)[0])
        outcomes.append(run(core, sessions.renew, session, number, ali["id"], DEVICE, "", "", START, instance=behind)[0])
        if outcomes[-1] == "kapali":
            break
    assert outcomes == ["tamam"] * 5 + ["kapali"]
    # A number ahead is never accepted.
    session, _ = run(core, sessions.start, ali, DEVICE, "", "", START)
    assert run(core, sessions.renew, session, 5, ali["id"], DEVICE, "", "", START) == ("kapali", "KOPYA")


def test_another_user_or_device_cannot_use_a_session_and_signing_out_closes_it(core):
    ali, veli = make_user(core, "ali@x.test"), make_user(core, "veli@x.test")
    session, _ = run(core, sessions.start, ali, DEVICE, "", "", START)
    assert run(core, sessions.renew, session, 0, veli["id"], DEVICE, "", "", START) == ("kapali", "UYUSMUYOR")
    assert run(core, sessions.renew, session, 0, ali["id"], OTHER, "", "", START) == ("kapali", "UYUSMUYOR")
    assert run(core, sessions.renew, session, "0", ali["id"], DEVICE, "", "", START) == ("kapali", "UYUSMUYOR")
    assert run(core, sessions.renew, "f" * 32, 0, ali["id"], DEVICE, "", "", START) == ("kapali", "YOK")
    assert sessions_of(core, ali)[0]["durum"] == "aktif"  # a wrong request never closes the valid session
    run(core, sessions.end, session, veli["id"], START)  # nobody closes another account's session
    assert sessions_of(core, ali)[0]["durum"] == "aktif"
    run(core, sessions.end, session, ali["id"], START + timedelta(minutes=1))
    assert run(core, sessions.renew, session, 0, ali["id"], DEVICE, "", "", START) == ("kapali", "CIKIS")


def test_closed_sessions_are_forgotten_after_ninety_days(core):
    ali = make_user(core, "ali@x.test")
    oldest, _ = run(core, sessions.start, ali, DEVICE, "", "", START - timedelta(days=200))
    middle, _ = run(core, sessions.start, ali, OTHER, "", "", START - timedelta(days=30))
    newest, _ = run(core, sessions.start, ali, THIRD, "", "", START)
    # "oldest" closed 30 days ago: it goes once those reach 90 days, the more recently closed one stays.
    assert {row["id"] for row in sessions_of(core, ali)} == {oldest, middle, newest}
    run(core, sessions.start, ali, DEVICE, "", "", START + timedelta(days=70))
    assert {row["id"] for row in sessions_of(core, ali)} >= {middle, newest} and oldest not in {
        row["id"] for row in sessions_of(core, ali)}


def test_closing_all_sessions_of_an_account_or_one_of_them(core):
    ali, veli = make_user(core, "ali@x.test"), make_user(core, "veli@x.test")
    first, _ = run(core, sessions.start, ali, DEVICE, "", "", START, limit=2)
    second, _ = run(core, sessions.start, ali, OTHER, "", "", START + timedelta(minutes=1), limit=2)
    run(core, sessions.start, veli, THIRD, "", "", START)
    assert run(core, sessions.close_all, ali["id"], "YONETICI", START, first) == 1
    assert run(core, sessions.close_all, ali["id"], "YONETICI", START) == 1
    assert run(core, sessions.close_all, ali["id"], "YONETICI", START) == 0
    assert [(row["id"], row["kapanma_nedeni"]) for row in sessions_of(core, ali)] == [
        (first, "YONETICI"), (second, "YONETICI")]
    assert sessions_of(core, veli)[0]["durum"] == "aktif"
