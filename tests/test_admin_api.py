"""Yönetim paneli API'si (/api/yonetim/*): her uçta oturum, CSRF ve yönetici kapısı; doğru çağrının sonucu ve
yonetim_olaylari kaydı; hatalı girdi. Hesaplar, başvurular, destek talepleri, ajan ofisleri ve ortak model işleri
gerçek servislerle ve kendi uçlarından (Studio lisansı ve destek uçları, başvuru formu) kurulur."""

import base64
import json
import re
from datetime import date, timedelta

import pytest
from conftest import DEVICE, START, make_user, open_session
from test_accounts import login as studio_login
from test_accounts_applications import apply
from test_admin_auth import CSRF, HEADERS, PASSWORD, panel_sessions, post, signed_in
from test_admin_auth import events as audit
from test_agents import API as AGENTS_API
from test_agents import Agents
from test_llm import payload as model_request
from test_llm_api import enable, submit
from werkzeug.security import generate_password_hash

from peyrev import tokens, users
from peyrev.accounts import applications
from peyrev.admin import events, routes, service
from peyrev.llm import admin as llm_admin
from peyrev.llm import jobs

FIELDS = "İstek alanları geçersiz."
PHONE = "Telefonu rakamlarla yazın (ör. +90 5xx xxx xx xx)."
SESSION_REQUIRED = {"success": False, "kod": "OTURUM_GEREKLI", "message": "Yönetim oturumu gerekli. Giriş yapın."}
SESSION_CLOSED = {"success": False, "kod": "OTURUM_GECERSIZ", "message": "Oturumunuz kapandı. Yeniden giriş yapın."}
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
FLOW = {"adimlar": [{"tur": "window.click", "parametreler": {"hedef": "***"}}]}
NEW_USER = {"giris": "ORNEK_7", "ad_soyad": "Örnek Yedi", "baslangic": "2026-10-26", "bitis": "2027-10-26"}


def get(client, path):
    return client.get("/api/yonetim/" + path)


def call(client, method, path, body=None, headers=None):
    """Panelin isteği: POST'lar panel başlığı ve kökenle gider; değeri None olan başlık gönderilmez."""
    sent = {key: value for key, value in {**(HEADERS if method == "POST" else {}), **(headers or {})}.items()
            if value is not None}
    if method == "GET":
        return client.get("/api/yonetim/" + path, headers=sent)
    return client.post("/api/yonetim/" + path, json=body, headers=sent)


def snapshot(core, *, sessions=True):
    """Bütün tabloların içeriği; `sessions=False` panel oturumlarını (son işlem anı her istekte ilerler) dışarıda bırakır."""
    skip = {"schema_migrations"} | (set() if sessions else {"yonetim_oturumlari"})
    with core.db.transaction() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
        names = [row["tablename"] for row in cur.fetchall() if row["tablename"] not in skip]
        state = {}
        for name in names:
            cur.execute(f'SELECT * FROM "{name}"')
            state[name] = sorted(repr(row) for row in cur.fetchall())
    return state


def sql(core, statement, parameters=()):
    with core.db.transaction() as cur:
        cur.execute(statement, parameters)
        return cur.fetchall() if cur.description else cur.rowcount


def studio_ticket(core, user, session, client, **changes):
    """Studio'nun "Öneri ve destek" düğmesinin gönderdiği talep (lisans yanıtındaki destek belirteciyle)."""
    body = {"tur": "hata", "baslik": "Pencerede tıklama durdu", "metin": "Hedef bulunamadı uyarısı çıktı.",
            "adim": "window.click", "surum": "1.0.0", "platform": "Windows", **changes}
    response = client.post("/api/rpa/destek/talepler", json=body, headers=support_headers(core, user, session))
    assert response.status_code == 201, response.get_json()
    return response.get_json()["talep"]


def support_headers(core, user, session):
    return {"Authorization": "Bearer " + tokens.issue(core, "destek", user, DEVICE, session)}


def pending(core, email):
    [row] = sql(core, "SELECT id FROM basvurular WHERE eposta = %s AND durum = 'bekliyor'", (email,))
    return row["id"]


def later(core, **delta):
    """Saatten bu kadar sonrası, olay ayrıntısındaki biçimde (UTC, ISO 8601)."""
    return (core.clock.now() + timedelta(**delta)).isoformat()


def new_events(core, since):
    """`since` kimliğinden sonra yazılan olaylar: (yönetici, tür, hedef, ayrıntı)."""
    return [(event["yonetici_id"], event["tur"], event["hedef"], event["ayrinti"]) for event in audit(core)
            if event["id"] > since]


def last_event_id(core):
    rows = sql(core, "SELECT coalesce(max(id), 0) AS n FROM yonetim_olaylari")
    return rows[0]["n"]


class World:
    """Bir yönetici; ajan lisanslı, Studio oturumu açık, bir ofisi ve ekli bir destek talebi olan kullanıcı; bekleyen
    bir başvuru."""

    def __init__(self, core, app):
        self.core, self.app = core, app
        self.agents = Agents(core)
        self.manager = make_user(core, "yonetici@peyrev.test", rol="yonetici", ad_soyad="Yönetici Kişi")
        self.ayse = self.agents.account("ayse@ornek.test", ad_soyad="Ayşe Yılmaz")
        self.session = self.agents.sessions[self.ayse["id"]]
        self.office = self.agents.office(self.ayse, "Satış Ofisi")
        self.ticket = studio_ticket(core, self.ayse, self.session, app.test_client(), ekler=[
            {"tur": "akis", "icerik": FLOW},
            {"tur": "gorsel", "ad": "ekran görüntüsü.png", "bicim": "image/png",
             "veri": base64.b64encode(PNG).decode()}])
        assert apply(app.test_client(), ad_soyad="Mehmet Kaya", eposta="mehmet@ornek.test").status_code == 201
        self.application = pending(core, "mehmet@ornek.test")
        self.video = sql(core, "INSERT INTO egitim_videolari (baslik, video_kimligi) VALUES ('İlk akış', "
                               "'dQw4w9WgXcQ') RETURNING id")[0]["id"]
        self.topic = sql(core, "INSERT INTO topluluk_konulari (kategori, baslik, metin, yazar_id, olusturma, son_hareket) "
                               "VALUES ('soru', 'Bir soru', 'Metin', %s, now(), now()) RETURNING id",
                         (self.ayse["id"],))[0]["id"]
        self.comment = sql(core, "INSERT INTO topluluk_yorumlari (konu_id, yazar_id, metin, olusturma) VALUES "
                                 "(%s, %s, 'Yorum', now()) RETURNING id", (self.topic, self.ayse["id"]))[0]["id"]
        self.message = sql(core, "INSERT INTO iletisim_mesajlari (ad, eposta, konu, mesaj) VALUES ('Ali Veli', "
                                 "'ali@ornek.test', 'satis', 'Fiyat hakkında bir sorum var.') RETURNING id")[0]["id"]
        self.ids = {"user": self.ayse["id"], "office": self.office, "ticket": self.ticket["no"],
                    "attachment": self.ticket["ekler"][0]["id"], "application": self.application,
                    "module": users.AGENTS, "video": self.video, "topic": self.topic, "comment": self.comment,
                    "message": self.message}

    def path(self, template):
        return template.format(**self.ids)


@pytest.fixture
def world(core, app):
    return World(core, app)


@pytest.fixture
def browser(app, world):
    """Yöneticinin oturumu açık tarayıcısı."""
    return signed_in(app)


