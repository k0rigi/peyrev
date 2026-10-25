"""Şifre işlemleri: /api/rpa/hesap/sifre ve /api/rpa/hesap/sifre-yenile (orkestrai test_rpa_bireysel karşılığı)."""

import re

from conftest import make_user
from test_accounts import login, renew
from werkzeug.security import check_password_hash, generate_password_hash

from peyrev import users
from peyrev.accounts import admin


def change(client, current, new="Yepyeni2027", user="calisan"):
    return client.post("/api/rpa/hesap/sifre", json={"kullanici": user, "sifre": current, "yeni_sifre": new})


def reset(client, name, code, password="Yepyeni2027", field="kullanici"):
    return client.post("/api/rpa/hesap/sifre-yenile", json={field: name, "kod": code, "yeni_sifre": password})


def stored_hash(core, user):
    with core.db.transaction() as cur:
        return users.get(cur, user["id"])["sifre_hash"]


def test_password_change_needs_the_current_password(client, core):
    staff = make_user(core, "calisan@firma.test", password="Calisan123", modules={users.RPA: None})
    wrong = change(client, "Yanlis123")
    assert wrong.status_code == 401 and wrong.get_json() == {
        "success": False, "kod": "HATALI_GIRIS", "message": "Kullanıcı adı veya mevcut şifre hatalı."}
    same = change(client, "Calisan123", "Calisan123")
    assert same.status_code == 400 and same.get_json()["message"] == "Yeni şifre mevcut şifreyle aynı olamaz."
    assert change(client, "Calisan123", "kisa").get_json()["message"] == "Şifre 8-128 karakter olmalıdır."
    assert change(client, "Calisan123", "yalnizharf").get_json()["message"] == "Şifrede en az bir harf ve bir rakam bulunmalıdır."
    # The new password is checked only after the current one: a wrong current password answers 401, not 400.
    assert change(client, "Yanlis123", "kisa").status_code == 401
    done = change(client, "Calisan123")
    assert done.status_code == 200 and done.get_json() == {"success": True, "message": "Şifreniz değiştirildi."}
    assert done.headers["Cache-Control"] == "no-store"
    assert check_password_hash(stored_hash(core, staff), "Yepyeni2027")
    # Two accounts behind the same user name and password: the full address is needed (not counted).
    make_user(core, "calisan@baska.test", password="Yepyeni2027")
    ambiguous = change(client, "Yepyeni2027", "Sonraki2028")
    assert ambiguous.status_code == 409 and ambiguous.get_json()["kod"] == "BELIRSIZ"
    for _ in range(9):
        change(client, "Yanlis123", user="calisan@firma.test")
    assert change(client, "Yanlis123", user="calisan@firma.test").status_code == 401  # the tenth failure
    blocked = change(client, "Yepyeni2027", "Sonraki2028", user="calisan@firma.test")
    assert blocked.status_code == 429 and blocked.get_json()["kod"] == "COK_DENEME"
    assert check_password_hash(stored_hash(core, staff), "Yepyeni2027")


def test_a_migrated_werkzeug_hash_can_be_changed(client, core):
    legacy = generate_password_hash("Eski12345", method="pbkdf2:sha256:260000", salt_length=16)
    user = make_user(core, "eski@ornek.test", modules={users.RPA: None})
    with core.db.transaction() as cur:
        cur.execute("UPDATE kullanicilar SET sifre_hash = %s WHERE id = %s", (legacy, user["id"]))
    assert change(client, "Eski12345", user="eski").status_code == 200
    assert stored_hash(core, user) != legacy and check_password_hash(stored_hash(core, user), "Yepyeni2027")
    assert login(client, kullanici="eski", sifre="Yepyeni2027").status_code == 200


