#!/usr/bin/env bash
# deploy/destek-eposta.sh — destek bildirimlerini info@peyrev.com kutusuna bağlar (Sunucu işleri → destek-eposta).
# 1) CyberPanel'de info@peyrev.com kutusu yoksa açılır; şifresi burada üretilir. Kutu varsa ve destek.env onu zaten
#    kullanıyorsa şifreye dokunulmaz; varsa ama destek.env'de değilse şifre yenilenir (kutuyu bu iş açar, başkası kullanmaz).
# 2) Postfix'in SNI haritasına peyrev.com sertifikası eklenir (465 portunda peyrev.com adına doğrulanabilir TLS); harita
#    denetimi geçmezse eski harita geri yüklenir.
# 3) /etc/peyrev/destek.env uygulamanın kendi aracıyla yazılır ve deneme bildirimi gönderilir; kutuya ulaştığı denetlenir.
# Şifre komut satırına yalnız CyberPanel aracına verilirken girer; günlüğe yazılmaz. Sahibin e-posta istemcisine girebilmesi
# için şifreli rapora "KUTU_SIFRESI:" satırıyla yazılır (rapor yalnız sahibin anahtarıyla açılır).

set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8
DOMAIN=peyrev.com
BOX=info
ADDRESS=$BOX@$DOMAIN
REL=$(readlink -f /opt/peyrev/current)
MAP=/etc/postfix/vmail_ssl.map
CERT=/etc/letsencrypt/live/$DOMAIN
TRANSFER=/var/tmp/peyrev-aktarim
genel() { printf 'GENEL: %s\n' "$*"; }
sql() { mysql --defaults-file=/root/.my.cnf -N -B cyberpanel -e "$1"; }
code() { curl -sk -o /dev/null -w '%{http_code}' --max-time 10 --resolve "$1:443:127.0.0.1" "https://$1/" </dev/null || true; }
BEFORE="peyrev=$(code peyrev.com) hmtgross=$(code hmtgross.com) mkticaret=$(code mkticaret.com)"
echo "siteler: $BEFORE"

current_user=$(grep -E '^DESTEK_SMTP_USER=' /etc/peyrev/destek.env 2>/dev/null | cut -d= -f2- | tr -d "\"'" || true)
exists=$(sql "SELECT COUNT(*) FROM e_users WHERE email = '$ADDRESS'")
if [ "$exists" != 0 ] && [ "$current_user" = "$ADDRESS" ]; then
  echo "Kutu var ve destek.env onu kullanıyor; şifreye dokunulmadı."
  PW=$(grep -E '^DESTEK_SMTP_PASSWORD=' /etc/peyrev/destek.env | cut -d= -f2-)
else
  # Letters and digits only: the CyberPanel tool takes the password as an argument (a leading - would be an option).
  PW=$(python3 -c 'import secrets, string; print("".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32)))')
  if [ "$exists" = 0 ]; then
    cyberpanel createEmail --domainName "$DOMAIN" --userName "$BOX" --password="$PW" 2>&1 | tail -2 | sed 's/^/  /'
  else
    cyberpanel changeEmailPassword --email "$ADDRESS" --password="$PW" 2>&1 | tail -2 | sed 's/^/  /'
  fi
fi
[ "$(sql "SELECT COUNT(*) FROM e_users WHERE email = '$ADDRESS'")" = 1 ] || { echo "Kutu açılamadı."; genel "kutu açılamadı"; exit 1; }
echo "kutu: $ADDRESS hazır"

echo "-- Postfix SNI"
if [ -f "$CERT/fullchain.pem" ] && ! grep -qE "^$DOMAIN " "$MAP"; then
  cp -a "$MAP" "$MAP.peyrev-yedek"
  printf '%s %s %s\n' "$DOMAIN" "$CERT/privkey.pem" "$CERT/fullchain.pem" >> "$MAP"
  if postmap -F "hash:$MAP" 2>/dev/null && postfix check 2>/dev/null; then
    systemctl reload postfix && echo "  peyrev.com sertifikası eklendi, postfix yeniden yüklendi"
  else
    cp -a "$MAP.peyrev-yedek" "$MAP"; postmap -F "hash:$MAP" 2>/dev/null
    echo "  harita denetimi geçmedi; eski harita geri yüklendi"; genel "postfix haritası değiştirilemedi"; exit 1
  fi
  rm -f "$MAP.peyrev-yedek"
else
  echo "  değişiklik gerekmedi"
fi
python3 - <<PY
import socket, ssl
context = ssl.create_default_context()
with socket.create_connection(("$DOMAIN", 465), timeout=15) as raw:
    with context.wrap_socket(raw, server_hostname="$DOMAIN") as tls:
        print("  465 TLS doğrulandı:", tls.version())
PY

echo "-- destek.env ve deneme bildirimi"
install -d -m 0700 "$TRANSFER"
INPUT=$TRANSFER/destek-eposta.env
trap 'rm -f -- "$INPUT"' EXIT
umask 077
{
  grep -E '^DESTEK_TELEGRAM_(TOKEN|CHAT)=' /etc/peyrev/destek.env 2>/dev/null || true
  printf 'DESTEK_SMTP_HOST=%s\nDESTEK_SMTP_PORT=465\nDESTEK_SMTP_USER=%s\nDESTEK_SMTP_PASSWORD=%s\n' "$DOMAIN" "$ADDRESS" "$PW"
  printf 'DESTEK_EPOSTA_GONDEREN=%s\nDESTEK_EPOSTA_ALICI=%s\n' "$ADDRESS" "$ADDRESS"
} > "$INPUT"
MAILDIR=/home/vmail/$DOMAIN/$BOX
count() { find "$MAILDIR" -type f \( -path "*/new/*" -o -path "*/cur/*" \) 2>/dev/null | wc -l; }
before=$(count)
status=0
result=$(PYTHONPATH="$REL/src" "$REL/venv/bin/python" -B - "$INPUT" <<'PY'
import os, runpy, sys
for line in open(sys.argv[1], encoding="utf-8"):
    key, sep, value = line.rstrip("\n").partition("=")
    if sep and key.startswith("DESTEK_"):
        os.environ[key] = value
sys.argv = ["peyrev.support", "/etc/peyrev/destek.env"]
runpy.run_module("peyrev.support", run_name="__main__")
PY
) || status=$?
echo "  $(printf '%s' "$result" | tail -1)"
ls -l /etc/peyrev/destek.env | awk '{print "  " $1, $3":"$4, $NF}'
for i in 1 2 3 4 5 6 7 8 9 10; do
  after=$(count)
  [ "$after" -gt "$before" ] && break; sleep 2
done
echo "  kutudaki ileti: $before → $after"
printf 'KUTU_SIFRESI: %s\n' "$PW"
AFTER="peyrev=$(code peyrev.com) hmtgross=$(code hmtgross.com) mkticaret=$(code mkticaret.com)"
echo "siteler: $AFTER"
[ "$AFTER" = "$BEFORE" ] || { genel "site durumları değişti"; exit 1; }
if [ "$status" = 0 ] && [ "$after" -gt "$before" ]; then
  genel "destek bildirimleri info@peyrev.com kutusuna bağlandı; deneme iletisi ulaştı"
else
  genel "destek e-postası kuruldu ama deneme iletisi doğrulanamadı; rapora bakın"; exit 1
fi
