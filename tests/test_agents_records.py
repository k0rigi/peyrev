"""Ajan merkezi kayıtları: kendine görevler, oturuma bağlı onaylar, ofisler, kapsam ve eşzamanlılık."""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from conftest import START
from test_agents import API, OTHER_DEVICE, Agents, approval_payload, consume_payload, refused, task_payload

from peyrev.agents import entitlement, records
from peyrev.common import ApiError


@pytest.fixture
def agents(core):
    return Agents(core)


@pytest.fixture
def owner(agents):
    return agents.account("sahip@peyrev.test", ad_soyad="Ofis Sahibi")


@pytest.fixture
def office(agents, owner):
    return agents.office(owner, "Claim & İade Departmanı")


def elsewhere(agents, user):
    """The same account through another, still open Studio session."""
    return agents.identity(user, session=agents.second_session(user), device=OTHER_DEVICE)


# ----- human tasks ------------------------------------------------------------------------------------------------
def test_a_task_is_the_owners_own_and_hidden_from_every_other_account(agents, core, owner, office):
    other = agents.account("baska@peyrev.test")
    agents.office(other, "Başka Ofis")
    lone = agents.account("ofissiz@peyrev.test")
    me = agents.identity(owner)
    payload = task_payload(owner["id"])
    task = records.create_task(core, me, payload)
    assert (task["assignee_user_id"], task["created_by"], task["status"], task["version"]) == (
        owner["id"], owner["id"], "queued", 1)
    assert records.get_task(core, me, task["id"])["instructions"] == payload["instructions"]
    assert "instructions" not in records.list_tasks(core, me)[0]
    for identity in (agents.identity(other), agents.identity(lone)):
        assert records.list_tasks(core, identity) == []
        refused(lambda identity=identity: records.get_task(core, identity, task["id"]), "BULUNAMADI", 404)
    # Nobody else can be handed the task, and an account without an office has nothing to manage.
    refused(lambda: records.create_task(core, me, task_payload(other["id"])), "ATAMA_YETKISI_YOK", 403)
    refused(lambda: records.create_task(core, agents.identity(lone), task_payload(lone["id"])), "AJAN_YETKISI_YOK", 403)
    assert agents.sql("SELECT sahip_id, ofis_id, olusturan_id, atanan_id FROM ajan_gorevleri") == [
        {"sahip_id": owner["id"], "ofis_id": office, "olusturan_id": owner["id"], "atanan_id": owner["id"]}]


def test_a_task_request_id_is_idempotent_and_bound_to_its_body(agents, core, owner, office):
    me = agents.identity(owner)
    payload = task_payload(owner["id"], priority="high")
    first = records.create_task(core, me, payload)
    assert records.create_task(core, me, payload) == first
    refused(lambda: records.create_task(core, me, dict(payload, title="Değiştirilen görev")), "ISTEK_CAKISMASI", 409)
    refused(lambda: records.create_task(core, me, task_payload(owner["id"], priority="acil")), "GECERSIZ_GIRDI", 400)
    assert len(agents.sql("SELECT id FROM ajan_gorevleri")) == 1
    assert len(agents.sql("SELECT id FROM ajan_olaylari WHERE kayit_turu = 'task'")) == 1


def test_task_states_are_versioned_and_follow_the_allowed_transitions(agents, core, owner, office):
    me = agents.identity(owner)
    task = records.create_task(core, me, task_payload(owner["id"]))
    refused(lambda: records.transition_task(core, me, task["id"], "succeeded", 1), "DURUM_CAKISMASI", 409)
    refused(lambda: records.transition_task(core, me, task["id"], "bilinmeyen", 1), "DURUM_CAKISMASI", 409)
    started = records.transition_task(core, me, task["id"], "running", 1)
    assert started["started_at"] and started["finished_at"] is None and started["version"] == 2
    refused(lambda: records.transition_task(core, me, task["id"], "failed", 1), "SURUM_CAKISMASI", 409)
    waiting = records.transition_task(core, me, task["id"], "waiting_user", 2, "Yanıt bekleniyor.")
    assert waiting["summary"] == "Yanıt bekleniyor."
    assert records.transition_task(core, me, task["id"], "running", 3)["summary"] == ""  # rewritten on every change
    core.clock.advance(minutes=5)
    done = records.transition_task(core, me, task["id"], "succeeded", 4, "Kontrol tamamlandı.")
    assert done["version"] == 5 and done["summary"] == "Kontrol tamamlandı."
    assert done["started_at"] == started["started_at"] and done["finished_at"] > done["started_at"]
    refused(lambda: records.transition_task(core, me, task["id"], "running", 5), "DURUM_CAKISMASI", 409)
    refused(lambda: records.transition_task(core, me, task["id"], "running", True), "GECERSIZ_GIRDI", 400)
    refused(lambda: records.transition_task(core, me, task["id"], "running", 5, "x" * 4001), "GECERSIZ_GIRDI", 400)
    events = agents.sql("SELECT olay, ayrinti FROM ajan_olaylari WHERE kayit_turu = 'task' "
                        "ORDER BY (ayrinti->>'version')::int")
    assert [(row["olay"], row["ayrinti"].get("after")) for row in events] == [
        ("created", None), ("status_changed", "running"), ("status_changed", "waiting_user"),
        ("status_changed", "running"), ("status_changed", "succeeded")]


