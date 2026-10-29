"""Yönetim paneli ofis işlemleri ve taşınan ajan geçmişinin şema sözleşmesi (gerçek PostgreSQL)."""

import json
from contextlib import contextmanager
from datetime import timedelta

import psycopg
import pytest
from conftest import START
from test_agents import API, Agents, Recording, approval_payload, locks, refused, task_payload

from peyrev.agents import admin, records


@pytest.fixture
def agents(core):
    return Agents(core)


@pytest.fixture
def owner(agents):
    return agents.account("sahip@peyrev.test")


@pytest.fixture
def manager(agents):
    return agents.account("yonetici@peyrev.test", rol="yonetici")


def test_the_panel_lists_an_accounts_offices_with_task_and_approval_counts(agents, core, owner):
    first = agents.office(owner, "Satış")
    closed = agents.add_office(owner, "Kapalı Ofis", durum="pasif", created=START + timedelta(hours=1))
    identity = agents.identity(owner, first)
    for keep in (True, False):
        work = records.create_task(core, identity, task_payload(owner["id"]))
        records.transition_task(core, identity, work["id"], "cancelled", 1)
        if not keep:
            records.delete_task(core, identity, work["id"], 2)  # hidden tasks are not counted
    records.create_task(core, identity, task_payload(owner["id"]))
    records.request_approval(core, identity, approval_payload())
    records.request_approval(core, identity, approval_payload(expires_in=5))
    core.clock.advance(seconds=6)  # counted, but no longer waiting
    result = admin.user_offices(core, owner["id"])
    assert (result["kullanici_id"], result["ofis_siniri"]) == (owner["id"], 50)
    assert [(item["id"], item["ad"], item["durum"], item["gorev"], item["acik_gorev"], item["onay"],
             item["bekleyen_onay"]) for item in result["ofisler"]] == [
        (first, "Satış", "aktif", 2, 1, 2, 1), (closed, "Kapalı Ofis", "pasif", 0, 0, 0, 0)]
    assert result["ofisler"][1]["olusturma"] == (START + timedelta(hours=1)).isoformat()
    assert "GİZLİ" not in json.dumps(result) and "Kontrol görevi" not in json.dumps(result)
    empty = agents.account("bos@peyrev.test")
    assert admin.user_offices(core, empty["id"]) == {"kullanici_id": empty["id"], "ofis_siniri": 50, "ust_sinir": 50, "ofisler": []}
    refused(lambda: admin.user_offices(core, 999999), "BULUNAMADI", 404)


def test_the_panel_closes_and_reopens_an_office_and_every_change_is_audited(agents, core, client, owner, manager):
    first, second = agents.office(owner, "Birinci"), agents.office(owner, "İkinci")
    assert admin.set_office_status(core, manager["id"], second, "pasif") == {
        "id": second, "kullanici_id": owner["id"], "ad": "İkinci", "durum": "pasif", "degisti": True}
    assert [item["id"] for item in agents.identity(owner).entitlement["ofisler"]] == [first]
    refused(lambda: agents.identity(owner, second), "OFIS_ERISIMI_YOK", 403)
    assert client.get(API + "/ofisim", headers=agents.headers(owner, second)).status_code == 403
    assert admin.set_office_status(core, manager["id"], second, "pasif")["degisti"] is False  # nothing to write
    assert agents.sql("SELECT yonetici_id, tur, hedef, ayrinti FROM yonetim_olaylari") == [
        {"yonetici_id": manager["id"], "tur": "ajan_ofisi_durumu", "hedef": "ajan_ofisi:" + second,
         "ayrinti": {"kullanici_id": owner["id"], "onceki": "aktif", "durum": "pasif"}}]
    assert admin.set_office_status(core, manager["id"], second, "aktif")["degisti"] is True
    assert [item["id"] for item in agents.identity(owner).entitlement["ofisler"]] == [first, second]
    audit = agents.sql("SELECT * FROM yonetim_olaylari ORDER BY id")
    assert [row["ayrinti"]["durum"] for row in audit] == ["pasif", "aktif"]
    assert owner["sifre_hash"] not in json.dumps(audit, default=str)


def test_the_panel_status_change_checks_its_input(agents, core, owner, manager):
    office = agents.office(owner)
    refused(lambda: admin.set_office_status(core, manager["id"], "KISA", "pasif"), "GECERSIZ_GIRDI", 400)
    refused(lambda: admin.set_office_status(core, manager["id"], office.upper(), "pasif"), "GECERSIZ_GIRDI", 400)
    refused(lambda: admin.set_office_status(core, manager["id"], office, "Pasif"), "GECERSIZ_GIRDI", 400)
    refused(lambda: admin.set_office_status(core, manager["id"], "f" * 32, "pasif"), "BULUNAMADI", 404)
    assert agents.sql("SELECT id FROM yonetim_olaylari") == []


def test_the_panel_queues_behind_the_owners_account_row_like_studio(agents, core, owner, manager, monkeypatch):
    office = agents.office(owner)
    statements, original = [], core.db.transaction

    @contextmanager
    def recorded():
        with original() as cur:
            recording = Recording(cur)
            yield recording
            statements.extend(recording.statements)

    monkeypatch.setattr(core.db, "transaction", recorded)
    admin.set_office_status(core, manager["id"], office, "pasif")
    assert locks(statements) == ["kullanicilar", "ajan_ofisleri"]


