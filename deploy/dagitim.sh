#!/usr/bin/env bash
# deploy/dagitim.sh — Peyrev uygulamasını sunucuya dağıtır (Dağıtım iş akışı; main'e her push'ta testlerden sonra).
#
# Girdi (ortam): SURUM (commit SHA), ARSIV (aktarım klasöründeki tar.gz), ARSIV_SHA256.
# Her sürüm kendi klasörüne (/opt/peyrev/releases/<sha>) ve kendi sanal ortamına kurulur; bağımlılıklar
# deploy/requirements.lock'tan özetleri denetlenerek gelir. Eksik sunucu sırları bir kez üretilir (asla üzerine yazılmaz),
# geçişler peyrev veritabanına uygulanır, current bağlantısı atomik değişir ve servis yeniden başlar.
# /api/saglik 200 dönmezse önceki sürüme geri dönülür. Statik site (web/) public_html'e eşitlenir; indir/ (Studio paketleri)
# ve .well-known/ korunur. Sonunda orkestrai.net ve peyrev.com denetlenir.

set -Eeuo pipefail
umask 022
export LC_ALL=C.UTF-8 LANG=C.UTF-8
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

ROOT=/opt/peyrev
SECRETS=/etc/peyrev
SITE=peyrev.com
PORT=5101
WEB=/home/$SITE/public_html
TRANSFER=/var/tmp/peyrev-aktarim
STEP=başlangıç

genel() { printf 'GENEL: %s\n' "$*"; }
adim() { STEP=$1; printf '\n========== %s ==========\n' "$1"; }
trap 'rc=$?; printf "HATA: %s adımında, satır %s (çıkış %s)\n" "$STEP" "$LINENO" "$rc"; genel "dağıtım başarısız: $STEP"' ERR
code() { curl -sk -o /dev/null -w '%{http_code}' --max-time 10 --resolve "$1:443:127.0.0.1" "https://$1${2:-/}" </dev/null || true; }
local_health() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/api/saglik" </dev/null || true; }

adim "Arşiv"
[[ "${SURUM:-}" =~ ^[0-9a-f]{40}$ ]] || { echo "SURUM geçersiz"; exit 1; }
[ "${ARSIV:-}" = "$TRANSFER/peyrev-$SURUM.tar.gz" ] || { echo "ARSIV yolu beklenmiyor"; exit 1; }
[[ "${ARSIV_SHA256:-}" =~ ^[0-9a-f]{64}$ ]] || { echo "ARSIV_SHA256 geçersiz"; exit 1; }
printf '%s  %s\n' "$ARSIV_SHA256" "$ARSIV" | sha256sum -c - | sed 's/^/  /'
[ -x "$ROOT/bin/uv" ] && [ -f /etc/systemd/system/peyrev.service ] || { echo "Önce kurulum çalıştırılmalı."; exit 1; }
[ "$(code orkestrai.net)" = 200 ] || { echo "orkestrai.net sağlıklı değil; dağıtım yapılmadı."; genel "orkestrai.net sağlıklı değil; dağıtım yapılmadı"; exit 1; }

REL=$ROOT/releases/$SURUM
adim "Sürüm klasörü ve sanal ortam"
if [ ! -f "$REL/.tamam" ]; then
  rm -rf -- "$REL"
  install -d -o root -g root -m 0755 "$REL"
  tar -xzf "$ARSIV" -C "$REL" --no-same-owner --no-same-permissions
  chown -R root:root "$REL"
  chmod -R u=rwX,go=rX "$REL"
  export UV_PYTHON_INSTALL_DIR=$ROOT/python UV_NO_PROGRESS=1
  PY=$("$ROOT/bin/uv" python find 3.12)
  # The virtual environment is created at its final path: its scripts carry absolute paths.
  "$ROOT/bin/uv" venv --quiet --python "$PY" "$REL/venv"
  "$ROOT/bin/uv" pip install --quiet --python "$REL/venv/bin/python" --require-hashes -r "$REL/deploy/requirements.lock"
  chmod -R u=rwX,go=rX "$REL/venv"
  touch "$REL/.tamam"
