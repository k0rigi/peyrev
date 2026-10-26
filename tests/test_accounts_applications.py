"""Bireysel başvuru: POST /api/rpa/basvuru (orkestrai test_rpa_bireysel karşılığı; firma yok)."""

from datetime import timedelta

import pytest
from conftest import make_user
from werkzeug.security import check_password_hash

from peyrev.accounts import applications
from peyrev.support import notify

RECEIVED = ("Başvurunuz alındı. Fiyat ve ödeme bilgisini başvurudaki e-posta veya telefonla size iletiyoruz. "
            "Ödemeniz onaylandığında hesabınız açılır; Peyrev'e bu e-posta adresi ve belirlediğiniz şifreyle "
            "girersiniz. Bu e-postayla daha önce hesap açıldıysa mevcut şifrenizle giriş yapabilirsiniz.")


def apply(client, address="10.0.0.1", **changes):
    body = {"ad_soyad": "Ayşe Yılmaz", "eposta": "ayse@ornek.test", "telefon": "+90 532 000 00 00",
            "sifre": "Guclu2026", "paket": "rpa", "onay": True, **changes}
    return client.post("/api/rpa/basvuru", json=body, headers={"X-Forwarded-For": address})


def stored(core, durum=None):
    with core.db.transaction() as cur:
        cur.execute("SELECT * FROM basvurular" + (" WHERE durum = %s" if durum else "") + " ORDER BY olusturma, eposta",
                    (durum,) if durum else ())
        return cur.fetchall()


def test_an_application_keeps_only_a_password_hash_and_notifies_the_owner(client, core):
    response = apply(client, paket="rpa_ajan", telefon="  +90 (532)   000 00 00 ")
    assert response.status_code == 201 and response.get_json() == {"success": True, "message": RECEIVED}
    assert response.headers["Cache-Control"] == "no-store"
    rows = stored(core)
    assert len(rows) == 1
    row = rows[0]
    assert (row["durum"], row["paket"], row["ad_soyad"], row["eposta"], row["telefon"], row["ip"]) == (
        "bekliyor", "rpa_ajan", "Ayşe Yılmaz", "ayse@ornek.test", "+90 (532) 000 00 00", "10.0.0.1")
    assert row["aydinlatma_surumu"] == applications.CONSENT_VERSION and row["olusturma"] == core.clock.now()
    assert "Guclu2026" not in str(row) and check_password_hash(row["sifre_hash"], "Guclu2026")
    assert core.notifier.messages == [("Yeni Peyrev başvurusu",
                                       "Ayşe Yılmaz · Peyrev + Ajanlar\nOnay bekliyor: https://peyrev.com/yonetim/")]
    # No account and no license exist before the payment is approved.
    with core.db.transaction() as cur:
        cur.execute("SELECT (SELECT count(*) FROM kullanicilar) AS hesap, (SELECT count(*) FROM kullanici_modulleri) AS hak")
        assert cur.fetchone() == {"hesap": 0, "hak": 0}


@pytest.mark.parametrize("changes, message", [
    ({"sifre": "kisa1"}, "Şifre 8-128 karakter olmalıdır."),
    ({"sifre": "sadeceharf"}, "Şifrede en az bir harf ve bir rakam bulunmalıdır."),
    ({"sifre": "1234567890"}, "Şifrede en az bir harf ve bir rakam bulunmalıdır."),
    ({"sifre": "ayse12345", "eposta": "ayse12345@x.test"}, "Şifre e-posta adresinizle aynı olamaz."),
    ({"eposta": "gecersiz"}, "Geçerli bir e-posta adresi yazın."),
    ({"eposta": "a@b"}, "Geçerli bir e-posta adresi yazın."),
    ({"eposta": "x" * 140 + "@ornek.test"}, "Geçerli bir e-posta adresi yazın."),
    ({"ad_soyad": "A"}, "Ad soyad 3-120 karakter olmalıdır."),
    ({"ad_soyad": "<script>"}, "Ad soyad geçersiz karakter içeriyor."),
    ({"ad_soyad": "Ay\x00se"}, "Ad soyad geçersiz karakter içeriyor."),
    ({"paket": "hepsi"}, "Paket seçin."),
    ({"paket": ["rpa"]}, "Paket seçin."),
    ({"onay": False}, "Başvuru için bilgilendirme metnini onaylamanız gerekir."),
    ({"onay": "true"}, "Başvuru için bilgilendirme metnini onaylamanız gerekir."),
    ({"telefon": "abc"}, "Telefonu rakamlarla yazın (ör. +90 5xx xxx xx xx)."),
    ({"fazla": 1}, "İstek alanları geçersiz."),
])
def test_invalid_applications_are_refused_without_storing(client, core, changes, message):
    response = apply(client, **changes)
    assert response.status_code == 400 and response.get_json() == {"success": False, "kod": "GECERSIZ", "message": message}
    assert stored(core) == [] and core.notifier.messages == []


def test_existing_accounts_open_applications_and_bots_get_the_same_answer_without_a_new_row(client, core):
    make_user(core, "Calisan@Firma.test", durum="pasif")  # a passive account counts as existing too
    assert apply(client).status_code == 201
    first_hash = stored(core)[0]["sifre_hash"]
    second = apply(client, ad_soyad="Başka Kişi", eposta="AYSE@ornek.test", sifre="Baska2026")
    existing = apply(client, eposta="calisan@firma.test")
    bot = apply(client, eposta="bot@ornek.test", web_sitesi="http://spam.test")
    fresh = apply(client, eposta="yeni@ornek.test")
    assert second.get_json() == existing.get_json() == bot.get_json() == fresh.get_json() == {"success": True,
                                                                                                "message": RECEIVED}
    assert sorted(row["eposta"] for row in stored(core)) == ["ayse@ornek.test", "yeni@ornek.test"]
    # The first application's password stays; only new rows notify the owner.
    assert stored(core)[0]["sifre_hash"] == first_hash and len(core.notifier.messages) == 2
    # The bot was not even counted: four counted applications from this address.
    with core.db.transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM hesap_izleri WHERE tur = 'basvuru'")
        assert cur.fetchone()["n"] == 4


