"""Yönetim paneline açılan hesap işlemleri (peyrev.accounts.admin): başvuru onayı ve reddi, modül hakları, şifre kodu,
oturum kapatma, hesap durumu, yönetimden hesap açma ve her değişikliğin yonetim_olaylari kaydı."""

import json
from datetime import date, timedelta

import pytest
from conftest import make_user
from test_accounts import login, renew, studio_agents, studio_license
from test_accounts_applications import apply
from werkzeug.security import check_password_hash

from peyrev import users
from peyrev.accounts import admin, applications
from peyrev.common import ApiError


@pytest.fixture
def manager(core):
    return make_user(core, "yonetici@peyrev.test", rol="yonetici")


def refused(call, *args, **options):
    with pytest.raises(ApiError) as error:
        call(*args, **options)
    return error.value


def events(core, kind=None):
    with core.db.transaction() as cur:
        cur.execute("SELECT * FROM yonetim_olaylari" + (" WHERE tur = %s" if kind else "") + " ORDER BY id",
                    (kind,) if kind else ())
        return cur.fetchall()


def pending_id(core, email="ayse@ornek.test"):
    with core.db.transaction() as cur:
        cur.execute("SELECT id FROM basvurular WHERE eposta = %s AND durum = 'bekliyor'", (email,))
        return cur.fetchone()["id"]


def account(core, user_id):
    with core.db.transaction() as cur:
        return users.get(cur, user_id), users.modules(cur, user_id)


def test_only_an_active_administrator_may_act(core, manager):
    staff = make_user(core, "calisan@ornek.test", modules={users.RPA: None})
    retired = make_user(core, "eski@peyrev.test", rol="yonetici", durum="pasif")
    calls = [(admin.list_applications, ()), (admin.list_users, ()), (admin.issue_password_code, (staff["id"],)),
             (admin.close_sessions, (staff["id"],)), (admin.set_status, (staff["id"], "pasif")),
             (admin.set_module, (staff["id"], users.AGENTS), {"bitis": None}),
             (admin.remove_module, (staff["id"], users.RPA)), (admin.reject_application, ("a" * 32,)),
             (admin.create_user, (), {"giris": "BASBUG_1", "ad_soyad": "Basbuğ Bir", "baslangic": "2026-10-26",
                                      "bitis": "2027-10-26"})]
    for call, args, *options in calls:
        for actor in (staff["id"], retired["id"], 999999):
            error = refused(call, core, actor, *args, **(options[0] if options else {}))
            assert (error.status, error.kod, str(error)) == (403, "YETKI_YOK", "Bu işlem yalnız platform yöneticisine açıktır.")
        for actor in (None, "1", True, 0):
            assert refused(call, core, actor, *args, **(options[0] if options else {})).status == 401
    # Nothing changed and nothing was recorded.
    assert account(core, staff["id"])[0]["durum"] == "aktif" and users.AGENTS not in account(core, staff["id"])[1]
    assert events(core) == []


