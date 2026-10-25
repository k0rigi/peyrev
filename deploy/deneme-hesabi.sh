#!/usr/bin/env bash
# deploy/deneme-hesabi.sh — taşıma sonrası uçtan uca denemede kullanılan 'peyrev.deneme' hesabını kapatır
# (Sunucu işleri → deneme-hesabi-kapat): hesap pasif olur, açık Studio oturumları YONETICI nedeniyle kapanır ve olay
# yonetim_olaylari'na yazılır. Başka hiçbir hesaba dokunmaz; kişisel veri yazdırmaz.

set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8
REL=$(readlink -f /opt/peyrev/current)
runuser -u peyrev -- env PYTHONPATH="$REL/src" PEYREV_SECRETS_DIR=/etc/peyrev "$REL/venv/bin/python" - <<'PY'
import json

from peyrev.config import Settings
from peyrev.db import Database

database = Database(Settings.from_environment().database_url, max_size=1)
with database.transaction() as cur:
    cur.execute("UPDATE kullanicilar SET durum = 'pasif', guncelleme = now() WHERE lower(giris) = 'peyrev.deneme' "
                "RETURNING id")
    ids = [row["id"] for row in cur.fetchall()]
    closed = 0
    if ids:
        cur.execute("UPDATE oturumlar SET durum = 'kapandi', kapanma_nedeni = 'YONETICI' "
                    "WHERE kullanici_id = ANY(%s) AND durum = 'aktif'", (ids,))
        closed = cur.rowcount
        for user_id in ids:
            cur.execute("INSERT INTO yonetim_olaylari (yonetici_id, tur, hedef, ayrinti) VALUES (NULL, 'durum_degisti', %s, "
                        "%s::jsonb)", (f"kullanici:{user_id}", json.dumps({"durum": "pasif", "neden": "gecis_denemesi_bitti"})))
database.close()
print(f"GENEL: deneme hesabı kapatıldı ({len(ids)} hesap, {closed} oturum)")
PY
