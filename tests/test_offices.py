"""Hesap başına ofis sınırı (her ofis bağımsız bir müşteridir): varsayılan tek ofis, panelden sınır ve müşteri ofisi,
"Sunucu hesabı" ile açılan hesap ve mevcut ofisleri koruyan geçiş."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_user, open_session
from test_admin_auth import events, post, signed_in
from test_agents import LICENSED, Agents, extension_of, refused, studio_reads

from peyrev import users
from peyrev.agents import entitlement, records

MIGRATION = Path(__file__).resolve().parents[1] / "src" / "peyrev" / "migrations" / "0012_ofis_siniri.sql"


@pytest.fixture
def agents(core):
    return Agents(core)


def customer(agents, giris="musteri@ornek.test", **columns):
    """A customer as the panel or an application opens it: the default limit of one office."""
    user = make_user(agents.core, giris, modules=dict(LICENSED), **columns)
    agents.sessions[user["id"]] = open_session(agents.core, user)
    return user


def office_rows(agents, user):
    return [row["ad"] for row in agents.sql("SELECT ad FROM ajan_ofisleri WHERE sahip_id = %s ORDER BY olusturma, id",
                                            (user["id"],))]


@pytest.fixture
def admin(app, core):
    make_user(core, "yonetici@peyrev.test", rol="yonetici")
    return signed_in(app)


def test_a_customer_opens_one_office_and_studio_then_offers_no_other(agents, core):
    user = customer(agents)
    assert user["ofis_siniri"] == 1
    extension, _limit = extension_of(core, user)
    assert studio_reads(extension, user)["can_create"] is True
    records.create_office(core, agents.identity(user), "A Firması")
    extension, _limit = extension_of(core, user)
    # Studio hides "Yeni ofis kur" when the flag is missing; the server refuses a second office all the same.
    assert "ofis_kurabilir" not in extension and studio_reads(extension, user)["can_create"] is False
    error = refused(lambda: records.create_office(core, agents.identity(user), "B Firması"), "OFIS_SINIRI", 409)
    assert str(error) == entitlement.OFFICE_LIMIT_REACHED
    assert office_rows(agents, user) == ["A Firması"]


def test_a_closed_office_still_uses_the_single_place(agents, core):
    user = customer(agents)
    agents.add_office(user, "Eski Ofis", durum="pasif")
    refused(lambda: records.create_office(core, agents.identity(user), "Yeni Ofis"), "OFIS_SINIRI", 409)


def test_the_panel_raises_the_limit_and_opens_customer_offices(admin, agents, core):
    user = customer(agents)
    records.create_office(core, agents.identity(user), "A Firması")
    full = post(admin, f"kullanicilar/{user['id']}/ofisler", {"ad": "B Firması"})
    assert (full.status_code, full.get_json()["kod"]) == (409, "OFIS_SINIRI")
    assert "Önce hesabın ofis sınırını artırın." in full.get_json()["message"]
    changed = post(admin, f"kullanicilar/{user['id']}/ofis-siniri", {"sinir": 3}).get_json()["ofis"]
    assert changed == {"kullanici_id": user["id"], "ofis_siniri": 3, "ofis_sayisi": 1}
    core.clock.advance(seconds=1)  # offices are listed by opening time
    opened = post(admin, f"kullanicilar/{user['id']}/ofisler", {"ad": "  B   Firması "})
    assert opened.status_code == 201
    assert opened.get_json()["ofis"] | {"id": None} == {"id": None, "kullanici_id": user["id"], "ad": "B Firması",
                                                         "durum": "aktif"}
    same = post(admin, f"kullanicilar/{user['id']}/ofisler", {"ad": "b firması"})
    assert (same.status_code, same.get_json()["kod"]) == (409, "OFIS_ADI")
    # The owner's Studio sees the new office and may open the third one itself.
    extension, _limit = extension_of(core, user)
    assert [item["ad"] for item in extension["ofisler"]] == ["A Firması", "B Firması"]
    assert extension["ofis_kurabilir"] is True
    records.create_office(core, agents.identity(user), "C Firması")
    refused(lambda: records.create_office(core, agents.identity(user), "D Firması"), "OFIS_SINIRI", 409)
    kinds = [(row["tur"], row["ayrinti"]) for row in events(core) if row["tur"] in ("ofis_siniri", "ajan_ofisi_acildi")]
    assert kinds == [("ofis_siniri", {"onceki": 1, "yeni": 3}),
                     ("ajan_ofisi_acildi", {"kullanici_id": user["id"], "ad": "B Firması"})]
    detail = admin.get(f"/api/yonetim/kullanicilar/{user['id']}").get_json()["ofisler"]
    assert (detail["sinir"], detail["ust_sinir"], len(detail["liste"])) == (3, entitlement.MAX_OFFICES, 3)


def test_a_lower_limit_keeps_existing_offices_and_only_stops_new_ones(admin, agents, core):
    user = customer(agents, ofis_siniri=5)
    for name in ("A Firması", "B Firması", "C Firması"):
        agents.office(user, name)
    assert post(admin, f"kullanicilar/{user['id']}/ofis-siniri", {"sinir": 1}).get_json()["ofis"]["ofis_sayisi"] == 3
    extension, _limit = extension_of(core, user)
    assert len(extension["ofisler"]) == 3 and "ofis_kurabilir" not in extension
    # Setting the same limit again writes no event.
    post(admin, f"kullanicilar/{user['id']}/ofis-siniri", {"sinir": 1})
    assert len([row for row in events(core) if row["tur"] == "ofis_siniri"]) == 1


@pytest.mark.parametrize("body", [{"sinir": 0}, {"sinir": 51}, {"sinir": "3"}, {"sinir": 2.5}, {"sinir": True}, {}])
def test_the_limit_must_be_a_whole_number_within_bounds(admin, agents, core, body):
    user = customer(agents)
    response = post(admin, f"kullanicilar/{user['id']}/ofis-siniri", body)
    assert response.status_code == 400
    assert agents.sql("SELECT ofis_siniri FROM kullanicilar WHERE id = %s", (user["id"],))[0]["ofis_siniri"] == 1


@pytest.mark.parametrize("name", ["A", "", "  ", "A_Firması", "<b>A</b>", 42, None, "x" * 121])
def test_a_panel_office_needs_a_valid_name(admin, agents, name):
    user = customer(agents, ofis_siniri=3)
    assert post(admin, f"kullanicilar/{user['id']}/ofisler", {"ad": name}).status_code == 400
    assert office_rows(agents, user) == []


def test_unknown_accounts_are_refused(admin):
    assert post(admin, "kullanicilar/999999/ofis-siniri", {"sinir": 2}).status_code == 404
    assert post(admin, "kullanicilar/999999/ofisler", {"ad": "A Firması"}).status_code == 404


NEW = {"giris": "sunucu", "ad_soyad": "Peyrev Sunucu", "baslangic": "2026-10-26", "bitis": "2027-10-26", "ajan": True,
       "sifre": "Sunucu2026x"}


def test_a_server_account_is_opened_with_room_for_several_customers(admin, core):
    created = post(admin, "kullanicilar", {**NEW, "sunucu": True})
    assert created.status_code == 201, created.get_json()
    user_id = created.get_json()["kullanici"]["kullanici_id"]
    with core.db.transaction() as cur:
        account = users.get(cur, user_id)
    assert (account["rol"], account["ofis_siniri"]) == ("kullanici", 20)
    listed = admin.get("/api/yonetim/kullanicilar?ara=sunucu").get_json()["kullanicilar"]
    assert [(item["giris"], item["ofis_siniri"]) for item in listed] == [("sunucu", 20)]
    plain = post(admin, "kullanicilar", {**NEW, "giris": "musteri2", "sifre": "Musteri2026x"}).get_json()
    with core.db.transaction() as cur:
        assert users.get(cur, plain["kullanici"]["kullanici_id"])["ofis_siniri"] == 1


@pytest.mark.parametrize(("changes", "message"), [
    ({"sunucu": True, "rol": "yonetici"}, "Sunucu hesabı yönetici rolü olmadan, kullanıcı olarak açılır."),
    ({"sunucu": "evet"}, "Sunucu hesabı yönetici rolü olmadan, kullanıcı olarak açılır."),
])
def test_a_server_account_is_never_an_administrator(admin, core, changes, message):
    response = post(admin, "kullanicilar", {**NEW, **changes})
    assert (response.status_code, response.get_json()["message"]) == (400, message)
    with core.db.transaction() as cur:
        cur.execute("SELECT 1 FROM kullanicilar WHERE giris = 'sunucu'")
        assert cur.fetchone() is None


def test_the_migration_keeps_every_existing_office(agents, core):
    many, one, _none = (customer(agents, giris) for giris in ("cok@ornek.test", "tek@ornek.test", "yok@ornek.test"))
    for number in range(3):
        agents.add_office(many, f"Ofis {number}", durum="pasif" if number == 2 else "aktif")
    agents.add_office(one, "Tek Ofis")
    agents.sql("UPDATE kullanicilar SET ofis_siniri = 1")
    update = next(line for line in MIGRATION.read_text(encoding="utf-8").splitlines() if line.startswith("UPDATE"))
    agents.sql(update)
    limits = {row["giris"]: row["ofis_siniri"] for row in agents.sql("SELECT giris, ofis_siniri FROM kullanicilar")}
    assert (limits["cok@ornek.test"], limits["tek@ornek.test"], limits["yok@ornek.test"]) == (3, 1, 1)


def test_an_account_with_several_offices_opens_agents_only_from_studio_1_3_0(agents, core):
    """Studio 1.3.0 isolates offices; an earlier Studio would let one office reach another."""
    user = customer(agents, ofis_siniri=5)
    for name in ("A Firması", "B Firması"):
        agents.office(user, name)
    for version, active in (("1.2.1", False), ("1.0.0", False), ("", False), ("1.3.0", True), ("1.10.2", True)):
        agents.sql("UPDATE oturumlar SET surum = %s WHERE kullanici_id = %s", (version, user["id"]))
        extension, _limit = extension_of(core, user, session=agents.sessions[user["id"]])
        assert extension["aktif"] is active, version
        assert extension.get("guncelleme_gerekli", False) is not active
    agents.sql("UPDATE oturumlar SET surum = '1.2.1' WHERE kullanici_id = %s", (user["id"],))
    error = refused(lambda: agents.identity(user), "SURUM_ESKI", 403)
    assert "1.3.0" in str(error)
    # A single office keeps working with every earlier Studio.
    single = customer(agents, "tek@ornek.test")
    agents.office(single, "Tek Ofis")
    agents.sql("UPDATE oturumlar SET surum = '1.2.1' WHERE kullanici_id = %s", (single["id"],))
    assert extension_of(core, single, session=agents.sessions[single["id"]])[0]["aktif"] is True
