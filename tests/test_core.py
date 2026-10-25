"""Çekirdek: geçişler, sağlık ucu, ortak hata biçimi, giriş adı eşleşmesi, modül hakları, Studio belirteçleri."""

from datetime import date, timedelta

import pytest
from conftest import DEVICE, make_user, open_session

from peyrev import migrate, tokens, users
from peyrev.common import ApiError


def test_migrations_are_applied_once(core):
    assert migrate.apply(core.db) == []
    with core.db.transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM schema_migrations")
        assert cur.fetchone()["n"] == len(migrate.files())


def test_health_reaches_the_database_and_errors_keep_their_shape(client):
    answer = client.get("/api/saglik")
    assert answer.status_code == 200 and answer.get_json()["durum"] == "ok"
    assert answer.headers["Cache-Control"] == "no-store"
    missing = client.get("/api/yok-boyle-bir-uc")
    assert missing.status_code == 404 and missing.get_json() == {
        "success": False, "kod": "BULUNAMADI", "message": "İstenen kayıt veya uç bulunamadı."}


def test_login_names_match_like_orkestrai(core):
    ayse = make_user(core, "Ayse@Firma.Test")
    make_user(core, "BASBUG_1")
    with core.db.transaction() as cur:
        assert [row["id"] for row in users.find_by_login(cur, "ayse@firma.test")] == [ayse["id"]]
        assert [row["id"] for row in users.find_by_login(cur, "AYSE")] == [ayse["id"]]
        assert [row["giris"] for row in users.find_by_login(cur, "basbug_1")] == ["BASBUG_1"]
        assert users.find_by_login(cur, "  ") == []


def test_module_rights_are_per_user_and_the_end_day_counts(core):
    today = core.clock.today_tr()
    open_ended = make_user(core, "a@x.test", modules={users.RPA: None})
    ends_today = make_user(core, "b@x.test", modules={users.RPA: today})
    ended = make_user(core, "c@x.test", modules={users.RPA: today - timedelta(days=1)})
    admin = make_user(core, "d@x.test", rol="yonetici")
    with core.db.transaction() as cur:
        assert users.module_state(cur, open_ended, users.RPA, today) == (users.ACTIVE, None)
        assert users.module_state(cur, ends_today, users.RPA, today) == (users.ACTIVE, today)
        assert users.module_state(cur, ended, users.RPA, today)[0] == users.EXPIRED
        assert users.module_state(cur, admin, users.RPA, today) == (users.ACTIVE, None)
        assert users.module_state(cur, admin, users.AGENTS, today) == (users.MISSING, None)


def test_studio_tokens_check_purpose_password_and_session_every_time(core):
    user = make_user(core, "e@x.test", modules={users.RPA: None}, alan_id=7)
    session = open_session(core, user)
    token = tokens.issue(core, "destek", user, DEVICE, session)

    def check(value, purpose="destek"):
        with core.db.transaction() as cur:
            return tokens.verify(core, cur, purpose, value)

    found, claims = check("Bearer " + token)
    assert found["id"] == user["id"] and claims["firma_id"] == 7
    for value, kod in ((None, "OTURUM_GEREKLI"), ("Basic x", "OTURUM_GEREKLI"), ("Bearer bozuk", "OTURUM_GECERSIZ")):
        with pytest.raises(ApiError) as error:
            check(value)
        assert error.value.kod == kod and error.value.status == 401
    with pytest.raises(ApiError):
        check("Bearer " + token, purpose="ajan")  # a support token is not an agent token
    core.clock.advance(seconds=tokens.TOKEN_SECONDS + 1)
    with pytest.raises(ApiError):
        check("Bearer " + token)
    core.clock.advance(seconds=-(tokens.TOKEN_SECONDS + 1))
    with core.db.transaction() as cur:
        cur.execute("UPDATE oturumlar SET durum = 'kapandi' WHERE id = %s", (session,))
    with pytest.raises(ApiError):
        check("Bearer " + token)
    with core.db.transaction() as cur:
        cur.execute("UPDATE oturumlar SET durum = 'aktif' WHERE id = %s", (session,))
        cur.execute("UPDATE kullanicilar SET sifre_hash = 'yeni' WHERE id = %s", (user["id"],))
    with pytest.raises(ApiError):
        check("Bearer " + token)


def test_agent_token_ends_with_the_module(core):
    user = make_user(core, "f@x.test", modules={users.RPA: None})
    session = open_session(core, user)
    end = core.clock.now() + timedelta(seconds=60)
    token = tokens.issue(core, "ajan", user, DEVICE, session, expires=end)
    core.clock.advance(seconds=61)
    with core.db.transaction() as cur, pytest.raises(ApiError):
        tokens.verify(core, cur, "ajan", "Bearer " + token)


def test_new_accounts_get_a_workspace_id_that_cannot_clash_with_migrated_ones(core):
    migrated = make_user(core, "g@x.test", alan_id=2)
    fresh = make_user(core, "h@x.test")
    assert migrated["alan_id"] == 2 and fresh["alan_id"] >= 1000000
    assert date.today()  # keeps the import used


def test_bodies_with_values_the_database_would_refuse_are_invalid_input(app):
    from flask import request as current

    from peyrev.common import json_body, ok

    @app.post("/api/deneme-govde")
    def echo():
        assert current.path == "/api/deneme-govde"
        return ok(alan=json_body({"alan"})["alan"])

    client = app.test_client()
    assert client.post("/api/deneme-govde", data=b'{"alan": 1.5}', content_type="application/json").status_code == 200
    for raw in (b'{"alan": 1e999}', b'{"alan": "\\ud800"}', b'{"\\udfff": 1}', b'{"alan": NaN}'):
        answer = client.post("/api/deneme-govde", data=raw, content_type="application/json")
        assert answer.status_code == 400 and answer.get_json()["kod"] == "GECERSIZ", raw


def test_unexpected_errors_log_only_the_type_and_place(app, caplog):
    @app.get("/api/deneme-hata")
    def broken():
        raise RuntimeError("SELECT gizli_deger FROM tablo")

    answer = app.test_client().get("/api/deneme-hata")
    assert answer.status_code == 503 and answer.get_json()["kod"] == "SUNUCU"
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in logged and "test_core.py" in logged and "gizli_deger" not in logged
    assert not any(record.exc_info for record in caplog.records)
