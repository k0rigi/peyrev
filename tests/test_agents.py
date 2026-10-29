"""Ajan yetkisi: lisans uzantısı, ofis seçimi, belirteçler, kilit sırası ve /api/studio/ekip kapısı (gerçek PostgreSQL).

Diğer ajan test dosyaları buradaki yardımcıları (Agents, yük üreticileri) kullanır.
"""

import json
import re
import uuid
from datetime import UTC, date, datetime, time, timedelta

import jwt
import pytest
from conftest import DEVICE, START, make_user, open_session

from peyrev import tokens, users
from peyrev.agents import entitlement, records
from peyrev.agents.entitlement import authenticate, license_extension
from peyrev.clock import TR, FixedClock
from peyrev.common import ApiError

LICENSED = {users.RPA: None, users.AGENTS: None}
OTHER_DEVICE = "b" * 32
API = "/api/studio/ekip"


class Agents:
    """Licensed accounts with an open Studio session each; tokens and identities as Studio presents them."""

    def __init__(self, core):
        self.core = core
        self.sessions = {}

    def account(self, giris, modules=LICENSED, **columns):
        # Most agent tests use several offices; the default limit of one office has its own tests (test_offices.py).
        columns.setdefault("ofis_siniri", entitlement.MAX_OFFICES)
        user = make_user(self.core, giris, modules=dict(modules), **columns)
        self.sessions[user["id"]] = open_session(self.core, user)
        return user

    def second_session(self, user):
        """The same account signed in on another computer while the first session is still open."""
        return open_session(self.core, user, OTHER_DEVICE, "%032x" % (user["id"] * 104729))

    def token(self, user, session=None, device=DEVICE):
        session = session or self.sessions[user["id"]]
        with self.core.db.transaction() as cur:
            current = users.get(cur, user["id"])
            _extension, limit = license_extension(self.core, cur, current, session)
        return tokens.issue(self.core, "ajan", current, device, session, expires=limit)

    def identity(self, user, office=None, session=None, device=DEVICE):
        with self.core.db.transaction() as cur:
            return authenticate(self.core, cur, "Bearer " + self.token(user, session, device), office)

    def headers(self, user, office=None):
        headers = {"Authorization": "Bearer " + self.token(user)}
        if office:
            headers["X-Rpa-Ofis"] = office
        return headers

    def office(self, user, name="Satış Ofisi"):
        """An office set up the way Studio does it, a second after the previous one (offices are ordered by time)."""
        self.core.clock.advance(seconds=1)
        return records.create_office(self.core, self.identity(user), name)["id"]

    def add_office(self, user, name, *, office_id=None, durum="aktif", created=START):
        """An office row as a migration writes it (own identifier, status and creation time)."""
        office_id = office_id or uuid.uuid4().hex
        self.sql("INSERT INTO ajan_ofisleri (id, sahip_id, ad, durum, olusturma, guncelleme) "
                 "VALUES (%s, %s, %s, %s, %s, %s)", (office_id, user["id"], name, durum, created, created))
        return office_id

    def sql(self, statement, params=()):
        with self.core.db.transaction() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if cur.description else cur.rowcount


@pytest.fixture
def agents(core):
    return Agents(core)


def extension_of(core, user, session=None, office=None, lock=False):
    # The user's Studio session as open_session wrote it (its version decides whether several offices may open).
    session = session or "%032x" % (user["id"] * 7919)
    with core.db.transaction() as cur:
        return license_extension(core, cur, users.get(cur, user["id"]), session, office=office, lock=lock)


def task_payload(assignee, **changes):
    return dict({"request_id": uuid.uuid4().hex, "title": "Kontrol görevi",
                 "instructions": "Yalnız atanmış kişinin göreceği iş açıklaması.", "assignee_user_id": assignee},
                **changes)


def approval_payload(**changes):
    return dict({"request_id": uuid.uuid4().hex, "run_id": uuid.uuid4().hex, "workflow_id": uuid.uuid4().hex,
                 "workflow_digest": "a" * 64, "inputs_digest": "b" * 64, "preview": {"deger": "***"},
                 "reason": "Kullanıcının hedefindeki güncelleme.", "attempt": 1, "expires_in": 600}, **changes)


def consume_payload(approval):
    return {key: approval[key] for key in
            ("run_id", "workflow_id", "workflow_digest", "inputs_digest", "preview_digest", "attempt")}


