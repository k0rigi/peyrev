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

# Rows that point at a row: CyberPanel versions differ in which tables refer to e_domains and websites, so the
# references are read from information_schema and removed from the leaves up.
delete_rows() {   # delete_rows TABLE COLUMN VALUE
  local table=$1 column=$2 value=$3 ref_table ref_column referenced child
  while IFS=$'\t' read -r ref_table ref_column referenced; do
    [ -n "$ref_table" ] || continue
    for child in $(sql cyberpanel -e "SELECT \`$referenced\` FROM \`$table\` WHERE \`$column\` = '$value'"); do
      delete_rows "$ref_table" "$ref_column" "$child"
    done
  done < <(sql -e "SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE
                   WHERE TABLE_SCHEMA = 'cyberpanel' AND REFERENCED_TABLE_NAME = '$table'")
  local count
  count=$(sql cyberpanel -e "SELECT COUNT(*) FROM \`$table\` WHERE \`$column\` = '$value'")
  if [ "${count:-0}" != 0 ]; then
    sql cyberpanel -e "DELETE FROM \`$table\` WHERE \`$column\` = '$value'" && printf '    %s: %s satır\n' "$table" "$count"
  fi
}

if [ "$KIP" = son ]; then
  section "Son temizlik"
  echo "  -- CyberPanel kayıtları"
  delete_rows e_domains domain "$D"
  for id in $(sql cyberpanel -e "SELECT id FROM websiteFunctions_websites WHERE domain = '$D'"); do
    delete_rows websiteFunctions_websites id "$id"
  done
  for q in "websiteFunctions_websites WHERE domain LIKE '%orkestrai%'" "e_domains WHERE domain LIKE '%orkestrai%'" \
           "e_users WHERE email LIKE '%orkestrai%'"; do
    printf '  kalan %s: %s\n' "${q%% *}" "$(sql cyberpanel -e "SELECT COUNT(*) FROM $q" 2>&1)"
  done
  echo "  -- sistem kullanıcıları (ad ve ev klasörü)"
  grep -i orkestrai /etc/passwd | cut -d: -f1,6 | indent
  # The site's user and the earlier model service's user (studio-ollama, home under /var/lib/orkestrai-ollama).
  for user in $(awk -F: '$6 ~ /orkestrai/ {print $1}' /etc/passwd); do
    [ "$user" = root ] && continue
    userdel "$user" >/dev/null 2>&1 && echo "    silindi: $user"
  done
  # Mail that was queued before the domain went may have created the mailbox folder again.
  rm -rf "/home/vmail/$D"
  echo "  -- webmail önbelleği"
  RL=/usr/local/lscp/cyberpanel/rainloop/data/_data_/_default_
  rm -rf "$RL/storage/$D" "$RL/domains/$D.json" && echo "    orkestrai.net klasörü ve alan ayarı silindi"
  echo "  -- kalan izler (yalnız adlar)"
  grep -rlI -i orkestrai /etc /usr/local/lsws/conf /var/spool/cron 2>/dev/null | grep -v '/root/orkestrai-kaldirma-' | indent
  find / -xdev \( -path /proc -o -path /sys -o -path '/root/orkestrai-kaldirma-*' \) -prune -o -iname '*orkestrai*' -print 2>/dev/null | head -20 | indent
  section "Siteler (son)"
  AFTER=$(snapshot); echo "  $AFTER"
  [ "$AFTER" = "$BEFORE" ] || { genel "site durumları değişti"; exit 1; }
  genel "son temizlik tamamlandı: $AFTER"
  exit 0
fi

if [ "$KIP" != gercek ]; then
  genel "prova tamamlandı; hiçbir şey değiştirilmedi"
  exit 0
fi

BACKUP=/root/orkestrai-kaldirma-$(date +%Y%m%d-%H%M%S)
install -d -m 0700 "$BACKUP"
for f in "$OLS_CONF" /etc/dovecot/dovecot.conf /etc/postfix/vmail_ssl.map /etc/opendkim/KeyTable /etc/opendkim/SigningTable \
         /etc/opendkim/TrustedHosts; do
  [ -f "$f" ] && cp -a "$f" "$BACKUP/$(echo "$f" | tr / _)"
done
echo "  yapılandırma kopyası: $BACKUP"
SITE_USER=$(stat -c %U /home/$D 2>/dev/null || true)
FAILED=0
step() { printf '\n-- %s\n' "$*"; }
fail() { echo "  HATA: $*"; FAILED=1; }

step "Servis"
systemctl disable --now orkestrai_api.service 2>&1 | indent
rm -f /etc/systemd/system/orkestrai_api.service /etc/systemd/system/multi-user.target.wants/orkestrai_api.service
systemctl daemon-reload; systemctl reset-failed orkestrai_api.service 2>/dev/null
systemctl list-unit-files --no-pager | grep -qi orkestrai && fail "servis kaydı kaldı" || echo "  orkestrai_api kaldırıldı"

step "CyberPanel sitesi (site, alt alan, veritabanı, e-posta alanı, DNS bölgesi)"
cyberpanel deleteWebsite --domainName "$D" 2>&1 | tail -5 | indent

step "Kalanlar: veritabanı ve kullanıcıları"
sql -e "DROP DATABASE IF EXISTS \`$DB\`" || fail "veritabanı silinemedi"
for host in $(sql -e "SELECT Host FROM mysql.user WHERE User = '$DBUSER'"); do
  sql -e "DROP USER IF EXISTS '$DBUSER'@'$host'" || fail "kullanıcı silinemedi: $host"
done
sql cyberpanel -e "DELETE FROM databases_databases WHERE dbName = '$DB'" 2>/dev/null
echo "  kalan: $(sql -e "SELECT COUNT(*) FROM mysql.user WHERE User = '$DBUSER'") kullanıcı, $(sql -e "SHOW DATABASES LIKE '$DB'" | wc -l) veritabanı"

step "Kalanlar: CyberPanel kayıtları"
sql cyberpanel -e "DELETE FROM e_users WHERE emailOwner_id = '$D'" 2>/dev/null
sql cyberpanel -e "DELETE FROM e_domains WHERE domain = '$D'" 2>/dev/null
for id in $(sql cyberpanel -e "SELECT id FROM domains WHERE name = '$D'"); do
  sql cyberpanel -e "DELETE FROM records WHERE domain_id = $id" && sql cyberpanel -e "DELETE FROM domains WHERE id = $id" || fail "DNS bölgesi silinemedi"
done
sql cyberpanel -e "DELETE FROM websiteFunctions_childdomains WHERE domain LIKE '%.$D'" 2>/dev/null
sql cyberpanel -e "DELETE FROM websiteFunctions_websites WHERE domain = '$D'" 2>/dev/null || fail "site kaydı silinemedi (bağlı kayıt var)"
for q in "websiteFunctions_websites WHERE domain LIKE '%orkestrai%'" "websiteFunctions_childdomains WHERE domain LIKE '%orkestrai%'" \
         "e_domains WHERE domain LIKE '%orkestrai%'" "domains WHERE name LIKE '%orkestrai%'" "records WHERE name LIKE '%orkestrai%'"; do
  printf '  %s: %s\n' "${q%% *}" "$(sql cyberpanel -e "SELECT COUNT(*) FROM $q")"
done

step "OpenLiteSpeed"
if grep -qi orkestrai "$OLS_CONF"; then
  awk '/^virtualHost .*orkestrai/{skip=1} skip{ if (/^}/) skip=0; next } !/^[ \t]*map[ \t].*orkestrai/' "$OLS_CONF" > "$OLS_CONF.yeni" \
    && cat "$OLS_CONF.yeni" > "$OLS_CONF" && rm -f "$OLS_CONF.yeni"
fi
rm -rf "$LSWS/conf/vhosts/$D" "$LSWS/conf/vhosts/mail.$D" "$LSWS/cachedata/$D" "$LSWS/cachedata/mail.$D"
grep -ci orkestrai "$OLS_CONF" | sed 's/^/  httpd_config.conf içinde kalan satır: /'
"$LSWS/bin/lswsctrl" restart 2>&1 | tail -1 | indent

step "Dovecot"
awk '/^local_name [^ ]*orkestrai\.net *\{/{skip=1} skip{ if (/^}/) skip=0; next } {print}' /etc/dovecot/dovecot.conf > /etc/dovecot/dovecot.conf.yeni
if doveconf -n -c /etc/dovecot/dovecot.conf.yeni >/dev/null 2>&1; then
  cat /etc/dovecot/dovecot.conf.yeni > /etc/dovecot/dovecot.conf
  systemctl reload dovecot && echo "  dovecot yeniden yüklendi" || fail "dovecot yeniden yüklenemedi"
else
  fail "yeni dovecot yapılandırması doğrulanamadı; değiştirilmedi"
fi
rm -f /etc/dovecot/dovecot.conf.yeni

step "Postfix"
grep -vi 'orkestrai\.net' /etc/postfix/vmail_ssl.map > /etc/postfix/vmail_ssl.map.yeni && cat /etc/postfix/vmail_ssl.map.yeni > /etc/postfix/vmail_ssl.map
rm -f /etc/postfix/vmail_ssl.map.yeni
if postmap -F hash:/etc/postfix/vmail_ssl.map && postfix check; then
  systemctl reload postfix && echo "  postfix yeniden yüklendi" || fail "postfix yeniden yüklenemedi"
else
  cp -a "$BACKUP/_etc_postfix_vmail_ssl.map" /etc/postfix/vmail_ssl.map; postmap -F hash:/etc/postfix/vmail_ssl.map
  fail "postfix denetimi geçmedi; eski harita geri yüklendi"
fi

step "OpenDKIM"
for t in KeyTable SigningTable TrustedHosts; do
  grep -vi 'orkestrai\.net' "/etc/opendkim/$t" > "/etc/opendkim/$t.yeni" && cat "/etc/opendkim/$t.yeni" > "/etc/opendkim/$t"; rm -f "/etc/opendkim/$t.yeni"
done
rm -rf "/etc/opendkim/keys/$D"
systemctl restart opendkim && echo "  opendkim yeniden başlatıldı" || fail "opendkim başlatılamadı"

step "Sertifikalar"
for name in "$D" "mail.$D"; do
  /root/.acme.sh/acme.sh --remove -d "$name" --ecc --home /root/.acme.sh >/dev/null 2>&1
  rm -rf "/root/.acme.sh/${name}_ecc" "/etc/letsencrypt/live/$name" "/etc/letsencrypt/archive/$name" \
         "/etc/letsencrypt/renewal/$name.conf" "/etc/letsencrypt/accounts/$name.key"
done
/root/.acme.sh/acme.sh --list --home /root/.acme.sh 2>/dev/null | grep -ci orkestrai | sed 's/^/  acme.sh içinde kalan: /'

step "Dosyalar ve kullanıcı"
rm -rf "/home/$D" "/home/vmail/$D" /etc/orkestrai /opt/orkestrai-ollama /var/lib/orkestrai-ollama /var/lib/orkestrai-llm-management \
       "/home/cyberpanel/$D.bwmeta" "/home/cyberpanel/git/$D"
if [ -n "$SITE_USER" ] && [ "$SITE_USER" != root ] && id "$SITE_USER" >/dev/null 2>&1 \
   && [ "$(getent passwd "$SITE_USER" | cut -d: -f6)" = "/home/$D" ]; then
  userdel "$SITE_USER" 2>&1 | indent; echo "  site kullanıcısı silindi"
fi
if [ -S /root/.pm2/rpc.sock ]; then pm2 kill >/dev/null 2>&1; echo "  boş pm2 süreci durduruldu"; fi

step "Kalan izler (yalnız adlar)"
grep -rlI -i orkestrai /etc /usr/local/lsws/conf /var/spool/cron 2>/dev/null | grep -v "^$BACKUP" | indent
find / -xdev \( -path /proc -o -path /sys -o -path "$BACKUP" \) -prune -o -iname '*orkestrai*' -print 2>/dev/null | head -20 | indent

section "Siteler (son)"
for i in 1 2 3 4 5 6 7 8 9 10; do   # OpenLiteSpeed yeniden başladı: kısa dalgalanmaya izin
  AFTER=$(snapshot); [ "$AFTER" = "$BEFORE" ] && break; sleep 3
done
echo "  $AFTER"
for svc in lsws dovecot postfix opendkim mariadb pdns peyrev; do printf '  %s: %s\n' "$svc" "$(systemctl is-active "$svc" 2>/dev/null)"; done
[ "$AFTER" = "$BEFORE" ] || fail "site durumları değişti (önce: $BEFORE)"
for u in "https://orkestrai.net/"; do
  printf '  yerel %s -> %s\n' "$u" "$(curl -sk -o /dev/null -w '%{http_code}' --max-time 8 --resolve orkestrai.net:443:127.0.0.1 "$u" </dev/null)"
done
if [ "$FAILED" = 0 ]; then
  genel "orkestrai.net kaldırıldı; diğer siteler değişmedi: $AFTER"
else
  genel "kaldırmada sorun var; rapora bakın"; exit 1
fi
