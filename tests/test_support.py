"""Destek talepleri: Studio uçları (destek belirteci, Studio oturumu ve lisans kapısı, sahiplik, sınırlar, ekler) ve
yönetim panelinin servis işlevleri (liste, açma, yanıt ve durum, ek, okunmamış rozeti, denetim kaydı).

Davranışlar PostgreSQL ile ve firma olmadan sınanır.
"""

import base64
import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from conftest import DEVICE, START, make_user, open_session

from peyrev import migrate, support, tokens, users
from peyrev.common import ApiError
from peyrev.support import (
    answer_ticket,
    file_response,
    list_tickets,
    open_ticket,
    service,
    ticket_attachment,
    unread_count,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
# The statement the migration header gives for the import tool.
AFTER_IMPORT = ("SELECT setval(pg_get_serial_sequence('destek_talepleri', 'talep_no'), "
                "GREATEST(1000, (SELECT coalesce(max(talep_no), 0) FROM destek_talepleri)))")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class Desk:
    """Bir yönetici ve iki lisanslı kullanıcı; Studio istekleri lisans yanıtındaki destek belirteciyle gider."""

    def __init__(self, core, client):
        self.core, self.client = core, client
        self.admin = make_user(core, "yonetici@peyrev.test", rol="yonetici", ad_soyad="Yönetici")
        self.ayse = make_user(core, "ayse@firma.test", modules={users.RPA: None}, ad_soyad="Ayşe <b>Yılmaz</b>")
        self.mehmet = make_user(core, "mehmet@firma.test", modules={users.RPA: None}, ad_soyad="Mehmet Kaya")
        self.sessions = {user["id"]: open_session(core, user) for user in (self.admin, self.ayse, self.mehmet)}

    def studio(self, user=None):
        user = user or self.ayse
        return {"Authorization": "Bearer " + tokens.issue(self.core, "destek", user, DEVICE, self.sessions[user["id"]])}

    def create(self, user=None, **changes):
        body = {"tur": "adim_onerisi", "baslik": "Excel sayfasını PDF yapan adım",
                "metin": "Satır 1\nSatır 2 <kod> örneği", "adim": "file.excel_read", "surum": "1.0.0",
                "platform": "Windows"}
        body.update(changes)
        return self.client.post("/api/rpa/destek/talepler", json=body, headers=self.studio(user))

    def number(self, **changes):
        response = self.create(**changes)
        assert response.status_code == 201, response.get_json()
        return response.get_json()["talep"]["no"]

    def get(self, path, user=None):
        return self.client.get("/api/rpa/destek" + path, headers=self.studio(user))

    def reply(self, number, text, user=None):
        return self.client.post(f"/api/rpa/destek/talepler/{number}/mesaj", json={"metin": text},
                                headers=self.studio(user))

    def manage(self, function, *args, **kwargs):
        """Yönetim paneli gibi: kendi işleminde, yönetici kimliğiyle."""
        with self.core.db.transaction() as cur:
            return function(self.core, cur, self.admin["id"], *args, **kwargs)

    def sql(self, statement, parameters=()):
        with self.core.db.transaction() as cur:
            cur.execute(statement, parameters)
            return cur.fetchall() if cur.description else []

    def count(self, table="destek_talepleri"):
        return self.sql(f"SELECT count(*) AS n FROM {table}")[0]["n"]


@pytest.fixture
def desk(core, client):
    return Desk(core, client)


def test_a_request_is_stored_with_its_attachments_and_the_admin_is_told_after_saving(desk):
    flow = {"adimlar": [{"tur": "window.click", "parametreler": {"hedef": "<metin 12 karakter>"}}]}
    log = {"adimlar": [{"baslik": "Tıkla", "hata": "Hedef bulunamadı"}]}
    response = desk.create(ekler=[{"tur": "akis", "icerik": flow}, {"tur": "hata_kaydi", "icerik": log},
                                  {"tur": "gorsel", "ad": "ekran.png", "bicim": "image/png", "veri": b64(PNG)}])
    assert response.status_code == 201, response.get_json()
    ticket = response.get_json()["talep"]
    assert set(ticket) == {"no", "tur", "tur_adi", "baslik", "durum", "durum_adi", "adim", "surum", "platform",
                           "yayin_surumu", "olusturma", "guncelleme", "okunmamis", "mesajlar", "ekler"}
    assert ticket["no"] == 1001 and ticket["durum"] == "acik" and ticket["okunmamis"] is False
    assert ticket["tur_adi"] == "Yeni adım önerisi" and ticket["durum_adi"] == "Açık" and ticket["adim"] == "file.excel_read"
    assert ticket["olusturma"] == ticket["guncelleme"] == "2026-10-26 12:00"  # Türkiye saati
    assert [message["yazan"] for message in ticket["mesajlar"]] == ["kullanici"]
    assert ticket["mesajlar"][0]["metin"] == "Satır 1\nSatır 2 <kod> örneği"  # shown as text, never as HTML
    assert [(item["tur"], item["ad"], item["bicim"], item["boyut"]) for item in ticket["ekler"]] == [
        ("akis", "akis.json", "application/json", len(json.dumps(flow, ensure_ascii=False, sort_keys=True).encode())),
        ("hata_kaydi", "hata-kaydi.json", "application/json",
         len(json.dumps(log, ensure_ascii=False, sort_keys=True).encode())),
        ("gorsel", "ekran.png", "image/png", len(PNG))]  # in the order they were attached
    stored = {row["tur"]: bytes(row["icerik"]) for row in desk.sql("SELECT tur, icerik FROM destek_ekleri")}
    assert json.loads(stored["akis"]) == flow and json.loads(stored["hata_kaydi"]) == log and stored["gorsel"] == PNG
    # One plain-text notice once the ticket is saved; its link opens the ticket in the panel.
    [(subject, text)] = desk.core.notifier.messages
    assert subject == f"Yeni destek talebi #{ticket['no']} · Yeni adım önerisi"
    assert "Kimden: Ayşe <b>Yılmaz</b> (ayse@firma.test)" in text and "Başlık: Excel sayfasını PDF yapan adım" in text
    assert "Studio: 1.0.0 · Windows" in text and "Satır 2 <kod> örneği" in text
    assert text.endswith(f"Yanıtlamak için: https://peyrev.com/yonetim/#destek-{ticket['no']}")


@pytest.mark.parametrize(("change", "message"), [
    ({"baslik": "<script>x</script>"}, "< veya >"),
    ({"baslik": ""}, "Başlık gerekli"),
    ({"baslik": "   "}, "Başlık gerekli"),
    ({"baslik": "x" * 121}, "en fazla 120 karakter"),
    ({"baslik": 42}, "Başlık geçersiz"),
    ({"metin": "x" * 5001}, "5000"),
    ({"metin": "Satır\x00"}, "geçersiz karakter"),
    ({"tur": "baska"}, "tür"),
    ({"tur": ["soru"]}, "tür"),
    ({"adim": "DROP TABLE"}, "adım"),
    ({"adim": "file.excel_read\n"}, "adım"),
    ({"surum": "son"}, "sürümü"),
    ({"surum": "1.0.0\n"}, "sürümü"),
    ({"platform": "Amiga"}, "platform"),
    ({"ekler": {"tur": "akis"}}, "Ekler geçersiz"),
    ({"ekler": [{"tur": "ses"}]}, "Ekler geçersiz"),
    ({"ekler": [{"tur": "gorsel", "bicim": "image/png", "veri": b64(JPEG)}]}, "türde değil"),
    ({"ekler": [{"tur": "gorsel", "bicim": "image/gif", "veri": "R0lG"}]}, "PNG veya JPEG"),
    ({"ekler": [{"tur": "gorsel", "bicim": ["image/png"], "veri": "R0lG"}]}, "PNG veya JPEG"),
    ({"ekler": [{"tur": "gorsel", "bicim": "image/png", "veri": "%%%%"}]}, "okunamadı"),
    ({"ekler": [{"tur": "gorsel", "bicim": "image/png", "veri": ""}]}, "1,5 MB"),
    ({"ekler": [{"tur": "gorsel", "bicim": "image/png", "veri": b64(PNG + b"\x00" * 1536 * 1024)}]}, "1,5 MB"),
    ({"ekler": [{"tur": "gorsel", "bicim": "image/png", "veri": b64(PNG), "yol": "/etc"}]}, "Görsel eki"),
    ({"ekler": [{"tur": "gorsel", "ad": "<b>.png", "bicim": "image/png", "veri": b64(PNG)}]}, "< veya >"),
    ({"ekler": [{"tur": "gorsel", "bicim": "image/png", "veri": b64(PNG)}] * 4}, "En fazla 3"),
    ({"ekler": [{"tur": "akis", "icerik": {"x": "y" * (200 * 1024)}}]}, "200 KB"),
    ({"ekler": [{"tur": "akis", "icerik": {}}, {"tur": "akis", "icerik": {}}]}, "Ekler geçersiz"),
    ({"ekler": [{"tur": "akis", "icerik": "metin"}]}, "Ekler geçersiz"),
    ({"ekler": [{"tur": "akis", "icerik": {}, "ad": "x"}]}, "Ekler geçersiz"),
    ({"bilinmeyen": 1}, "İstek alanları geçersiz"),
])
def test_what_does_not_fit_is_refused_and_nothing_is_stored(desk, change, message):
    response = desk.create(**change)
    assert response.status_code == 400 and message in response.get_json()["message"], response.get_json()
    assert response.get_json()["kod"] == "GECERSIZ"
    assert desk.count() == 0 and desk.core.notifier.messages == []


def test_odd_bodies_are_refused_before_anything_is_stored(desk):
    def post(raw):
        return desk.client.post("/api/rpa/destek/talepler", data=raw, content_type="application/json",
                                headers=desk.studio())

    valid = '"tur": "soru", "metin": "Açıklama", "surum": "1.0.0", "platform": "macOS"'
    for raw in ("{", "[]", '{"baslik": "a", "baslik": "b", ' + valid + "}", '{"baslik": NaN, ' + valid + "}",
                '{"metin": "eksik"}', b'{"baslik": "\xff"}'):
        answer = post(raw)
        assert answer.status_code == 400 and answer.get_json()["message"] == "İstek alanları geçersiz.", raw
    # A lone surrogate cannot be stored as text: it is a bad character, not a server error.
    answer = post('{"baslik": "yar\\ud800im", ' + valid + "}")
    assert answer.status_code == 400 and "geçersiz karakter" in answer.get_json()["message"]
    answer = post('{"baslik": "Başlık", "ekler": [{"tur": "akis", "icerik": {"x": "\\udfff"}}], ' + valid + "}")
    assert answer.status_code == 400 and answer.get_json()["message"] == "Ekler geçersiz."
    too_big = post(b"{" + b" " * service.CREATE_BODY_LIMIT + b"}")
    assert too_big.status_code == 413 and too_big.get_json()["kod"] == "GOVDE_SINIRI"
    assert desk.count() == 0


def test_a_new_ticket_body_is_read_only_after_the_caller_is_identified(desk, monkeypatch):
    """Görselli gövde 7 MB'a varabilir: oturumsuz ya da lisanssız istekte gövde hiç okunmaz."""
    read = []
    original = support._create_body
    monkeypatch.setattr(support, "_create_body", lambda: read.append(1) or original())
    other = make_user(desk.core, "lisanssiz@firma.test")
    unlicensed = {"Authorization": "Bearer " + tokens.issue(desk.core, "destek", other, DEVICE,
                                                             open_session(desk.core, other))}
    for headers, status, code in (({}, 401, "OTURUM_GEREKLI"), ({"Authorization": "Bearer x.y.z"}, 401, "OTURUM_GECERSIZ"),
                                  (unlicensed, 403, "YETKI_YOK")):
        for raw in (b"{", b"{" + b" " * service.CREATE_BODY_LIMIT + b"}"):
            answer = desk.client.post("/api/rpa/destek/talepler", data=raw, content_type="application/json",
                                      headers=headers)
            assert (answer.status_code, answer.get_json()["kod"]) == (status, code)
    assert read == [] and desk.count() == 0
    assert desk.create().status_code == 201 and read == [1]


def test_a_deeply_nested_or_long_flow_outline_is_accepted(desk):
    """Ortak json_body'nin 20 düzey ve 10.000 değer sınırı akış ekine uygulanmaz; boyut sınırı 200 KB'tır."""
    step = {"tur": "control.if", "ad": "Koşul"}
    for _ in range(14):
        step = {"tur": "control.for_each", "ad": "Tekrarla", "parametreler": {"items": "${rows}"}, "ic_adimlar": [step]}
    outline = {"adim_sayisi": 4015, "adimlar": [step] + [{"tur": "window.click", "ad": "Tıkla"}] * 4000}
    number = desk.number(ekler=[{"tur": "akis", "icerik": outline}])
    [row] = desk.sql("SELECT icerik FROM destek_ekleri WHERE talep_no = %s", (number,))
    assert json.loads(bytes(row["icerik"])) == outline


def test_only_a_live_session_with_a_license_may_write(desk):
    core, ayse = desk.core, desk.ayse

    def status(headers):
        return desk.client.get("/api/rpa/destek/talepler", headers=headers)

    missing = status({})
    assert missing.status_code == 401 and missing.get_json()["kod"] == "OTURUM_GEREKLI"
    # An agent token, or a support token signed with another key, is not a support token.
    agent = tokens.issue(core, "ajan", ayse, DEVICE, desk.sessions[ayse["id"]])
    assert status({"Authorization": "Bearer " + agent}).get_json()["kod"] == "OTURUM_GECERSIZ"
    claims = jwt.decode(desk.studio()["Authorization"][7:], options={"verify_signature": False})
    forged = jwt.encode(claims, "baska-bir-sunucunun-anahtari-ile-imzalandi", algorithm="HS256")
    assert status({"Authorization": "Bearer " + forged}).status_code == 401
    headers = desk.studio()
    assert status(headers).status_code == 200

    # The license is read from the database on every request; the end day itself still counts.
    today = core.clock.today_tr()
    desk.sql("UPDATE kullanici_modulleri SET bitis = %s WHERE kullanici_id = %s", (today - timedelta(days=1), ayse["id"]))
    refused = status(headers)
    assert refused.status_code == 403 and refused.get_json() == {
        "success": False, "kod": "YETKI_YOK", "message": "Destek talebi için geçerli Peyrev lisansı gerekli."}
    assert desk.create().status_code == 403 and desk.count() == 0
    desk.sql("UPDATE kullanici_modulleri SET bitis = %s WHERE kullanici_id = %s", (today, ayse["id"]))
    assert status(headers).status_code == 200
    desk.sql("DELETE FROM kullanici_modulleri WHERE kullanici_id = %s", (ayse["id"],))
    assert status(headers).status_code == 403
    # An administrator needs no MOD_RPA row, as for the license itself.
    assert status(desk.studio(desk.admin)).status_code == 200
    desk.sql("INSERT INTO kullanici_modulleri (kullanici_id, modul) VALUES (%s, 'MOD_RPA')", (ayse["id"],))
    assert status(headers).status_code == 200

    # A closed Studio session, a passive account, an old token and a changed password all end it.
    desk.sql("UPDATE oturumlar SET durum = 'kapandi' WHERE kullanici_id = %s", (ayse["id"],))
    assert status(headers).status_code == 401
    desk.sql("UPDATE oturumlar SET durum = 'aktif' WHERE kullanici_id = %s", (ayse["id"],))
    desk.sql("UPDATE kullanicilar SET durum = 'pasif' WHERE id = %s", (ayse["id"],))
    assert status(headers).status_code == 401
    desk.sql("UPDATE kullanicilar SET durum = 'aktif' WHERE id = %s", (ayse["id"],))
    core.clock.advance(seconds=tokens.TOKEN_SECONDS + 1)
    assert status(headers).status_code == 401
    core.clock.advance(seconds=-(tokens.TOKEN_SECONDS + 1))
    assert status(headers).status_code == 200
    desk.sql("UPDATE kullanicilar SET sifre_hash = 'yeni' WHERE id = %s", (ayse["id"],))
    assert status(headers).status_code == 401


def test_every_studio_endpoint_is_behind_the_support_token(desk, app):
    number = desk.number()
    other = make_user(desk.core, "lisanssiz@firma.test")
    session = open_session(desk.core, other)
    unlicensed = {"Authorization": "Bearer " + tokens.issue(desk.core, "destek", other, DEVICE, session)}
    rules = [rule for rule in app.url_map.iter_rules() if rule.rule.startswith("/api/rpa/destek/")]
    routes = {(method, rule.rule) for rule in rules for method in rule.methods - {"HEAD", "OPTIONS"}}
    assert len(routes) == 6
    bodies = {"/api/rpa/destek/talepler": {"tur": "soru", "baslik": "Soru", "metin": "Metin", "surum": "1.0.0",
                                           "platform": "macOS"}}
    for method, rule in routes:
        path = rule.replace("<int:number>", str(number)).replace("<identifier>", "a" * 32)
        body = bodies.get(rule, {"metin": "Yanıt"}) if method == "POST" else None
        anonymous = desk.client.open(path, method=method, json=body)
        assert anonymous.status_code == 401 and anonymous.get_json()["kod"] == "OTURUM_GEREKLI", (method, path)
        assert desk.client.open(path, method=method, json=body, headers=unlicensed).status_code == 403, path
    assert desk.count() == 1 and desk.count("destek_mesajlari") == 1
    # The old system's management paths are not served here: the panel calls the service functions.
    assert desk.client.get("/api/rpa/yonetim/talepler", headers=desk.studio(desk.admin)).status_code == 404


def test_everyone_sees_only_their_own_requests(desk):
    number = desk.number(ekler=[{"tur": "gorsel", "bicim": "image/png", "veri": b64(PNG)}])
    attachment = desk.sql("SELECT id FROM destek_ekleri")[0]["id"]
    assert desk.get("/talepler", desk.mehmet).get_json()["talepler"] == []
    assert desk.get("/ozet", desk.mehmet).get_json() == {"success": True, "okunmamis": 0, "yayinlanan": []}
    hidden = desk.get(f"/talepler/{number}", desk.mehmet)
    assert hidden.status_code == 404 and hidden.get_json() == {
        "success": False, "kod": "BULUNAMADI", "message": "Talep bulunamadı."}
    assert desk.reply(number, "x", desk.mehmet).status_code == 404
    assert desk.get(f"/talepler/{number}/ek/{attachment}", desk.mehmet).status_code == 404
    assert desk.count("destek_mesajlari") == 1
    own = desk.get(f"/talepler/{number}/ek/{attachment}")
    assert own.status_code == 200 and own.data == PNG and own.mimetype == "image/png"
    # The Studio always receives attachments as downloads, never to be rendered by a browser.
    assert own.headers["Content-Disposition"] == 'attachment; filename="gorsel"'
    assert own.headers["X-Content-Type-Options"] == "nosniff" and own.headers["Cache-Control"] == "no-store"


def test_unknown_numbers_and_attachment_names_are_not_found(desk):
    number = desk.number()
    for path in ("/talepler/0", "/talepler/99999999999", "/talepler/-1", f"/talepler/{number}/ek/../../etc",
                 f"/talepler/{number}/ek/" + "F" * 32, f"/talepler/{number}/ek/" + "a" * 32, "/talepler/1002"):
        assert desk.get(path).status_code == 404, path
    assert desk.get(f"/talepler/{number}/ek/" + "a" * 32).get_json()["message"] == "Ek bulunamadı."


def test_daily_limit_follows_the_turkish_day(desk):
    for _ in range(service.TICKETS_PER_DAY):
        assert desk.create().status_code == 201
    refused = desk.create()
    assert refused.status_code == 429 and refused.get_json()["kod"] == "SINIR"
    assert refused.get_json()["message"].startswith("Bugün için talep sınırına ulaştınız.")
    assert desk.create(desk.mehmet).status_code == 201  # everyone has their own limit
    desk.core.clock.at = datetime(2026, 10, 26, 20, 59, tzinfo=UTC)  # 23:59 in Türkiye: the same day
    assert desk.create().status_code == 429
    desk.core.clock.at = datetime(2026, 10, 26, 21, 0, tzinfo=UTC)  # 00:00 in Türkiye: a new day
    assert desk.create().status_code == 201
    assert desk.count() == service.TICKETS_PER_DAY + 2


def test_daily_message_limit_counts_the_first_message_too(desk):
    number = desk.number()
    for index in range(service.MESSAGES_PER_DAY - 1):
        assert desk.reply(number, f"Ek bilgi {index}").status_code == 200
    refused = desk.reply(number, "Bir tane daha")
    assert refused.status_code == 429 and refused.get_json() == {
        "success": False, "kod": "SINIR", "message": "Bugün için mesaj sınırına ulaştınız."}
    desk.core.clock.advance(days=1)
    assert desk.reply(number, "Ertesi gün").status_code == 200
    assert desk.count("destek_mesajlari") == service.MESSAGES_PER_DAY + 1


def test_a_reply_is_plain_text_within_limits(desk):
    number = desk.number()
    for body, message in (({"metin": ""}, "Yanıt gerekli."), ({"metin": "   "}, "Yanıt gerekli."),
                          ({"metin": 5}, "Yanıt geçersiz."), ({"metin": "x" * 5001}, "5000"),
                          ({}, "İstek alanları geçersiz."), ({"metin": "x", "ek": 1}, "İstek alanları geçersiz.")):
        answer = desk.client.post(f"/api/rpa/destek/talepler/{number}/mesaj", json=body, headers=desk.studio())
        assert answer.status_code == 400 and message in answer.get_json()["message"], body
    too_big = desk.client.post(f"/api/rpa/destek/talepler/{number}/mesaj", data=json.dumps({"metin": "x" * 20000}),
                               content_type="application/json", headers=desk.studio())
    assert too_big.status_code == 413
    kept = desk.reply(number, "  Satır 1\r\nSatır 2\t<b>  ").get_json()["talep"]["mesajlar"][-1]
    assert kept["metin"] == "Satır 1\nSatır 2\t<b>"


def test_the_admin_answers_the_user_sees_it_once_and_a_reply_reopens(desk):
    clock = desk.core.clock
    number = desk.number()
    listed = desk.manage(list_tickets)
    first = listed["talepler"][0]
    assert first["okunmamis"] is True and listed["okunmamis"] == 1 and desk.manage(unread_count) == 1
    assert first["kullanici"] == "Ayşe <b>Yılmaz</b>" and first["kullanici_id"] == desk.ayse["id"]
    assert first["giris"] == first["eposta"] == "ayse@firma.test" and "firma" not in first and "firma_id" not in first
    assert listed["turler"]["hata"] == "Hata bildirimi" and listed["durumlar"]["yayinlandi"] == "Yayınlandı"
    opened = desk.manage(open_ticket, number)
    assert opened["okunmamis"] is False and opened["eposta"] == "ayse@firma.test"
    assert [message["yazan"] for message in opened["mesajlar"]] == ["kullanici"]
    assert desk.manage(unread_count) == 0

    clock.advance(minutes=5)
    answered = desk.manage(answer_ticket, number, metin="Teşekkürler, sıradaki sürüme alıyoruz.")
    assert answered["durum"] == "yanitlandi" and answered["okunmamis"] is False
    assert desk.get("/ozet").get_json()["okunmamis"] == 1
    assert desk.get("/talepler").get_json()["talepler"][0]["okunmamis"] is True
    seen = desk.get(f"/talepler/{number}").get_json()["talep"]
    assert [message["yazan"] for message in seen["mesajlar"]] == ["kullanici", "yonetici"]
    assert seen["mesajlar"][1]["zaman"] == "2026-10-26 12:05" and seen["okunmamis"] is False
    assert desk.get("/ozet").get_json()["okunmamis"] == 0

    clock.advance(minutes=5)
    reply = desk.reply(number, "Harika!").get_json()["talep"]
    assert reply["durum"] == "acik"
    assert desk.core.notifier.messages[-1][0] == f"Destek talebine yanıt #{number}"
    assert "Harika!" in desk.core.notifier.messages[-1][1]
    assert desk.manage(list_tickets)["talepler"][0]["okunmamis"] is True and desk.manage(unread_count) == 1

    # Shipped: the version is required and the Studio lists it among the shipped suggestions.
    clock.advance(minutes=1)
    with pytest.raises(ApiError) as refused:
        desk.manage(answer_ticket, number, durum="yayinlandi")
    assert refused.value.status == 400 and "sürüm" in str(refused.value)
    shipped = desk.manage(answer_ticket, number, durum="yayinlandi", yayin_surumu="1.0.1")
    assert shipped["yayin_surumu"] == "1.0.1" and shipped["durum_adi"] == "Yayınlandı"
    overview = desk.get("/ozet").get_json()
    assert overview["yayinlanan"] == [{"no": number, "baslik": "Excel sayfasını PDF yapan adım", "surum": "1.0.1"}]
    assert overview["okunmamis"] == 1  # a change of state is news for the person too
    assert [item["no"] for item in desk.manage(list_tickets, durum="yayinlandi")["talepler"]] == [number]
    assert desk.manage(list_tickets, durum="acik")["talepler"] == []
    assert [item["no"] for item in desk.manage(list_tickets, tur="adim_onerisi", durum="")["talepler"]] == [number]
    assert desk.manage(list_tickets, tur="hata")["talepler"] == []

    desk.manage(answer_ticket, number, durum="kapandi")
    closed = desk.reply(number, "Bir şey daha")
    assert closed.status_code == 409 and closed.get_json() == {
        "success": False, "kod": "KAPALI", "message": "Bu talep kapatıldı. Yeni bir talep açabilirsiniz."}


def test_answers_and_states_follow_the_support_rules(desk):
    number = desk.number()

    def refused(**changes):
        with pytest.raises(ApiError) as error:
            desk.manage(answer_ticket, number, **changes)
        assert error.value.status == 400
        return str(error.value)

    assert refused() == "Yanıt yazın veya durumu değiştirin."
    assert refused(metin="   ") == "Yanıt yazın veya durumu değiştirin."
    assert refused(durum="acik") == "Yanıt yazın veya durumu değiştirin."
    assert refused(durum="silindi") == "Durum geçersiz."
    assert refused(durum=["acik"]) == "Durum geçersiz."
    assert refused(metin="x" * 5001) == "Yanıt en fazla 5000 karakter olabilir."
    assert refused(metin=7) == "Yanıt geçersiz."
    assert refused(durum="yayinlandi", yayin_surumu="1.0") == refused(durum="yayinlandi", yayin_surumu=101)
    # A planned suggestion carries no release version; a shipped one does until it moves on.
    planned = desk.manage(answer_ticket, number, durum="planlandi", yayin_surumu="1.0.1")
    assert planned["durum"] == "planlandi" and planned["yayin_surumu"] is None and len(planned["mesajlar"]) == 1
    assert desk.manage(answer_ticket, number, durum="yayinlandi", yayin_surumu="1.0.1")["yayin_surumu"] == "1.0.1"
    # The same state with another version is a change; with the same version it is not.
    assert desk.manage(answer_ticket, number, durum="yayinlandi", yayin_surumu="1.0.2")["yayin_surumu"] == "1.0.2"
    assert refused(durum="yayinlandi", yayin_surumu="1.0.2") == "Yanıt yazın veya durumu değiştirin."
    # An answer without a state moves the ticket to "answered" and drops the version.
    answered = desk.manage(answer_ticket, number, metin="Yeni sürümde düzeldi mi?")
    assert answered["durum"] == "yanitlandi" and answered["yayin_surumu"] is None
    # An answer may set the state at the same time.
    review = desk.manage(answer_ticket, number, metin="İnceliyoruz.", durum="inceleniyor")
    assert review["durum"] == "inceleniyor" and [item["yazan"] for item in review["mesajlar"]][-1] == "yonetici"
    for wrong in (999999, 0, -5, "1001", True, 2**40):
        with pytest.raises(ApiError) as missing:
            desk.manage(open_ticket, wrong)
        assert missing.value.status == 404 and missing.value.kod == "BULUNAMADI"
    for filters in ({"durum": "silindi"}, {"tur": "baska"}, {"durum": ["acik"]}):
        with pytest.raises(ApiError) as bad:
            desk.manage(list_tickets, **filters)
        assert bad.value.status == 400 and "süzgeci geçersiz" in str(bad.value)


@pytest.mark.parametrize(("function", "arguments"), [
    (list_tickets, ()), (open_ticket, (1001,)), (answer_ticket, (1001,)), (ticket_attachment, (1001, "f" * 32)),
    (unread_count, ())])
def test_only_an_active_platform_admin_manages(desk, function, arguments):
    desk.number()
    for admin_id in (desk.ayse["id"], 10**12, 0, -1, True, str(desk.admin["id"]), None):
        with pytest.raises(ApiError) as refused, desk.core.db.transaction() as cur:
            function(desk.core, cur, admin_id, *arguments)
        assert refused.value.status == 403 and str(refused.value) == "Bu işlem yalnız platform yöneticisine açıktır."
    desk.sql("UPDATE kullanicilar SET durum = 'pasif' WHERE id = %s", (desk.admin["id"],))
    with pytest.raises(ApiError) as passive:
        desk.manage(function, *arguments)
    assert passive.value.kod == "YETKI_YOK"
    assert desk.count("yonetim_olaylari") == 0


def test_the_admin_opens_images_inline_and_flows_as_a_download(desk):
    number = desk.number(ekler=[{"tur": "akis", "icerik": {"adimlar": []}},
                                {"tur": "gorsel", "ad": "ekran görüntüsü.jpg", "bicim": "image/jpeg",
                                 "veri": b64(JPEG)}])
    rows = {row["tur"]: row["id"] for row in desk.sql("SELECT tur, id FROM destek_ekleri")}
    found = desk.manage(ticket_attachment, number, rows["gorsel"])
    assert found == ("ekran görüntüsü.jpg", "image/jpeg", JPEG)
    image = file_response(found, inline=found[1].startswith("image/"))
    assert image.mimetype == "image/jpeg" and image.get_data() == JPEG
    assert image.headers["Content-Disposition"] == 'inline; filename="ekran_g_r_nt_s_.jpg"'
    assert image.headers["X-Content-Type-Options"] == "nosniff"
    flow = file_response(desk.manage(ticket_attachment, number, rows["akis"]))
    assert flow.mimetype == "application/json" and json.loads(flow.get_data()) == {"adimlar": []}
    assert flow.headers["Content-Disposition"] == 'attachment; filename="akis.json"'
    other = desk.number()
    for ticket, identifier in ((number, "f" * 32), (number, "../../etc"), (other, rows["gorsel"]),
                               (999999, rows["gorsel"])):
        with pytest.raises(ApiError) as missing:
            desk.manage(ticket_attachment, ticket, identifier)
        assert missing.value.status == 404, (ticket, identifier)


def test_every_admin_change_is_recorded_without_the_text(desk):
    number = desk.number()
    desk.manage(open_ticket, number)  # reading is not a change
    text = "Müşteri adı geçen gizli yanıt"
    desk.manage(answer_ticket, number, metin=text, durum="inceleniyor")
    desk.manage(answer_ticket, number, durum="yayinlandi", yayin_surumu="1.0.2")
    with pytest.raises(ApiError):
        desk.manage(answer_ticket, number)  # nothing changed, nothing recorded
    rows = desk.sql("SELECT yonetici_id, tur, hedef, ayrinti, zaman FROM yonetim_olaylari ORDER BY id")
    admin = desk.admin["id"]
    assert [(row["yonetici_id"], row["tur"], row["hedef"], row["ayrinti"]) for row in rows] == [
        (admin, "destek_yanit", f"talep:{number}",
         {"onceki_durum": "acik", "durum": "inceleniyor", "yanit_uzunlugu": len(text)}),
        (admin, "destek_durum", f"talep:{number}",
         {"onceki_durum": "inceleniyor", "durum": "yayinlandi", "yanit_uzunlugu": 0, "yayin_surumu": "1.0.2"})]
    assert rows[0]["zaman"] == START and "gizli" not in json.dumps([row["ayrinti"] for row in rows])


def test_messages_written_at_the_same_moment_keep_their_order(desk):
    number = desk.number()
    for text in ("Birinci yanıt", "İkinci yanıt", "Üçüncü yanıt"):
        desk.manage(answer_ticket, number, metin=text)
    thread = desk.get(f"/talepler/{number}").get_json()["talep"]["mesajlar"]
    assert [item["metin"] for item in thread] == ["Satır 1\nSatır 2 <kod> örneği", "Birinci yanıt", "İkinci yanıt",
                                                  "Üçüncü yanıt"]


def test_lists_show_the_latest_first_and_at_most_200(desk):
    desk.sql("INSERT INTO destek_talepleri (kullanici_id, tur, baslik, surum, platform, olusturma, guncelleme) "
             "SELECT %s, 'soru', 'Talep ' || n, '1.0.0', 'macOS', %s, %s + n * interval '1 minute' "
             "FROM generate_series(1, 205) AS n", (desk.ayse["id"], START, START))
    mine = desk.get("/talepler").get_json()["talepler"]
    assert len(mine) == service.LIST_LIMIT and mine[0]["baslik"] == "Talep 205" and mine[-1]["baslik"] == "Talep 6"
    everything = desk.manage(list_tickets)
    assert len(everything["talepler"]) == service.LIST_LIMIT and everything["okunmamis"] == 205


def test_new_numbers_continue_above_the_migrated_ones(desk):
    header = [line.lstrip("- ") for line in (migrate.FOLDER / "0003_destek.sql").read_text(encoding="utf-8")
              .splitlines() if line.startswith("--")]
    assert AFTER_IMPORT in " ".join(" ".join(header).split())
    assert desk.number() == 1001
    # A migrated ticket keeps its number (with its own messages); then the sequence moves past the largest one.
    desk.sql("INSERT INTO destek_talepleri (talep_no, kullanici_id, tur, baslik, surum, platform) "
             "VALUES (1500, %s, 'soru', 'Taşınan talep', '0.9.53', 'macOS')", (desk.mehmet["id"],))
    desk.sql(AFTER_IMPORT)
    assert desk.number() == 1501
    assert [item["no"] for item in desk.get("/talepler", desk.mehmet).get_json()["talepler"]] == [1500]


def test_a_failing_notifier_never_loses_the_request(desk, caplog):
    class Failing:
        def send(self, subject, text):
            raise OSError("bot12345678:" + "B" * 35)

    desk.core.notifier = Failing()
    assert desk.create().status_code == 201
    assert desk.count() == 1 and "Destek bildirimi gönderilemedi." in caplog.text and "B" * 35 not in caplog.text


def test_nothing_is_announced_when_saving_fails(desk, monkeypatch):
    def broken(*_args):
        raise RuntimeError("kayıt sırasında hata")

    monkeypatch.setattr(service, "_detail", broken)
    response = desk.create()
    assert response.status_code == 503 and response.get_json()["kod"] == "SUNUCU"
    assert desk.count() == 0 and desk.count("destek_mesajlari") == 0 and desk.core.notifier.messages == []


def test_database_errors_do_not_reach_the_answer(desk, monkeypatch):
    headers = desk.studio()

    class Broken:
        def transaction(self):
            raise RuntimeError("GIZLI veritabanı parolası")

    monkeypatch.setattr(desk.core, "db", Broken())
    response = desk.client.get("/api/rpa/destek/talepler", headers=headers)
    assert response.status_code == 503 and response.get_json()["kod"] == "SUNUCU"
    assert "GIZLI" not in response.get_data(as_text=True)