def refused(work, kod, status=None):
    with pytest.raises(ApiError) as error:
        work()
    assert error.value.kod == kod, (error.value.kod, str(error.value))
    if status is not None:
        assert error.value.status == status
    return error.value


def studio_reads(extension, user):
    """Studio's licensing._agent_claims: any broken rule silently switches the agents off (None)."""
    value = extension
    if value.get("surum") != 1 or value.get("aktif") is not True:
        return None
    user_id, company_id, owner_id = value["kullanici_id"], value["firma_id"], value["onayci_id"]
    if (any(type(item) is not int or item < 1 for item in (user_id, company_id, owner_id))
            or user_id != user["id"] or company_id != user["alan_id"]):
        return None
    session, permissions = value["oturum_id"], value["yetkiler"]
    if (not isinstance(session, str) or not 1 <= len(session) <= 200 or not isinstance(permissions, list)
            or len(permissions) > 10 or any(item not in entitlement.CAPABILITIES for item in permissions)):
        return None
    if user_id != owner_id and {"agents.manage", "agents.approve"} & set(permissions):
        return None
    if value.get("bitis") is not None:
        date.fromisoformat(value["bitis"])
    office, name = value.get("ofis_id"), value.get("ofis_adi")
    if office is not None and (not re.fullmatch(r"[a-f0-9]{32}", office) or not isinstance(name, str)
                               or not 1 <= len(name) <= 120 or any(ord(char) < 32 for char in name)):
        return None
    listed, offices = value.get("ofisler"), []
    if listed is not None:
        if not isinstance(listed, list) or len(listed) > 50:
            return None
        for item in listed:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    or not re.fullmatch(r"[a-f0-9]{32}", item["id"]) or not isinstance(item.get("ad"), str)
                    or not 1 <= len(item["ad"]) <= 120 or any(ord(char) < 32 for char in item["ad"])
                    or type(item.get("yetkili")) is not bool):
                return None
            offices.append((item["id"], item["ad"], item["yetkili"]))
        identities = [item[0] for item in offices]
        if len(set(identities)) != len(identities) or (office is None) != (not offices) or (
                office is not None and office not in identities):
            return None
    elif office is not None:
        offices.append((office, name, owner_id == user_id))
    return {"office": office, "offices": offices, "permissions": tuple(permissions),
            "can_create": value.get("ofis_kurabilir") is True}


# ----- license extension ----------------------------------------------------------------------------------------
def test_a_licensed_account_without_offices_uses_agents_and_may_set_up_an_office(agents, core):
    user = agents.account("ayse@peyrev.test", alan_id=8)
    extension, limit = extension_of(core, user, session="c" * 32)
    assert extension == {"surum": 1, "aktif": True, "firma_id": 8, "kullanici_id": user["id"],
                         "onayci_id": user["id"], "oturum_id": "c" * 32, "bitis": None, "yetkiler": ["agents.use"],
                         "ofis_id": None, "ofis_adi": None, "ofis_kurabilir": True}
    assert limit is None
    assert studio_reads(extension, user) == {"office": None, "offices": [], "permissions": ("agents.use",),
                                             "can_create": True}


@pytest.mark.parametrize("module", [users.RPA, users.AGENTS])
@pytest.mark.parametrize("mode", ["missing", "expired"])
def test_an_admin_has_no_agent_privilege_and_both_module_rows_are_needed(agents, core, module, mode):
    today = core.clock.today_tr()
    modules = dict(LICENSED)
    if mode == "missing":
        del modules[module]
    else:
        modules[module] = today - timedelta(days=2)
    admin = agents.account("yonetici@peyrev.test", modules=modules, rol="yonetici")
    extension, limit = extension_of(core, admin)
    assert extension["aktif"] is False and extension["yetkiler"] == [] and extension["onayci_id"] is None
    assert "ofis_kurabilir" not in extension and limit is None
    assert extension["bitis"] == (None if mode == "missing" else (today - timedelta(days=2)).isoformat())
    refused(lambda: agents.identity(admin), "AJAN_YETKISI_YOK", 403)


def test_an_inactive_account_gets_no_agent_rights(agents, core):
    user = agents.account("pasif@peyrev.test", durum="pasif")
    extension, limit = extension_of(core, user)
    assert extension["aktif"] is False and extension["bitis"] is None and limit is None


