#!/usr/bin/env bash
# deploy/yonetici-sifre.sh — panele girebilen tek hesabın şifresini unutan sahip için (Sunucu işleri → yonetici-sifre):
# giriş adı admin@peyrev.com, adı "Peyrev Yönetici" olur ve yeni şifre sunucuda üretilir (peyrev.admin.kurtarma).
# Birden fazla etkin yönetici varsa iş reddedilir. Giriş adı ve şifre yalnız şifreli rapora yazılır; günlükte görünmez.

set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8
REL=$(readlink -f /opt/peyrev/current)
runuser -u peyrev -- env PYTHONPATH="$REL/src" PEYREV_SECRETS_DIR=/etc/peyrev "$REL/venv/bin/python" - <<'PY'
from peyrev.config import Settings
from peyrev.core import Core
from peyrev.db import Database
from peyrev.keys import Keys
from peyrev.admin import kurtarma

settings = Settings.from_environment()
core = Core(settings=settings, db=Database(settings.database_url, max_size=1), keys=Keys(settings.secrets_dir, False))
result = kurtarma.recover(core, "admin@peyrev.com", "Peyrev Yönetici")
core.db.close()
print(f"YONETICI_GIRISI: {result['giris']}")
print(f"YONETICI_SIFRESI: {result['sifre']}")
print(f"GENEL: yönetici #{result['kullanici_id']} için yeni giriş adı ve şifre üretildi (şifreli raporda)")
PY