def test_a_migrated_colleagues_task_stays_readable_and_the_owner_may_only_cancel_it(agents, core, owner, office):
    old_member, task_id = 9001, uuid.uuid4().hex  # a former team member needs no Peyrev account for history rows
    agents.sql("INSERT INTO ajan_gorevleri (id, sahip_id, ofis_id, olusturan_id, atanan_id, istek_id, istek_ozeti, "
               "baslik, talimat, oncelik, durum, surum, olusturma, baslama, guncelleme) "
               "VALUES (%s, %s, %s, %s, %s, %s, %s, 'Eski görev', 'Eski talimat', 'high', 'running', 4, %s, %s, %s)",
               (task_id, owner["id"], office, owner["id"], old_member, uuid.uuid4().hex, "e" * 64, START, START, START))
    me = agents.identity(owner)
    assert [item["id"] for item in records.list_tasks(core, me)] == [task_id]
    row = records.get_task(core, me, task_id)
    assert (row["assignee_user_id"], row["instructions"], row["priority"], row["version"]) == (
        old_member, "Eski talimat", "high", 4)
    refused(lambda: records.transition_task(core, me, task_id, "succeeded", 4), "GOREV_YETKISI_YOK", 403)
    cancelled = records.transition_task(core, me, task_id, "cancelled", 4)
    assert cancelled["status"] == "cancelled" and cancelled["finished_at"]
    assert records.delete_task(core, me, task_id, 5) == {"id": task_id, "deleted": True}


def test_only_finished_tasks_are_deleted_and_a_deleted_task_disappears(agents, core, owner, office):
    me = agents.identity(owner)
    other = agents.account("baska@peyrev.test")
    agents.office(other, "Başka Ofis")
    task = records.create_task(core, me, task_payload(owner["id"]))
    refused(lambda: records.delete_task(core, me, task["id"], 1), "DURUM_CAKISMASI", 409)  # cancel a queued task first
    cancelled = records.transition_task(core, me, task["id"], "cancelled", 1)
    refused(lambda: records.delete_task(core, agents.identity(other), task["id"], 2), "BULUNAMADI", 404)
    refused(lambda: records.delete_task(core, me, task["id"], 1), "SURUM_CAKISMASI", 409)
    assert records.delete_task(core, me, task["id"], cancelled["version"]) == {"id": task["id"], "deleted": True}
    assert records.list_tasks(core, me) == []
    refused(lambda: records.get_task(core, me, task["id"]), "BULUNAMADI", 404)
    refused(lambda: records.delete_task(core, me, task["id"], 3), "BULUNAMADI", 404)
    # The row and its audit events remain; metrics ignore the hidden task.
    assert agents.sql("SELECT durum FROM ajan_gorevleri") == [{"durum": "deleted"}]
    assert len(agents.sql("SELECT id FROM ajan_olaylari WHERE olay = 'deleted'")) == 1
    start, end = (core.clock.now() + timedelta(days=days) for days in (-1, 1))
    assert records.task_metrics(core, me, start.isoformat(), end.isoformat())["totals"]["total"] == 0


def test_the_task_delete_route_takes_exactly_the_version(agents, core, client, owner, office):
    me = agents.identity(owner)
    task = records.create_task(core, me, task_payload(owner["id"]))
    done = records.transition_task(core, me, task["id"], "cancelled", 1)
    endpoint, headers = API + "/gorevler/" + task["id"] + "/sil", agents.headers(owner)
    assert client.post(endpoint, json={}, headers=headers).status_code == 400
    assert client.post(endpoint, json={"expected_version": done["version"], "x": 1}, headers=headers).status_code == 400
    answer = client.post(endpoint, json={"expected_version": done["version"]}, headers=headers)
    assert answer.status_code == 200 and answer.get_json()["task"] == {"id": task["id"], "deleted": True}
    assert client.get(API + "/gorevler", headers=headers).get_json()["tasks"] == []