# ----- migrated history ---------------------------------------------------------------------------------------------
def test_migrated_offices_keep_their_identifiers_and_former_members_rows_stay_readable(agents, core):
    owner = agents.account("tasinan@peyrev.test", alan_id=2)
    office = agents.add_office(owner, "Claim & İade Departmanı", office_id="c" * 32, created=START - timedelta(days=30))
    member, task_id, approval_id = 9001, "1" * 32, "4" * 32
    agents.sql("INSERT INTO ajan_gorevleri (id, sahip_id, ofis_id, olusturan_id, atanan_id, istek_id, istek_ozeti, "
               "baslik, talimat, oncelik, durum, surum, olusturma, baslama, bitis, guncelleme) VALUES "
               "(%s, %s, %s, %s, %s, %s, %s, 'Eski görev', 'Eski talimat', 'normal', 'succeeded', 3, %s, %s, %s, %s)",
               (task_id, owner["id"], office, owner["id"], member, "2" * 32, "3" * 64, START - timedelta(days=2),
                START - timedelta(days=2), START - timedelta(days=1), START - timedelta(days=1)))
    agents.sql("INSERT INTO ajan_onaylari (id, sahip_id, ofis_id, isteyen_id, onayci_id, oturum_id, istek_id, "
               "istek_ozeti, calisma_id, akis_id, akis_ozeti, girdi_ozeti, onizleme_ozeti, baglam_ozeti, onizleme, "
               "gerekce, deneme, durum, surum, karar_nedeni, olusturma, gecerlilik, karar_zamani, tuketim_zamani) "
               "VALUES "
               "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Eski gerekçe', 1, 'consumed', 3, 'user', "
               "%s, %s, %s, %s)",
               (approval_id, owner["id"], office, member, owner["id"], "5" * 32, "6" * 32, "7" * 64, "8" * 32, "9" * 32,
                "a" * 64, "b" * 64, "c" * 64, "d" * 64, '{"deger":"***"}', START - timedelta(days=1),
                START - timedelta(days=1) + timedelta(minutes=10), START - timedelta(days=1),
                START - timedelta(days=1)))
    agents.sql("INSERT INTO ajan_olaylari (id, sahip_id, kayit_turu, kayit_id, kullanici_id, aktor, olay, ayrinti, "
               "olusturma) VALUES (%s, %s, 'task', %s, %s, 'human', 'status_changed', %s::jsonb, %s)",
               ("e" * 32, owner["id"], task_id, member, json.dumps({"before": "running", "after": "succeeded",
                                                                    "version": 3}), START - timedelta(days=1)))
    identity = agents.identity(owner)
    assert (identity.office_id, identity.entitlement["firma_id"], identity.entitlement["ofis_adi"]) == (
        office, 2, "Claim & İade Departmanı")
    tasks, approvals = records.list_tasks(core, identity), records.list_approvals(core, identity)
    assert [(item["id"], item["assignee_user_id"]) for item in tasks] == [(task_id, member)]
    assert [(item["id"], item["requester_user_id"], item["status"]) for item in approvals] == [
        (approval_id, member, "consumed")]
    assert records.get_approval(core, identity, approval_id)["preview"] == {"deger": "***"}
    # The history is read only: the old session is gone, so the record can never be used or withdrawn again.
    refused(lambda: records.cancel_approval(core, identity, approval_id), "ONAY_OTURUMU", 403)
    start, end = (START + timedelta(days=days) for days in (-3, 1))
    result = records.task_metrics(core, identity, start.isoformat(), end.isoformat())
    assert [row["user_id"] for row in result["users"]] == [member] and result["totals"]["total_seconds"] == 86400


def test_the_schema_refuses_rows_studio_or_the_rules_cannot_use(agents, owner):
    agents.add_office(owner, "Depo", office_id="d" * 32)

    def office(office_id, user_id, name, durum="aktif"):
        return lambda: agents.sql("INSERT INTO ajan_ofisleri (id, sahip_id, ad, durum) VALUES (%s, %s, %s, %s)",
                                  (office_id, user_id, name, durum))

    for work, error in ((office("D" * 32, owner["id"], "Büyük Harfli Kimlik"), psycopg.errors.CheckViolation),
                        (office("e" * 32, owner["id"], "Durum", "Aktif"), psycopg.errors.CheckViolation),
                        (office("e" * 32, owner["id"], "A"), psycopg.errors.CheckViolation),
                        (office("e" * 32, owner["id"], "DEPO"), psycopg.errors.UniqueViolation),
                        (office("e" * 32, 999999, "Sahipsiz"), psycopg.errors.ForeignKeyViolation)):
        with pytest.raises(error):
            work()
    with pytest.raises(psycopg.errors.CheckViolation):
        agents.sql("INSERT INTO ajan_olaylari (id, sahip_id, kayit_turu, kayit_id, aktor, olay, olusturma) "
                   "VALUES (%s, %s, 'account', %s, 'human', 'account_created', now())",
                   ("f" * 32, owner["id"], "0" * 32))


def test_deleting_an_account_removes_its_offices_and_records_but_not_the_audit(agents, core, owner):
    identity = agents.identity(owner)
    office = agents.office(owner)
    records.create_task(core, agents.identity(owner), task_payload(owner["id"]))
    records.request_approval(core, identity, approval_payload())
    agents.sql("DELETE FROM kullanicilar WHERE id = %s", (owner["id"],))
    assert agents.sql("SELECT id FROM ajan_ofisleri") == [] and agents.sql("SELECT id FROM ajan_gorevleri") == []
    assert agents.sql("SELECT id FROM ajan_onaylari") == []
    kept = agents.sql("SELECT kayit_id FROM ajan_olaylari WHERE kayit_turu = 'office'")
    assert {row["kayit_id"] for row in kept} == {office}
