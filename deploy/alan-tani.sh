#!/usr/bin/env bash
# deploy/alan-tani.sh — Dijital İkiz alt alan adları (ikiz.peyrev.com, <firma>.peyrev.com) için SALT OKUNUR inceleme
# (Sunucu işleri → alan-tani): peyrev.com sanal sunucusu, dinleyici eşlemeleri, sertifika adları ve yenileme düzeni,
# alt alan adıyla gelen isteğin hangi sanal sunucuya düştüğü. Hiçbir ayar ve servis değişmez; çıktı şifreli rapordadır.

set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8
LSWS=/usr/local/lsws
SITE=peyrev.com
genel() { printf 'GENEL: %s\n' "$*"; }

echo "== peyrev.com vhost.conf"
cat "$LSWS/conf/vhosts/$SITE/vhost.conf"
echo; echo "== httpd_config.conf: dinleyiciler ve eşlemeler"
awk '/^listener /,/^}/' "$LSWS/conf/httpd_config.conf"
echo; echo "== httpd_config.conf: peyrev.com sanal sunucu tanımı"
awk '/^virtualhost peyrev.com/,/^}/' "$LSWS/conf/httpd_config.conf"
echo; echo "== sertifika"
for c in /etc/letsencrypt/live/$SITE/fullchain.pem; do
  openssl x509 -in "$c" -noout -subject -enddate -ext subjectAltName 2>/dev/null || true
done
ls -la /etc/letsencrypt/live/ 2>/dev/null | head -40
echo; echo "== acme.sh"
ls /root/.acme.sh/ 2>/dev/null | head -40
crontab -l 2>/dev/null | grep -i acme || true
/root/.acme.sh/acme.sh --list 2>/dev/null | head -20 || true
echo; echo "== istekler (yerel)"
for h in peyrev.com ikiz.peyrev.com deneme-alan.peyrev.com; do
  printf '%s http: ' "$h"; curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' --max-time 10 -H "Host: $h" http://127.0.0.1/ </dev/null || true
  printf '%s https: ' "$h"; curl -sk -o /dev/null -w '%{http_code} %{redirect_url}\n' --max-time 10 --resolve "$h:443:127.0.0.1" "https://$h/" </dev/null || true
  printf '%s acme: ' "$h"; curl -s -o /dev/null -w '%{http_code}\n' --max-time 10 -H "Host: $h" http://127.0.0.1/.well-known/acme-challenge/yok </dev/null || true
done
genel "alan-tani tamamlandı"