# ----- locked re-checks -----------------------------------------------------------------------------------------
def test_every_change_rechecks_the_session_license_and_office_under_the_lock(agents, core, owner, office):
    me = agents.identity(owner)
    agents.sql("UPDATE oturumlar SET durum = 'kapandi' WHERE kullanici_id = %s", (owner["id"],))
    refused(lambda: records.request_approval(core, me, approval_payload()), "OTURUM_GECERSIZ", 401)
    agents.sql("UPDATE oturumlar SET durum = 'aktif' WHERE kullanici_id = %s", (owner["id"],))
    agents.sql("DELETE FROM kullanici_modulleri WHERE kullanici_id = %s AND modul = 'MOD_AJAN'", (owner["id"],))
    refused(lambda: records.create_task(core, me, task_payload(owner["id"])), "AJAN_YETKISI_YOK", 403)
    agents.sql("INSERT INTO kullanici_modulleri (kullanici_id, modul) VALUES (%s, 'MOD_AJAN')", (owner["id"],))
    agents.sql("UPDATE ajan_ofisleri SET durum = 'pasif' WHERE id = %s", (office,))
    refused(lambda: records.list_tasks(core, me), "OFIS_ERISIMI_YOK", 403)
    assert agents.sql("SELECT id FROM ajan_gorevleri") == [] and agents.sql("SELECT id FROM ajan_onaylari") == []


def test_a_request_never_moves_to_another_office_while_it_waits(agents, core, owner, office):
    second = agents.office(owner, "İkinci Ofis")
    default = agents.identity(owner)  # no header: the first office
    assert default.office_id == office
    records.delete_office(core, agents.identity(owner, office), "Claim & İade Departmanı")
    refused(lambda: records.request_approval(core, default, approval_payload()), "OFIS_ERISIMI_YOK", 403)
    assert agents.identity(owner).office_id == second and agents.sql("SELECT id FROM ajan_onaylari") == []


@pytest.mark.parametrize("revoke", ["password", "token_expiry"])
def test_an_approval_is_not_consumed_when_the_identity_ends_after_authentication(agents, core, owner, office, revoke):
    me = agents.identity(owner)
    pending = records.request_approval(core, me, approval_payload(expires_in=900))
    approved = records.decide_approval(core, me, pending["id"], "approve", 1)
    if revoke == "password":
        agents.sql("UPDATE kullanicilar SET sifre_hash = 'yenilenen-test-izi' WHERE id = %s", (owner["id"],))
    else:
        core.clock.advance(seconds=721)
    consume = consume_payload(approved)
    refused(lambda: records.consume_approval(core, me, pending["id"], consume), "OTURUM_GECERSIZ", 401)
    assert agents.sql("SELECT durum FROM ajan_onaylari") == [{"durum": "approved"}]
    assert not agents.sql("SELECT id FROM ajan_olaylari WHERE olay = 'consumed'")


def test_the_token_lifetime_is_checked_again_after_waiting_for_the_license_locks(agents, core, owner, office,
                                                                                  monkeypatch):
    me = agents.identity(owner)
    pending = records.request_approval(core, me, approval_payload(expires_in=900))
    approved = records.decide_approval(core, me, pending["id"], "approve", 1)
    original = entitlement.license_extension

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        core.clock.advance(seconds=721)
        return result

    monkeypatch.setattr(entitlement, "license_extension", delayed)
    consume = consume_payload(approved)
    refused(lambda: records.consume_approval(core, me, pending["id"], consume), "OTURUM_GECERSIZ", 401)
    assert not agents.sql("SELECT id FROM ajan_olaylari WHERE olay = 'consumed'")


# ----- approvals --------------------------------------------------------------------------------------------------
def test_the_owner_decides_and_only_the_requesting_session_consumes_once(agents, core, owner, office):
    me, lone = agents.identity(owner), agents.identity(agents.account("ofissiz@peyrev.test"))
    other = agents.account("baska@peyrev.test")
    agents.office(other, "Başka Ofis")
    pending = records.request_approval(core, me, approval_payload())
    assert (pending["requester_user_id"], pending["approver_user_id"], pending["status"], pending["version"]) == (
        owner["id"], owner["id"], "pending", 1)
    refused(lambda: records.decide_approval(core, lone, pending["id"], "approve", 1), "AJAN_YETKISI_YOK", 403)
    refused(lambda: records.decide_approval(core, agents.identity(other), pending["id"], "approve", 1), "BULUNAMADI")
    refused(lambda: records.decide_approval(core, me, pending["id"], "evet", 1), "GECERSIZ_GIRDI", 400)
    refused(lambda: records.consume_approval(core, me, pending["id"], consume_payload(pending)), "ONAY_KULLANILAMAZ")
    approved = records.decide_approval(core, me, pending["id"], "approve", 1)
    assert approved["status"] == "approved" and approved["decision_reason"] == "user" and approved["decided_at"]
    assert records.decide_approval(core, me, pending["id"], "approve", 1) == approved  # a repeated request
    refused(lambda: records.decide_approval(core, me, pending["id"], "reject", 1), "SURUM_CAKISMASI", 409)
    refused(lambda: records.consume_approval(core, elsewhere(agents, owner), pending["id"], consume_payload(approved)),
            "ONAY_OTURUMU", 403)
    consumed = records.consume_approval(core, me, pending["id"], consume_payload(approved))
    assert consumed["status"] == "consumed" and consumed["consumed_at"] and consumed["version"] == 3
    refused(lambda: records.consume_approval(core, me, pending["id"], consume_payload(approved)), "ONAY_KULLANILAMAZ")
    events = agents.sql("SELECT olay FROM ajan_olaylari WHERE kayit_turu = 'approval'")
    assert sorted(row["olay"] for row in events) == ["approved", "consumed", "requested"]