def test_approval_opens_the_account_with_the_applicants_password_and_module_rows(client, core, manager):
    apply(client, paket="rpa_ajan")
    application = pending_id(core)
    listed = admin.list_applications(core, manager["id"])
    assert [(row["id"], row["eposta"], row["durum"], row["telefon"]) for row in listed] == [
        (application, "ayse@ornek.test", "bekliyor", "+90 532 000 00 00")]
    assert listed[0]["olusturma"] == "2026-10-26 12:00" and "sifre" not in json.dumps(listed)
    result = admin.approve_application(core, manager["id"], application, baslangic="2026-10-20", bitis="2027-10-26",
                                       note="Havale 26.10")
    assert result == {"kullanici_id": result["kullanici_id"], "eposta": "ayse@ornek.test", "paket": "rpa_ajan",
                      "bitis": "2027-10-26"}
    user, rights = account(core, result["kullanici_id"])
    assert (user["giris"], user["ad_soyad"], user["telefon"], user["rol"], user["durum"], user["kaynak"]) == (
        "ayse@ornek.test", "Ayşe Yılmaz", "+90 532 000 00 00", "kullanici", "aktif", "basvuru")
    assert user["alan_id"] >= 1000000 and check_password_hash(user["sifre_hash"], "Guclu2026")
    assert rights == {users.RPA: {"baslangic": date(2026, 10, 20), "bitis": date(2027, 10, 26)},
                      users.AGENTS: {"baslangic": date(2026, 10, 20), "bitis": date(2027, 10, 26)}}
    with core.db.transaction() as cur:
        cur.execute("SELECT * FROM basvurular WHERE id = %s", (application,))
        decided = cur.fetchone()
    assert (decided["durum"], decided["sifre_hash"], decided["yonetici_notu"], decided["karar_veren_id"],
            decided["kullanici_id"]) == ("onaylandi", "", "Havale 26.10", manager["id"], result["kullanici_id"])
    # The person signs in with the password of the application; agents come with the package.
    claims = studio_license(client, login(client, kullanici="ayse", sifre="Guclu2026"))
    assert claims["bitis"] == "2027-10-26" and studio_agents(claims)["aktif"] is True
    with core.db.transaction() as cur:
        assert applications.pending(cur, "ayse") == []  # no longer a waiting application
    second = refused(admin.approve_application, core, manager["id"], application, bitis="2027-10-26")
    assert (second.status, second.kod) == (409, "SONUCLANDI")
    assert admin.list_applications(core, manager["id"]) == []  # approved ones are not in the default list
    assert [row["kullanici_id"] for row in admin.list_applications(core, manager["id"], durum="onaylandi")] == [
        result["kullanici_id"]]
    [event] = events(core, "basvuru_onaylandi")
    assert (event["yonetici_id"], event["hedef"]) == (manager["id"], f"kullanici:{result['kullanici_id']}")
    assert event["ayrinti"] == {"basvuru_id": application, "giris": "ayse@ornek.test", "paket": "rpa_ajan",
                                "moduller": ["MOD_RPA", "MOD_AJAN"], "baslangic": "2026-10-20", "bitis": "2027-10-26",
                                "not": "Havale 26.10"}


def test_approval_checks_dates_package_note_and_existing_accounts(client, core, manager):
    apply(client)
    application = pending_id(core)
    for options, message in (({"bitis": "2026-10-25"}, "Bitiş bugünden ve başlangıçtan önce olamaz."),
                             ({"bitis": "2027-01-01", "baslangic": "2027-02-01"},
                              "Bitiş bugünden ve başlangıçtan önce olamaz."),
                             ({"bitis": "26.10.2027"}, "Bitiş YYYY-AA-GG biçiminde olmalıdır."),
                             ({"bitis": "2027-02-30"}, "Bitiş geçersiz."),
                             ({"bitis": None}, "Bitiş YYYY-AA-GG biçiminde olmalıdır."),
                             ({"bitis": "2027-01-01", "paket": "tam"}, "Paket geçersiz."),
                             ({"bitis": "2027-01-01", "note": "x" * 301}, "Not en fazla 300 karakter olabilir.")):
        error = refused(admin.approve_application, core, manager["id"], application, **options)
        assert (error.status, error.kod, str(error)) == (400, "GECERSIZ", message)
    for wrong in ("yok", "A" * 32, None, "f" * 32):
        assert refused(admin.approve_application, core, manager["id"], wrong, bitis="2027-01-01").status == 404
    # An account opened meanwhile with the same address: the application keeps waiting.
    make_user(core, "AYSE@ornek.test")
    taken = refused(admin.approve_application, core, manager["id"], application, bitis="2027-01-01")
    assert (taken.status, taken.kod) == (409, "HESAP_VAR")
    assert pending_id(core) == application and events(core) == []


def test_an_rpa_only_approval_has_no_agent_row_and_starts_today_by_default(client, core, manager):
    apply(client, paket="rpa_ajan")
    result = admin.approve_application(core, manager["id"], pending_id(core), bitis="2026-10-26", paket="rpa")
    assert result["paket"] == "rpa"
    assert account(core, result["kullanici_id"])[1] == {users.RPA: {"baslangic": date(2026, 10, 26),
                                                                    "bitis": date(2026, 10, 26)}}
    claims = studio_license(client, login(client, kullanici="ayse@ornek.test", sifre="Guclu2026"))
    assert claims["bitis"] == "2026-10-26" and studio_agents(claims) == {}