# ----- her uçta oturum, CSRF ve yönetici kapısı ------------------------------------------------------------------
ENDPOINTS = [
    ("GET", "ben", None),
    ("POST", "cikis", {}),
    ("POST", "oturumlar/digerlerini-kapat", {}),
    ("POST", "iki-adim/baslat", {"sifre": PASSWORD}),
    ("POST", "iki-adim/onayla", {"kod": "123456"}),
    ("POST", "iki-adim/kapat", {"sifre": PASSWORD, "kod": "123456"}),
    ("GET", "ozet", None),
    ("GET", "ortak-model", None),
    ("POST", "ortak-model/kuyrugu-bosalt", {}),
    ("GET", "kullanicilar", None),
    ("POST", "kullanicilar", NEW_USER),
    ("GET", "kullanicilar/{user}", None),
    ("POST", "kullanicilar/{user}/bilgiler", {"ad_soyad": "Başka Ad"}),
    ("POST", "kullanicilar/{user}/rol", {"rol": "yonetici"}),
    ("POST", "kullanicilar/{user}/durum", {"durum": "pasif"}),
    ("POST", "kullanicilar/{user}/moduller/{module}", {"bitis": ""}),
    ("POST", "kullanicilar/{user}/moduller/{module}/kaldir", {}),
    ("POST", "kullanicilar/{user}/sifre-kodu", {}),
    ("POST", "kullanicilar/{user}/sifre", {"sifre": "Yeni12345"}),
    ("POST", "kullanicilar/{user}/indirme", {"eposta": False}),
    ("POST", "kullanicilar/{user}/sil", {"giris": "ayse@ornek.test"}),
    ("POST", "kullanicilar/{user}/oturumlari-kapat", {}),
    ("POST", "kullanicilar/{user}/iki-adim-sifirla", {}),
    ("POST", "ofisler/{office}/durum", {"durum": "pasif"}),
    ("POST", "kullanicilar/{user}/ofis-siniri", {"sinir": 3}),
    ("POST", "kullanicilar/{user}/ofisler", {"ad": "Yeni Müşteri"}),
    ("GET", "basvurular", None),
    ("POST", "basvurular/{application}/onayla", {"bitis": "2027-10-26"}),
    ("POST", "basvurular/{application}/reddet", {}),
    ("GET", "destek", None),
    ("GET", "destek/{ticket}", None),
    ("POST", "destek/{ticket}/yanit", {"metin": "İnceliyoruz."}),
    ("GET", "destek/{ticket}/ek/{attachment}", None),
    ("GET", "olaylar", None),
    ("GET", "egitim", None),
    ("POST", "egitim", {"baglanti": "https://youtu.be/dQw4w9WgXcQ", "baslik": "Yeni video"}),
    ("POST", "egitim/{video}", {"baglanti": "https://youtu.be/dQw4w9WgXcQ", "baslik": "Değişen video"}),
    ("POST", "egitim/{video}/sil", {}),
    ("GET", "topluluk", None),
    ("GET", "topluluk/{topic}", None),
    ("POST", "topluluk/{topic}", {"sabit": True}),
    ("POST", "topluluk/{topic}/sil", {}),
    ("POST", "topluluk/yorum/{comment}", {"gizli": True}),
    ("POST", "topluluk/yorum/{comment}/sil", {}),
    ("GET", "iletisim", None),
    ("POST", "iletisim/{message}", {"okundu": True}),
    ("POST", "iletisim/{message}/sil", {}),
    ("GET", "ziyaretler", None),
]
POSTS = [endpoint for endpoint in ENDPOINTS if endpoint[0] == "POST"]


def test_the_gate_tests_cover_every_panel_endpoint(app):
    listed = {(method, re.sub(r"\{\w+\}", "*", path)) for method, path, _body in ENDPOINTS}
    served = {(method, re.sub(r"<[^>]+>", "*", rule.rule.removeprefix(routes.PREFIX + "/")))
              for rule in app.url_map.iter_rules() if rule.rule.startswith(routes.PREFIX + "/")
              for method in rule.methods & {"GET", "POST"}}
    assert served - listed == {("POST", "giris")}  # the only endpoint without a session
    assert listed <= served


def test_every_endpoint_needs_a_panel_session(app, core, world):
    before = snapshot(core)
    # A Studio token is no panel session either.
    studio = (support_headers(core, world.ayse, world.session),
              {"Authorization": "Bearer " + world.agents.token(world.ayse)})
    for method, path, body in ENDPOINTS:
        for headers in ({}, *studio):
            response = call(app.test_client(), method, world.path(path), body, headers)
            assert (response.status_code, response.get_json()) == (401, SESSION_REQUIRED), path
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert snapshot(core) == before


def test_state_changing_endpoints_need_the_panel_header_and_the_origin(app, core, world, browser):
    before = snapshot(core)
    for method, path, body in POSTS:
        for headers in ({"X-Peyrev-Yonetim": None}, {"X-Peyrev-Yonetim": "true"}, {"Origin": "https://evil.test"},
                        {"Origin": "https://peyrev.com.evil.test"}, {"Origin": None},
                        {"Origin": None, "Referer": "https://evil.test/yonetim/"}, {"Sec-Fetch-Site": "cross-site"}):
            response = call(browser, method, world.path(path), body, headers)
            assert (response.status_code, response.get_json()) == (403, CSRF), (path, headers)
    # Nothing changed, not even the idle time of the session, and the session still works.
    assert snapshot(core) == before
    assert get(browser, "ben").status_code == 200


def test_a_session_of_an_account_that_is_no_longer_an_active_administrator_is_refused(app, core, world):
    former = make_user(core, "eski.yonetici@peyrev.test", rol="yonetici")
    for method, path, body in ENDPOINTS:
        for change in ("rol = 'kullanici'", "durum = 'pasif'"):
            browser = signed_in(app, "eski.yonetici@peyrev.test")
            sql(core, f"UPDATE kullanicilar SET {change} WHERE id = %s", (former["id"],))
            before = snapshot(core, sessions=False)
            response = call(browser, method, world.path(path), body)
            assert (response.status_code, response.get_json()) == (401, SESSION_CLOSED), (path, change)
            assert snapshot(core, sessions=False) == before
            assert panel_sessions(core, former["id"]) == []  # the session is gone for good
            sql(core, "UPDATE kullanicilar SET rol = 'yonetici', durum = 'aktif' WHERE id = %s", (former["id"],))


BAD_INPUT = [
    ("POST", "ortak-model/kuyrugu-bosalt", {"adet": 1}, FIELDS),
    ("GET", "kullanicilar?sayfa=0", None, "Sayfa geçersiz."),
    ("GET", "kullanicilar?sayfa=iki", None, "Sayfa geçersiz."),
    ("GET", "kullanicilar?sayfa=%C2%B2", None, "Sayfa geçersiz."),  # "²" is a digit to str.isdigit()
    ("GET", "kullanicilar?sayfa=100001", None, "Sayfa geçersiz."),
    ("GET", "kullanicilar?durum=silindi", None, "Hesap durumu geçersiz."),
    ("GET", "kullanicilar?ara=" + "a" * 151, None, "Arama metni en fazla 150 karakter olabilir."),
    ("POST", "kullanicilar", {**NEW_USER, "giris": "a b"},
     "Kullanıcı adı 3-64 karakter (harf, rakam, . _ + -) ya da geçerli bir e-posta olmalıdır."),
    ("POST", "kullanicilar", {**NEW_USER, "rol": "patron"}, "Rol geçersiz."),
    ("POST", "kullanicilar", {**NEW_USER, "rol": None}, "Rol geçersiz."),
    ("POST", "kullanicilar", {**NEW_USER, "ad_soyad": "B"}, "Ad soyad 2-120 karakter olmalıdır."),
    ("POST", "kullanicilar", {**NEW_USER, "ajan": "evet"}, "Ajanlar seçimi geçersiz."),
    ("POST", "kullanicilar", {**NEW_USER, "telefon": "yok"}, PHONE),
    ("POST", "kullanicilar", {**NEW_USER, "bitis": "26.10.2027"}, "Bitiş YYYY-AA-GG biçiminde olmalıdır."),
    ("POST", "kullanicilar", {**NEW_USER, "baslangic": "2026-10-27"}, "Lisans dönemi bugünü kapsamalıdır."),
    ("POST", "kullanicilar", {**NEW_USER, "baslangic": "2027-11-01"}, "Başlangıç bitişten sonra olamaz."),
    ("POST", "kullanicilar", {key: value for key, value in NEW_USER.items() if key != "bitis"}, FIELDS),
    ("POST", "kullanicilar", {**NEW_USER, "onizleme": True}, FIELDS),
    ("POST", "kullanicilar/{user}/bilgiler", {"ad_soyad": "A"}, "Ad soyad 2-120 karakter olmalıdır."),
    ("POST", "kullanicilar/{user}/bilgiler", {"ad_soyad": "<b>Ayşe</b>"}, "Ad soyad geçersiz karakter içeriyor."),
    ("POST", "kullanicilar/{user}/bilgiler", {"ad_soyad": "Ayşe Yılmaz", "telefon": "yok"}, PHONE),
    ("POST", "kullanicilar/{user}/bilgiler", {"telefon": "+90 532 000 00 00"}, FIELDS),
    ("POST", "kullanicilar/{user}/rol", {"rol": "patron"}, "Rol geçersiz."),
    ("POST", "kullanicilar/{user}/rol", {"rol": ["yonetici"]}, "Rol geçersiz."),
    ("POST", "kullanicilar/{user}/rol", {}, FIELDS),
    ("POST", "kullanicilar/{user}/durum", {"durum": "silindi"}, "Hesap durumu geçersiz."),
    ("POST", "kullanicilar/{user}/durum", {}, FIELDS),
    ("POST", "kullanicilar/{user}/moduller/MOD_WMS", {"bitis": ""}, "Modül geçersiz."),
    ("POST", "kullanicilar/{user}/moduller/{module}", {"bitis": "2026-10-25"},
     "Bitiş bugünden önce olamaz; erişimi kapatmak için bugünü seçin."),
    ("POST", "kullanicilar/{user}/moduller/{module}", {"bitis": "2027-02-30"}, "Bitiş geçersiz."),
    ("POST", "kullanicilar/{user}/moduller/{module}", {"baslangic": "2027-03-01", "bitis": "2027-02-01"},
     "Başlangıç bitişten sonra olamaz."),
    ("POST", "kullanicilar/{user}/moduller/{module}", {"sure": 30}, FIELDS),
    ("POST", "kullanicilar/{user}/moduller/MOD_WMS/kaldir", {}, "Modül geçersiz."),
    ("POST", "kullanicilar/{user}/moduller/{module}/kaldir", {"neden": "x"}, FIELDS),
    ("POST", "kullanicilar/{user}/sifre-kodu", {"saat": 0}, "Kod süresi geçersiz."),
    ("POST", "kullanicilar/{user}/sifre-kodu", {"saat": 169}, "Kod süresi geçersiz."),
    ("POST", "kullanicilar/{user}/sifre-kodu", {"saat": "24"}, "Kod süresi geçersiz."),
    ("POST", "kullanicilar/{user}/sifre-kodu", {"saat": 24.0}, "Kod süresi geçersiz."),
    ("POST", "kullanicilar/{user}/sifre-kodu", {"saat": True}, "Kod süresi geçersiz."),
    ("POST", "kullanicilar/{user}/oturumlari-kapat", {"hepsi": True}, FIELDS),
    ("POST", "kullanicilar/{user}/iki-adim-sifirla", {"neden": "x"}, FIELDS),
    ("POST", "ofisler/KISA/durum", {"durum": "pasif"}, "Ofis kimliği geçersiz."),
    ("POST", "ofisler/{office}/durum", {"durum": "kapali"}, "Ofis durumu 'aktif' ya da 'pasif' olmalı."),
    ("POST", "ofisler/{office}/durum", {}, FIELDS),
    ("GET", "basvurular?durum=hepsi", None, "Başvuru durumu geçersiz."),
    ("POST", "basvurular/{application}/onayla", {}, FIELDS),
    ("POST", "basvurular/{application}/onayla", {"bitis": "2026-10-25"}, "Bitiş bugünden ve başlangıçtan önce olamaz."),
    ("POST", "basvurular/{application}/onayla", {"baslangic": "2027-11-01", "bitis": "2027-10-26"},
     "Bitiş bugünden ve başlangıçtan önce olamaz."),
    ("POST", "basvurular/{application}/onayla", {"bitis": "2027-10-26", "paket": "hepsi"}, "Paket geçersiz."),
    ("POST", "basvurular/{application}/onayla", {"bitis": "2027-10-26", "not": "x" * 301},
     "Not en fazla 300 karakter olabilir."),
    ("POST", "basvurular/{application}/reddet", {"not": 5}, "Not en fazla 300 karakter olabilir."),
    ("POST", "basvurular/{application}/reddet", {"gerekce": "x"}, FIELDS),
    ("GET", "destek?durum=bitti", None, "Durum süzgeci geçersiz."),
    ("GET", "destek?tur=sikayet", None, "Tür süzgeci geçersiz."),
    ("POST", "destek/{ticket}/yanit", {}, "Yanıt yazın veya durumu değiştirin."),
    ("POST", "destek/{ticket}/yanit", {"durum": "acik"}, "Yanıt yazın veya durumu değiştirin."),
    ("POST", "destek/{ticket}/yanit", {"durum": "bitti"}, "Durum geçersiz."),
    ("POST", "destek/{ticket}/yanit", {"durum": "yayinlandi"},
     "Yayınlandı durumu için sürüm numarası yazın (ör. 0.9.53)."),
    ("POST", "destek/{ticket}/yanit", {"durum": "yayinlandi", "yayin_surumu": "yarın"},
     "Yayınlandı durumu için sürüm numarası yazın (ör. 0.9.53)."),
    ("POST", "destek/{ticket}/yanit", {"metin": "x" * 5001}, "Yanıt en fazla 5000 karakter olabilir."),
    ("POST", "destek/{ticket}/yanit", {"metin": 5}, "Yanıt geçersiz."),
    ("POST", "destek/{ticket}/yanit", {"metin": "Tamam\x00"}, "Yanıt geçersiz karakter içeriyor."),
    ("POST", "destek/{ticket}/yanit", {"metin": "Tamam", "ek": "x"}, FIELDS),
    ("GET", "olaylar?tur=Giris", None, "Olay türü geçersiz."),
    ("GET", "olaylar?hedef=kullanici:1%20OR%201=1", None, "Olay hedefi geçersiz."),
    ("GET", "olaylar?sayfa=-1", None, "Sayfa geçersiz."),
]


