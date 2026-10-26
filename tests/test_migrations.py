"""Şema geçişleri: 0007 eski sistemden taşınan hesapların kaynak değerini 'tasima' yapar ve eski değeri artık kabul etmez."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from peyrev import migrate
from peyrev.db import Database


@pytest.fixture
def fresh(server_url):
    with psycopg.connect(server_url, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS peyrev_gecis WITH (FORCE)")
        admin.execute("CREATE DATABASE peyrev_gecis")
    database = Database(make_conninfo(server_url, dbname="peyrev_gecis"), max_size=1)
    yield database
    database.close()


def test_migrated_accounts_become_tasima_and_the_old_value_is_refused(fresh):
    with fresh.transaction() as cur:
        cur.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, name TEXT NOT NULL, "
                    "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        for path in migrate.files():
            if path.name < "0007":
                cur.execute(path.read_text(encoding="utf-8"))
                cur.execute("INSERT INTO schema_migrations (version, name) VALUES (%s, %s)", (path.name[:4], path.name))
        cur.execute("INSERT INTO kullanicilar (giris, kaynak) VALUES ('tasinan', 'orkestrai'), ('yeni', 'basvuru')")
    assert migrate.apply(fresh) == ["0007_eski_sistem.sql"]
    with fresh.transaction() as cur:
        cur.execute("SELECT giris, kaynak FROM kullanicilar ORDER BY id")
        assert [tuple(row.values()) for row in cur.fetchall()] == [("tasinan", "tasima"), ("yeni", "basvuru")]
    with pytest.raises(psycopg.errors.CheckViolation), fresh.transaction() as cur:
        cur.execute("INSERT INTO kullanicilar (giris, kaynak) VALUES ('eski', 'orkestrai')")
