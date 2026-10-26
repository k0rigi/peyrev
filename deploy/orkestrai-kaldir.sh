#!/usr/bin/env bash
# deploy/orkestrai-kaldir.sh — orkestrai.net'i (sevkiyat dahil) sunucudan tamamen kaldırır (Sunucu işleri →
# orkestrai-kaldir-prova / orkestrai-kaldir). Sahibin 2026-10-26 kararı: site, veritabanı, e-posta, DNS, sertifikalar,
# servis ve model dosyaları yedeksiz silinir; sunucu ve diğer siteler (peyrev.com, hmtgross.com, mkticaret.com) kalır.
# KIP=prova  hiçbir şey değiştirmez; silinecekleri ve değişecek yapılandırma satırlarını gösterir.
# KIP=gercek önce ve sonra sitelerin durum kodlarını karşılaştırır. Değişen yapılandırma dosyalarının (OLS, Dovecot,
# Postfix, OpenDKIM) kopyası /root/orkestrai-kaldirma-<zaman> altına alınır; doğrulama başarısızsa geri yüklenir.

set -uo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH
KIP=${KIP:-prova}
D=orkestrai.net
DB=orke_OrkestraDB
DBUSER=orke_Fatih
LSWS=/usr/local/lsws
OLS_CONF=$LSWS/conf/httpd_config.conf
SITES="peyrev.com hmtgross.com mkticaret.com"
section() { printf '\n========== %s ==========\n' "$*"; }
indent() { sed 's/^/    /'; }
genel() { printf 'GENEL: %s\n' "$*"; }
sql() { mysql --defaults-file=/root/.my.cnf --default-character-set=utf8mb4 -N -B -r "$@"; }
code() { curl -sk -o /dev/null -w '%{http_code}' --max-time 10 --resolve "$1:443:127.0.0.1" "https://$1/" </dev/null || true; }
snapshot() { for s in $SITES; do printf '%s=%s ' "$s" "$(code "$s")"; done; printf 'peyrev-saglik=%s' \
  "$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 --resolve peyrev.com:443:127.0.0.1 https://peyrev.com/api/saglik </dev/null)"; }

section "Siteler (başlangıç)"
BEFORE=$(snapshot); echo "  $BEFORE"
case "$BEFORE" in *"peyrev.com=200"*"peyrev-saglik=200"*) ;; *) echo "peyrev.com sağlıklı değil; hiçbir şey yapılmadı."; genel "peyrev.com sağlıklı değil; işlem yapılmadı"; exit 1 ;; esac

section "Kaldırılacaklar"
echo "  -- servis"; systemctl list-unit-files --no-pager 2>/dev/null | grep -i orkestrai | indent
echo "  -- 8888 portunu dinleyen süreç (yalnız program ve klasör)"
for pid in $(ss -ltnpH 'sport = :8888' 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u); do
  printf '    pid %s exe=%s cwd=%s\n' "$pid" "$(readlink /proc/$pid/exe)" "$(readlink /proc/$pid/cwd)"
  systemctl status "$pid" --no-pager 2>/dev/null | head -2 | indent
done
echo "  -- CyberPanel kayıtları"
for q in "SELECT id, domain FROM websiteFunctions_websites WHERE domain LIKE '%orkestrai%'" \
         "SELECT id, domain FROM websiteFunctions_childdomains WHERE domain LIKE '%orkestrai%'" \
         "SELECT id, dbName FROM databases_databases WHERE dbName LIKE 'orke%'" \
         "SELECT domain FROM e_domains WHERE domain LIKE '%orkestrai%'" \
         "SELECT COUNT(*) FROM e_users WHERE emailOwner_id LIKE '%orkestrai%'" \
         "SELECT id, name FROM domains WHERE name LIKE '%orkestrai%'" \
         "SELECT COUNT(*) FROM records WHERE name LIKE '%orkestrai%'"; do
  printf '    %s\n' "$q"; sql cyberpanel -e "$q" 2>&1 | indent | indent
done
echo "  -- MariaDB"; sql -e "SHOW DATABASES LIKE 'orke%'" | indent; sql -e "SELECT User, Host FROM mysql.user WHERE User LIKE 'orke%'" | indent
echo "  -- OLS: orkestrai geçen satırlar"; grep -n -i orkestrai "$OLS_CONF" | indent
echo "  -- OLS: virtualHost blokları"; awk '/^virtualHost .*orkestrai/{p=1} p{print NR": "$0} p&&/^}/{p=0}' "$OLS_CONF" | indent
echo "  -- Dovecot"; grep -n -i -B2 -A8 orkestrai /etc/dovecot/dovecot.conf | indent
echo "  -- Postfix"; grep -n -i orkestrai /etc/postfix/vmail_ssl.map | indent; postconf -h tls_server_sni_maps 2>/dev/null | indent
grep -rlI -i orkestrai /etc/postfix 2>/dev/null | indent
echo "  -- OpenDKIM"; grep -n -i orkestrai /etc/opendkim/KeyTable /etc/opendkim/SigningTable /etc/opendkim/TrustedHosts | indent
echo "  -- acme.sh"; [ -x /root/.acme.sh/acme.sh ] && /root/.acme.sh/acme.sh --list 2>/dev/null | grep -i -E 'Main_Domain|orkestrai' | indent
crontab -l 2>/dev/null | grep -i -E 'acme|certbot' | indent
echo "  -- cyberpanel CLI"; command -v cyberpanel | indent
echo "  -- dosya ve klasörler"
for p in /home/$D /home/vmail/$D /etc/orkestrai /opt/orkestrai-ollama /var/lib/orkestrai-ollama /var/lib/orkestrai-llm-management \
         /etc/letsencrypt/live/$D /etc/letsencrypt/live/mail.$D /etc/letsencrypt/archive/$D /etc/letsencrypt/archive/mail.$D \
         /etc/letsencrypt/renewal/$D.conf /etc/letsencrypt/renewal/mail.$D.conf /etc/letsencrypt/accounts/$D.key \
         /etc/letsencrypt/accounts/mail.$D.key /root/.acme.sh/${D}_ecc /root/.acme.sh/mail.${D}_ecc /etc/opendkim/keys/$D \
         $LSWS/conf/vhosts/$D $LSWS/conf/vhosts/mail.$D $LSWS/cachedata/$D $LSWS/cachedata/mail.$D \
         /home/cyberpanel/$D.bwmeta /home/cyberpanel/git/$D /etc/systemd/system/orkestrai_api.service; do
  [ -e "$p" ] && printf '    %s  %s\n' "$(du -sh "$p" 2>/dev/null | cut -f1)" "$p"
done
echo "  -- pm2"; if [ -S /root/.pm2/rpc.sock ]; then pm2 jlist 2>/dev/null | python3 -c 'import json,sys; print(len(json.load(sys.stdin)), "süreç")' | indent; else echo "    pm2 çalışmıyor"; fi

if [ "$KIP" != gercek ]; then
  genel "prova tamamlandı; hiçbir şey değiştirilmedi"
  exit 0
fi
echo "Gerçek kaldırma bu sürümde etkin değil." ; genel "gerçek kaldırma henüz etkin değil"; exit 1