def test_invalid_input_is_refused_and_changes_nothing(core, world, browser):
    before = snapshot(core, sessions=False)
    for method, path, body, message in BAD_INPUT:
        response = call(browser, method, world.path(path), body)
        answer = response.get_json()
        assert (response.status_code, answer["message"]) == (400, message), (path, body)
        assert answer["kod"] in ("GECERSIZ", "GECERSIZ_GIRDI") and answer["success"] is False
    assert snapshot(core, sessions=False) == before


def test_malformed_and_oversized_bodies_are_refused(core, world, browser):
    before = snapshot(core, sessions=False)
    for _method, path, _body in POSTS:
        url = "/api/yonetim/" + world.path(path)
        for raw in (b"", b"{", b"[]", b'"metin"', b'{"a": 1, "a": 2}', b'{"kod": NaN}', b"\xff\xfe"):
            response = browser.post(url, data=raw, content_type="application/json", headers=HEADERS)
            assert (response.status_code, response.get_json()) == (400, {"success": False, "kod": "GECERSIZ",
                                                                         "message": FIELDS}), (path, raw)
        limit = routes.REPLY_LIMIT if path.endswith("/yanit") else 8 * 1024
        huge = browser.post(url, data=b" " * (limit + 1), content_type="application/json", headers=HEADERS)
        assert (huge.status_code, huge.get_json()["kod"]) == (413, "GOVDE_SINIRI"), path
    assert snapshot(core, sessions=False) == before


def test_unknown_records_are_not_found(core, world, browser):
    before = snapshot(core, sessions=False)
    for method, path, body, message in (
            ("GET", "kullanicilar/999999", None, "Hesap bulunamadı."),
            ("POST", "kullanicilar/999999/bilgiler", {"ad_soyad": "Kimse"}, "Hesap bulunamadı."),
            ("POST", "kullanicilar/999999/rol", {"rol": "yonetici"}, "Hesap bulunamadı."),
            ("POST", "kullanicilar/999999/durum", {"durum": "pasif"}, "Hesap bulunamadı."),
            ("POST", "kullanicilar/999999/moduller/MOD_RPA", {"bitis": ""}, "Hesap bulunamadı."),
            ("POST", "kullanicilar/999999/moduller/MOD_RPA/kaldir", {}, "Hesap bulunamadı."),
            ("POST", "kullanicilar/999999/sifre-kodu", {}, "Hesap bulunamadı."),
            ("POST", "kullanicilar/999999/oturumlari-kapat", {}, "Hesap bulunamadı."),
            ("POST", "kullanicilar/{user}/oturumlari-kapat", {"oturum_id": "gecersiz"}, "Oturum bulunamadı."),
            ("POST", "kullanicilar/999999/iki-adim-sifirla", {}, "Hesap bulunamadı."),
            ("POST", "ofisler/" + "f" * 32 + "/durum", {"durum": "pasif"}, "Ofis bulunamadı."),
            ("POST", "basvurular/" + "a" * 32 + "/onayla", {"bitis": "2027-10-26"}, "Başvuru bulunamadı."),
            ("POST", "basvurular/gecersiz/onayla", {"bitis": "2027-10-26"}, "Başvuru bulunamadı."),
            ("POST", "basvurular/" + "a" * 32 + "/reddet", {}, "Bekleyen başvuru bulunamadı."),
            ("GET", "destek/9999", None, "Talep bulunamadı."),
            ("GET", "destek/0", None, "Talep bulunamadı."),
            ("GET", "destek/99999999999", None, "Talep bulunamadı."),
            ("POST", "destek/9999/yanit", {"metin": "Merhaba"}, "Talep bulunamadı."),
            ("GET", "destek/{ticket}/ek/" + "0" * 32, None, "Ek bulunamadı."),
            ("GET", "destek/{ticket}/ek/gecersiz", None, "Ek bulunamadı."),
            ("GET", "destek/9999/ek/{attachment}", None, "Talep bulunamadı.")):
        response = call(browser, method, world.path(path), body)
        assert (response.status_code, response.get_json()) == (404, {
            "success": False, "kod": "BULUNAMADI", "message": message}), path
    assert snapshot(core, sessions=False) == before
    # An attachment is reached only through its own ticket.
    other = studio_ticket(core, world.ayse, world.session, world.app.test_client(), baslik="Başka talep")
    assert get(browser, f"destek/{other['no']}/ek/{world.ids['attachment']}").get_json()["kod"] == "BULUNAMADI"
    assert get(browser, "kullanicilar/abc").get_json()["kod"] == "BULUNAMADI"
    assert get(browser, "yok").status_code == 404
    assert get(browser, "cikis").status_code == 405


