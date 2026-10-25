#!/usr/bin/env bash
# deploy/destek-ayarlari.sh — destek bildirim ayarlarını (Telegram, e-posta) /etc/peyrev/destek.env dosyasına yazar ve deneme
# bildirimi gönderir (Destek bildirim ayarları iş akışı). Değerler Secrets'tan gelir ve sunucuya 0600 aktarım dosyasıyla ulaşır;
# komut satırına, ortam listesine ve çıktıya girmez. Çıktıda yalnız kanal durumları bulunur.

set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8
INPUT=/var/tmp/peyrev-aktarim/destek.env.yeni
REL=$(readlink -f /opt/peyrev/current)
trap 'rm -f -- "$INPUT"' EXIT
[ -f "$INPUT" ] || { echo "Aktarım dosyası yok."; exit 1; }
status=0
result=$(PYTHONPATH="$REL/src" "$REL/venv/bin/python" -B - "$INPUT" <<'PY'
import os, runpy, sys
# KEY=value lines only, no shell parsing: passwords may contain any character.
for line in open(sys.argv[1], encoding="utf-8"):
    key, sep, value = line.rstrip("\n").partition("=")
    if sep and key.startswith("DESTEK_"):
        os.environ[key] = value
sys.argv = ["peyrev.support", "/etc/peyrev/destek.env"]
runpy.run_module("peyrev.support", run_name="__main__")
PY
) || status=$?
printf 'GENEL: destek bildirimleri: %s\n' "$(printf '%s' "$result" | tail -1)"
ls -l /etc/peyrev/destek.env 2>/dev/null | awk '{print "  " $1, $3":"$4, $NF}'
exit "$status"