@pytest.mark.parametrize("field", ["run_id", "workflow_id", "workflow_digest", "inputs_digest", "preview_digest",
                                   "attempt"])
def test_consumption_is_bound_to_the_exact_approved_content(agents, core, owner, office, field):
    me = agents.identity(owner)
    pending = records.request_approval(core, me, approval_payload())
    approved = records.decide_approval(core, me, pending["id"], "approve", 1)
    consume = consume_payload(approved)
    consume[field] = 2 if field == "attempt" else "f" * len(consume[field])
    refused(lambda: records.consume_approval(core, me, pending["id"], consume), "ONAY_UYUSMUYOR", 409)
    assert records.get_approval(core, me, pending["id"])["status"] == "approved"


def test_rejected_and_expired_approvals_never_execute_and_the_timeout_is_audited(agents, core, owner, office):
    me = agents.identity(owner)
    first = records.request_approval(core, me, approval_payload())
    records.decide_approval(core, me, first["id"], "reject", 1)
    refused(lambda: records.consume_approval(core, me, first["id"], consume_payload(first)), "ONAY_KULLANILAMAZ")
    second = records.request_approval(core, me, approval_payload(expires_in=10))
    records.decide_approval(core, me, second["id"], "approve", 1)
    core.clock.advance(seconds=11)
    refused(lambda: records.consume_approval(core, me, second["id"], consume_payload(second)), "ONAY_KULLANILAMAZ")
    saved = records.get_approval(core, me, second["id"])
    assert saved["status"] == "rejected" and saved["decision_reason"] == "timeout"
    assert agents.sql("SELECT sahip_id, kullanici_id, aktor, ayrinti FROM ajan_olaylari WHERE olay = 'timeout'") == [
        {"sahip_id": owner["id"], "kullanici_id": None, "aktor": "system", "ayrinti": {}}]


def test_an_approval_request_is_idempotent_and_cannot_change_its_preview_or_session(agents, core, owner, office):
    me = agents.identity(owner)
    payload = approval_payload()
    first = records.request_approval(core, me, payload)
    assert records.request_approval(core, me, payload) == first
    refused(lambda: records.request_approval(core, me, dict(payload, preview={"deger": "değişti"})), "ISTEK_CAKISMASI")
    refused(lambda: records.request_approval(core, elsewhere(agents, owner), payload), "ISTEK_CAKISMASI", 409)
    for broken in ({"preview": ["liste"]}, {"preview": {"x": "y" * 32768}}, {"attempt": 101}, {"expires_in": 901},
                   {"reason": " "}, {"workflow_digest": "A" * 64}, {"run_id": "kisa"}):
        refused(lambda broken=broken: records.request_approval(core, me, approval_payload(**broken)), "GECERSIZ_GIRDI")
    assert len(agents.sql("SELECT id FROM ajan_onaylari")) == 1


@pytest.mark.parametrize("approve", [False, True])
def test_the_requesting_session_withdraws_a_pending_or_approved_request_once(agents, core, owner, office, approve):
    me = agents.identity(owner)
    other = agents.account("baska@peyrev.test")
    agents.office(other, "Başka Ofis")
    pending = records.request_approval(core, me, approval_payload())
    if approve:
        records.decide_approval(core, me, pending["id"], "approve", 1)
    refused(lambda: records.cancel_approval(core, elsewhere(agents, owner), pending["id"]), "ONAY_OTURUMU", 403)
    refused(lambda: records.cancel_approval(core, agents.identity(other), pending["id"]), "BULUNAMADI", 404)
    cancelled = records.cancel_approval(core, me, pending["id"])
    assert cancelled["status"] == "rejected" and cancelled["decision_reason"] == "withdrawn"
    assert records.cancel_approval(core, me, pending["id"]) == cancelled
    refused(lambda: records.consume_approval(core, me, pending["id"], consume_payload(pending)), "ONAY_KULLANILAMAZ")
    assert len(agents.sql("SELECT id FROM ajan_olaylari WHERE olay = 'withdrawn'")) == 1


def test_a_consumed_approval_cannot_be_withdrawn_and_the_route_takes_an_empty_object(agents, core, client, owner,
                                                                                      office):
    me = agents.identity(owner)
    pending = records.request_approval(core, me, approval_payload())
    approved = records.decide_approval(core, me, pending["id"], "approve", 1)
    consumed = records.consume_approval(core, me, pending["id"], consume_payload(approved))
    assert records.cancel_approval(core, me, pending["id"]) == consumed
    fresh = records.request_approval(core, me, approval_payload())
    endpoint, headers = API + "/onaylar/" + fresh["id"] + "/iptal", agents.headers(owner)
    assert client.post(endpoint, json={"approved": True}, headers=headers).status_code == 400
    assert client.post(endpoint, data=b"", content_type="application/json", headers=headers).status_code == 400
    answer = client.post(endpoint, json={}, headers=headers)
    assert answer.status_code == 200 and answer.get_json()["approval"]["status"] == "rejected"