# ----- özet ve ortak model -------------------------------------------------------------------------------------
def test_the_summary_counts_accounts_rights_applications_and_unread_tickets(app, core, world, browser):
    ali = make_user(core, "ali@ornek.test", modules={users.RPA: date(2026, 11, 5),
                                                     users.AGENTS: date(2026, 10, 26)}, ad_soyad="Ali Veli")
    make_user(core, "veli@ornek.test", modules={users.RPA: date(2026, 10, 25)})  # ended yesterday
    burak = make_user(core, "burak@ornek.test", modules={users.RPA: date(2026, 11, 25)}, ad_soyad="Burak Can")
    make_user(core, "ahmet@ornek.test", modules={users.RPA: date(2026, 11, 26)})  # 31 days: not yet
    make_user(core, "eski@ornek.test", durum="pasif", modules={users.RPA: date(2026, 10, 30)})
    # An application older than the keeping period is not waiting any more.
    assert apply(app.test_client(), eposta="unutulan@ornek.test").status_code == 201
    sql(core, "UPDATE basvurular SET olusturma = %s WHERE eposta = 'unutulan@ornek.test'",
        (START - applications.KEEP_APPLICATIONS - timedelta(seconds=1),))
    response = get(browser, "ozet")
    assert response.status_code == 200
    assert response.get_json() == {
        "success": True, "aktif_kullanici": 6, "lisansli_kullanici": 5, "yakinda_bitecek": 2,
        "bitecekler": [
            {"kullanici_id": ali["id"], "giris": "ali@ornek.test", "ad_soyad": "Ali Veli", "modul": users.AGENTS,
             "bitis": "2026-10-26", "kalan_gun": 0},
            {"kullanici_id": ali["id"], "giris": "ali@ornek.test", "ad_soyad": "Ali Veli", "modul": users.RPA,
             "bitis": "2026-11-05", "kalan_gun": 10},
            {"kullanici_id": burak["id"], "giris": "burak@ornek.test", "ad_soyad": "Burak Can", "modul": users.RPA,
             "bitis": "2026-11-25", "kalan_gun": 30}],
        "bekleyen_basvuru": 1, "okunmamis_destek": 1, "okunmamis_iletisim": 1}
    # Opening the ticket takes it off the badge; reading changes nothing else and records nothing.
    before = last_event_id(core)
    assert get(browser, f"destek/{world.ticket['no']}").status_code == 200
    assert get(browser, "ozet").get_json()["okunmamis_destek"] == 0
    assert new_events(core, before) == []


def test_the_shared_model_status_and_clearing_its_queue(core, world, browser):
    off = get(browser, "ortak-model")
    assert off.status_code == 200 and off.get_json() == {"success": True, **llm_admin.status(core)}
    assert off.get_json()["enabled"] is False and off.get_json()["ayar"]["dosya"] is False
    config = enable(core)
    other = make_user(core, "ali@ornek.test", modules={users.RPA: None, users.AGENTS: None})
    for owner in ((world.ayse["id"], world.session), (other["id"], "1" * 32)):
        submit(core, owner, model_request(), config)
    with core.db.transaction() as cur:
        jobs.claim(cur, core.clock.now())
    running = get(browser, "ortak-model").get_json()
    assert running["enabled"] is True and running["isler"]["queued"] == 1 and running["isler"]["running"] == 1
    before = last_event_id(core)
    assert post(browser, "ortak-model/kuyrugu-bosalt").get_json() == {"success": True, "iptal": 2}
    assert get(browser, "ortak-model").get_json()["isler"] == {"queued": 0, "running": 0, "succeeded": 0,
                                                               "failed": 0, "cancelled": 2}
    assert post(browser, "ortak-model/kuyrugu-bosalt").get_json() == {"success": True, "iptal": 0}
    assert new_events(core, before) == [(world.manager["id"], "llm_kuyruk_bosaltma", "llm_isler", {"adet": 2}),
                                        (world.manager["id"], "llm_kuyruk_bosaltma", "llm_isler", {"adet": 0})]
    assert sql(core, "SELECT count(*) AS n FROM llm_isler WHERE istek IS NOT NULL")[0]["n"] == 0


# ----- kullanıcılar --------------------------------------------------------------------------------------------
def test_the_user_list_searches_filters_and_pages(core, world, browser):
    digest = generate_password_hash(PASSWORD)  # one digest for the whole crowd keeps the test fast
    with core.db.transaction() as cur:
        for index in range(50):
            cur.execute("INSERT INTO kullanicilar (giris, ad_soyad, sifre_hash, durum) VALUES (%s, %s, %s, %s)",
                        (f"kisi{index:02d}@kalabalik.test", f"Kişi {index:02d}", digest,
                         "pasif" if index % 10 == 0 else "aktif"))
    first = get(browser, "kullanicilar").get_json()
    assert (first["toplam"], first["sayfa"], first["sayfa_boyu"], len(first["kullanicilar"])) == (52, 1, 50, 50)
    second = get(browser, "kullanicilar?sayfa=2").get_json()
    # Newest first; the second page holds the two oldest accounts.
    assert [row["giris"] for row in second["kullanicilar"]] == ["ayse@ornek.test", "yonetici@peyrev.test"]
    assert second["sayfa"] == 2 and get(browser, "kullanicilar?sayfa=3").get_json()["kullanicilar"] == []
    ayse = second["kullanicilar"][0]
    ayse.pop("acilis")
    assert ayse == {"kullanici_id": world.ayse["id"], "giris": "ayse@ornek.test", "ad_soyad": "Ayşe Yılmaz",
                    "telefon": None, "rol": "kullanici", "durum": "aktif", "kaynak": "yonetim", "oturum_acik": True,
                    "rpa": "aktif", "ofis_siniri": 50, "moduller": {users.AGENTS: {"baslangic": None, "bitis": None},
                                                 users.RPA: {"baslangic": None, "bitis": None}}}
    assert second["kullanicilar"][1]["rpa"] == "aktif" and second["kullanicilar"][1]["moduller"] == {}
    found = get(browser, "kullanicilar?ara=KISI0").get_json()
    assert found["toplam"] == 10 and {row["giris"] for row in found["kullanicilar"]} == {
        f"kisi0{index}@kalabalik.test" for index in range(10)}
    assert get(browser, "kullanicilar?ara=Yılmaz").get_json()["toplam"] == 1  # the name is searched too
    assert get(browser, "kullanicilar?ara=%25").get_json()["toplam"] == 0  # LIKE wildcards are plain text
    assert get(browser, "kullanicilar?durum=pasif").get_json()["toplam"] == 5
    assert get(browser, "kullanicilar?durum=aktif&ara=kalabalik").get_json()["toplam"] == 45
    assert get(browser, "kullanicilar?ara=&durum=&sayfa=").get_json()["toplam"] == 52  # empty means no filter


def test_a_new_account_gets_a_first_code_that_is_shown_once(app, core, world, browser):
    response = post(browser, "kullanicilar", {**NEW_USER, "telefon": "+90 532 111 22 33", "ajan": True})
    assert response.status_code == 201
    data = response.get_json()
    user_id = data["kullanici"]["kullanici_id"]
    assert data["kullanici"] == {"kullanici_id": user_id, "giris": "ORNEK_7", "rol": "kullanici"}
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", data["kod"]["kod"])
    assert data["kod"]["gecerlilik"] == "2026-11-02 12:00" and "uyari" not in data
    [(admin_id, _kind, target, details)] = [entry for entry in new_events(core, 0) if entry[1] == "kullanici_acildi"]
    assert (admin_id, target) == (world.manager["id"], f"kullanici:{user_id}")
    assert details == {"giris": "ORNEK_7", "ad_soyad": "Örnek Yedi", "moduller": [users.RPA, users.AGENTS],
                       "baslangic": "2026-10-26", "bitis": "2027-10-26", "kod_gecerlilik": later(core, days=7)}
    assert data["kod"]["kod"] not in json.dumps([event["ayrinti"] for event in audit(core)])
    # The person sets a password with the code and signs in to Studio.
    reset = app.test_client().post("/api/rpa/hesap/sifre-yenile", json={
        "kullanici": "ornek_7", "kod": data["kod"]["kod"], "yeni_sifre": "Ornek2026x"})
    assert reset.status_code == 200, reset.get_json()
    assert studio_login(app.test_client(), kullanici="ORNEK_7", sifre="Ornek2026x").status_code == 200
    # The same user name, or one that is the part before "@" of an address, signs in already: refused.
    for login in ("ornek_7", "AYSE"):
        taken = post(browser, "kullanicilar", {**NEW_USER, "giris": login})
        assert (taken.status_code, taken.get_json()) == (409, {
            "success": False, "kod": "HESAP_VAR",
            "message": "Bu kullanıcı adıyla bir hesap zaten var; başka bir ad seçin."})


def test_a_new_account_can_be_opened_with_a_password_the_administrator_chooses(app, core, world, browser):
    weak = post(browser, "kullanicilar", {**NEW_USER, "sifre": "kisa"})
    assert (weak.status_code, weak.get_json()["message"]) == (400, "Şifre 8-128 karakter olmalıdır.")
    assert sql(core, "SELECT id FROM kullanicilar WHERE giris = 'ORNEK_7'") == []
    response = post(browser, "kullanicilar", {**NEW_USER, "sifre": "Ornek2026x"})
    assert response.status_code == 201
    data = response.get_json()
    user_id = data["kullanici"]["kullanici_id"]
    assert "kod" not in data and sql(core, f"SELECT id FROM sifre_kodlari WHERE kullanici_id = {user_id}") == []
    assert studio_login(app.test_client(), kullanici="ORNEK_7", sifre="Ornek2026x").status_code == 200
    [details] = [entry[3] for entry in new_events(core, 0) if entry[1] == "kullanici_acildi"]
    assert details["sifre"] == "yonetici_belirledi" and "kod_gecerlilik" not in details
    assert "Ornek2026x" not in json.dumps([event["ayrinti"] for event in audit(core)])
    # An empty password field means "send a code", as before.
    assert "kod" in post(browser, "kullanicilar", {**NEW_USER, "giris": "ikinci", "sifre": ""}).get_json()


