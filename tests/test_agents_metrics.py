"""İnsan görevi performansı (/metrics): dönem, kapsam, bekleme süresi ve sıkı sorgu (gerçek PostgreSQL)."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from test_agents import API, Agents, refused, task_payload

from peyrev.agents import metrics, records
from peyrev.common import ApiError

START_TEXT = "2026-10-01T00:00:00Z"
END_TEXT = "2026-11-01T00:00:00Z"
BEGIN = datetime(2026, 10, 1, tzinfo=UTC)
COLLEAGUE = 9001  # a former team member whose tasks moved to the owner with the office


@pytest.fixture
def agents(core):
    return Agents(core)


@pytest.fixture
def team(agents):
    owner, other = agents.account("sahip@peyrev.test"), agents.account("baska@peyrev.test")
    return owner, agents.office(owner), other, agents.office(other, "Başka Ofis")


def task(agents, owner, status, created=BEGIN, started=None, finished=None, *, office=None, assignee=None):
    """A task row as it is stored; migrated history arrives in the same shape."""
    task_id = uuid.uuid4().hex
    agents.sql("INSERT INTO ajan_gorevleri (id, sahip_id, ofis_id, olusturan_id, atanan_id, istek_id, istek_ozeti, "
               "baslik, talimat, oncelik, durum, olusturma, baslama, bitis, guncelleme) VALUES (%s, %s, %s, %s, %s, "
               "%s, %s, 'GİZLİ-İŞ-BAŞLIĞI', 'GİZLİ-İŞ-TALİMATI', 'normal', %s, %s, %s, %s, %s)",
               (task_id, owner["id"], office, owner["id"], assignee or owner["id"], uuid.uuid4().hex, "a" * 64, status,
                created, started, finished, created))
    return task_id


def event(agents, owner, task_id, before, after, when, version):
    agents.sql("INSERT INTO ajan_olaylari (id, sahip_id, kayit_turu, kayit_id, kullanici_id, aktor, olay, ayrinti, "
               "olusturma) VALUES (%s, %s, 'task', %s, %s, 'human', 'status_changed', %s::jsonb, %s)",
               (uuid.uuid4().hex, owner["id"], task_id, owner["id"],
                json.dumps({"before": before, "after": after, "version": version}), when))


def populate(agents, team):
    owner, office, other, theirs = team
    task(agents, owner, "succeeded", started=BEGIN, finished=BEGIN + timedelta(seconds=60), office=office)
    task(agents, owner, "failed", started=BEGIN, finished=BEGIN + timedelta(seconds=120), office=office)
    task(agents, owner, "cancelled", finished=BEGIN, office=office)
    task(agents, owner, "running", started=BEGIN, office=office)
    task(agents, owner, "succeeded", started=BEGIN, finished=BEGIN + timedelta(seconds=30), office=office,
         assignee=COLLEAGUE)
    task(agents, owner, "cancelled", started=BEGIN + timedelta(seconds=1), finished=BEGIN, office=office,
         assignee=COLLEAGUE)
    task(agents, owner, "succeeded", started=BEGIN, finished=BEGIN + timedelta(seconds=999))  # outside every office
    task(agents, other, "succeeded", started=BEGIN, finished=BEGIN + timedelta(seconds=5000), office=theirs)


def test_totals_use_finished_tasks_as_the_denominator_and_stay_in_the_office(agents, core, team):
    populate(agents, team)
    owner = team[0]
    result = records.task_metrics(core, agents.identity(owner), START_TEXT, END_TEXT)
    totals = result["totals"]
    assert {key: totals[key] for key in ("total", "succeeded", "failed", "cancelled", "terminal")} == {
        "total": 6, "succeeded": 2, "failed": 1, "cancelled": 2, "terminal": 5}
    assert totals["success_rate"] == 0.4
    assert totals["total_seconds"] == 210 and totals["average_seconds"] == 42
    assert totals["duration_missing_count"] == 2 and "waiting_user_seconds" not in totals
    # Migrated history of a former colleague belongs to the owner and keeps its own bucket.
    assert [row["user_id"] for row in result["users"]] == [owner["id"], COLLEAGUE]
    assert result["users"][0]["success_rate"] == pytest.approx(1 / 3)
    assert result["users"][1]["average_seconds"] == 15
    assert (result["range_basis"], result["start"], result["end"]) == (
        "created_at", "2026-10-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00")
    serialized = json.dumps(result)
    assert "GİZLİ" not in serialized and "ad_soyad" not in serialized and "token" not in serialized


def test_without_an_office_only_own_tasks_count_and_nothing_is_invented(agents, core):
    lone = agents.account("ofissiz@peyrev.test")
    task(agents, lone, "succeeded", started=BEGIN, finished=BEGIN + timedelta(seconds=10))
    task(agents, lone, "succeeded", started=BEGIN, finished=BEGIN + timedelta(seconds=10), assignee=COLLEAGUE)
    result = records.task_metrics(core, agents.identity(lone), START_TEXT, END_TEXT)
    assert result["totals"]["total"] == 1 and [row["user_id"] for row in result["users"]] == [lone["id"]]
    empty = records.task_metrics(core, agents.identity(agents.account("bos@peyrev.test")), START_TEXT, END_TEXT)
    assert empty["users"] == [] and empty["totals"]["total"] == 0
    assert empty["totals"]["success_rate"] == empty["totals"]["average_seconds"] == 0
    assert "waiting_user_seconds" not in empty["totals"]


def test_the_period_is_start_inclusive_end_exclusive_and_not_the_last_hundred(agents, core, team):
    owner, office = team[0], team[1]
    finish = datetime(2026, 11, 1, tzinfo=UTC)
    for created in (BEGIN - timedelta(microseconds=1), BEGIN, finish - timedelta(microseconds=1), finish):
        task(agents, owner, "queued", created, office=office)
    for _ in range(110):
        task(agents, owner, "queued", BEGIN + timedelta(days=1), office=office)
    identity = agents.identity(owner)
    assert len(records.list_tasks(core, identity)) == 100
    assert records.task_metrics(core, identity, START_TEXT, END_TEXT)["totals"]["total"] == 112


def test_waiting_time_comes_from_recorded_transitions_and_an_open_wait_runs_to_now(agents, core, team):
    owner, office = team[0], team[1]
    core.clock.at = BEGIN + timedelta(hours=1)
    closed = task(agents, owner, "succeeded", started=BEGIN, finished=BEGIN + timedelta(minutes=20), office=office)
    event(agents, owner, closed, "queued", "running", BEGIN, 2)
    event(agents, owner, closed, "running", "waiting_user", BEGIN + timedelta(minutes=2), 3)
    event(agents, owner, closed, "waiting_user", "running", BEGIN + timedelta(minutes=7), 4)
    event(agents, owner, closed, "running", "succeeded", BEGIN + timedelta(minutes=20), 5)
    current = task(agents, owner, "waiting_user", started=BEGIN, office=office)
    event(agents, owner, current, "queued", "running", BEGIN, 2)
    event(agents, owner, current, "running", "waiting_user", BEGIN + timedelta(minutes=50), 3)
    result = records.task_metrics(core, agents.identity(owner), START_TEXT, END_TEXT)
    assert result["totals"]["total"] == 2
    assert result["totals"]["waiting_user_seconds"] == result["users"][0]["waiting_user_seconds"] == 900
    assert result["as_of"] == core.clock.now().isoformat()
    assert result["totals"]["total_seconds"] == 1200


def test_waiting_time_of_a_task_reported_through_studio(agents, core, team):
    owner = team[0]
    identity = agents.identity(owner)
    work = records.create_task(core, identity, task_payload(owner["id"]))
    for version, (status, minutes) in enumerate((("running", 1), ("waiting_user", 3), ("running", 1),
                                                 ("succeeded", 0)), start=1):
        records.transition_task(core, identity, work["id"], status, version)
        core.clock.advance(minutes=minutes)
    start, end = (core.clock.now() + timedelta(days=days) for days in (-1, 1))
    result = records.task_metrics(core, identity, start.isoformat(), end.isoformat())
    assert result["totals"]["waiting_user_seconds"] == 180 and result["totals"]["total_seconds"] == 300
    assert result["totals"]["succeeded"] == 1 and result["users"][0]["user_id"] == owner["id"]


def test_a_missing_wait_entry_does_not_invent_waiting_time(agents, core, team):
    owner, office = team[0], team[1]
    waiting = task(agents, owner, "succeeded", started=BEGIN, finished=BEGIN + timedelta(minutes=10), office=office)
    event(agents, owner, waiting, "waiting_user", "running", BEGIN + timedelta(minutes=5), 2)
    result = records.task_metrics(core, agents.identity(owner), START_TEXT, END_TEXT)
    assert "waiting_user_seconds" not in result["totals"]


@pytest.mark.parametrize("start,end", [
    ("2026-10-01", END_TEXT), ("2026-10-01T00:00:00", END_TEXT), ("2026-10-01T03:00:00+03:00", END_TEXT),
    ("2026-02-30T00:00:00Z", END_TEXT), (END_TEXT, START_TEXT), (START_TEXT, START_TEXT),
    ("2025-10-01T00:00:00Z", "2026-10-03T00:00:00Z"), (None, END_TEXT)])
def test_an_invalid_or_too_long_utc_period_is_refused(start, end):
    with pytest.raises(ApiError) as error:
        metrics.period(start, end)
    assert error.value.status == 400 and error.value.kod == "TARIH_ARALIGI"


def test_fractional_seconds_a_utc_offset_and_a_full_366_days_are_accepted():
    start, end = metrics.period("2024-01-01T00:00:00.123456+00:00", "2025-01-01T00:00:00.123456Z")
    assert end - start == timedelta(days=366) and start.microsecond == 123456 and start.tzinfo == UTC


def test_the_metrics_route_is_read_only_strict_and_live_scoped(agents, client, team):
    populate(agents, team)
    owner = team[0]
    path, headers, query = API + "/metrics", agents.headers(owner), {"start": START_TEXT, "end": END_TEXT}
    answer = client.get(path, query_string=query, headers=headers)
    assert answer.status_code == 200 and answer.headers["Cache-Control"] == "no-store"
    assert answer.get_json()["metrics"]["totals"]["total"] == 6
    assert client.get(path, query_string=query).status_code == 401
    assert client.get(path, query_string=dict(query, user_id=19), headers=headers).get_json()["kod"] == "GECERSIZ_GIRDI"
    for invalid in ({"start": START_TEXT}, dict(query, unexpected="x"), [("start", START_TEXT), ("start", START_TEXT),
                                                                         ("end", END_TEXT)]):
        answer = client.get(path, query_string=invalid, headers=headers)
        assert answer.status_code == 400 and answer.get_json()["kod"] == "TARIH_ARALIGI"
    assert client.post(path, json=query, headers=headers).status_code == 405
    agents.sql("DELETE FROM kullanici_modulleri WHERE kullanici_id = %s AND modul = 'MOD_AJAN'", (owner["id"],))
    assert client.get(path, query_string=query, headers=headers).status_code == 403


def test_the_office_is_checked_again_before_the_metrics_query(agents, core, team):
    owner, office = team[0], team[1]
    identity = agents.identity(owner)
    agents.sql("UPDATE ajan_ofisleri SET durum = 'pasif' WHERE id = %s", (office,))
    refused(lambda: records.task_metrics(core, identity, START_TEXT, END_TEXT), "OFIS_ERISIMI_YOK", 403)