def test_records_of_an_account_without_an_office_stay_outside_offices(agents, core):
    user = agents.account("once@peyrev.test")
    lone = agents.identity(user)
    pending = records.request_approval(core, lone, approval_payload())
    assert agents.sql("SELECT ofis_id, onayci_id FROM ajan_onaylari") == [{"ofis_id": None, "onayci_id": user["id"]}]
    assert [item["id"] for item in records.list_approvals(core, lone)] == [pending["id"]]
    # Without an office the account approves nothing; the request simply runs out of time.
    refused(lambda: records.decide_approval(core, lone, pending["id"], "approve", 1), "AJAN_YETKISI_YOK", 403)
    agents.office(user, "İlk Ofis")
    assert records.list_approvals(core, agents.identity(user)) == []


# ----- concurrency ------------------------------------------------------------------------------------------------
def test_concurrent_creates_and_consumes_are_single_operations(agents, core, owner, office):
    me = agents.identity(owner)
    payload = task_payload(owner["id"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = list(pool.map(lambda _: records.create_task(core, me, payload), range(4)))
    assert len({task["id"] for task in tasks}) == 1 and len(agents.sql("SELECT id FROM ajan_gorevleri")) == 1
    pending = records.request_approval(core, me, approval_payload())
    records.decide_approval(core, me, pending["id"], "approve", 1)

    def consume(_):
        try:
            return records.consume_approval(core, me, pending["id"], consume_payload(pending))["status"]
        except ApiError as error:
            return error.kod

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(consume, range(4)))
    assert sorted(outcomes) == ["ONAY_KULLANILAMAZ"] * 3 + ["consumed"]
    assert len(agents.sql("SELECT id FROM ajan_olaylari WHERE olay = 'consumed'")) == 1


def test_concurrent_opposite_decisions_cannot_overwrite_each_other(agents, core, owner, office):
    me = agents.identity(owner)
    pending = records.request_approval(core, me, approval_payload())

    def decide(decision):
        try:
            return records.decide_approval(core, me, pending["id"], decision, 1)["status"]
        except ApiError as error:
            return error.kod

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(decide, ["approve", "reject"]))
    assert decisions.count("SURUM_CAKISMASI") == 1
    assert sum(value in ("approved", "rejected") for value in decisions) == 1
    assert len(agents.sql("SELECT id FROM ajan_olaylari WHERE kayit_turu = 'approval'")) == 2


