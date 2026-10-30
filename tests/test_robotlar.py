"""Robot Paneli: Studio durum bildirimi (robot belirteci, lisans, sıkı doğrulama, sıklık sınırı), komutların yaşamı
(yazılır, bildirimle gider, sonucu döner, süresi dolar, silinir), panel ve Hesabım görünümleri (üye yalnız kendi
robotunu görür ve yönetir), olay kaydı, art arda hata ve sessizlik uyarıları."""

from __future__ import annotations

import copy
import json
import secrets
from datetime import date, timedelta

import pytest
from conftest import DEVICE, START, make_user, open_session
from test_admin_auth import events, post, signed_in
from test_topluluk import HEADERS, member

from peyrev import tokens, users
from peyrev.robotlar import service

OFFICE, RUN, FLOW, RUN2, FLOW2 = "0" * 31 + "1", "a1" * 16, "b1" * 16, "a2" * 16, "b2" * 16
URL = "/api/rpa/robot/durum"


def status(**changes):
    body = {
        "surum": 1, "kurulum": DEVICE, "uygulama": "1.5.0", "isletim_sistemi": "Windows 11", "etiket": "Muhasebe PC",
        "ekran_kilitli": False, "bos_disk": 42, "sirada": 0,
        "ofisler": [{
            "id": OFFICE, "ad": "Satış Ofisi", "telegram": True,
            "calisan": [{"run_id": RUN, "workflow_id": FLOW, "akis": "Fatura girişi",
                         "baslangic": "2026-10-26T08:55:00+00:00", "adim": 3}],
            "son_24_saat": {"basarili": 4, "hatali": 1, "iptal": 0}, "ardisik_hata": 1,
            "son_hatalar": [{"run_id": RUN2, "workflow_id": FLOW2, "akis": "Banka mutabakatı",
                             "zaman": "2026-10-26T08:30:00+00:00", "kategori": "baglanti"}],
        }],
    }
    body.update(changes)
    return body


def failures(count, start="2026-10-26T08:00:00+00:00", category="hedef_bulunamadi"):
    from datetime import datetime

    first = datetime.fromisoformat(start)
    return [{"run_id": secrets.token_hex(16), "workflow_id": FLOW2, "akis": "Banka mutabakatı",
             "zaman": (first + timedelta(minutes=index)).isoformat(), "kategori": category} for index in range(count)]


class Fleet:
    """Bir yönetici ve iki lisanslı üye; her üyenin açık bir Studio oturumu var."""

    def __init__(self, core, app):
        self.core, self.app = core, app
        self.admin = make_user(core, "yonetici@peyrev.test", rol="yonetici", password="Gizli12345")
        self.ayse = make_user(core, "ayse@ornek.test", modules={users.RPA: None}, ad_soyad="Ayşe Yılmaz")
        self.mehmet = make_user(core, "mehmet@ornek.test", modules={users.RPA: None}, ad_soyad="Mehmet Kaya")
        self.sessions = {user["id"]: open_session(core, user) for user in (self.ayse, self.mehmet)}

    def headers(self, user=None, purpose="robot"):
        user = user or self.ayse
        return {"Authorization": "Bearer " + tokens.issue(self.core, purpose, user, DEVICE, self.sessions[user["id"]])}

    def report(self, body=None, user=None, client=None, raw=None):
        client = client or self.app.test_client()
        if raw is not None:
            return client.post(URL, data=raw, content_type="application/json", headers=self.headers(user))
        return client.post(URL, json=status() if body is None else body, headers=self.headers(user))

    def later(self, **delta):
        self.core.clock.advance(**delta)

    def sql(self, statement, parameters=()):
        with self.core.db.transaction() as cur:
            cur.execute(statement, parameters)
            return cur.fetchall() if cur.description else []

    def robot_id(self, user=None):
        return self.sql("SELECT id FROM robotlar WHERE kullanici_id = %s", ((user or self.ayse)["id"],))[0]["id"]


@pytest.fixture
def fleet(core, app):
    return Fleet(core, app)


def admin_post(browser, path, body):
    return post(browser, path, body)