def test_rejection_clears_the_password_and_allows_a_new_application(client, core, manager):
    apply(client)
    application = pending_id(core)
    assert admin.reject_application(core, manager["id"], application, note="Ödeme yok") == {"id": application,
                                                                                             "durum": "reddedildi"}
    [row] = admin.list_applications(core, manager["id"], durum="reddedildi")
    assert (row["durum"], row["not"], row["karar"]) == ("reddedildi", "Ödeme yok", "2026-10-26 12:00")
    with core.db.transaction() as cur:
        cur.execute("SELECT sifre_hash FROM basvurular WHERE id = %s", (application,))
        assert cur.fetchone()["sifre_hash"] == ""
    again = refused(admin.reject_application, core, manager["id"], application)
    assert (again.status, str(again)) == (404, "Bekleyen başvuru bulunamadı.")
    assert login(client, kullanici="ayse", sifre="Guclu2026").get_json()["kod"] == "HATALI_GIRIS"
    apply(client)
    assert pending_id(core) != application
    assert [event["ayrinti"] for event in events(core, "basvuru_reddedildi")] == [{"not": "Ödeme yok"}]
    assert refused(admin.list_applications, core, manager["id"], durum="hepsi").kod == "GECERSIZ"


def test_module_rights_are_given_changed_without_an_end_and_removed(client, core, manager):
    ali = make_user(core, "ali@ornek.test")
    assert login(client).get_json()["kod"] == "YETKI_YOK"
    given = admin.set_module(core, manager["id"], ali["id"], users.RPA, bitis="2026-12-31")
    assert given == {"kullanici_id": ali["id"], "modul": "MOD_RPA", "baslangic": "2026-10-26", "bitis": "2026-12-31"}
    token = login(client).get_json()["yenileme"]
    # NULL is open-ended; the start date stays unless a new one is given.
    assert admin.set_module(core, manager["id"], ali["id"], users.RPA, bitis=None)["baslangic"] == "2026-10-26"
    assert studio_license(client, renew(client, token))["bitis"] is None
    changed = admin.set_module(core, manager["id"], ali["id"], users.RPA, bitis="2027-06-30", baslangic="2026-11-01")
    assert (changed["baslangic"], changed["bitis"]) == ("2026-11-01", "2027-06-30")
    for options, message in (({"bitis": "2026-10-25"}, "Bitiş bugünden önce olamaz; erişimi kapatmak için bugünü seçin."),
                             ({"bitis": "2027-01-01", "baslangic": "2027-02-01"}, "Başlangıç bitişten sonra olamaz."),
                             ({"bitis": "2027/01/01"}, "Bitiş YYYY-AA-GG biçiminde olmalıdır.")):
        assert str(refused(admin.set_module, core, manager["id"], ali["id"], users.RPA, **options)) == message
    assert str(refused(admin.set_module, core, manager["id"], ali["id"], "MOD_WMS", bitis=None)) == "Modül geçersiz."
    assert refused(admin.set_module, core, manager["id"], 999999, users.RPA, bitis=None).status == 404
    admin.set_module(core, manager["id"], ali["id"], users.AGENTS, bitis="2027-06-30")
    token = login(client).get_json()["yenileme"]
    assert "ajan_erisim" in renew(client, token).get_json()
    assert admin.remove_module(core, manager["id"], ali["id"], users.AGENTS)["kaldirildi"] is True
    assert admin.remove_module(core, manager["id"], ali["id"], users.AGENTS)["kaldirildi"] is False
    renewed = renew(client, renew(client, login(client).get_json()["yenileme"]).get_json()["yenileme"])
    assert "ajan_erisim" not in renewed.get_json()
    admin.remove_module(core, manager["id"], ali["id"], users.RPA)
    # The open session is not closed: it is refused until a license is given again.
    refusal = renew(client, renewed.get_json()["yenileme"])
    assert (refusal.status_code, refusal.get_json()["kod"]) == (403, "YETKI_YOK")
    kinds = [(event["tur"], event["ayrinti"]["modul"]) for event in events(core)]
    assert kinds == [("modul_ayarlandi", "MOD_RPA")] * 3 + [("modul_ayarlandi", "MOD_AJAN"),
                                                            ("modul_kaldirildi", "MOD_AJAN"), ("modul_kaldirildi", "MOD_RPA")]
    assert events(core)[1]["ayrinti"] == {"modul": "MOD_RPA", "baslangic": "2026-10-26", "bitis": None,
                                          "onceki": {"baslangic": "2026-10-26", "bitis": "2026-12-31"}}
    assert events(core)[0]["ayrinti"]["onceki"] is None