def test_the_administrator_sets_another_accounts_password(app, core, world, browser):
    path = f"kullanicilar/{world.ayse['id']}/sifre"
    code = post(browser, f"kullanicilar/{world.ayse['id']}/sifre-kodu", {}).get_json()["kod"]["kod"]
    for bad, message in (("kisa1", "Şifre 8-128 karakter olmalıdır."),
                         ("sadeceharf", "Şifrede en az bir harf ve bir rakam bulunmalıdır.")):
        answer = post(browser, path, {"sifre": bad})
        assert (answer.status_code, answer.get_json()["message"]) == (400, message), bad
    assert post(browser, path, {"sifre": "Yeni2026ab"}).get_json()["kullanici"] == {
        "kullanici_id": world.ayse["id"], "kapatilan_panel_oturumu": 0}
    assert studio_login(app.test_client(), kullanici="ayse@ornek.test", sifre=PASSWORD).status_code == 401
    assert studio_login(app.test_client(), kullanici="ayse@ornek.test", sifre="Yeni2026ab").status_code == 200
    # The code issued before is spent.
    assert app.test_client().post("/api/rpa/hesap/sifre-yenile", json={
        "kullanici": "ayse@ornek.test", "kod": code, "yeni_sifre": "Baska2026"}).status_code == 401
    [event] = [entry for entry in new_events(core, 0) if entry[1] == "sifre_belirlendi"]
    assert event == (world.manager["id"], "sifre_belirlendi", f"kullanici:{world.ayse['id']}",
                     {"kapatilan_panel_oturumu": 0})
    assert "Yeni2026ab" not in json.dumps([row["ayrinti"] for row in audit(core)])
    own = post(browser, f"kullanicilar/{world.manager['id']}/sifre", {"sifre": "Kendim2026"})
    assert (own.status_code, own.get_json()["message"]) == (400, "Kendi şifrenizi Hesabım bölümünden değiştirin.")


def test_a_new_administrator_account_is_opened_and_the_role_follows(app, core, world, browser, monkeypatch):
    response = post(browser, "kullanicilar", {**NEW_USER, "giris": "ikinci@peyrev.test", "rol": "yonetici"})
    assert response.status_code == 201
    created = response.get_json()
    assert created["kullanici"]["rol"] == "yonetici" and "uyari" not in created
    user_id = created["kullanici"]["kullanici_id"]
    assert [entry[1:] for entry in new_events(core, 0) if entry[2] == f"kullanici:{user_id}"] == [
        ("kullanici_acildi", f"kullanici:{user_id}", {"giris": "ikinci@peyrev.test", "ad_soyad": "Örnek Yedi",
                                                      "moduller": [users.RPA], "baslangic": "2026-10-26",
                                                      "bitis": "2027-10-26",
                                                      "kod_gecerlilik": later(core, days=7)}),
        ("rol_degisti", f"kullanici:{user_id}", {"onceki": "kullanici", "rol": "yonetici",
                                                 "kapatilan_panel_oturumu": 0})]
    assert app.test_client().post("/api/rpa/hesap/sifre-yenile", json={
        "kullanici": "ikinci@peyrev.test", "kod": created["kod"]["kod"], "yeni_sifre": "Ikinci2026"}).status_code == 200
    assert signed_in(app, "ikinci@peyrev.test", sifre="Ikinci2026").get("/api/yonetim/ozet").status_code == 200

    # If the role cannot be given, the account and its one-time code are still shown, with a warning.
    def refuse(*_args, **_options):
        raise RuntimeError("rol yazılamadı")

    monkeypatch.setattr(service, "set_role", refuse)
    warned = post(browser, "kullanicilar", {**NEW_USER, "giris": "ucuncu@peyrev.test", "rol": "yonetici"})
    assert warned.status_code == 201
    body = warned.get_json()
    assert body["uyari"] == routes.ROLE_NOT_SET and body["kullanici"]["rol"] == "kullanici"
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", body["kod"]["kod"])
    assert sql(core, "SELECT rol FROM kullanicilar WHERE giris = 'ucuncu@peyrev.test'") == [{"rol": "kullanici"}]


