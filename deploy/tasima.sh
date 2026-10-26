#!/usr/bin/env bash
# deploy/tasima.sh — orkestrai (MariaDB) → Peyrev (PostgreSQL) veri taşıması (Sunucu işleri → tasima-prova / tasima-gercek).
#
# KIP=prova   peyrev_prova sıfırdan kurulur ve doldurulur; canlı peyrev veritabanına dokunulmaz.
# KIP=gercek  peyrev veritabanına aktarılır (önce gece yedeği alınır); yalnız orkestrai'de bakım bayrağı açıkken çalışır.
#             Taşıma daha önce yapılmışsa yalnız YENIDEN=1 ile (tasima-gercek-yeniden) yeniden çalışır.
# Kaynak yalnız okunur (orkestrai'nin kendi bağlantı ayarıyla, tek tutarlı okuma). Her tablo için kaynak ve hedef sayıları
# karşılaştırılır; uyuşmazlık varsa iş başarısız olur. Rapor kişisel veri içermez ve yalnız şifreli yapıta gider.

set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8
genel() { printf 'GENEL: %s\n' "$*"; }
REL=$(readlink -f /opt/peyrev/current)
SOURCE=/home/orkestrai.net/public_html/api/db.py
case "${KIP:-}" in prova|gercek) ;; *) echo "KIP prova ya da gercek olmalı"; exit 1 ;; esac
again=()
case "${YENIDEN:-}" in "") ;; 1) [ "$KIP" = gercek ] && again=(--yeniden) ;; *) echo "YENIDEN yalnız 1 olabilir"; exit 1 ;; esac
[ -r "$SOURCE" ] || { echo "Kaynak ayarı okunamadı."; exit 1; }
if [ "$KIP" = gercek ]; then
  [ -e /etc/orkestrai/rpa-bakim ] || { echo "Gerçek taşıma için önce bakım açılmalı."; genel "bakım kapalı; gerçek taşıma yapılmadı"; exit 1; }
  /opt/peyrev/bin/yedek.sh
  echo "  taşıma öncesi yedek: $(ls -1t /var/backups/peyrev/peyrev-*.dump | head -1 | xargs -n1 basename)"
fi
status=0
runuser -u peyrev -- env PYTHONPATH="$REL/src" PEYREV_SECRETS_DIR=/etc/peyrev "$REL/venv/bin/python" -m peyrev.tasima \
  --hedef "$KIP" --kaynak-ayar "$SOURCE" "${again[@]}" || status=$?
if [ "$status" = 0 ]; then genel "taşıma ($KIP) tamamlandı; bütün tablolarda sayılar uyuşuyor"; else genel "taşıma ($KIP) başarısız (çıkış $status)"; fi
exit "$status"
