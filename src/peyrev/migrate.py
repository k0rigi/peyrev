"""Şema geçişleri: src/peyrev/migrations/NNNN_ad.sql dosyaları sırayla ve bir kez uygulanır.

Dağıtımda `python -m peyrev.migrate` çalışır (Gunicorn başlamadan önce). Her dosya kendi işleminde uygulanır;
aynı anda iki uygulayıcı danışma kilidiyle sıraya girer. Uygulanmış bir dosya DEĞİŞTİRİLMEZ; değişiklik yeni dosyayla yapılır.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from .db import Database, advisory_lock

FOLDER = Path(__file__).with_name("migrations")
NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def files() -> list[Path]:
    found = sorted(path for path in FOLDER.iterdir() if NAME.match(path.name))
    versions = [path.name[:4] for path in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError("Aynı numaralı iki geçiş dosyası var.")
    return found


def apply(db: Database) -> list[str]:
    applied: list[str] = []
    with db.transaction() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version TEXT PRIMARY KEY, name TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    for path in files():
        with db.transaction() as cur:
            advisory_lock(cur, "peyrev-migrate")
            cur.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (path.name[:4],))
            if cur.fetchone():
                continue
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute("INSERT INTO schema_migrations (version, name) VALUES (%s, %s)", (path.name[:4], path.name))
            applied.append(path.name)
    return applied


if __name__ == "__main__":  # pragma: no cover - dağıtım çalıştırır
    from .config import Settings

    database = Database(Settings.from_environment().database_url, max_size=1)
    try:
        done = apply(database)
    finally:
        database.close()
    print("Uygulanan geçişler: " + (", ".join(done) if done else "yok (şema güncel)"))
    sys.exit(0)
