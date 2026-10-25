#!/usr/bin/env bash
# deploy/bakim.sh — Peyrev taşıması sırasında orkestrai.net'teki RPA veri uçlarını kısa süre bakıma alır ya da açar
# (Sunucu işleri → bakim-ac / bakim-kapat). KIP=ac bayrağı yazar, KIP=kapat siler.
# Bayrak varken orkestrai yalnız taşınan veriyi yazan uçlarda (hesap, başvuru, destek, yönetim, ajan merkezi) 503 BAKIM döner;
# lisans girişi, yenileme ve anahtar ucu açık kalır. Studio 503'ü "ulaşılamadı" sayar ve en çok 60 dakika çalışmayı sürdürür.

set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8
FLAG=/etc/orkestrai/rpa-bakim
APP=/home/orkestrai.net/public_html/api/app.py
genel() { printf 'GENEL: %s\n' "$*"; }
code() { curl -sk -o /dev/null -w '%{http_code}' --max-time 10 --resolve "orkestrai.net:443:127.0.0.1" "https://orkestrai.net${1:-/}" </dev/null || true; }

grep -q 'def rpa_bakim_kontrolu' "$APP" || { echo "orkestrai'nin bakım kapısı dağıtılmamış."; genel "orkestrai bakım kapısı yok; değişiklik yapılmadı"; exit 1; }
case "${KIP:-}" in
  ac) install -d -m 0755 /etc/orkestrai; : > "$FLAG"; chmod 0644 "$FLAG" ;;
  kapat) rm -f -- "$FLAG" ;;
  *) echo "KIP ac ya da kapat olmalı"; exit 1 ;;
esac
sleep 1
home=$(code /); key=$(code /api/rpa/lisans/anahtar); office=$(code /api/studio/ekip/ofisim)
genel "bakım: $([ -e "$FLAG" ] && echo açık || echo kapalı); orkestrai.net / -> $home, anahtar -> $key, /api/studio/ekip/ofisim -> $office"
[ "$home" = 200 ] && [ "$key" = 200 ] || exit 1
if [ -e "$FLAG" ]; then [ "$office" = 503 ] || exit 1; else [ "$office" != 503 ] || exit 1; fi