# ----- kimlik --------------------------------------------------------------------------------------------------------
def test_the_license_answer_carries_a_robot_token_that_reports(app, core):
    make_user(core, "ali@ornek.test", modules={users.RPA: None})
    client = app.test_client()
    signed = client.post("/api/rpa/lisans/giris", json={"kullanici": "ali@ornek.test", "sifre": "Gizli12345",
                                                         "cihaz": DEVICE, "surum": "1.5.0", "protokol": 2,
                                                         "nonce": secrets.token_hex(16)}).get_json()
    token = signed["robot_erisim"]
    response = client.post(URL, json=status(), headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == {"success": True, "komutlar": [], "aralik": 300}
    # The token is bound to this computer: another installation identifier is refused.
    other = client.post(URL, json=status(kurulum="b" * 32), headers={"Authorization": "Bearer " + token})
    assert (other.status_code, other.get_json()["kod"]) == (403, "YETKI_YOK")


def test_a_report_needs_a_robot_token_an_open_session_and_a_license(fleet, core):
    assert fleet.app.test_client().post(URL, json=status()).status_code == 401
    # The support token of the same session is for support requests only.
    support = fleet.app.test_client().post(URL, json=status(), headers=fleet.headers(purpose="destek"))
    assert (support.status_code, support.get_json()["kod"]) == (401, "OTURUM_GECERSIZ")
    headers = fleet.headers()
    fleet.sql("UPDATE kullanici_modulleri SET bitis = %s WHERE kullanici_id = %s", (date(2026, 10, 1), fleet.ayse["id"]))
    expired = fleet.app.test_client().post(URL, json=status(), headers=headers)
    assert (expired.status_code, expired.get_json()["kod"]) == (403, "YETKI_YOK")
    fleet.sql("UPDATE kullanici_modulleri SET bitis = NULL")
    fleet.sql("UPDATE oturumlar SET durum = 'kapandi' WHERE kullanici_id = %s", (fleet.ayse["id"],))
    closed = fleet.app.test_client().post(URL, json=status(), headers=headers)
    assert (closed.status_code, closed.get_json()["kod"]) == (401, "OTURUM_GECERSIZ")
    assert fleet.sql("SELECT * FROM robotlar") == []


# ----- doğrulama -----------------------------------------------------------------------------------------------------
def broken_bodies():
    office = status()["ofisler"][0]

    def with_office(**changes):
        return status(ofisler=[{**copy.deepcopy(office), **changes}])

    running = office["calisan"][0]
    failure = office["son_hatalar"][0]
    return [
        status(bilinmeyen=1),                                           # unknown key at the top
        with_office(fazla=True),                                        # unknown key in an office
        with_office(calisan=[{**running, "degiskenler": {"a": 1}}]),     # variables never travel
        with_office(son_hatalar=[{**failure, "mesaj": "C:\\gizli\\dosya.xlsx bulunamadı"}]),  # nor error text
        with_office(son_hatalar=[{**failure, "kategori": "bilinmiyor"}]),
        with_office(son_24_saat={"basarili": "4", "hatali": 1, "iptal": 0}),
        with_office(son_24_saat={"basarili": True, "hatali": 1, "iptal": 0}),
        with_office(telegram="evet"),
        with_office(id="SATIS"),
        with_office(calisan=[{**running, "baslangic": "2026-10-26T08:55:00"}]),  # no time zone
        with_office(calisan=[running, running]),
        with_office(calisan=[{**running, "run_id": secrets.token_hex(16)} for _ in range(11)]),
        with_office(son_hatalar=failures(6)),
        status(ofisler=[{**copy.deepcopy(office), "id": secrets.token_hex(16)} for _ in range(51)]),
        status(ofisler=[office, office]),
        status(bos_disk=101), status(bos_disk=4.5), status(ekran_kilitli="hayir"), status(sirada=-1),
        status(etiket=""), status(etiket="x" * 61), status(etiket="satır\nkırığı"), status(uygulama="beş"),
        status(isletim_sistemi=7), status(ofisler={}),
        status(yanitlar=[{"id": 1, "sonuc": "harika"}]), status(yanitlar=[{"id": "1", "sonuc": "tamam"}]),
        {k: v for k, v in status().items() if k != "ofisler"},
        {k: v for k, v in status().items() if k != "kurulum"},
    ]


def test_invalid_reports_are_refused_and_store_nothing(fleet):
    for body in broken_bodies():
        response = fleet.report(body)
        assert (response.status_code, response.get_json()["kod"]) == (400, "GECERSIZ"), body
    wrong_version = fleet.report(status(surum=2))
    assert (wrong_version.status_code, wrong_version.get_json()["kod"]) == (400, "SURUM")
    for raw in (b"[]", b'{"surum": 1, "surum": 1, "kurulum": "' + DEVICE.encode() + b'"}', b"{", b'{"surum": NaN}'):
        assert fleet.report(raw=raw).status_code == 400
    big = json.dumps(status(ofisler=[{**copy.deepcopy(status()["ofisler"][0]), "id": secrets.token_hex(16),
                                      "ad": "x" * 120, "son_hatalar": failures(5)} for _ in range(50)])).encode()
    assert len(big) > service.BODY_LIMIT
    too_big = fleet.report(raw=big)
    assert (too_big.status_code, too_big.get_json()["kod"]) == (413, "GOVDE_SINIRI")
    assert fleet.sql("SELECT * FROM robotlar") == []
    fifty = status(ofisler=[{**copy.deepcopy(status()["ofisler"][0]), "id": secrets.token_hex(16)} for _ in range(50)])
    assert fleet.report(fifty).status_code == 200
    rows = fleet.sql("SELECT * FROM robotlar")
    assert len(rows) == 1 and len(rows[0]["durum"]["ofisler"]) == 50


def test_a_report_is_stored_and_reports_come_at_most_every_twenty_seconds(fleet):
    assert fleet.report().status_code == 200
    [robot] = fleet.sql("SELECT * FROM robotlar")
    assert (robot["kurulum_kimligi"], robot["etiket"], robot["surum"], robot["isletim_sistemi"], robot["kapali"]) == (
        DEVICE, "Muhasebe PC", "1.5.0", "Windows 11", False)
    assert robot["durum"]["ofisler"][0]["calisan"][0]["adim"] == 3
    fleet.later(seconds=5)
    soon = fleet.report()
    assert (soon.status_code, soon.get_json()["kod"]) == (429, "COK_SIK")
    fleet.later(seconds=20)
    assert fleet.report(status(etiket="  Yeni   ad ")).status_code == 200
    assert fleet.sql("SELECT etiket FROM robotlar")[0]["etiket"] == "Yeni ad"
    # Closing the Studio is always heard and marks the robot closed; the next report opens it again.
    assert fleet.report({"surum": 1, "kurulum": DEVICE, "kapaniyor": True}).status_code == 200
    [closed] = fleet.sql("SELECT * FROM robotlar")
    assert closed["kapali"] is True and closed["durum"]["etiket"] == "Yeni ad"
    fleet.later(seconds=1)
    assert fleet.report().status_code == 200
    assert fleet.sql("SELECT kapali FROM robotlar")[0]["kapali"] is False


def test_an_account_keeps_its_twenty_most_recent_computers(fleet):
    for number in range(service.ROBOTS_PER_ACCOUNT + 2):
        device = f"{number:032x}"
        session = open_session(fleet.core, fleet.ayse, device=device, session_id=f"{number + 100:032x}")
        token = tokens.issue(fleet.core, "robot", fleet.ayse, device, session)
        response = fleet.app.test_client().post(URL, json=status(kurulum=device), headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
        fleet.later(minutes=1)
    devices = {row["kurulum_kimligi"] for row in fleet.sql("SELECT kurulum_kimligi FROM robotlar")}
    assert len(devices) == service.ROBOTS_PER_ACCOUNT and f"{0:032x}" not in devices


# ----- komutlar ------------------------------------------------------------------------------------------------------
def test_a_command_travels_with_the_next_report_and_its_result_comes_back(fleet, app, core):
    assert fleet.report().status_code == 200
    robot = fleet.robot_id()
    browser = signed_in(app)
    created = admin_post(browser, f"robotlar/{robot}/komut", {"tur": "durdur", "hedef": RUN})
    assert created.status_code == 200, created.get_json()
    [command] = created.get_json()["robot"]["komutlar"]
    assert (command["tur"], command["akis"], command["sonuc"], command["sonuc_adi"]) == (
        "durdur", "Fatura girişi", None, "Robotun bildirimi bekleniyor")
    [event] = events(core, "robot_komutu")
    assert (event["yonetici_id"], event["hedef"], event["ayrinti"]) == (
        fleet.admin["id"], f"robot:{robot}", {"komut": "durdur", "akis": "Fatura girişi", "kullanici_id": fleet.ayse["id"]})
    again = admin_post(browser, f"robotlar/{robot}/komut", {"tur": "durdur", "hedef": RUN})
    assert (again.status_code, again.get_json()["kod"]) == (409, "BEKLIYOR")
    fleet.later(minutes=5)
    answer = fleet.report().get_json()
    assert answer["komutlar"] == [{"id": command["id"], "tur": "durdur", "run_id": RUN, "ofis_id": OFFICE}]
    fleet.later(minutes=5)
    assert fleet.report().get_json()["komutlar"] == []  # sent once
    fleet.later(minutes=5)
    # A result for a command of another robot, or one never sent, changes nothing.
    assert fleet.report(status(yanitlar=[{"id": command["id"], "sonuc": "tamam"}, {"id": 999, "sonuc": "tamam"}])
                        ).status_code == 200
    [row] = fleet.sql("SELECT * FROM robot_komutlari")
    assert (row["sonuc"], row["sonuclandi"]) == ("tamam", core.clock.now())
    listed = browser.get("/api/yonetim/robotlar").get_json()["robotlar"][0]["komutlar"][0]
    assert (listed["sonuc"], listed["sonuc_adi"]) == ("tamam", "Yapıldı")


def test_commands_name_only_what_the_robot_reported(fleet, app):
    assert fleet.report().status_code == 200
    robot = fleet.robot_id()
    browser = signed_in(app)
    for body in ({"tur": "durdur", "hedef": FLOW2}, {"tur": "yeniden_calistir", "hedef": RUN}):
        response = admin_post(browser, f"robotlar/{robot}/komut", body)
        assert (response.status_code, response.get_json()["kod"]) == (409, "HEDEF_YOK"), body
    for body in ({"tur": "sil", "hedef": RUN}, {"tur": "durdur", "hedef": "../x"}, {"tur": "durdur"},
                 {"tur": "durdur", "hedef": RUN, "fazla": 1}):
        assert admin_post(browser, f"robotlar/{robot}/komut", body).status_code == 400
    assert admin_post(browser, "robotlar/999999/komut", {"tur": "durdur", "hedef": RUN}).status_code == 404
    rerun = admin_post(browser, f"robotlar/{robot}/komut", {"tur": "yeniden_calistir", "hedef": FLOW2})
    assert rerun.status_code == 200
    # A robot that has gone quiet or closed gets no new command.
    fleet.later(minutes=61)
    quiet = admin_post(browser, f"robotlar/{robot}/komut", {"tur": "durdur", "hedef": RUN})
    assert (quiet.status_code, quiet.get_json()["kod"]) == (409, "ROBOT_KAPALI")
    assert fleet.sql("SELECT count(*) AS n FROM robot_komutlari")[0]["n"] == 1


def test_commands_expire_unanswered_and_are_deleted_after_thirty_days(fleet, app):
    assert fleet.report().status_code == 200
    robot = fleet.robot_id()
    browser = signed_in(app)
    assert admin_post(browser, f"robotlar/{robot}/komut", {"tur": "durdur", "hedef": RUN}).status_code == 200
    fleet.later(minutes=31)
    assert browser.get("/api/yonetim/robotlar").get_json()["robotlar"][0]["komutlar"][0]["sonuc"] == "suresi_doldu"
    assert fleet.report().get_json()["komutlar"] == []  # an expired command never reaches the robot
    assert fleet.sql("SELECT sonuc FROM robot_komutlari")[0]["sonuc"] == "suresi_doldu"
    fleet.later(days=31)
    assert fleet.report().status_code == 200
    assert fleet.sql("SELECT * FROM robot_komutlari") == []


# ----- panel ve Hesabım ----------------------------------------------------------------------------------------------
def test_the_panel_lists_every_robot_with_its_state(fleet, app, core):
    assert fleet.report(status(bos_disk=5, ekran_kilitli=True)).status_code == 200
    assert fleet.report(status(etiket="Depo"), user=fleet.mehmet).status_code == 200
    browser = signed_in(app)
    fleet.later(minutes=11)
    data = browser.get("/api/yonetim/robotlar").get_json()
    assert data["kategoriler"]["baglanti"] == "Bağlantı hatası"
    first, second = data["robotlar"]
    assert {first["kullanici"]["giris"], second["kullanici"]["giris"]} == {"ayse@ornek.test", "mehmet@ornek.test"}
    ayse = first if first["kullanici"]["giris"] == "ayse@ornek.test" else second
    assert (ayse["renk"], ayse["dakika"], ayse["disk_uyari"], ayse["ekran_kilitli"], ayse["komut_verilebilir"]) == (
        "sari", 11, True, True, True)
    office = ayse["ofisler"][0]
    assert office["son_hatalar"][0]["kategori_adi"] == "Bağlantı hatası"
    assert office["calisan"][0]["baslangic"] == "2026-10-26 11:55"  # Türkiye saati
    assert office["son_24_saat"] == {"basarili": 4, "hatali": 1, "iptal": 0}
    fleet.later(minutes=50)
    assert browser.get("/api/yonetim/robotlar").get_json()["robotlar"][0]["renk"] == "kirmizi"
    # Studio's own token is no panel session.
    assert fleet.app.test_client().get("/api/yonetim/robotlar", headers=fleet.headers()).status_code == 401


def test_a_member_sees_and_commands_only_their_own_robots(fleet, app, core):
    assert fleet.report().status_code == 200
    assert fleet.report(user=fleet.mehmet).status_code == 200
    ayse_robot, mehmet_robot = fleet.robot_id(), fleet.robot_id(fleet.mehmet)
    ayse = member(app, "ayse@ornek.test")
    [mine] = ayse.get("/api/uye/robotlar").get_json()["robotlar"]
    assert mine["id"] == ayse_robot and "kullanici" not in mine
    refused = ayse.post(f"/api/uye/robotlar/{mehmet_robot}/komut", json={"tur": "durdur", "hedef": RUN}, headers=HEADERS)
    assert (refused.status_code, refused.get_json()["kod"]) == (404, "BULUNAMADI")
    no_header = ayse.post(f"/api/uye/robotlar/{ayse_robot}/komut", json={"tur": "durdur", "hedef": RUN})
    assert (no_header.status_code, no_header.get_json()["kod"]) == (403, "CSRF")
    assert app.test_client().get("/api/uye/robotlar").status_code == 401
    done = ayse.post(f"/api/uye/robotlar/{ayse_robot}/komut", json={"tur": "yeniden_calistir", "hedef": FLOW2},
                     headers=HEADERS)
    assert done.status_code == 200 and done.get_json()["robot"]["komutlar"][0]["olusturan"] == "uye"
    [event] = events(core, "robot_komutu_uye")
    assert (event["yonetici_id"], event["hedef"], event["ayrinti"]) == (
        None, f"robot:{ayse_robot}", {"komut": "yeniden_calistir", "akis": "Banka mutabakatı",
                                      "kullanici_id": fleet.ayse["id"], "kaynak": "hesabim"})
    assert [row["robot_id"] for row in fleet.sql("SELECT robot_id FROM robot_komutlari")] == [ayse_robot]
    # Mehmet's robot hears nothing of it.
    fleet.later(minutes=5)
    assert fleet.report(user=fleet.mehmet).get_json()["komutlar"] == []
    assert len(fleet.report().get_json()["komutlar"]) == 1


# ----- uyarılar ------------------------------------------------------------------------------------------------------
def streak(count, start="2026-10-26T08:00:00+00:00"):
    office = copy.deepcopy(status()["ofisler"][0])
    office.update(ardisik_hata=count, son_hatalar=failures(min(count, 5), start))
    return status(ofisler=[office])


def robot_alerts(core):
    return [message for message in core.notifier.messages if message[0].startswith("Robot uyarısı")]


def test_three_failed_runs_in_a_row_alert_the_owner_once_an_hour(fleet, core):
    assert fleet.report(streak(2)).status_code == 200
    assert robot_alerts(core) == []
    fleet.later(minutes=1)
    assert fleet.report(streak(3)).status_code == 200
    [(subject, text)] = robot_alerts(core)
    assert subject == "Robot uyarısı: art arda 3 hatalı çalışma · Muhasebe PC"
    assert "ayse@ornek.test" in text and "Banka mutabakatı · Hedef bulunamadı" in text and "/yonetim/#robotlar" in text
    fleet.later(minutes=5)
    assert fleet.report(streak(3)).status_code == 200  # the same failures: no new alert
    fleet.later(minutes=5)
    assert fleet.report(streak(4, "2026-10-26T09:00:00+00:00")).status_code == 200  # new, but within the hour
    assert len(robot_alerts(core)) == 1
    fleet.later(hours=1)
    assert fleet.report(streak(4, "2026-10-26T09:00:00+00:00")).status_code == 200
    assert len(robot_alerts(core)) == 2
    fleet.later(hours=2)
    assert fleet.report(streak(4, "2026-10-26T09:00:00+00:00")).status_code == 200  # nothing newer
    assert len(robot_alerts(core)) == 2


def test_a_silent_robot_is_reported_once_until_it_reports_again(fleet, core):
    assert fleet.report().status_code == 200
    fleet.later(minutes=14)
    assert service.check_silence(core) == 0
    fleet.later(minutes=2)
    assert service.check_silence(core) == 1
    [(subject, text)] = robot_alerts(core)
    assert "15 dakikadır" in subject and "Muhasebe PC · ayse@ornek.test" in text
    fleet.later(minutes=20)
    assert service.check_silence(core) == 0
    assert fleet.report().status_code == 200
    fleet.later(minutes=16)
    assert service.check_silence(core) == 1
    # A Studio closed on purpose, or one silent for more than a day, raises nothing.
    assert fleet.report({"surum": 1, "kurulum": DEVICE, "kapaniyor": True}).status_code == 200
    fleet.later(minutes=30)
    assert service.check_silence(core) == 0
    fleet.sql("UPDATE robotlar SET kapali = FALSE, son_gorulme = %s, sessizlik_uyarisi = NULL",
              (core.clock.now() - timedelta(hours=25),))
    assert service.check_silence(core) == 0
    assert len(robot_alerts(core)) == 2


def test_reports_check_for_silent_robots_at_most_once_a_minute(fleet, core):
    assert fleet.report(user=fleet.mehmet).status_code == 200
    fleet.later(minutes=16)
    assert fleet.report().status_code == 200  # Ayşe's report notices that Mehmet's robot has gone quiet
    assert len(robot_alerts(core)) == 1
    assert core.extras["robot_sessizlik_denetimi"] == core.clock.now()
    fleet.sql("UPDATE robotlar SET sessizlik_uyarisi = NULL")
    fleet.later(seconds=30)
    assert fleet.report().status_code == 200  # checked less than a minute ago: not again
    assert len(robot_alerts(core)) == 1
    fleet.later(seconds=31)
    assert fleet.report().status_code == 200
    assert len(robot_alerts(core)) == 2


def test_the_silence_check_runs_from_the_command_line(fleet, core, monkeypatch, capsys):
    from peyrev.robotlar import __main__ as entry

    assert fleet.report().status_code == 200
    fleet.later(minutes=16)
    monkeypatch.setattr(entry, "Settings", type("S", (), {"from_environment": staticmethod(lambda: core.settings)}))
    monkeypatch.setattr(entry, "Clock", lambda: core.clock)
    sent = []
    monkeypatch.setattr(entry, "Notifier", lambda settings, background: type(
        "N", (), {"send": lambda self, subject, text: sent.append((subject, background))})())
    assert entry.main() == 0
    assert capsys.readouterr().out.strip() == "uyarilan_robot=1"
    assert sent == [(sent[0][0], False)] and START < core.clock.now()
