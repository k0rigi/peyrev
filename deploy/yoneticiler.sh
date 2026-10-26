#!/usr/bin/env bash
# deploy/yoneticiler.sh — yönetim paneline girebilen hesapları SALT OKUNUR listeler (Sunucu işleri → yoneticiler):
# giriş adı, durum, iki adımlı doğrulama açık mı, şifre özetinin biçimi (taşınan ya da Peyrev'de belirlenen) ve son giriş
# zamanları. Şifre, özet, kod ve gizli anahtar yazdırılmaz; çıktı yalnız şifreli rapora gider.

set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8
REL=$(readlink -f /opt/peyrev/current)
runuser -u peyrev -- env PYTHONPATH="$REL/src" PEYREV_SECRETS_DIR=/etc/peyrev "$REL/venv/bin/python" - <<'PY'
from peyrev.config import Settings
from peyrev.db import Database

database = Database(Settings.from_environment().database_url, max_size=1)
with database.transaction() as cur:
    cur.execute("SET TRANSACTION READ ONLY")
    cur.execute("""
        SELECT k.id, k.giris, k.ad_soyad, k.durum, k.kaynak, k.totp_gizli IS NOT NULL AS iki_adim,
               split_part(k.sifre_hash, '$', 1) AS ozet,
               (SELECT max(o.zaman) FROM yonetim_olaylari o WHERE o.yonetici_id = k.id AND o.tur = 'yonetim_girisi') AS panel,
               (SELECT max(s.son_gorulme) FROM oturumlar s WHERE s.kullanici_id = k.id) AS studio
        FROM kullanicilar k WHERE k.rol = 'yonetici' ORDER BY k.id""")
    rows = cur.fetchall()
database.close()
for row in rows:
    print(f"  #{row['id']} giriş adı: {row['giris']} | ad: {row['ad_soyad']} | durum: {row['durum']} | kaynak: {row['kaynak']}"
          f" | iki adımlı doğrulama: {'açık' if row['iki_adim'] else 'kapalı'} | şifre özeti: {row['ozet'] or 'yok'}"
          f" | son panel girişi: {row['panel'] or '—'} | son Studio: {row['studio'] or '—'}")
print(f"GENEL: {len(rows)} yönetici hesabı listelendi (ayrıntılar şifreli raporda)")
PY