def test_concurrent_office_setups_keep_the_name_and_the_limit(agents, core, owner):
    identity = agents.identity(owner)

    def create(name):
        try:
            return records.create_office(core, identity, name)["name"]
        except ApiError as error:
            return error.kod

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(create, ["Depo", "DEPO"])).count("OFIS_ADI") == 1
    for number in range(entitlement.MAX_OFFICES - 2):
        agents.add_office(owner, f"Ofis {number}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(create, ["Son Ofis", "Fazla Ofis"])).count("OFIS_SINIRI") == 1
    assert len(agents.sql("SELECT id FROM ajan_ofisleri")) == entitlement.MAX_OFFICES


# ----- HTTP and audit ---------------------------------------------------------------------------------------------
def test_the_http_flow_rejects_identity_injection_and_keeps_texts_literal(agents, client, owner, office):
    headers, lone = agents.headers(owner), agents.headers(agents.account("ofissiz@peyrev.test"))
    payload = task_payload(owner["id"], instructions='<etiket> "aynen" kalır')
    assert client.post(API + "/gorevler", json=payload).status_code == 401
    assert client.post(API + "/gorevler", json=payload, headers=lone).status_code == 403
    assert client.post(API + "/gorevler", json=dict(payload, firma_id=9), headers=headers).status_code == 400
    made = client.post(API + "/gorevler", json=payload, headers=headers)
    assert made.status_code == 201 and made.get_json()["task"]["instructions"] == payload["instructions"]
    task = made.get_json()["task"]
    status = client.post(API + "/gorevler/" + task["id"] + "/durum", json={"status": "running", "expected_version": 1},
                         headers=headers)
    assert status.status_code == 200 and status.get_json()["task"]["status"] == "running"
    running = client.get(API + "/gorevler?status=running", headers=headers).get_json()["tasks"]
    assert [item["id"] for item in running] == [task["id"]]
    for value in ("", "deleted", "bilinmeyen"):
        assert client.get(API + "/gorevler?status=" + value, headers=headers).status_code == 400
    assert client.get(API + "/gorevler/kisa", headers=headers).get_json()["kod"] == "GECERSIZ_GIRDI"
    answer = client.post(API + "/onaylar", json=approval_payload(reason="<b>gerekçe</b> & ayrıntı"), headers=headers)
    assert answer.status_code == 201
    record = answer.get_json()["approval"]
    assert "session_id" not in record and owner["id"] == record["requester_user_id"] == record["approver_user_id"]
    assert record["reason"] == "<b>gerekçe</b> & ayrıntı" and record["preview"] == {"deger": "***"}
    path = API + "/onaylar/" + record["id"]
    decided = client.post(path + "/karar", json={"decision": "approve", "expected_version": 1}, headers=headers)
    assert decided.status_code == 200 and decided.get_json()["approval"]["status"] == "approved"
    consumed = client.post(path + "/tuket", json=consume_payload(record), headers=headers)
    assert consumed.status_code == 200 and consumed.get_json()["approval"]["status"] == "consumed"
    listed = client.get(API + "/onaylar", headers=headers).get_json()["approvals"]
    assert [item["id"] for item in listed] == [record["id"]] and "preview" not in listed[0]
    assert client.get(path, headers=headers).get_json()["approval"]["preview"] == {"deger": "***"}
    assert agents.sessions[owner["id"]] not in json.dumps([record, listed])


def test_the_audit_keeps_states_and_hashes_but_no_work_details(agents, core, owner, office):
    me = agents.identity(owner)
    secret = "İş açıklamasındaki özel metin"
    records.create_task(core, me, task_payload(owner["id"], instructions=secret, title=secret))
    records.request_approval(core, me, approval_payload(reason=secret, preview={"gizli": secret}))
    events = agents.sql("SELECT * FROM ajan_olaylari")
    assert secret not in json.dumps(events, default=str)
    assert all(event["sahip_id"] == owner["id"] == event["kullanici_id"] for event in events)
    (stored,) = agents.sql("SELECT onizleme, onizleme_ozeti FROM ajan_onaylari")
    # The stored text is exactly what was hashed.
    assert records._hash(json.loads(stored["onizleme"])) == stored["onizleme_ozeti"]


# ----- offices ----------------------------------------------------------------------------------------------------
def test_offices_of_different_accounts_never_see_each_other(agents, core, owner, office):
    other = agents.account("baska@peyrev.test")
    theirs = agents.office(other, "Lojistik")
    me, them = agents.identity(owner), agents.identity(other)
    task = records.create_task(core, me, task_payload(owner["id"]))
    pending = records.request_approval(core, me, approval_payload())
    assert records.list_tasks(core, them) == [] and records.list_approvals(core, them) == []
    refused(lambda: records.get_task(core, them, task["id"]), "BULUNAMADI", 404)
    refused(lambda: records.decide_approval(core, them, pending["id"], "approve", 1), "BULUNAMADI", 404)
    refused(lambda: records.cancel_approval(core, them, pending["id"]), "BULUNAMADI", 404)
    refused(lambda: agents.identity(owner, theirs), "OFIS_ERISIMI_YOK", 403)
    assert records.my_offices(core, them)["offices"][0]["open_tasks"] == 0
    # Names are unique per account only.
    assert records.create_office(core, them, "Claim & İade Departmanı")["name"] == "Claim & İade Departmanı"


def test_each_request_names_its_office_and_records_stay_in_it(agents, core, owner, office):
    second = agents.office(owner, "İade Analiz Departmanı")
    default, other = agents.identity(owner), agents.identity(owner, second)
    assert (default.office_id, other.office_id) == (office, second)
    assert other.entitlement["yetkiler"] == ["agents.use", "agents.manage", "agents.approve"]
    task = records.create_task(core, other, task_payload(owner["id"]))
    assert [item["id"] for item in records.list_tasks(core, other)] == [task["id"]]
    assert records.list_tasks(core, default) == []
    refused(lambda: records.get_task(core, default, task["id"]), "BULUNAMADI", 404)
    pending = records.request_approval(core, other, approval_payload())
    summary = records.my_offices(core, default)
    assert summary["can_create"] is True
    assert [(item["name"], item["is_authority"], item["people"], item["open_tasks"], item["pending_approvals"])
            for item in summary["offices"]] == [("Claim & İade Departmanı", True, 1, 0, 0),
                                                ("İade Analiz Departmanı", True, 1, 1, 1)]
    refused(lambda: records.decide_approval(core, default, pending["id"], "approve", 1), "BULUNAMADI", 404)
    assert records.decide_approval(core, other, pending["id"], "approve", 1)["status"] == "approved"
    assert agents.sql("SELECT ofis_id FROM ajan_onaylari") == [{"ofis_id": second}]
    # A closed office is refused even for an identity that was valid a moment ago.
    agents.sql("UPDATE ajan_ofisleri SET durum = 'pasif' WHERE id = %s", (second,))
    refused(lambda: records.list_tasks(core, other), "OFIS_ERISIMI_YOK", 403)
    assert [item["id"] for item in agents.identity(owner).entitlement["ofisler"]] == [office]


def test_studio_names_the_office_in_a_header_and_the_routes_check_it(agents, client, owner, office):
    second = agents.office(owner, "Rota Planlama")
    foreign = agents.office(agents.account("baska@peyrev.test"), "Başka Ofis")

    def get(path, chosen=None):
        return client.get(API + path, headers=agents.headers(owner, chosen))

    assert get("/ofisim").get_json()["office"]["id"] == office
    assert get("/ofisim", second).get_json()["office"]["name"] == "Rota Planlama"
    assert [item["name"] for item in get("/ofislerim").get_json()["offices"]] == ["Claim & İade Departmanı",
                                                                                  "Rota Planlama"]
    answer = get("/ofisim", foreign)
    assert answer.status_code == 403 and answer.get_json()["kod"] == "OFIS_ERISIMI_YOK"
    assert get("/ofislerim?x=1").get_json()["message"] == "Ofis özeti sorgu parametresi kabul etmez."
    assert get("/ofisim", "OFIS").get_json()["kod"] == "GECERSIZ_GIRDI"


def test_an_account_sets_up_names_and_lists_its_offices(agents, core):
    user = agents.account("kurucu@peyrev.test", ad_soyad="Kurucu Kişi")
    lone = agents.identity(user)
    assert records.my_office(core, lone) == {"office": None, "can_create": True, "members": [], "candidates": []}
    assert records.my_offices(core, lone) == {"offices": [], "can_create": True}
    refused(lambda: records.rename_office(core, lone, "Yeni Ad"), "AJAN_YETKISI_YOK", 403)
    for bad in ("A", " Satış", "Satış ", "Satış_1", "İki  boşluk", "x" * 121, "Satır\nkırık", "<b>", 5, None):
        refused(lambda bad=bad: records.create_office(core, lone, bad), "GECERSIZ_GIRDI", 400)
    first = records.create_office(core, lone, "Satış Departmanı")
    assert first == {"id": first["id"], "name": "Satış Departmanı", "authority_user_id": user["id"]}
    refused(lambda: records.create_office(core, lone, "satış departmanı"), "OFIS_ADI", 409)
    owner = agents.identity(user)
    assert owner.office_id == first["id"] and "agents.manage" in owner.entitlement["yetkiler"]
    assert records.my_office(core, owner) == {
        "office": {"id": first["id"], "name": "Satış Departmanı", "authority_user_id": user["id"],
                   "is_authority": True},
        "can_create": True, "members": [{"user_id": user["id"], "name": "Kurucu Kişi", "authority": True}],
        "candidates": []}
    core.clock.advance(seconds=1)
    records.create_office(core, owner, "Satın Alma")
    refused(lambda: records.rename_office(core, owner, "satın alma"), "OFIS_ADI", 409)
    refused(lambda: records.rename_office(core, owner, "Satın_Alma"), "GECERSIZ_GIRDI", 400)
    assert records.rename_office(core, owner, "Satış ve Pazarlama") == {"id": first["id"], "name": "Satış ve Pazarlama"}
    assert records.rename_office(core, owner, "SATIŞ VE PAZARLAMA")["name"] == "SATIŞ VE PAZARLAMA"  # its own name
    assert [item["ad"] for item in agents.identity(user).entitlement["ofisler"]] == ["SATIŞ VE PAZARLAMA", "Satın Alma"]
    assert [row["olay"] for row in agents.sql("SELECT olay FROM ajan_olaylari WHERE kayit_turu = 'office' "
                                              "ORDER BY olusturma, olay")] == [
        "office_created", "office_created", "office_renamed", "office_renamed"]


def test_names_the_database_folds_alike_count_as_the_same_name(agents, core):
    (row,) = agents.sql("SELECT lower('İade') = lower('iade') AS ayni")
    if not row["ayni"]:
        pytest.skip("Bu veritabanında lower() İ harfini i'ye indirmiyor.")
    user = agents.account("harf@peyrev.test")
    records.create_office(core, agents.identity(user), "İade")
    refused(lambda: records.create_office(core, agents.identity(user), "iade"), "OFIS_ADI", 409)
    core.clock.advance(seconds=1)
    other = records.create_office(core, agents.identity(user), "Depo")
    refused(lambda: records.rename_office(core, agents.identity(user, other["id"]), "iade"), "OFIS_ADI", 409)
    assert sorted(row["ad"] for row in agents.sql("SELECT ad FROM ajan_ofisleri")) == ["Depo", "İade"]


def test_the_owner_deletes_only_a_quiet_office_and_its_history_stays(agents, core, owner, office):
    second = agents.office(owner, "Geçici Ofis")
    here = agents.identity(owner, second)
    refused(lambda: records.delete_office(core, here, "Geçici"), "OFIS_ADI_ONAYI", 400)  # the exact name confirms it
    refused(lambda: records.delete_office(core, here, "geçici ofis"), "OFIS_ADI_ONAYI", 400)
    pending = records.request_approval(core, here, approval_payload())
    refused(lambda: records.delete_office(core, here, "Geçici Ofis"), "BEKLEYEN_ONAY", 409)
    records.decide_approval(core, here, pending["id"], "approve", 1)
    refused(lambda: records.delete_office(core, here, "Geçici Ofis"), "BEKLEYEN_ONAY", 409)  # still usable
    records.cancel_approval(core, here, pending["id"])
    task = records.create_task(core, here, task_payload(owner["id"]))
    refused(lambda: records.delete_office(core, here, "Geçici Ofis"), "ACIK_GOREV", 409)
    records.transition_task(core, here, task["id"], "running", 1)
    records.transition_task(core, here, task["id"], "succeeded", 2)
    records.request_approval(core, here, approval_payload(expires_in=5))
    core.clock.advance(seconds=6)  # an expired request no longer holds the office
    assert records.delete_office(core, here, "  Geçici   Ofis ") == {"id": second, "name": "Geçici Ofis",
                                                                       "deleted": True}
    assert [item["id"] for item in agents.identity(owner).entitlement["ofisler"]] == [office]
    refused(lambda: agents.identity(owner, second), "OFIS_ERISIMI_YOK", 403)
    # Finished work, decided approvals and the audit keep their rows; the name can be used again.
    assert agents.sql("SELECT durum FROM ajan_gorevleri WHERE id = %s", (task["id"],)) == [{"durum": "succeeded"}]
    assert len(agents.sql("SELECT id FROM ajan_onaylari WHERE ofis_id = %s", (second,))) == 2
    assert "office_deleted" in {row["olay"] for row in agents.sql(
        "SELECT olay FROM ajan_olaylari WHERE kayit_turu = 'office' AND kayit_id = %s", (second,))}
    assert records.create_office(core, agents.identity(owner), "Geçici Ofis")["name"] == "Geçici Ofis"
    refused(lambda: records.delete_office(core, agents.identity(agents.account("bos@peyrev.test")), "Herhangi"),
            "AJAN_YETKISI_YOK", 403)


def test_the_office_delete_route_takes_only_the_name_and_the_office_header(agents, client, owner, office):
    second = agents.office(owner, "Silinecek")
    headers = agents.headers(owner, second)
    extra = client.post(API + "/ofisim/sil", json={"name": "Silinecek", "ofis_id": "x"}, headers=headers)
    assert extra.status_code == 400
    deleted = client.post(API + "/ofisim/sil", json={"name": "Silinecek"}, headers=headers)
    assert deleted.status_code == 200 and deleted.get_json()["office"] == {"id": second, "name": "Silinecek",
                                                                          "deleted": True}
    again = client.post(API + "/ofisim/sil", json={"name": "Silinecek"}, headers=headers)
    assert again.status_code == 403 and again.get_json()["kod"] == "OFIS_ERISIMI_YOK"


def test_office_routes_accept_only_exact_bodies(agents, client):
    user = agents.account("rota@peyrev.test")
    headers = agents.headers(user)
    assert client.get(API + "/ofisim", headers=headers).get_json() == {
        "success": True, "office": None, "can_create": True, "members": [], "candidates": []}
    assert client.post(API + "/ofisim", json={"name": "Depo", "firma_id": 9}, headers=headers).status_code == 400
    assert client.post(API + "/ofisim", json={"name": "A"}, headers=headers).status_code == 400
    made = client.post(API + "/ofisim", json={"name": "Depo"}, headers=headers)
    assert made.status_code == 201 and made.get_json()["office"] == {
        "id": made.get_json()["office"]["id"], "name": "Depo", "authority_user_id": user["id"]}
    headers = agents.headers(user)
    renamed = client.post(API + "/ofisim/ad", json={"name": "Depo Ofisi"}, headers=headers)
    assert renamed.status_code == 200 and renamed.get_json()["office"]["name"] == "Depo Ofisi"
    assert client.post(API + "/ofisim/ad", json={}, headers=headers).status_code == 400
    assert client.post(API + "/ofisim/ayril", json={}, headers=headers).status_code == 404


def test_former_team_views_show_the_owner_alone(agents, client, owner, office):
    headers = agents.headers(owner)
    assert client.get(API + "/uyeler", headers=headers).get_json()["uyeler"] == [
        {"kullanici_id": owner["id"], "ad_soyad": "Ofis Sahibi", "tur": "human", "onayci": True}]
    view = client.get(API + "/ofisim", headers=headers).get_json()
    assert view["office"] == {"id": office, "name": "Claim & İade Departmanı", "authority_user_id": owner["id"],
                              "is_authority": True}
    assert view["members"] == [{"user_id": owner["id"], "name": "Ofis Sahibi", "authority": True}]
    assert view["candidates"] == [] and view["can_create"] is True
    assert client.get(API + "/ofislerim", headers=headers).get_json()["offices"] == [
        {"id": office, "name": "Claim & İade Departmanı", "is_authority": True, "people": 1, "open_tasks": 0,
         "pending_approvals": 0}]