def test_the_end_day_is_inclusive_and_the_earlier_module_end_wins(agents, core):
    today = core.clock.today_tr()
    user = agents.account("b@peyrev.test", modules={users.RPA: today + timedelta(days=10), users.AGENTS: today})
    extension, limit = extension_of(core, user)
    assert extension["aktif"] is True and extension["bitis"] == today.isoformat()
    # The agent token never outlives the license day: it ends at the next midnight in Türkiye.
    assert limit == datetime.combine(today + timedelta(days=1), time(), TR)
    assert studio_reads(extension, user) is not None
    core.clock.advance(days=1)
    extension, limit = extension_of(core, user)
    assert extension["aktif"] is False and extension["bitis"] == today.isoformat() and limit is None


def test_the_owner_holds_every_right_in_each_office_and_the_first_office_is_the_default(agents, core):
    owner, other = agents.account("sahip@peyrev.test"), agents.account("baska@peyrev.test")
    later = agents.add_office(owner, "Lojistik", office_id="b" * 32, created=START)
    first = agents.add_office(owner, "Claim & İade", office_id="c" * 32, created=START - timedelta(days=3))
    same_time = agents.add_office(owner, "Arşiv", office_id="a" * 32, created=START)
    foreign = agents.add_office(other, "Başkasının Ofisi")
    extension, _limit = extension_of(core, owner)
    assert (extension["ofis_id"], extension["ofis_adi"]) == (first, "Claim & İade")
    assert extension["ofisler"] == [{"id": first, "ad": "Claim & İade", "yetkili": True},
                                    {"id": same_time, "ad": "Arşiv", "yetkili": True},
                                    {"id": later, "ad": "Lojistik", "yetkili": True}]
    assert extension["yetkiler"] == ["agents.use", "agents.manage", "agents.approve"]
    assert extension["onayci_id"] == owner["id"] and extension["ofis_kurabilir"] is True
    assert studio_reads(extension, owner)["offices"] == [(first, "Claim & İade", True), (same_time, "Arşiv", True),
                                                         (later, "Lojistik", True)]
    chosen, _limit = extension_of(core, owner, office=later)
    assert chosen["ofis_id"] == later and chosen["ofisler"] == extension["ofisler"]
    assert agents.identity(owner, later).office_id == later
    assert agents.identity(owner, "").office_id == first  # an empty header means "no office chosen"
    for office in (foreign, "f" * 32):
        assert extension_of(core, owner, office=office)[0]["ofis_erisimi_yok"] is True
        refused(lambda office=office: agents.identity(owner, office), "OFIS_ERISIMI_YOK", 403)
    refused(lambda: agents.identity(owner, "OFIS"), "GECERSIZ_GIRDI", 400)
    refused(lambda: agents.identity(owner, "C" * 32), "GECERSIZ_GIRDI", 400)


def test_a_closed_office_is_out_of_reach_but_still_counts_toward_the_limit(agents, core):
    owner = agents.account("kapali@peyrev.test")
    closed = agents.add_office(owner, "Kapalı Ofis", durum="pasif")
    extension, _limit = extension_of(core, owner)
    # Only open offices are listed: an account whose offices are all closed is an account without an office.
    assert extension["ofis_id"] is None and "ofisler" not in extension and extension["yetkiler"] == ["agents.use"]
    refused(lambda: agents.identity(owner, closed), "OFIS_ERISIMI_YOK", 403)
    for number in range(entitlement.MAX_OFFICES - 1):
        agents.add_office(owner, f"Ofis {number}")
    extension, _limit = extension_of(core, owner)
    assert len(extension["ofisler"]) == 49 and "ofis_kurabilir" not in extension
    refused(lambda: records.create_office(core, agents.identity(owner), "Elli Birinci"), "OFIS_SINIRI", 409)


def test_fifty_offices_is_the_limit_and_studio_still_accepts_the_list(agents, core):
    owner = agents.account("elli@peyrev.test")
    identity = agents.identity(owner)
    for number in range(entitlement.MAX_OFFICES):
        core.clock.advance(seconds=1)
        records.create_office(core, identity, f"Departman {number}")
    error = refused(lambda: records.create_office(core, identity, "Bir Fazlası"), "OFIS_SINIRI", 409)
    assert str(error) == "Hesabınızın ofis sınırına ulaşıldı."
    extension, _limit = extension_of(core, owner)
    assert len(extension["ofisler"]) == 50 and "ofis_kurabilir" not in extension
    assert extension["ofis_adi"] == "Departman 0"
    assert studio_reads(extension, owner)["can_create"] is False