def test_a_password_code_is_shown_once_and_stored_only_as_a_digest(client, core, manager):
    ayse = make_user(core, "ayse@ornek.test", modules={users.RPA: None})
    first = admin.issue_password_code(core, manager["id"], ayse["id"])
    second = admin.issue_password_code(core, manager["id"], ayse["id"], hours=2)
    assert second["gecerlilik"] == "2026-10-26 14:00"
    with core.db.transaction() as cur:
        cur.execute("SELECT * FROM sifre_kodlari WHERE kullanici_id = %s ORDER BY olusturma, kullanildi NULLS LAST",
                    (ayse["id"],))
        codes = cur.fetchall()
    text = json.dumps([{key: str(value) for key, value in row.items()} for row in codes])
    assert first["kod"] not in text and second["kod"] not in text and first["kod"].replace("-", "") not in text
    assert [row["kullanildi"] is None for row in codes] == [False, True]  # the earlier code was replaced
    assert all(row["olusturan_id"] == manager["id"] for row in codes)
    assert "kod" not in json.dumps([event["ayrinti"] for event in events(core)]).replace("kod_", "")
    assert [event["tur"] for event in events(core)] == ["sifre_kodu_uretildi"] * 2
    for hours in (0, 24 * 7 + 1, "24"):
        assert str(refused(admin.issue_password_code, core, manager["id"], ayse["id"], hours=hours)) == "Kod süresi geçersiz."
    assert refused(admin.issue_password_code, core, manager["id"], 999999).status == 404


def test_closing_sessions_tells_studio_that_an_administrator_closed_them(client, core, manager):
    ali = make_user(core, "ali@ornek.test", modules={users.RPA: None})
    token = login(client).get_json()["yenileme"]
    assert admin.close_sessions(core, manager["id"], ali["id"]) == {"kullanici_id": ali["id"], "kapatilan": 1}
    closed = renew(client, token)
    assert closed.status_code == 401 and closed.get_json() == {
        "success": False, "kod": "OTURUM_GECERSIZ", "neden": "YONETICI",
        "message": "Oturumunuz yönetici tarafından kapatıldı. Yeniden giriş yapın."}
    assert admin.close_sessions(core, manager["id"], ali["id"])["kapatilan"] == 0
    session = login(client).get_json()
    with core.db.transaction() as cur:
        cur.execute("SELECT id FROM oturumlar WHERE kullanici_id = %s AND durum = 'aktif'", (ali["id"],))
        only = cur.fetchone()["id"]
    assert admin.close_sessions(core, manager["id"], ali["id"], session_id="f" * 32)["kapatilan"] == 0
    assert admin.close_sessions(core, manager["id"], ali["id"], session_id=only)["kapatilan"] == 1
    assert renew(client, session["yenileme"]).get_json()["neden"] == "YONETICI"
    assert refused(admin.close_sessions, core, manager["id"], ali["id"], session_id="bozuk").status == 404
    assert [event["ayrinti"] for event in events(core, "oturumlar_kapatildi")] == [{"adet": 1},
                                                                                     {"adet": 1, "oturum_id": only}]


def test_a_passive_account_cannot_sign_in_and_its_sessions_close(client, core, manager):
    ali = make_user(core, "ali@ornek.test", modules={users.RPA: None})
    token = login(client).get_json()["yenileme"]
    assert admin.set_status(core, manager["id"], ali["id"], "pasif") == {"kullanici_id": ali["id"], "durum": "pasif",
                                                                       "kapatilan": 1}
    assert renew(client, token).get_json()["kod"] == "OTURUM_GECERSIZ"
    assert login(client).get_json()["kod"] == "HATALI_GIRIS"
    assert admin.set_status(core, manager["id"], ali["id"], "pasif")["kapatilan"] == 0  # nothing more to do
    admin.set_status(core, manager["id"], ali["id"], "aktif")
    assert login(client).status_code == 200
    assert [(event["tur"], event["ayrinti"]) for event in events(core)] == [
        ("durum_degisti", {"onceki": "aktif", "durum": "pasif", "kapatilan_oturum": 1}),
        ("durum_degisti", {"onceki": "pasif", "durum": "aktif", "kapatilan_oturum": 0})]
    assert str(refused(admin.set_status, core, manager["id"], ali["id"], "silindi")) == "Hesap durumu geçersiz."
    assert str(refused(admin.set_status, core, manager["id"], manager["id"], "pasif")) == "Kendi hesabınızı pasif yapamazsınız."
    assert refused(admin.set_status, core, manager["id"], 999999, "pasif").status == 404