def test_the_user_detail_brings_rights_sessions_offices_tickets_and_the_application(app, core, world, browser):
    response = get(browser, f"kullanicilar/{world.ayse['id']}")
    assert response.status_code == 200
    data = response.get_json()
    account = data["kullanici"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", account.pop("acilis"))
    account.pop("guncelleme")
    assert account == {"kullanici_id": world.ayse["id"], "giris": "ayse@ornek.test", "ad_soyad": "Ayşe Yılmaz",
                       "telefon": None, "rol": "kullanici", "durum": "aktif", "kaynak": "yonetim", "iki_adim": False,
                       "rpa": "aktif", "moduller": {
                           users.AGENTS: {"baslangic": None, "bitis": None, "durum": "aktif"},
                           users.RPA: {"baslangic": None, "bitis": None, "durum": "aktif"}}}
    [session] = data["oturumlar"]
    assert (session["id"], session["cihaz"], session["surum"], session["ip"]) == (world.session, DEVICE[:8], "1.0.0",
                                                                                  "127.0.0.1")
    [office] = data["ofisler"]["liste"]
    assert data["ofisler"]["sinir"] == 50
    assert (office["id"], office["ad"], office["durum"], office["gorev"], office["onay"]) == (
        world.office, "Satış Ofisi", "aktif", 0, 0)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", office["olusturma"])
    assert data["destek"] == {"toplam": 1, "acik": 1} and data["basvuru"] is None
    # An account opened from an application shows it.
    approved = post(browser, f"basvurular/{world.application}/onayla", {"bitis": "2027-10-26"}).get_json()
    detail = get(browser, f"kullanicilar/{approved['hesap']['kullanici_id']}").get_json()
    assert detail["basvuru"] == {"id": world.application, "paket": "rpa", "olusturma": "2026-10-26 12:00",
                                 "karar": "2026-10-26 12:00"}
    assert detail["kullanici"]["kaynak"] == "basvuru" and detail["oturumlar"] == []
    assert detail["ofisler"]["liste"] == [] and detail["destek"] == {"toplam": 0, "acik": 0}
    assert detail["kullanici"]["moduller"] == {users.RPA: {"baslangic": "2026-10-26", "bitis": "2027-10-26",
                                                           "durum": "aktif"}}


def test_name_and_phone_changes_are_recorded_without_their_values(core, world, browser):
    path = f"kullanicilar/{world.ayse['id']}/bilgiler"
    changed = post(browser, path, {"ad_soyad": "  Ayşe   Demir ", "telefon": "+90 (532) 111 22 33"})
    assert changed.get_json() == {"success": True, "kullanici": {
        "kullanici_id": world.ayse["id"], "giris": "ayse@ornek.test", "ad_soyad": "Ayşe Demir",
        "telefon": "+90 (532) 111 22 33", "degisti": True}}
    same = post(browser, path, {"ad_soyad": "Ayşe Demir", "telefon": "+90 (532) 111 22 33"})
    assert same.get_json()["kullanici"]["degisti"] is False
    assert post(browser, path, {"ad_soyad": "Ayşe Demir"}).get_json()["kullanici"]["telefon"] is None
    assert [entry for entry in new_events(core, 0) if entry[1] == "bilgiler_degisti"] == [
        (world.manager["id"], "bilgiler_degisti", f"kullanici:{world.ayse['id']}", {"alanlar": ["ad_soyad", "telefon"]}),
        (world.manager["id"], "bilgiler_degisti", f"kullanici:{world.ayse['id']}", {"alanlar": ["telefon"]})]
    assert "Demir" not in json.dumps([event["ayrinti"] for event in audit(core)], ensure_ascii=False)
    assert get(browser, f"kullanicilar/{world.ayse['id']}").get_json()["kullanici"]["ad_soyad"] == "Ayşe Demir"


def test_the_login_name_changes_only_to_a_free_valid_name_and_signs_in_with_it(core, world, browser):
    path = f"kullanicilar/{world.ayse['id']}/bilgiler"
    other = make_user(core, "veli@ornek.test")
    make_user(core, "kisa")
    taken = [("veli@ornek.test", 409), ("VELI@ornek.test", 409), ("veli", 409), ("kisa", 409)]
    for name, status in taken:
        answer = post(browser, path, {"giris": name, "ad_soyad": "Ayşe Yılmaz"})
        assert answer.status_code == status and answer.get_json()["kod"] == "HESAP_VAR", name
    for bad in ("ay", "ayşe", "a b", "x@y", "<b>"):
        assert post(browser, path, {"giris": bad, "ad_soyad": "Ayşe Yılmaz"}).status_code == 400, bad
    changed = post(browser, path, {"giris": "ayse.yeni@peyrev.test", "ad_soyad": "Ayşe Yılmaz"}).get_json()["kullanici"]
    assert changed["giris"] == "ayse.yeni@peyrev.test" and changed["degisti"] is True
    with core.db.transaction() as cur:
        assert [row["id"] for row in users.find_by_login(cur, "ayse.yeni@peyrev.test")] == [world.ayse["id"]]
        assert users.find_by_login(cur, "ayse@ornek.test") == []
    assert [entry[3] for entry in new_events(core, 0) if entry[1] == "bilgiler_degisti"][-1] == {"alanlar": ["giris"]}
    assert "ayse.yeni" not in json.dumps([event["ayrinti"] for event in audit(core)], ensure_ascii=False)
    # Leaving the field out keeps the name; the same name is no change.
    assert post(browser, path, {"ad_soyad": "Ayşe Yılmaz"}).get_json()["kullanici"]["giris"] == "ayse.yeni@peyrev.test"
    assert post(browser, path, {"giris": "ayse.yeni@peyrev.test", "ad_soyad": "Ayşe Yılmaz"}).get_json()[
        "kullanici"]["degisti"] is False
    assert other["giris"] == "veli@ornek.test"


def test_roles_are_given_and_taken_and_a_lost_role_ends_the_panel_sessions(app, core, world, browser):
    path = f"kullanicilar/{world.ayse['id']}/rol"
    promoted = post(browser, path, {"rol": "yonetici"})
    assert promoted.get_json() == {"success": True, "kullanici": {"kullanici_id": world.ayse["id"], "rol": "yonetici",
                                                                  "degisti": True}}
    assert post(browser, path, {"rol": "yonetici"}).get_json()["kullanici"]["degisti"] is False
    # Ayşe signs in to the panel with her Studio password; taking the role away closes that session.
    ayse = signed_in(app, "ayse@ornek.test")
    demoted = post(browser, path, {"rol": "kullanici"})
    assert demoted.get_json()["kullanici"] == {"kullanici_id": world.ayse["id"], "rol": "kullanici", "degisti": True}
    assert get(ayse, "ozet").get_json() == SESSION_CLOSED
    assert [entry for entry in new_events(core, 0) if entry[1] == "rol_degisti"] == [
        (world.manager["id"], "rol_degisti", f"kullanici:{world.ayse['id']}",
         {"onceki": "kullanici", "rol": "yonetici", "kapatilan_panel_oturumu": 0}),
        (world.manager["id"], "rol_degisti", f"kullanici:{world.ayse['id']}",
         {"onceki": "yonetici", "rol": "kullanici", "kapatilan_panel_oturumu": 1})]


def test_a_passive_account_loses_its_studio_and_panel_sessions(app, core, world, browser):
    second = make_user(core, "ikinci@peyrev.test", rol="yonetici")
    open_session(core, second)
    other = signed_in(app, "ikinci@peyrev.test")
    before = last_event_id(core)
    passive = post(browser, f"kullanicilar/{second['id']}/durum", {"durum": "pasif"})
    assert passive.get_json() == {"success": True, "kullanici": {
        "kullanici_id": second["id"], "durum": "pasif", "kapatilan": 1, "kapatilan_panel_oturumu": 1}}
    assert get(other, "ben").get_json() == SESSION_CLOSED
    assert sql(core, "SELECT durum, kapanma_nedeni FROM oturumlar WHERE kullanici_id = %s", (second["id"],)) == [
        {"durum": "kapandi", "kapanma_nedeni": "YONETICI"}]
    again = post(browser, f"kullanicilar/{second['id']}/durum", {"durum": "pasif"})
    assert again.get_json()["kullanici"] == {"kullanici_id": second["id"], "durum": "pasif", "kapatilan": 0,
                                             "kapatilan_panel_oturumu": 0}
    active = post(browser, f"kullanicilar/{second['id']}/durum", {"durum": "aktif"})
    assert active.get_json()["kullanici"] == {"kullanici_id": second["id"], "durum": "aktif", "kapatilan": 0}
    assert new_events(core, before) == [
        (world.manager["id"], "durum_degisti", f"kullanici:{second['id']}",
         {"onceki": "aktif", "durum": "pasif", "kapatilan_oturum": 1}),
        (world.manager["id"], "yonetim_oturumlari_kapatildi", f"kullanici:{second['id']}", {"adet": 1}),
        (world.manager["id"], "durum_degisti", f"kullanici:{second['id']}",
         {"onceki": "pasif", "durum": "aktif", "kapatilan_oturum": 0})]
    assert signed_in(app, "ikinci@peyrev.test").get("/api/yonetim/ben").status_code == 200


def test_an_administrator_cannot_make_their_own_account_passive_or_drop_their_own_role(core, world, browser):
    me = world.manager["id"]
    before = snapshot(core, sessions=False)
    passive = post(browser, f"kullanicilar/{me}/durum", {"durum": "pasif"})
    assert (passive.status_code, passive.get_json()) == (400, {
        "success": False, "kod": "GECERSIZ", "message": "Kendi hesabınızı pasif yapamazsınız."})
    demoted = post(browser, f"kullanicilar/{me}/rol", {"rol": "kullanici"})
    assert (demoted.status_code, demoted.get_json()) == (400, {
        "success": False, "kod": "GECERSIZ", "message": "Kendi yönetici yetkinizi kaldıramazsınız."})
    reset = post(browser, f"kullanicilar/{me}/iki-adim-sifirla")
    assert (reset.status_code, reset.get_json()["message"]) == (
        400, "Kendi iki adımlı doğrulamanızı Hesabım bölümünden kapatın.")
    # Keeping one's own role and status is no change at all.
    assert post(browser, f"kullanicilar/{me}/rol", {"rol": "yonetici"}).get_json()["kullanici"]["degisti"] is False
    assert post(browser, f"kullanicilar/{me}/durum", {"durum": "aktif"}).status_code == 200
    assert snapshot(core, sessions=False) == before
    assert get(browser, "ben").get_json()["yonetici"]["kullanici_id"] == me


def test_module_rights_are_given_without_an_end_changed_and_removed(app, core, world, browser):
    ali = make_user(core, "ali@ornek.test", modules={users.RPA: date(2026, 12, 31)})
    path = f"kullanicilar/{ali['id']}/moduller/"
    given = post(browser, path + users.AGENTS, {"baslangic": "", "bitis": ""})  # the form's empty end: no end
    assert given.get_json() == {"success": True, "modul": {"kullanici_id": ali["id"], "modul": users.AGENTS,
                                                           "baslangic": "2026-10-26", "bitis": None}}
    changed = post(browser, path + users.RPA, {"bitis": "2027-06-30"})
    assert changed.get_json()["modul"] == {"kullanici_id": ali["id"], "modul": users.RPA, "baslangic": None,
                                           "bitis": "2027-06-30"}
    today = post(browser, path + users.RPA, {"baslangic": "2026-10-01", "bitis": "2026-10-26"})  # ends tonight
    assert today.get_json()["modul"]["bitis"] == "2026-10-26"
    removed = post(browser, path + users.AGENTS + "/kaldir")
    assert removed.get_json() == {"success": True, "modul": {"kullanici_id": ali["id"], "modul": users.AGENTS,
                                                             "kaldirildi": True}}
    assert post(browser, path + users.AGENTS + "/kaldir").get_json()["modul"]["kaldirildi"] is False
    detail = get(browser, f"kullanicilar/{ali['id']}").get_json()["kullanici"]
    assert detail["moduller"] == {users.RPA: {"baslangic": "2026-10-01", "bitis": "2026-10-26", "durum": "aktif"}}
    target = f"kullanici:{ali['id']}"
    assert [entry for entry in new_events(core, 0) if entry[2] == target] == [
        (world.manager["id"], "modul_ayarlandi", target,
         {"modul": users.AGENTS, "baslangic": "2026-10-26", "bitis": None, "onceki": None}),
        (world.manager["id"], "modul_ayarlandi", target,
         {"modul": users.RPA, "baslangic": None, "bitis": "2027-06-30", "onceki": {"baslangic": None,
                                                                                    "bitis": "2026-12-31"}}),
        (world.manager["id"], "modul_ayarlandi", target,
         {"modul": users.RPA, "baslangic": "2026-10-01", "bitis": "2026-10-26",
          "onceki": {"baslangic": None, "bitis": "2027-06-30"}}),
        (world.manager["id"], "modul_kaldirildi", target,
         {"modul": users.AGENTS, "onceki": {"baslangic": "2026-10-26", "bitis": None}})]
    core.clock.advance(days=1)  # the end day is included; the next day Studio is refused
    assert studio_login(app.test_client(), kullanici="ali@ornek.test").get_json()["kod"] == "SURE_DOLDU"


def test_a_password_code_is_issued_for_the_chosen_hours_and_recorded_without_the_code(app, core, world, browser):
    path = f"kullanicilar/{world.ayse['id']}/sifre-kodu"
    first = post(browser, path).get_json()["kod"]
    assert first["gecerlilik"] == "2026-10-27 12:00"  # 24 hours by default
    second = post(browser, path, {"saat": 72}).get_json()["kod"]
    assert second["gecerlilik"] == "2026-10-29 12:00"
    assert post(browser, path, {"saat": 168}).status_code == 200  # the longest: seven days
    issued = [entry for entry in new_events(core, 0) if entry[1] == "sifre_kodu_uretildi"]
    assert [entry[3] for entry in issued] == [{"gecerlilik": later(core, hours=hours)} for hours in (24, 72, 168)]
    assert first["kod"] not in json.dumps([event["ayrinti"] for event in audit(core)])
    # Only the newest code works.
    client = app.test_client()
    old = client.post("/api/rpa/hesap/sifre-yenile", json={"kullanici": "ayse@ornek.test", "kod": second["kod"],
                                                           "yeni_sifre": "Ayse2026yeni"})
    assert old.status_code == 401


def test_studio_sessions_are_closed_one_by_one_or_all_together(app, core, world, browser):
    path = f"kullanicilar/{world.ayse['id']}/oturumlari-kapat"
    one = post(browser, path, {"oturum_id": world.session})
    assert one.get_json() == {"success": True, "kullanici_id": world.ayse["id"], "kapatilan": 1}
    assert get(browser, f"kullanicilar/{world.ayse['id']}").get_json()["oturumlar"] == []
    assert post(browser, path).get_json()["kapatilan"] == 0  # nothing left to close and nothing recorded
    second = open_session(core, world.ayse, "c" * 32, "d" * 32)
    assert post(browser, path, {}).get_json()["kapatilan"] == 1
    assert sql(core, "SELECT id, kapanma_nedeni FROM oturumlar WHERE kullanici_id = %s ORDER BY id",
               (world.ayse["id"],)) == [{"id": world.session, "kapanma_nedeni": "YONETICI"},
                                        {"id": second, "kapanma_nedeni": "YONETICI"}]
    target = f"kullanici:{world.ayse['id']}"
    assert [entry for entry in new_events(core, 0) if entry[1] == "oturumlar_kapatildi"] == [
        (world.manager["id"], "oturumlar_kapatildi", target, {"adet": 1, "oturum_id": world.session}),
        (world.manager["id"], "oturumlar_kapatildi", target, {"adet": 1})]
    # Studio's support token stops working with the closed session.
    refused = app.test_client().get("/api/rpa/destek/talepler", headers=support_headers(core, world.ayse,
                                                                                         world.session))
    assert refused.status_code == 401


def test_resetting_two_step_verification_of_an_account_without_it_is_refused(core, world, browser):
    response = post(browser, f"kullanicilar/{world.ayse['id']}/iki-adim-sifirla")
    assert (response.status_code, response.get_json()) == (409, {
        "success": False, "kod": "IKI_ADIM_KAPALI", "message": "Bu hesapta iki adımlı doğrulama kapalı."})
    assert [entry for entry in new_events(core, 0) if entry[1] == "iki_adim_sifirlandi"] == []


# ----- ajan ofisleri -------------------------------------------------------------------------------------------
def test_an_office_is_made_passive_and_active_again(app, core, world, browser):
    path = f"ofisler/{world.office}/durum"
    closed = post(browser, path, {"durum": "pasif"})
    assert closed.get_json() == {"success": True, "ofis": {"id": world.office, "kullanici_id": world.ayse["id"],
                                                           "ad": "Satış Ofisi", "durum": "pasif", "degisti": True}}
    studio = app.test_client().get(AGENTS_API + "/ofisim", headers=world.agents.headers(world.ayse, world.office))
    assert studio.status_code == 403  # the owner's Studio cannot reach it
    assert post(browser, path, {"durum": "pasif"}).get_json()["ofis"]["degisti"] is False
    office = get(browser, f"kullanicilar/{world.ayse['id']}").get_json()["ofisler"]["liste"][0]
    assert office["durum"] == "pasif"
    assert post(browser, path, {"durum": "aktif"}).get_json()["ofis"]["durum"] == "aktif"
    assert app.test_client().get(AGENTS_API + "/ofisim",
                                 headers=world.agents.headers(world.ayse, world.office)).status_code == 200
    target = "ajan_ofisi:" + world.office
    assert [entry for entry in new_events(core, 0) if entry[2] == target] == [
        (world.manager["id"], "ajan_ofisi_durumu", target,
         {"kullanici_id": world.ayse["id"], "onceki": "aktif", "durum": "pasif"}),
        (world.manager["id"], "ajan_ofisi_durumu", target,
         {"kullanici_id": world.ayse["id"], "onceki": "pasif", "durum": "aktif"})]


# ----- başvurular ----------------------------------------------------------------------------------------------
def test_applications_are_listed_approved_and_rejected(app, core, world, browser):
    core.clock.advance(minutes=90)  # within the panel's idle limit
    assert apply(app.test_client(), ad_soyad="Zeynep Ak", eposta="zeynep@ornek.test", paket="rpa_ajan",
                 telefon="+90 532 000 00 00").status_code == 201
    zeynep = pending(core, "zeynep@ornek.test")
    listed = get(browser, "basvurular").get_json()["basvurular"]
    assert [(item["eposta"], item["paket"], item["durum"]) for item in listed] == [  # newest first
        ("zeynep@ornek.test", "rpa_ajan", "bekliyor"), ("mehmet@ornek.test", "rpa", "bekliyor")]
    assert listed[0] == {"id": zeynep, "ad_soyad": "Zeynep Ak", "eposta": "zeynep@ornek.test",
                         "telefon": "+90 532 000 00 00", "paket": "rpa_ajan", "durum": "bekliyor",
                         "olusturma": "2026-10-26 13:30", "karar": None, "not": None, "kullanici_id": None}
    assert "sifre_hash" not in json.dumps(listed) and "Guclu2026" not in json.dumps(listed)

    approved = post(browser, f"basvurular/{zeynep}/onayla", {"baslangic": "", "bitis": "2027-10-26", "paket": "",
                                                             "not": "Havale 26.10"})
    assert approved.status_code == 200
    account = approved.get_json()["hesap"]
    assert account == {"kullanici_id": account["kullanici_id"], "eposta": "zeynep@ornek.test", "paket": "rpa_ajan",
                       "bitis": "2027-10-26"}
    # She signs in to Studio with the password she chose on the form.
    assert studio_login(app.test_client(), kullanici="zeynep@ornek.test", sifre="Guclu2026").status_code == 200
    again = post(browser, f"basvurular/{zeynep}/onayla", {"bitis": "2027-10-26"})
    assert (again.status_code, again.get_json()["kod"]) == (409, "SONUCLANDI")

    rejected = post(browser, f"basvurular/{world.application}/reddet", {"not": "Ödeme gelmedi"})
    assert rejected.get_json() == {"success": True, "basvuru": {"id": world.application, "durum": "reddedildi"}}
    assert post(browser, f"basvurular/{world.application}/reddet").get_json()["kod"] == "BULUNAMADI"

    assert [item["durum"] for item in get(browser, "basvurular").get_json()["basvurular"]] == ["reddedildi"]
    assert [item["kullanici_id"] for item in get(browser, "basvurular?durum=onaylandi").get_json()["basvurular"]] == [
        account["kullanici_id"]]
    assert get(browser, "basvurular?durum=bekliyor").get_json()["basvurular"] == []
    assert [entry for entry in new_events(core, 0) if entry[1].startswith("basvuru_")] == [
        (world.manager["id"], "basvuru_onaylandi", f"kullanici:{account['kullanici_id']}",
         {"basvuru_id": zeynep, "giris": "zeynep@ornek.test", "paket": "rpa_ajan",
          "moduller": [users.RPA, users.AGENTS], "baslangic": "2026-10-26", "bitis": "2027-10-26",
          "not": "Havale 26.10"}),
        (world.manager["id"], "basvuru_reddedildi", f"basvuru:{world.application}", {"not": "Ödeme gelmedi"})]


def test_an_application_can_be_approved_with_another_package(app, core, world, browser):
    approved = post(browser, f"basvurular/{world.application}/onayla", {"bitis": "2027-10-26", "paket": "rpa_ajan"})
    user_id = approved.get_json()["hesap"]["kullanici_id"]
    rights = get(browser, f"kullanicilar/{user_id}").get_json()["kullanici"]["moduller"]
    assert set(rights) == {users.RPA, users.AGENTS}


# ----- destek --------------------------------------------------------------------------------------------------
def test_support_tickets_are_listed_read_answered_and_their_files_opened(app, core, world, browser):
    number = world.ticket["no"]
    question = studio_ticket(core, world.ayse, world.session, app.test_client(), tur="soru", baslik="Zamanlayıcı",
                             adim=None)
    listing = get(browser, "destek").get_json()
    assert listing["okunmamis"] == 2 and listing["turler"]["hata"] == "Hata bildirimi"
    assert listing["durumlar"]["yayinlandi"] == "Yayınlandı"
    assert [(item["no"], item["okunmamis"]) for item in listing["talepler"]] == [(question["no"], True),
                                                                                (number, True)]
    assert {key: listing["talepler"][1][key] for key in ("kullanici", "kullanici_id", "giris", "eposta", "tur",
                                                         "durum", "adim")} == {
        "kullanici": "Ayşe Yılmaz", "kullanici_id": world.ayse["id"], "giris": "ayse@ornek.test",
        "eposta": "ayse@ornek.test", "tur": "hata", "durum": "acik", "adim": "window.click"}
    assert [item["no"] for item in get(browser, "destek?tur=soru").get_json()["talepler"]] == [question["no"]]
    assert get(browser, "destek?durum=yanitlandi").get_json()["talepler"] == []

    opened = get(browser, f"destek/{number}").get_json()["talep"]
    assert [message["yazan"] for message in opened["mesajlar"]] == ["kullanici"]
    assert [(item["tur"], item["ad"], item["bicim"]) for item in opened["ekler"]] == [
        ("akis", "akis.json", "application/json"), ("gorsel", "ekran görüntüsü.png", "image/png")]
    assert get(browser, "destek").get_json()["okunmamis"] == 1  # opening it marked it as seen

    flow, image = opened["ekler"]
    download = get(browser, f"destek/{number}/ek/{flow['id']}")
    assert download.status_code == 200 and json.loads(download.data) == FLOW
    assert download.mimetype == "application/json"
    assert download.headers["Content-Disposition"] == 'attachment; filename="akis.json"'
    shown = get(browser, f"destek/{number}/ek/{image['id']}")
    assert shown.status_code == 200 and shown.data == PNG and shown.mimetype == "image/png"
    assert shown.headers["Content-Disposition"] == 'inline; filename="ekran_g_r_nt_s_.png"'
    for response in (download, shown):
        assert response.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"

    before = last_event_id(core)
    core.clock.advance(minutes=5)
    answered = post(browser, f"destek/{number}/yanit", {"metin": "  Sürüm 1.0.1'de düzeltildi.  "})
    talep = answered.get_json()["talep"]
    assert (talep["durum"], talep["durum_adi"]) == ("yanitlandi", "Yanıtlandı")
    assert [(message["yazan"], message["metin"]) for message in talep["mesajlar"]] == [
        ("kullanici", "Hedef bulunamadı uyarısı çıktı."), ("yonetici", "Sürüm 1.0.1'de düzeltildi.")]
    # The user sees the answer as unread in Studio.
    overview = app.test_client().get("/api/rpa/destek/ozet", headers=support_headers(core, world.ayse,
                                                                                     world.session))
    assert overview.get_json()["okunmamis"] == 1
    released = post(browser, f"destek/{number}/yanit", {"durum": "yayinlandi", "yayin_surumu": "1.0.1"})
    assert (released.get_json()["talep"]["durum"], released.get_json()["talep"]["yayin_surumu"]) == ("yayinlandi",
                                                                                                    "1.0.1")
    planned = post(browser, f"destek/{number}/yanit", {"metin": "Bir sonraki sürümde.", "durum": "planlandi",
                                                       "yayin_surumu": "1.0.2"})
    assert planned.get_json()["talep"]["yayin_surumu"] is None  # a version is kept only for "yayınlandı"
    assert new_events(core, before) == [
        (world.manager["id"], "destek_yanit", f"talep:{number}",
         {"onceki_durum": "acik", "durum": "yanitlandi", "yanit_uzunlugu": len("Sürüm 1.0.1'de düzeltildi.")}),
        (world.manager["id"], "destek_durum", f"talep:{number}",
         {"onceki_durum": "yanitlandi", "durum": "yayinlandi", "yanit_uzunlugu": 0, "yayin_surumu": "1.0.1"}),
        (world.manager["id"], "destek_yanit", f"talep:{number}",
         {"onceki_durum": "yayinlandi", "durum": "planlandi", "yanit_uzunlugu": 20})]
    assert "düzeltildi" not in json.dumps([event["ayrinti"] for event in audit(core)], ensure_ascii=False)


def test_an_answer_longer_than_the_body_limit_of_other_endpoints_still_fits(core, world, browser):
    text = "ğ" * 5000  # 10 000 bytes: more than the 8 KB other bodies may carry
    response = post(browser, f"destek/{world.ticket['no']}/yanit", {"metin": text})
    assert response.status_code == 200
    assert response.get_json()["talep"]["mesajlar"][-1]["metin"] == text


# ----- olaylar -------------------------------------------------------------------------------------------------
def test_events_are_listed_newest_first_with_filters_and_pages(core, world, browser):
    path = f"kullanicilar/{world.ayse['id']}/bilgiler"
    for index in range(55):
        core.clock.advance(seconds=1)
        assert post(browser, path, {"ad_soyad": f"Ayşe {index:02d}"}).get_json()["kullanici"]["degisti"] is True
    first = get(browser, "olaylar").get_json()
    assert (first["toplam"], first["sayfa"], first["sayfa_boyu"], len(first["olaylar"])) == (56, 1, 50, 50)
    assert first["turler"] == events.TYPES
    newest = first["olaylar"][0]
    assert newest == {"id": newest["id"], "zaman": "2026-10-26 12:00", "tur": "bilgiler_degisti",
                      "tur_adi": "Hesap bilgileri değişti", "hedef": f"kullanici:{world.ayse['id']}",
                      "ayrinti": {"alanlar": ["ad_soyad"]},
                      "yonetici": {"kullanici_id": world.manager["id"], "giris": "yonetici@peyrev.test",
                                   "ad_soyad": "Yönetici Kişi"}}
    last = get(browser, "olaylar?sayfa=2").get_json()["olaylar"]
    assert [item["tur"] for item in last][-1] == "yonetim_girisi" and len(last) == 6
    assert get(browser, "olaylar?sayfa=3").get_json()["olaylar"] == []
    assert get(browser, "olaylar?tur=yonetim_girisi").get_json()["toplam"] == 1
    assert get(browser, f"olaylar?hedef=kullanici:{world.ayse['id']}").get_json()["toplam"] == 55
    assert get(browser, f"olaylar?hedef=kullanici:{world.manager['id']}&tur=yonetim_girisi").get_json()[
        "toplam"] == 1
    assert get(browser, "olaylar?tur=tasima_tamamlandi").get_json()["olaylar"] == []
    # An event of an unknown kind (e.g. written by the migration tool) and without an administrator is shown as is.
    sql(core, "INSERT INTO yonetim_olaylari (yonetici_id, tur, hedef, ayrinti, zaman) "
              "VALUES (NULL, 'tasima_tamamlandi', NULL, '{\"hesap\": 3}', %s)", (core.clock.now(),))
    [moved] = get(browser, "olaylar?tur=tasima_tamamlandi").get_json()["olaylar"]
    assert (moved["tur_adi"], moved["yonetici"], moved["hedef"], moved["ayrinti"]) == (
        "tasima_tamamlandi", None, None, {"hesap": 3})


def test_an_account_is_deleted_with_everything_it_owns_only_with_its_login_typed(app, core, world, browser):
    user_id = world.ayse["id"]
    before = {table: sql(core, f"SELECT count(*) AS n FROM {table} WHERE {column} = %s", (user_id,))[0]["n"]
              for table, column in (("kullanici_modulleri", "kullanici_id"), ("oturumlar", "kullanici_id"),
                                    ("destek_talepleri", "kullanici_id"), ("ajan_ofisleri", "sahip_id"))}
    assert all(before.values()), before
    for typed in ("", "AYSE@ornek.test", "ayse"):
        answer = post(browser, f"kullanicilar/{user_id}/sil", {"giris": typed})
        assert (answer.status_code, answer.get_json()["message"]) == (
            400, "Silmeyi onaylamak için hesabın giriş adını aynen yazın."), typed
    own = post(browser, f"kullanicilar/{world.manager['id']}/sil", {"giris": "yonetici@peyrev.test"})
    assert (own.status_code, own.get_json()["message"]) == (400, "Kendi hesabınızı silemezsiniz.")
    # Her approved application (same address) goes with the account.
    sql(core, "UPDATE basvurular SET eposta = 'ayse@ornek.test' WHERE id = %s", (world.application,))
    deleted = post(browser, f"kullanicilar/{user_id}/sil", {"giris": "ayse@ornek.test"})
    assert deleted.get_json()["kullanici"] == {"kullanici_id": user_id, "giris": "ayse@ornek.test", "silindi": True}
    assert sql(core, "SELECT id FROM kullanicilar WHERE id = %s", (user_id,)) == []
    for table, column in (("kullanici_modulleri", "kullanici_id"), ("oturumlar", "kullanici_id"),
                          ("destek_talepleri", "kullanici_id"), ("ajan_ofisleri", "sahip_id")):
        assert sql(core, f"SELECT count(*) AS n FROM {table} WHERE {column} = %s", (user_id,))[0]["n"] == 0, table
    assert sql(core, "SELECT count(*) AS n FROM basvurular WHERE id = %s", (world.application,))[0]["n"] == 0
    assert [entry[3] for entry in new_events(core, 0) if entry[1] == "kullanici_silindi"] == [
        {"giris": "ayse@ornek.test", "silinen_basvuru": 1}]
    assert get(browser, f"kullanicilar/{user_id}").status_code == 404
    assert post(browser, f"kullanicilar/{user_id}/sil", {"giris": "ayse@ornek.test"}).status_code == 404