class Recording:
    """A cursor that remembers its statements, to see the order in which rows are locked."""

    def __init__(self, cursor):
        self.cursor, self.statements = cursor, []

    def execute(self, statement, params=()):
        self.statements.append(" ".join(statement.split()))
        return self.cursor.execute(statement, params)

    def __getattr__(self, name):
        return getattr(self.cursor, name)


def locks(statements):
    return [re.search(r"FROM (\w+)", statement).group(1) for statement in statements if "FOR UPDATE" in statement]


def test_with_lock_the_account_row_is_the_first_lock(agents, core):
    owner = agents.account("kilit@peyrev.test")
    agents.office(owner)
    token = "Bearer " + agents.token(owner)
    with core.db.transaction() as cur:
        recording = Recording(cur)
        identity = authenticate(core, recording, token, lock=True)
    assert identity.entitlement["aktif"] and identity.office_id
    # Order of the first lock on each table (locking the already locked account row again is a no-op).
    assert list(dict.fromkeys(locks(recording.statements))) == [
        "kullanicilar", "oturumlar", "kullanici_modulleri", "ajan_ofisleri"]
    with core.db.transaction() as cur:
        recording = Recording(cur)
        license_extension(core, recording, owner, agents.sessions[owner["id"]], lock=True)
    assert recording.statements[0] == "SELECT * FROM kullanicilar WHERE id = %s FOR UPDATE"
    with core.db.transaction() as cur:
        recording = Recording(cur)
        authenticate(core, recording, token)
    assert locks(recording.statements) == []  # the HTTP gate itself never locks


class Ticking(FixedClock):
    """Every reading moves the clock on: the second look at the date comes after midnight."""

    def now(self):
        moment = self.at
        self.at += timedelta(seconds=2)
        return moment


def test_the_end_day_is_checked_again_after_waiting_for_the_locks(agents, core):
    ends = date(2026, 10, 26)
    user = agents.account("gece@peyrev.test", modules={users.RPA: None, users.AGENTS: ends})
    core.clock = Ticking(datetime(2026, 10, 26, 20, 59, 59, tzinfo=UTC))  # 23:59:59 in Türkiye
    extension, limit = extension_of(core, user, lock=True)
    assert extension["aktif"] is False and extension["yetkiler"] == [] and limit is None


def test_the_agent_token_lasts_twelve_minutes_keeps_the_session_and_ends_with_the_license(agents, core):
    user = agents.account("sure@peyrev.test")
    first = jwt.decode(agents.token(user), options={"verify_signature": False})
    again = jwt.decode(agents.token(user), options={"verify_signature": False})
    assert first["oturum"] == again["oturum"] == agents.sessions[user["id"]]
    assert first["exp"] - first["iat"] == tokens.TOKEN_SECONDS == 720
    assert first["firma_id"] == user["alan_id"]
    today = core.clock.today_tr()
    ending = agents.account("bugun@peyrev.test", modules={users.RPA: today, users.AGENTS: None})
    core.clock.at = datetime(2026, 10, 26, 20, 55, tzinfo=UTC)
    claims = jwt.decode(agents.token(ending), options={"verify_signature": False})
    assert claims["exp"] == int(datetime(2026, 10, 26, 21, 0, tzinfo=UTC).timestamp())  # 00:00 in Türkiye


@pytest.mark.parametrize("change", ["session", "password", "account", "license", "office"])
def test_an_issued_token_stops_working_after_revocation(agents, core, change):
    user = agents.account("iptal@peyrev.test")
    office = agents.office(user)
    token = "Bearer " + agents.token(user)
    statements = {
        "session": ("UPDATE oturumlar SET durum = 'kapandi' WHERE kullanici_id = %s", "OTURUM_GECERSIZ"),
        "password": ("UPDATE kullanicilar SET sifre_hash = 'yeni-test-izi' WHERE id = %s", "OTURUM_GECERSIZ"),
        "account": ("UPDATE kullanicilar SET durum = 'pasif' WHERE id = %s", "OTURUM_GECERSIZ"),
        "license": ("DELETE FROM kullanici_modulleri WHERE kullanici_id = %s AND modul = 'MOD_AJAN'",
                    "AJAN_YETKISI_YOK"),
        "office": ("UPDATE ajan_ofisleri SET durum = 'pasif' WHERE sahip_id = %s", "OFIS_ERISIMI_YOK")}
    statement, kod = statements[change]
    agents.sql(statement, (user["id"],))
    with core.db.transaction() as cur:
        refused(lambda: authenticate(core, cur, token, office), kod)