def test_password_requests_are_checked_strictly(client, core):
    make_user(core, "ayse@ornek.test", modules={users.RPA: None})
    for body in ({"kullanici": "ayse", "sifre": "Gizli12345"}, {"kullanici": "ayse", "sifre": "x", "yeni_sifre": "y", "fazla": 1}):
        response = client.post("/api/rpa/hesap/sifre", json=body)
        assert response.status_code == 400 and response.get_json() == {
            "success": False, "kod": "GECERSIZ", "message": "İstek alanları geçersiz."}
    for name, current in ((" ", "Gizli12345"), ("a" * 151, "Gizli12345"), ("ayse", ""), ("ayse", 5), ("ayse", "x" * 257)):
        response = change(client, current, user=name)
        assert response.status_code == 400 and response.get_json()["message"] == "Kullanıcı adı ve mevcut şifre gereklidir."
    # The new password is compared with the account's own login name (the whole address or the part before "@").
    make_user(core, "kemal2026@ornek.test")
    assert change(client, "Gizli12345", "KEMAL2026", user="kemal2026@ornek.test").get_json()["message"] == (
        "Şifre e-posta adresinizle aynı olamaz.")
    too_big = client.post("/api/rpa/hesap/sifre", data="x" * 9000, content_type="application/json")
    assert too_big.status_code == 413 and too_big.get_json()["kod"] == "GOVDE_SINIRI"
    # None of these were failed attempts.
    with core.db.transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM hesap_izleri")
        assert cur.fetchone()["n"] == 0


def test_a_new_password_ends_studio_sessions_at_the_next_renewal(client, core):
    make_user(core, "ali@ornek.test", modules={users.RPA: None})
    token = login(client).get_json()["yenileme"]
    assert change(client, "Gizli12345", user="ali@ornek.test").status_code == 200
    closed = renew(client, token)
    assert closed.status_code == 401 and closed.get_json()["kod"] == "OTURUM_GECERSIZ" and "neden" not in closed.get_json()
    assert login(client, sifre="Yepyeni2027").status_code == 200


def test_reset_code_works_once_within_its_time_for_any_account(client, core):
    manager = make_user(core, "yonetici@peyrev.test", rol="yonetici")
    ayse = make_user(core, "ayse@ornek.test", modules={users.RPA: None})
    first = admin.issue_password_code(core, manager["id"], ayse["id"])["kod"]
    code = admin.issue_password_code(core, manager["id"], ayse["id"])
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", code["kod"]) and code["gecerlilik"] == "2026-10-27 12:00"
    replaced = reset(client, "ayse@ornek.test", first, field="eposta")  # Studio 0.9.35 sent "eposta"
    assert replaced.status_code == 401 and replaced.get_json() == {
        "success": False, "kod": "KOD_HATALI", "message": "Kullanıcı adı veya şifre kodu hatalı ya da kodun süresi dolmuş."}
    done = reset(client, "AYSE@ornek.test", code["kod"].lower().replace("-", " "), field="eposta")
    assert done.status_code == 200 and done.get_json() == {
        "success": True, "message": "Şifreniz yenilendi. Yeni şifrenizle giriş yapabilirsiniz."}
    assert check_password_hash(stored_hash(core, ayse), "Yepyeni2027")
    assert reset(client, "ayse", code["kod"], "Baska2028").status_code == 401  # used once
    assert login(client, kullanici="ayse", sifre="Yepyeni2027").status_code == 200
    # Valid until its last moment, not at it.
    early = admin.issue_password_code(core, manager["id"], ayse["id"])["kod"]
    core.clock.advance(hours=23, minutes=59)
    assert reset(client, "ayse", early, "Sonraki2028").status_code == 200
    late = admin.issue_password_code(core, manager["id"], ayse["id"])["kod"]
    core.clock.advance(hours=24)
    assert reset(client, "ayse", late, "Sonraki2029").status_code == 401
    # Two accounts behind one user name, or a passive account, cannot take a code (401, not 409).
    ali = make_user(core, "ali@a.test")
    make_user(core, "ali@b.test")
    ali_code = admin.issue_password_code(core, manager["id"], ali["id"])["kod"]
    assert reset(client, "ali", ali_code).status_code == 401
    with core.db.transaction() as cur:
        cur.execute("UPDATE kullanicilar SET durum = 'pasif' WHERE id = %s", (ali["id"],))
    assert reset(client, "ali@a.test", ali_code).status_code == 401


