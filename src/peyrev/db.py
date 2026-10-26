"""PostgreSQL erişimi: bağlantı havuzu, işlem bloğu ve danışma kilidi.

Kullanım:
    with core.db.transaction() as cur:
        cur.execute("SELECT ... WHERE id = %s", (user_id,))
        row = cur.fetchone()          # dict ya da None

Her blok tek bir işlemdir (READ COMMITTED): hatasız biterse COMMIT, istisnada ROLLBACK. Satırlar sözlük döner,
kolon adları küçük harftir. Oturum saat dilimi UTC'dir; "bugün" hesapları clock.today_tr() ile yapılır.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from psycopg import Cursor
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def _configure(connection) -> None:
    connection.execute("SET TIME ZONE 'UTC'")
    connection.commit()


class Database:
    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 8):
        self.url = url
        # check: a connection the server closed (PostgreSQL restart, idle timeout) is replaced before it is handed
        # out, so the first requests after a restart do not fail.
        self.pool = ConnectionPool(url, min_size=min_size, max_size=max_size, open=True, configure=_configure,
                                   check=ConnectionPool.check_connection,
                                   kwargs={"row_factory": dict_row, "autocommit": False}, timeout=10)

    @contextmanager
    def transaction(self) -> Iterator[Cursor]:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            yield cursor
            # pool.connection() commits on a clean exit and rolls back on an exception.

    def close(self) -> None:
        self.pool.close()


def advisory_lock(cursor: Cursor, key: str) -> None:
    """İşlem sonuna kadar süren kilit (MySQL GET_LOCK karşılığı); aynı anahtarı isteyenler sıraya girer."""
    cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
