"""Testler gerçek PostgreSQL ile çalışır.

- CI: PEYREV_TEST_DATABASE_URL (postgres servis konteyneri).
- Yerel: pgserver paketiyle gelen gömülü PostgreSQL (kurulum gerekmez).
Oturum başında `peyrev_test` veritabanı sıfırdan kurulur ve geçişler uygulanır; her test öncesi tablolar boşaltılır.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, date, datetime
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from werkzeug.security import generate_password_hash

from peyrev import migrate
from peyrev.app import create_app
from peyrev.clock import FixedClock
from peyrev.config import Settings
from peyrev.core import Core
from peyrev.db import Database
from peyrev.keys import Keys

START = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # 12:00 Türkiye
DEVICE = "a" * 32


@pytest.fixture(scope="session")
def server_url():
    url = os.environ.get("PEYREV_TEST_DATABASE_URL")
    if url:
        yield url
        return
    import pgserver

    server = pgserver.get_server(tempfile.mkdtemp(prefix="peyrev-pg-"), cleanup_mode="stop")
    yield server.get_uri()
    server.cleanup()


@pytest.fixture(scope="session")
def db(server_url):
    with psycopg.connect(server_url, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS peyrev_test WITH (FORCE)")
        admin.execute("CREATE DATABASE peyrev_test")
    database = Database(make_conninfo(server_url, dbname="peyrev_test"), max_size=4)
    migrate.apply(database)
    yield database
    database.close()


class RecordingNotifier:
    """Telegram ve e-posta yerine gönderilenleri kaydeder."""

    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def send(self, subject: str, text: str) -> None:
        self.messages.append((subject, text))


def _reset(db: Database) -> None:
    with db.transaction() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'schema_migrations'")
        tables = [row["tablename"] for row in cur.fetchall()]
        if tables:
            cur.execute("TRUNCATE " + ", ".join(f'"{name}"' for name in tables) + " RESTART IDENTITY CASCADE")
        cur.execute("ALTER SEQUENCE alan_id_seq RESTART WITH 1000000")


@pytest.fixture
def core(db, tmp_path) -> Core:
    _reset(db)
    secrets = tmp_path / "secrets"
    settings = Settings(database_url=db.url, secrets_dir=secrets, allow_generate=True,
                        support_settings_path=tmp_path / "destek.env", llm_settings_path=tmp_path / "llm.env")
    return Core(settings=settings, db=db, keys=Keys(secrets, allow_generate=True), clock=FixedClock(START),
                notifier=RecordingNotifier())


@pytest.fixture
def app(core):
    return create_app(core=core)


@pytest.fixture
def client(app):
    return app.test_client()


def make_user(core: Core, giris: str, password: str = "Gizli12345", *, rol: str = "kullanici", durum: str = "aktif",
              modules: dict[str, date | None] | None = None, alan_id: int | None = None, ad_soyad: str = "Deneme Kişi",
              user_id: int | None = None, ofis_siniri: int | None = None) -> dict[str, Any]:
    """Kullanıcı ve modül hakları; `modules={"MOD_RPA": None}` süresiz hak verir. `ofis_siniri` verilmezse 1'dir."""
    columns = {"giris": giris, "ad_soyad": ad_soyad, "sifre_hash": generate_password_hash(password), "rol": rol,
               "durum": durum}
    if alan_id is not None:
        columns["alan_id"] = alan_id
    if user_id is not None:
        columns["id"] = user_id
    if ofis_siniri is not None:
        columns["ofis_siniri"] = ofis_siniri
    names = list(columns)
    with core.db.transaction() as cur:
        cur.execute(f"INSERT INTO kullanicilar ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))}) RETURNING *",
                    [columns[name] for name in names])
        user = cur.fetchone()
        for module, end in (modules or {}).items():
            cur.execute("INSERT INTO kullanici_modulleri (kullanici_id, modul, bitis) VALUES (%s, %s, %s)",
                        (user["id"], module, end))
    return user


def open_session(core: Core, user: dict[str, Any], device: str = DEVICE, session_id: str | None = None,
                 version: str = "1.3.0") -> str:
    """Lisans girişinin açtığı Studio oturumunun yerine geçer (belirteç testleri için)."""
    session_id = session_id or ("%032x" % (user["id"] * 7919))
    with core.db.transaction() as cur:
        cur.execute("INSERT INTO oturumlar (id, kullanici_id, cihaz, surum, ip) VALUES (%s, %s, %s, %s, '127.0.0.1')",
                    (session_id, user["id"], device, version))
    return session_id
