#!/usr/bin/env bash
# deploy/destek-ayarlari.sh — destek bildirim ayarlarını (Telegram, e-posta) /etc/peyrev/destek.env dosyasına yazar ve deneme
# bildirimi gönderir (Destek bildirim ayarları iş akışı). Değerler Secrets'tan gelir ve sunucuya 0600 aktarım dosyasıyla ulaşır;
# komut satırına, ortam listesine ve çıktıya girmez. Secrets'ta olmayan anahtarlar mevcut dosyadan korunur (--koru): ör.
# yalnız DESTEK_TELEGRAM_TOKEN girilirse destek-eposta işinin kurduğu e-posta ayarları kalır. Çıktıda yalnız kanal durumları.

set -Eeuo pipefail
INPUT=/var/tmp/peyrev-aktarim/destek.env.yeni
# The transfer file carries the secrets: it is removed on every exit, including a failure in the next lines.
trap 'rm -f -- "$INPUT"' EXIT
export LC_ALL=C.UTF-8 LANG=C.UTF-8
REL=$(readlink -f /opt/peyrev/current)
[ -f "$INPUT" ] || { echo "Aktarım dosyası yok."; exit 1; }
status=0
result=$(PYTHONPATH="$REL/src" "$REL/venv/bin/python" -B - "$INPUT" <<'PY'
import os, runpy, sys
# KEY=value lines only, no shell parsing: passwords may contain any character.
for line in open(sys.argv[1], encoding="utf-8"):
    key, sep, value = line.rstrip("\n").partition("=")
    if sep and key.startswith("DESTEK_"):
        os.environ[key] = value
sys.argv = ["peyrev.support", "--koru", "/etc/peyrev/destek.env"]
runpy.run_module("peyrev.support", run_name="__main__")
PY
) || status=$?
printf 'GENEL: destek bildirimleri: %s\n' "$(printf '%s' "$result" | tail -1)"
ls -l /etc/peyrev/destek.env 2>/dev/null | awk '{print "  " $1, $3":"$4, $NF}'
exit "$status"
