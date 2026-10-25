"""Ofis görünümü (/ofis): Studio'nun 3B ofisinde yalnız hesabın kendisi ve görev durumları; iş metni yoktur."""

import json

import pytest
from test_agents import API, Agents, refused
from test_agents_metrics import COLLEAGUE, task

from peyrev.agents import records

FIELDS = {"id", "user_id", "name", "kind", "role", "department", "status", "task_id", "task_label", "queue_count",
          "can_view_tasks"}


@pytest.fixture
def agents(core):
    return Agents(core)


@pytest.fixture
def owner(agents):
    return agents.account("sahip@peyrev.test", ad_soyad="Ofis Sahibi")


def test_the_office_shows_the_owner_with_task_states_and_no_business_data(agents, core, owner):
    office = agents.office(owner)
    other = agents.account("baska@peyrev.test")
    theirs = agents.office(other, "Başka Ofis")
    running = task(agents, owner, "running", office=office)
    task(agents, owner, "queued", office=office)
    task(agents, owner, "queued", office=office)
    task(agents, owner, "waiting_user", office=office, assignee=COLLEAGUE)  # a migrated colleague's task
    task(agents, owner, "waiting_user")  # outside the office
    task(agents, other, "waiting_user", office=theirs)
    rows = records.office_people(core, agents.identity(owner))
    assert rows == [{"id": "human-" + str(owner["id"]), "user_id": owner["id"], "name": "Ofis Sahibi", "kind": "human",
                     "role": "human", "department": "", "status": "running", "task_id": running,
                     "task_label": "Görev üzerinde çalışıyor", "queue_count": 2, "can_view_tasks": True}]
    assert set(rows[0]) == FIELDS and "GİZLİ" not in json.dumps(rows)


def test_the_most_urgent_state_is_shown_and_idle_without_open_work(agents, core, owner):
    office = agents.office(owner)
    identity = agents.identity(owner)
    idle = records.office_people(core, identity)[0]
    assert (idle["status"], idle["task_id"], idle["task_label"], idle["queue_count"]) == (
        "idle", None, "Görev bekliyor", 0)
    waiting = sorted(task(agents, owner, "waiting_user", office=office) for _ in range(3))
    task(agents, owner, "running", office=office)
    person = records.office_people(core, identity)[0]
    assert (person["status"], person["task_id"], person["task_label"]) == (
        "waiting_user", waiting[0], "Kullanıcı yanıtını bekliyor")


def test_an_account_without_an_office_still_appears_in_the_office(agents, core):
    lone = agents.account("ofissiz@peyrev.test")
    task(agents, lone, "queued")
    rows = records.office_people(core, agents.identity(lone))
    assert [(row["user_id"], row["status"], row["queue_count"]) for row in rows] == [(lone["id"], "queued", 1)]


def test_the_office_counts_every_open_task_without_the_last_hundred_limit(agents, core, owner):
    office = agents.office(owner)
    for _ in range(110):
        task(agents, owner, "queued", office=office)
    identity = agents.identity(owner)
    assert records.office_people(core, identity)[0]["queue_count"] == 110
    agents.sql("DELETE FROM kullanici_modulleri WHERE kullanici_id = %s AND modul = 'MOD_AJAN'", (owner["id"],))
    refused(lambda: records.office_people(core, identity), "AJAN_YETKISI_YOK", 403)


def test_the_office_route_is_authenticated_read_only_and_takes_no_query(agents, client, owner):
    agents.office(owner)
    headers = agents.headers(owner)
    assert client.get(API + "/ofis").status_code == 401
    answer = client.get(API + "/ofis", headers=headers)
    assert answer.status_code == 200 and answer.get_json()["success"] is True
    assert answer.headers["Cache-Control"] == "no-store"
    assert [person["user_id"] for person in answer.get_json()["people"]] == [owner["id"]]
    assert client.post(API + "/ofis", headers=headers, json={}).status_code == 405
    assert client.get(API + "/ofis?firma_id=9", headers=headers).get_json()["kod"] == "GECERSIZ_GIRDI"
    debug = client.get(API + "/ofis?debug=1", headers=headers)
    assert debug.status_code == 400 and debug.get_json()["message"] == "Ofis görünümü sorgu parametresi kabul etmez."
    agents.sql("DELETE FROM kullanici_modulleri WHERE kullanici_id = %s AND modul = 'MOD_AJAN'", (owner["id"],))
    assert client.get(API + "/ofis", headers=headers).status_code == 403