@pytest.mark.parametrize("field,value", [("firma_id", 9), ("kullanici_id", 999), ("cihaz", "c" * 64),
                                         ("oturum", "d" * 32), ("kullanici_id", True), ("surum", 2)])
def test_the_signed_identity_must_match_the_database_and_the_session(agents, core, field, value):
    user = agents.account("imza@peyrev.test", alan_id=8)
    claims = jwt.decode(agents.token(user), options={"verify_signature": False})
    claims[field] = value
    forged = jwt.encode(claims, core.keys.derive(tokens.AUDIENCES["ajan"]), algorithm="HS256")
    with core.db.transaction() as cur:
        refused(lambda: authenticate(core, cur, "Bearer " + forged), "OTURUM_GECERSIZ", 401)


def test_support_forged_or_expired_tokens_cannot_use_the_agent_api(agents, core, client):
    user = agents.account("destek@peyrev.test")
    session = agents.sessions[user["id"]]
    support = tokens.issue(core, "destek", user, DEVICE, session)
    claims = jwt.decode(agents.token(user), options={"verify_signature": False})
    foreign = jwt.encode(claims, "yalniz-test-icin-baska-anahtar-0123456789", algorithm="HS256")
    for token in (support, foreign):
        answer = client.get(API + "/ben", headers={"Authorization": "Bearer " + token})
        assert answer.status_code == 401 and answer.get_json()["kod"] == "OTURUM_GECERSIZ"
    token = agents.token(user)
    core.clock.advance(seconds=tokens.TOKEN_SECONDS + 1)
    assert client.get(API + "/ben", headers={"Authorization": "Bearer " + token}).status_code == 401


# ----- HTTP gate ----------------------------------------------------------------------------------------------------
def test_the_restart_health_check_gets_exactly_the_session_required_answer(client):
    for headers in ({}, {"Authorization": "Basic eA=="}, {"Authorization": "Bearer"}):
        answer = client.get(API + "/ben", headers={"Accept": "application/json", **headers})
        raw = answer.get_data()
        assert answer.status_code == 401 and len(raw) <= 4096 and "Location" not in answer.headers
        assert json.loads(raw) == {"success": False, "kod": "OTURUM_GEREKLI", "message": "Studio oturumu gerekli."}
        assert answer.headers["Cache-Control"] == "no-store"


def test_identity_never_comes_from_the_query_and_credentials_never_leave(agents, client):
    user = agents.account("sorgu@peyrev.test", alan_id=8, ad_soyad="Ayşe Yılmaz")
    assert client.get(API + "/ben?firma_id=8&kullanici_id=1").status_code == 400  # even before authentication
    headers = agents.headers(user)
    for query in ("firma_id=9", "company_id=9", "kullanici_id=2", "user_id=2"):
        answer = client.get(API + "/ben?" + query, headers=headers)
        assert answer.status_code == 400 and answer.get_json()["kod"] == "GECERSIZ_GIRDI"
    answer = client.get(API + "/ben", headers=headers)
    data = answer.get_json()
    assert answer.status_code == 200 and answer.headers["Cache-Control"] == "no-store"
    assert set(data) == {"success", "kullanici_id", "firma_id", "ad_soyad", "ajan"}
    assert (data["kullanici_id"], data["firma_id"], data["ad_soyad"]) == (user["id"], 8, "Ayşe Yılmaz")
    assert data["ajan"]["yetkiler"] == ["agents.use"] and data["ajan"]["ofis_kurabilir"] is True
    text = answer.get_data(as_text=True)
    assert user["sifre_hash"] not in text and headers["Authorization"][7:] not in text and DEVICE not in text
    assert client.get(API + "/uyeler", headers=headers).get_json()["uyeler"] == [
        {"kullanici_id": user["id"], "ad_soyad": "Ayşe Yılmaz", "tur": "human", "onayci": True}]