def test_an_account_opened_by_the_administrator_sets_its_first_password_with_a_seven_day_code(client, core, manager):
    options = {"giris": "BASBUG_1", "ad_soyad": "Basbuğ Bir", "baslangic": "2026-10-18", "bitis": "2027-09-27",
               "ajan": True}
    preview = admin.create_user(core, manager["id"], **options, onizleme=True)
    assert preview == {"durum": "hazir", "giris": "BASBUG_1", "ad_soyad": "Basbuğ Bir", "moduller": ["MOD_RPA", "MOD_AJAN"],
                       "baslangic": "2026-10-18", "bitis": "2027-09-27"}
    with core.db.transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM kullanicilar")
        assert cur.fetchone()["n"] == 1 and events(core) == []  # the preview writes nothing
    created = admin.create_user(core, manager["id"], **options)
    assert created["giris"] == "BASBUG_1" and created["gecerlilik"] == "2026-11-02 12:00"
    user, rights = account(core, created["kullanici_id"])
    assert (user["kaynak"], user["rol"], user["durum"]) == ("yonetim", "kullanici", "aktif")
    assert rights[users.AGENTS] == {"baslangic": date(2026, 10, 18), "bitis": date(2027, 9, 27)} == rights[users.RPA]
    # Nobody knows the first password: the person uses the code with the user name (any letter case).
    assert login(client, kullanici="BASBUG_1").get_json()["kod"] == "HATALI_GIRIS"
    reset = client.post("/api/rpa/hesap/sifre-yenile", json={"kullanici": "basbug_1", "kod": created["kod"],
                                                              "yeni_sifre": "Yepyeni2027"})
    assert reset.status_code == 200
    assert studio_license(client, login(client, kullanici="basbug_1", sifre="Yepyeni2027"))["kullanici"] == "BASBUG_1"
    [event] = events(core, "kullanici_acildi")
    assert event["ayrinti"] == {"giris": "BASBUG_1", "ad_soyad": "Basbuğ Bir", "moduller": ["MOD_RPA", "MOD_AJAN"],
                                "baslangic": "2026-10-18", "bitis": "2027-09-27",
                                "kod_gecerlilik": "2026-11-02T09:00:00+00:00"}
    # A user name also signs in as the part before "@" of any address, so both must be free.
    make_user(core, "ali@ornek.test")
    for name in ("basbug_1", "ALI", "ali@ornek.test"):
        taken = refused(admin.create_user, core, manager["id"], **{**options, "giris": name})
        assert (taken.status, taken.kod, str(taken)) == (409, "HESAP_VAR",
                                                         "Bu kullanıcı adıyla bir hesap zaten var; başka bir ad seçin.")
    assert admin.create_user(core, manager["id"], **{**options, "giris": "ali@baska.test", "ajan": False})["kod"]


@pytest.mark.parametrize("changes, message", [
    ({"giris": "ab"}, "Kullanıcı adı 3-64 karakter (harf, rakam, . _ + -) ya da geçerli bir e-posta olmalıdır."),
    ({"giris": "boşluklu ad"}, "Kullanıcı adı 3-64 karakter (harf, rakam, . _ + -) ya da geçerli bir e-posta olmalıdır."),
    ({"ad_soyad": "A"}, "Ad soyad 2-120 karakter olmalıdır."),
    ({"ad_soyad": "<b>Ad</b>"}, "Ad soyad geçersiz karakter içeriyor."),
    ({"telefon": "telefon"}, "Telefonu rakamlarla yazın (ör. +90 5xx xxx xx xx)."),
    ({"bitis": "2026-10-25"}, "Lisans dönemi bugünü kapsamalıdır."),
    ({"baslangic": "2026-10-27"}, "Lisans dönemi bugünü kapsamalıdır."),
    ({"baslangic": "2027-01-02", "bitis": "2027-01-01"}, "Başlangıç bitişten sonra olamaz."),
    ({"bitis": "2027-13-01"}, "Bitiş geçersiz."),
    ({"ajan": "evet"}, "Ajanlar seçimi geçersiz."),
])
def test_an_invalid_new_account_is_refused_without_writing(core, manager, changes, message):
    options = {"giris": "BASBUG_1", "ad_soyad": "Basbuğ Bir", "baslangic": "2026-10-18", "bitis": "2027-09-27",
               **changes}
    error = refused(admin.create_user, core, manager["id"], **options)
    assert (error.status, error.kod, str(error)) == (400, "GECERSIZ", message)
    with core.db.transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM kullanicilar")
        assert cur.fetchone()["n"] == 1 and events(core) == []