fi
"$REL/venv/bin/python" -c 'import sys, flask, psycopg, jwt, cryptography; print("  python", sys.version.split()[0], "flask", flask.__version__, "psycopg", psycopg.__version__)'

adim "Sırlar"
if [ ! -f "$SECRETS/jwt_secret" ] || [ ! -f "$SECRETS/lisans_ed25519" ]; then
  PYTHONPATH=$REL/src "$REL/venv/bin/python" -m peyrev.keys "$SECRETS"
fi
chown root:peyrev "$SECRETS/jwt_secret" "$SECRETS/lisans_ed25519"
chmod 0640 "$SECRETS/jwt_secret" "$SECRETS/lisans_ed25519"
ls -l "$SECRETS" | sed 's/^/  /'

adim "Geçişler"
# Database URLs stay in the environment of the peyrev account's process; they never reach a command line.
runuser -u peyrev -- env PYTHONPATH="$REL/src" PEYREV_SECRETS_DIR="$SECRETS" "$REL/venv/bin/python" -m peyrev.migrate

adim "Geçiş ve yeniden başlatma"
PREV=$(readlink -f "$ROOT/current" 2>/dev/null || true)
ln -sfn "$REL" "$ROOT/current.yeni"
mv -Tf "$ROOT/current.yeni" "$ROOT/current"
systemctl restart peyrev.service
ok=
for _ in $(seq 1 40); do [ "$(local_health)" = 200 ] && { ok=1; break; }; sleep 1; done
if [ -z "$ok" ]; then
  journalctl -u peyrev.service -n 60 --no-pager | sed 's/^/  /' || true
  if [ -n "$PREV" ] && [ -d "$PREV" ] && [ "$PREV" != "$REL" ]; then
    ln -sfn "$PREV" "$ROOT/current.yeni"; mv -Tf "$ROOT/current.yeni" "$ROOT/current"; systemctl restart peyrev.service
    echo "Yeni sürüm sağlık denetimini geçemedi; önceki sürüme dönüldü."; genel "yeni sürüm açılmadı; önceki sürüme dönüldü"
  fi
  exit 1
fi
printf '  servis: %s, sağlık: %s\n' "$(systemctl is-active peyrev.service)" "$(local_health)"

adim "Statik site"
SITE_USER=$(stat -c %U "/home/$SITE")
rsync -a --delete --exclude '/indir/' --exclude '/.well-known/' --chown="$SITE_USER:$SITE_USER" --chmod=D755,F644 \
  "$REL/web/" "$WEB/"
ls -la "$WEB" | sed 's/^/  /'

adim "Temizlik"
rm -f -- "$ARSIV"
current=$(readlink -f "$ROOT/current")
keep=0
for d in $(ls -1dt "$ROOT"/releases/*/ 2>/dev/null); do
  d=${d%/}
  keep=$((keep + 1))
  if [ "$keep" -gt 3 ] && [ "$d" != "$current" ] && [ "$d" != "${PREV:-}" ]; then rm -rf -- "$d"; echo "  silindi: ${d##*/}"; fi
done

adim "Denetim"
# The nightly backup must keep running; its newest files are listed here (names and sizes only).
ls -lt /var/backups/peyrev 2>/dev/null | sed -n 2,5p | awk '{print "  yedek:", $5, $6, $7, $8, $9}'
s1=$(code "$SITE" /api/saglik); s2=$(code "$SITE"); o1=$(code orkestrai.net); o2=$(code orkestrai.net /api/rpa/lisans/anahtar)
printf '  peyrev.com/api/saglik=%s peyrev.com/=%s orkestrai.net/=%s anahtar=%s\n' "$s1" "$s2" "$o1" "$o2"
genel "peyrev.com/api/saglik -> $s1, peyrev.com/ -> $s2"
genel "orkestrai.net / -> $o1, /api/rpa/lisans/anahtar -> $o2"
[ "$s1" = 200 ] && [ "$o1" = 200 ] && [ "$o2" = 200 ] || exit 1
genel "dağıtım tamamlandı: ${SURUM:0:7}"