def test_ben_names_the_office_chosen_in_the_header(agents, client):
    owner = agents.account("ofisli@peyrev.test")
    first, second = agents.office(owner, "Birinci"), agents.office(owner, "İkinci")
    assert client.get(API + "/ben", headers=agents.headers(owner)).get_json()["ajan"]["ofis_id"] == first
    data = client.get(API + "/ben", headers=agents.headers(owner, second)).get_json()["ajan"]
    assert data["ofis_id"] == second and [item["id"] for item in data["ofisler"]] == [first, second]
    answer = client.get(API + "/ben", headers=agents.headers(owner, "e" * 32))
    assert answer.status_code == 403 and answer.get_json() == {
        "success": False, "kod": "OFIS_ERISIMI_YOK", "message": entitlement.NO_OFFICE_ACCESS}


def test_removed_team_endpoints_answer_not_found(agents, client):
    owner = agents.account("uye@peyrev.test")
    agents.office(owner)
    made = client.post(API + "/gorevler", json=task_payload(owner["id"]), headers=agents.headers(owner))
    task = made.get_json()["task"]
    gone = {"success": False, "kod": "BULUNAMADI", "message": "İstenen kayıt veya uç bulunamadı."}
    for path, payload in (("/ofisim/uyeler", {"user_id": 5}), ("/ofisim/uyeler/5/cikar", {}), ("/ofisim/ayril", {}),
                          ("/gorevler/" + task["id"] + "/ata", {"assignee_user_id": 5, "expected_version": 1})):
        for headers in ({}, agents.headers(owner)):
            answer = client.post(API + path, json=payload, headers=headers)
            assert (answer.status_code, answer.get_json()) == (404, gone), path
    assert client.post(API + "/ofis", json={}, headers=agents.headers(owner)).get_json()["kod"] == "YONTEM"
    assert client.get(API + "/onaylar/" + "a" * 32 + "/karar", headers=agents.headers(owner)).status_code == 405


def test_bodies_are_read_strictly_and_kept_literally(agents, client):
    owner = agents.account("govde@peyrev.test")
    agents.office(owner)
    headers = agents.headers(owner)
    text = '<etiket> "tırnak" ve JSON: {"karar": true} & ünlü'
    made = client.post(API + "/gorevler", json=task_payload(owner["id"], instructions=text), headers=headers)
    assert made.status_code == 201 and made.get_json()["task"]["instructions"] == text
    request_id = '"' + uuid.uuid4().hex + '"'
    base = '"title":"Başlık","instructions":"x","assignee_user_id":' + str(owner["id"])
    for raw in ('{"request_id":' + request_id + ',"request_id":' + request_id + "," + base + "}",
                '{"request_id":' + request_id + "," + base + ',"firma_id":9}',
                '{"request_id":' + request_id + "," + base.replace(str(owner["id"]), "NaN") + "}",
                '{"request_id":' + request_id + "," + base.replace(str(owner["id"]), "1e999") + "}",
                '{"request_id":' + request_id + ',"title":"Başlık"}',
                '{"request_id":' + request_id + "," + base + ',"priority":' + "[" * 25 + "0" + "]" * 25 + "}",
                # A lone surrogate can be escaped in JSON but never hashed or stored as UTF-8 (would be a 503).
                '{"request_id":' + request_id + "," + base.replace("Başlık", "\\ud800") + "}",
                '{"request_id":' + request_id + "," + base + ',"priority":{"\\udfff":1}}',
                "[]", "﻿{}", "{"):
        answer = client.post(API + "/gorevler", data=raw.encode("utf-8"), content_type="application/json",
                             headers=headers)
        assert answer.status_code == 400 and answer.get_json()["kod"] == "GECERSIZ_GIRDI", raw
    form = client.post(API + "/gorevler", data={"title": "x"}, headers=headers)
    assert form.status_code == 400 and form.get_json()["message"] == "JSON gövdesi gerekli."
    big = "x" * (128 * 1024 + 1)
    for extra in ({}, headers):
        answer = client.post(API + "/gorevler", data=big, content_type="application/json", headers=extra)
        assert answer.status_code == 413 and answer.get_json()["kod"] == "GOVDE_SINIRI"


def test_a_database_failure_does_not_reveal_the_error_text(agents, core, client, monkeypatch):
    user = agents.account("hata@peyrev.test")
    headers = agents.headers(user)

    def unavailable():
        raise RuntimeError("test-gizli-db-sifresi SELECT * FROM kullanicilar")

    monkeypatch.setattr(core.db, "transaction", unavailable)
    answer = client.get(API + "/ben", headers=headers)
    assert answer.status_code == 503 and answer.get_json()["kod"] == "SUNUCU"
    assert "test-gizli-db-sifresi" not in answer.get_data(as_text=True)