def test_listing_users_shows_module_rights_sessions_and_license_state(client, core, manager):
    ali = make_user(core, "ali@ornek.test", modules={users.RPA: date(2026, 10, 25)})
    veli = make_user(core, "veli@ornek.test", ad_soyad="Veli %_ Kaya", modules={users.RPA: None, users.AGENTS: None})
    pasif = make_user(core, "pasif@ornek.test", durum="pasif")
    login(client, kullanici="veli")
    page = admin.list_users(core, manager["id"])
    assert page["toplam"] == 4 and [row["kullanici_id"] for row in page["kullanicilar"]] == [
        pasif["id"], veli["id"], ali["id"], manager["id"]]
    rows = {row["kullanici_id"]: row for row in page["kullanicilar"]}
    assert (rows[ali["id"]]["rpa"], rows[veli["id"]]["rpa"], rows[pasif["id"]]["rpa"], rows[manager["id"]]["rpa"]) == (
        "sure_doldu", "aktif", "yetki_yok", "aktif")
    assert rows[veli["id"]]["moduller"] == {"MOD_AJAN": {"baslangic": None, "bitis": None},
                                            "MOD_RPA": {"baslangic": None, "bitis": None}}
    assert rows[ali["id"]]["moduller"] == {"MOD_RPA": {"baslangic": None, "bitis": "2026-10-25"}}
    assert (rows[veli["id"]]["oturum_acik"], rows[ali["id"]]["oturum_acik"]) == (True, False)
    assert rows[ali["id"]]["acilis"] and rows[ali["id"]]["kaynak"] == "yonetim"
    assert [row["giris"] for row in admin.list_users(core, manager["id"], ara="VELI")["kullanicilar"]] == ["veli@ornek.test"]
    assert [row["giris"] for row in admin.list_users(core, manager["id"], ara="%_")["kullanicilar"]] == ["veli@ornek.test"]
    assert admin.list_users(core, manager["id"], durum="pasif")["toplam"] == 1
    second = admin.list_users(core, manager["id"], limit=2, offset=2)
    assert second["toplam"] == 4 and [row["kullanici_id"] for row in second["kullanicilar"]] == [ali["id"], manager["id"]]
    for options in ({"limit": 0}, {"offset": -1}, {"durum": "silindi"}, {"ara": "x" * 151}):
        assert refused(admin.list_users, core, manager["id"], **options).kod == "GECERSIZ"
    assert events(core) == []  # reading records nothing


def test_recorded_events_carry_no_secrets(client, core, manager):
    apply(client, paket="rpa_ajan")
    result = admin.approve_application(core, manager["id"], pending_id(core), bitis="2027-10-26")
    code = admin.issue_password_code(core, manager["id"], result["kullanici_id"])["kod"]
    created = admin.create_user(core, manager["id"], giris="BASBUG_1", ad_soyad="Basbuğ Bir", baslangic="2026-10-26",
                                bitis="2027-10-26")
    admin.close_sessions(core, manager["id"], result["kullanici_id"])
    admin.set_status(core, manager["id"], result["kullanici_id"], "pasif")
    with core.db.transaction() as cur:
        cur.execute("SELECT sifre_hash FROM kullanicilar")
        hashes = [row["sifre_hash"] for row in cur.fetchall()]
    recorded = json.dumps([event["ayrinti"] for event in events(core)], ensure_ascii=False)
    assert len(events(core)) == 4  # no session was open, so closing recorded nothing
    for secret in [code, code.replace("-", ""), created["kod"], created["kod"].replace("-", ""), "Guclu2026",
                   *(value.split("$")[-1] for value in hashes)]:
        assert secret not in recorded
    assert all(event["zaman"] == core.clock.now() for event in events(core))
    core.clock.advance(seconds=1)
    assert events(core)[0]["zaman"] < core.clock.now() + timedelta(seconds=1)