def test_reset_requests_are_checked_before_the_database(client, core):
    make_user(core, "ayse@ornek.test")
    cases = [({"kod": "ABCD-EFGH-JKLM", "yeni_sifre": "Yepyeni2027"}, "Kullanıcı adınızı veya e-posta adresinizi yazın."),
             ({"kullanici": " ", "kod": "ABCD-EFGH-JKLM", "yeni_sifre": "Yepyeni2027"},
              "Kullanıcı adınızı veya e-posta adresinizi yazın."),
             ({"kullanici": "a" * 151, "kod": "ABCD-EFGH-JKLM", "yeni_sifre": "Yepyeni2027"},
              "Kullanıcı adınızı veya e-posta adresinizi yazın."),
             ({"kullanici": "ayse", "kod": "ABCD", "yeni_sifre": "Yepyeni2027"}, "Şifre kodunu yazın."),
             ({"kullanici": "ayse", "kod": "A" * 33, "yeni_sifre": "Yepyeni2027"}, "Şifre kodunu yazın."),
             ({"kullanici": "ayse", "kod": "ABCD-EFGH-JKLM", "yeni_sifre": "kisa"}, "Şifre 8-128 karakter olmalıdır."),
             ({"kullanici": "ayse1234", "kod": "ABCD-EFGH-JKLM", "yeni_sifre": "AYSE1234"},
              "Şifre e-posta adresinizle aynı olamaz."),
             ({"kullanici": "ayse", "kod": "ABCD-EFGH-JKLM"}, "İstek alanları geçersiz.")]
    for body, message in cases:
        response = client.post("/api/rpa/hesap/sifre-yenile", json=body)
        assert response.status_code == 400 and response.get_json() == {"success": False, "kod": "GECERSIZ", "message": message}
    with core.db.transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM hesap_izleri")
        assert cur.fetchone()["n"] == 0


def test_code_guessing_stops_after_ten_failures_and_shares_the_counter_with_password_changes(client, core):
    manager = make_user(core, "yonetici@peyrev.test", rol="yonetici")
    ayse = make_user(core, "ayse@ornek.test")
    code = admin.issue_password_code(core, manager["id"], ayse["id"])["kod"]
    for _ in range(9):
        assert reset(client, "ayse@ornek.test", "AAAA-BBBB-CCCC").status_code == 401
    assert change(client, "Yanlis123", user="AYSE@ornek.test").status_code == 401  # the same counter
    blocked = reset(client, "ayse@ornek.test", code)
    assert blocked.status_code == 429 and blocked.get_json() == {
        "success": False, "kod": "COK_DENEME", "message": "Çok fazla hatalı deneme. 15 dakika sonra tekrar deneyin."}
    core.clock.advance(minutes=15, seconds=1)
    assert reset(client, "ayse@ornek.test", code).status_code == 200
    # Per address: 30 failures from one address stop every account there.
    for index in range(30):
        reset(client, f"kimse{index}", "AAAA-BBBB-CCCC")
    assert change(client, "Yepyeni2027", "Sonraki2028", user="ayse@ornek.test").status_code == 429
    elsewhere = client.post("/api/rpa/hesap/sifre", json={"kullanici": "ayse@ornek.test", "sifre": "Yepyeni2027",
                                                          "yeni_sifre": "Sonraki2028"},
                            headers={"X-Forwarded-For": "10.0.0.7"})
    assert elsewhere.status_code == 200


def test_password_errors_never_carry_database_details(client, core, monkeypatch):
    def broken():
        raise RuntimeError("GIZLI-DB-SIFRESI")

    monkeypatch.setattr(core.db, "transaction", broken)
    for response in (change(client, "Gizli12345"), reset(client, "ayse", "ABCD-EFGH-JKLM")):
        assert response.status_code == 503 and response.get_json()["kod"] == "SUNUCU"
        assert "GIZLI" not in response.get_data(as_text=True)
