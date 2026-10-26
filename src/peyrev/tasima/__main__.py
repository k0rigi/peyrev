"""Taşıma çalıştırıcısı: `python -m peyrev.tasima --hedef {prova,gercek} --kaynak-ayar <orkestrai api/db.py> [--yeniden]`.

deploy/tasima.sh (Sunucu işleri → tasima-prova / tasima-gercek) bunu peyrev hesabıyla çalıştırır. Kaynak bağlantısı
db.py'deki DB_CONFIG'ten ast ile okunur (dosya çalıştırılmaz) ve orkestrai veritabanı tek tutarlı, salt okunur işlemde
okunur. Hedef adresi PEYREV_SECRETS_DIR (varsayılan /etc/peyrev) altındaki db.env'dendir: gercek → DATABASE_URL,
prova → PROVA_DATABASE_URL; ortamda PEYREV_DATABASE_URL / PEYREV_PROVA_DATABASE_URL varsa önce onlar kullanılır.
Prova adresi gerçek veritabanının adını taşıyorsa hiçbir şey yapılmaz (prova şemayı siler). Gerçek taşıma daha önce
yapılmışsa (hedefte taşınmış hesap varsa) yalnız --yeniden ile yeniden çalışır.

Rapor stdout'a yazılır, kişisel veri içermez. Çıkış: 0 bütün denetimler tuttu; 1 sorun, uyuşmazlık ya da hata (gerçek
kipte işlem geri alınmıştır); 2 komut satırı hatası.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import psycopg
from psycopg.conninfo import conninfo_to_dict

from ..config import SECRETS_DIR, _read_env_file
from . import report, target
from .source import MysqlSource, Source, SourceError, read_settings

# Kip → (ortam değişkeni, db.env anahtarı)
URLS = {target.GERCEK: ("PEYREV_DATABASE_URL", "DATABASE_URL"),
        target.PROVA: ("PEYREV_PROVA_DATABASE_URL", "PROVA_DATABASE_URL")}


def target_url(mode: str, environ: Mapping[str, str]) -> str:
    """Hedef veritabanı adresi; hata metni adresi ya da şifreyi içermez."""
    secrets = Path(environ.get("PEYREV_SECRETS_DIR", str(SECRETS_DIR)))
    try:
        stored = _read_env_file(secrets / "db.env")
    except (OSError, UnicodeDecodeError):
        raise target.TargetError(f"{secrets / 'db.env'} okunamadı.") from None

    def lookup(kind: str) -> str:
        variable, key = URLS[kind]
        return environ.get(variable) or stored.get(key) or ""

    url = lookup(mode)
    if not url:
        variable, key = URLS[mode]
        raise target.TargetError(f"Hedef adresi bulunamadı: {secrets / 'db.env'} içinde {key} ya da ortamda {variable} "
                                 "gerekli.")
    if mode == target.PROVA:
        name, real = _database(url, URLS[mode][1]), lookup(target.GERCEK)
        if not name:
            raise target.TargetError("PROVA_DATABASE_URL veritabanı adı içermiyor; prova yapılmadı.")
        if real and _database(real, URLS[target.GERCEK][1]) == name:
            raise target.TargetError("PROVA_DATABASE_URL gerçek veritabanını gösteriyor; prova yapılmadı.")
    return url


def _database(url: str, key: str) -> str:
    try:
        return conninfo_to_dict(url).get("dbname") or ""
    except psycopg.ProgrammingError:
        raise target.TargetError(f"{key} geçerli bir PostgreSQL adresi değil.") from None


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] = os.environ,
         source_factory: Callable[[dict[str, Any]], Source] = MysqlSource, clock: Callable[[], datetime] | None = None,
         out: TextIO | None = None) -> int:
    out = out if out is not None else sys.stdout
    parser = argparse.ArgumentParser(prog="python -m peyrev.tasima", description="orkestrai → Peyrev veri taşıması.")
    parser.add_argument("--hedef", required=True, choices=target.MODES,
                        help="prova: PROVA_DATABASE_URL şeması silinip baştan kurulur; gercek: DATABASE_URL")
    parser.add_argument("--kaynak-ayar", required=True, type=Path, metavar="DB_PY",
                        help="orkestrai'nin api/db.py dosyası (çalıştırılmaz, DB_CONFIG okunur)")
    parser.add_argument("--yeniden", action="store_true",
                        help="gerçek taşıma daha önce yapılmışsa yeniden çalıştırır: taşınmış kayıtlarda Peyrev'de "
                             "yapılan değişiklikler kaynağın değerine döner (" + target.REVERTED + ")")
    args = parser.parse_args(argv)
    try:
        settings = read_settings(args.kaynak_ayar, environ)
        url = target_url(args.hedef, environ)
        tables = source_factory(settings).read()
        outcome = target.run(url, args.hedef, tables, clock() if clock else datetime.now(UTC), again=args.yeniden)
    except (SourceError, target.TargetError) as error:
        print(f"Taşıma yapılmadı: {error}", file=out)
        return 1
    except Exception as error:  # noqa: BLE001 - an exception text may carry row values: only its type and place
        frame = traceback.extract_tb(error.__traceback__)[-1]
        print(f"Taşıma yapılmadı: beklenmeyen hata {type(error).__name__} ({Path(frame.filename).name}:{frame.lineno}).",
              file=out)
        return 1
    out.write(report.render(outcome))
    return 0 if outcome.ok else 1


if __name__ == "__main__":  # pragma: no cover - deploy/tasima.sh çalıştırır
    sys.exit(main())