def test_applications_are_limited_per_address_and_per_day(client, core):
    for index in range(5):
        assert apply(client, eposta=f"kisi{index}@ornek.test").status_code == 201
    refused = apply(client, eposta="altinci@ornek.test")
    assert refused.status_code == 429 and refused.get_json() == {
        "success": False, "kod": "COK_DENEME", "message": "Bu bağlantıdan çok fazla başvuru yapıldı. Daha sonra tekrar deneyin."}
    assert apply(client, address="10.0.0.2", eposta="diger@ornek.test").status_code == 201
    core.clock.advance(hours=2)
    assert apply(client, eposta="sonra@ornek.test").status_code == 201
    # Twenty a day per address.
    for index in range(14):
        core.clock.advance(hours=1)
        assert apply(client, eposta=f"gun{index}@ornek.test").status_code == 201
    core.clock.advance(hours=1)
    assert apply(client, eposta="yirmibirinci@ornek.test").status_code == 429


def test_the_whole_service_takes_at_most_three_hundred_applications_a_day(client, core):
    with core.db.transaction() as cur:
        for index in range(300):
            cur.execute("INSERT INTO hesap_izleri (tur, anahtar, zaman) VALUES ('basvuru', %s, %s)",
                        (f"adres:10.1.{index // 250}.{index % 250}", core.clock.now() - timedelta(hours=23)))
    busy = apply(client)
    assert busy.status_code == 503 and busy.get_json() == {
        "success": False, "kod": "YOGUNLUK", "message": "Başvurular şu anda alınamıyor. Lütfen daha sonra tekrar deneyin."}
    core.clock.advance(hours=1, seconds=1)
    assert apply(client).status_code == 201


def test_old_applications_and_traces_are_forgotten(client, core):
    assert apply(client).status_code == 201
    assert apply(client, eposta="red@ornek.test").status_code == 201
    with core.db.transaction() as cur:
        cur.execute("UPDATE basvurular SET durum = 'reddedildi', sifre_hash = '' WHERE eposta = 'red@ornek.test'")
        cur.execute("INSERT INTO basvurular (id, ad_soyad, eposta, sifre_hash, paket, durum, aydinlatma_surumu, olusturma) "
                    "VALUES (%s, 'Onaylı Kişi', 'onayli@ornek.test', '', 'rpa', 'onaylandi', '2026-10-18', %s)",
                    ("e" * 32, core.clock.now()))
    core.clock.advance(days=90, seconds=1)
    assert apply(client, eposta="yeni@ornek.test").status_code == 201
    # Waiting and refused applications go after 90 days; an approved one is the account's audit trail and stays.
    assert sorted(row["eposta"] for row in stored(core)) == ["onayli@ornek.test", "yeni@ornek.test"]
    with core.db.transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM hesap_izleri")
        assert cur.fetchone()["n"] == 1  # traces older than two days went with it


def test_a_failing_notice_keeps_the_application(client, core):
    class Broken:
        def send(self, subject, text):
            raise RuntimeError("telegram anahtarı")

    core.notifier = Broken()
    assert apply(client).status_code == 201 and len(stored(core)) == 1
    core.notifier = None
    assert apply(client, eposta="sessiz@ornek.test").status_code == 201 and len(stored(core)) == 2


def test_without_a_notifier_the_notice_goes_through_the_support_channels(client, core, monkeypatch):
    """Üretimde core.notifier yoktur: bildirim destek talepleriyle aynı ayar dosyasından (Telegram, e-posta) gider."""
    sent = []

    class Channels:
        def __init__(self, settings):
            self.settings = settings

        def send(self, subject, text):
            sent.append((subject, text, self.settings()))

    monkeypatch.setattr(notify, "Notifier", Channels)
    monkeypatch.setattr(notify, "read_settings", lambda path: {"dosya": str(path)})
    core.notifier = None
    assert apply(client).status_code == 201
    assert sent == [("Yeni Peyrev başvurusu", "Ayşe Yılmaz · Peyrev\nOnay bekliyor: https://peyrev.com/yonetim/",
                     {"dosya": str(core.settings.support_settings_path)})]


def test_an_address_that_has_an_account_is_hashed_like_a_new_application(client, core, monkeypatch):
    """Yanıt süresi de adresin hesabı olduğunu belli etmez: şifre her başvuruda bir kez özetlenir."""
    hashed = []
    original = applications.generate_password_hash
    monkeypatch.setattr(applications, "generate_password_hash", lambda value: hashed.append(1) or original(value))
    make_user(core, "hesapli@ornek.test")
    for address in ("hesapli@ornek.test", "yeni@ornek.test", "yeni@ornek.test"):
        hashed.clear()
        assert apply(client, eposta=address).status_code == 201 and hashed == [1], address
    assert [row["eposta"] for row in stored(core)] == ["yeni@ornek.test"]


def test_application_errors_never_carry_database_details(client, core, monkeypatch):
    def broken():
        raise RuntimeError("GIZLI-DB-SIFRESI")

    monkeypatch.setattr(core.db, "transaction", broken)
    response = apply(client)
    assert response.status_code == 503 and response.get_json()["kod"] == "SUNUCU"
    assert "GIZLI" not in response.get_data(as_text=True)
