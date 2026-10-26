#!/usr/bin/env bash
# deploy/orkestrai-envanter.sh — orkestrai.net kapanmadan önce SALT OKUNUR envanter (Sunucu işleri → orkestrai-envanter).
# 1) Eski sürümlerin (0.9.x) orkestrai'yi hâlâ kullanıp kullanmadığı ve 2026-10-26 taşımasından sonra orkestrai
#    veritabanına yazılan kayıtlar (yalnız tablo, kolon, sayı ve kimlik numarası; ad, e-posta, metin yazdırılmaz).
# 2) Sunucuda orkestrai.net'e ve sevkiyata ait her şey: site, servis, veritabanı, zamanlanmış görev, e-posta, DNS, sertifika,
#    yedek ve dosyalar; kaldırma işi bu listeye göre yazılır.
# 3) Peyrev'in orkestrai.net'e dayanıp dayanmadığı: sunucu adı, destek e-postasının alan adı (adres yazdırılmaz).
# Sözleşme: hiçbir dosya, servis, veritabanı, cron ya da ayar değiştirilmez. Veritabanına root'un /root/.my.cnf dosyasıyla
# bağlanılır (parola komut satırına girmez); sorgular yalnız SHOW / SELECT'tir.

set -uo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH
DB=orke_OrkestraDB
CUTOFF='2026-10-26 00:00:00'
LSWS=/usr/local/lsws
section() { printf '\n========== %s ==========\n' "$*"; }
indent() { sed 's/^/    /'; }
have() { command -v "$1" >/dev/null 2>&1; }
genel() { printf 'GENEL: %s\n' "$*"; }
sql() { mysql --defaults-file=/root/.my.cnf --default-character-set=utf8mb4 -N -B -r "$@" 2>&1; }
q() { sql "$DB" -e "$1"; }
exists() { [ -n "$(q "SHOW TABLES LIKE '$1'")" ]; }

section "1. Eski sürümlerin orkestrai kullanımı"
if [ ! -r /root/.my.cnf ] || ! have mysql; then
  echo "  mysql ya da /root/.my.cnf yok; veritabanı bölümleri atlandı"; DBOK=0
else
  DBOK=1
fi
if [ "$DBOK" = 1 ] && exists SYS_RpaOturumlari; then
  echo "  -- SYS_RpaOturumlari kolonları"; q "SHOW COLUMNS FROM SYS_RpaOturumlari" | cut -f1,2 | indent
  cols=$(q "SHOW COLUMNS FROM SYS_RpaOturumlari" | cut -f1)
  echo "  -- duruma göre oturum sayısı"; q "SELECT Durum, COUNT(*) FROM SYS_RpaOturumlari GROUP BY Durum" | indent
  for c in $(q "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='$DB' AND TABLE_NAME='SYS_RpaOturumlari' AND DATA_TYPE IN ('datetime','timestamp')"); do
    printf '  -- %s: son 30 günde %s oturum, taşımadan sonra %s oturum; en yeni %s\n' "$c" \
      "$(q "SELECT COUNT(*) FROM SYS_RpaOturumlari WHERE \`$c\` >= NOW() - INTERVAL 30 DAY")" \
      "$(q "SELECT COUNT(*) FROM SYS_RpaOturumlari WHERE \`$c\` >= '$CUTOFF'")" \
      "$(q "SELECT MAX(\`$c\`) FROM SYS_RpaOturumlari")"
    if printf '%s\n' "$cols" | grep -qx KullaniciID; then
      echo "     taşımadan sonra bu kolonu ilerleyen hesap kimlikleri:"
      q "SELECT DISTINCT KullaniciID FROM SYS_RpaOturumlari WHERE \`$c\` >= '$CUTOFF' ORDER BY KullaniciID" | tr '\n' ' ' | indent; echo
    fi
  done
  for c in $(printf '%s\n' "$cols" | grep -iE 'surum|versiyon|version|platform'); do
    echo "  -- $c dağılımı (değer, oturum sayısı)"; q "SELECT \`$c\`, COUNT(*) FROM SYS_RpaOturumlari GROUP BY \`$c\` ORDER BY 2 DESC" | indent
  done
fi

section "2. Taşımadan ($CUTOFF) sonra orkestrai'ye yazılan kayıtlar"
if [ "$DBOK" = 1 ]; then
  total=0
  for t in $(q "SHOW TABLES" | grep -E '^SYS_(Kullanicilar|KullaniciModulleri|FirmaModulleri|Firmalar|Rpa[A-Za-z]*|Ajan[A-Za-z]*)$'); do
    for c in $(q "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='$DB' AND TABLE_NAME='$t' AND DATA_TYPE IN ('datetime','timestamp')"); do
      n=$(q "SELECT COUNT(*) FROM \`$t\` WHERE \`$c\` >= '$CUTOFF'")
      case "$n" in ''|*[!0-9]*) printf '  %s.%s: okunamadı (%s)\n' "$t" "$c" "$n"; continue ;; esac
      [ "$n" -gt 0 ] || continue
      total=$((total + n))
      printf '  %s.%s: %s satır (en yeni %s)\n' "$t" "$c" "$n" "$(q "SELECT MAX(\`$c\`) FROM \`$t\`")"
    done
  done
  echo "  -- yeni destek talepleri ve mesajları (numara, tür, durum, zaman; metin yok)"
  exists SYS_RpaTalepler && q "SELECT TalepNo, KullaniciID, Tur, Durum, Surum, Olusturma, Guncelleme FROM SYS_RpaTalepler WHERE Olusturma >= '$CUTOFF' OR Guncelleme >= '$CUTOFF' ORDER BY Guncelleme" | indent
  exists SYS_RpaTalepMesajlari && q "SELECT TalepNo, Yazan, Olusturma FROM SYS_RpaTalepMesajlari WHERE Olusturma >= '$CUTOFF' ORDER BY Olusturma" | indent
  echo "  -- yeni başvurular ve kararlar (numara, paket, durum, zaman)"
  exists SYS_RpaBasvurulari && q "SELECT BasvuruID, Paket, Durum, KullaniciID, Olusturma, KararZamani FROM SYS_RpaBasvurulari WHERE Olusturma >= '$CUTOFF' OR KararZamani >= '$CUTOFF' ORDER BY Olusturma" | indent
  echo "  -- yeni hesaplar (kimlik, rol, durum, zaman)"
  q "SELECT KullaniciID, Rol, Durum, OlusturmaTarihi FROM SYS_Kullanicilar WHERE OlusturmaTarihi >= '$CUTOFF'" | indent
  genel "orkestrai'de taşımadan sonra zaman damgası ilerleyen satır sayısı: $total"
fi

section "3. Veritabanları ve kullanıcıları (MariaDB)"
if [ "$DBOK" = 1 ]; then
  echo "  -- veritabanları ve boyutları (MB)"
  sql -e "SELECT s.SCHEMA_NAME, ROUND(COALESCE(SUM(t.DATA_LENGTH + t.INDEX_LENGTH), 0) / 1048576, 1) FROM information_schema.SCHEMATA s LEFT JOIN information_schema.TABLES t ON t.TABLE_SCHEMA = s.SCHEMA_NAME GROUP BY s.SCHEMA_NAME ORDER BY 1" | indent
  echo "  -- veritabanı kullanıcıları ve yetkili oldukları şemalar"
  sql -e "SELECT User, Host FROM mysql.user ORDER BY User" | indent
  sql -e "SELECT DISTINCT User, Db FROM mysql.db ORDER BY User" | indent
  if sql -e "SHOW DATABASES" | grep -qx cyberpanel; then
    echo "  -- CyberPanel kayıtları (site, veritabanı, e-posta alanı, DNS bölgesi)"
    for tq in "websiteFunctions_websites:domain" "databases_databases:dbName" "e_domains:domain" "domains:name"; do
      tb=${tq%%:*}; col=${tq##*:}
      if sql cyberpanel -e "SHOW TABLES LIKE '$tb'" | grep -q .; then
        printf '    %s: %s\n' "$tb" "$(sql cyberpanel -e "SELECT \`$col\` FROM \`$tb\` ORDER BY 1" | tr '\n' ' ')"
      fi
    done
    if sql cyberpanel -e "SHOW TABLES LIKE 'e_users'" | grep -q .; then
      echo "    e-posta kutusu sayısı (alan adına göre):"; sql cyberpanel -e "SELECT emailOwner_id, COUNT(*) FROM e_users GROUP BY emailOwner_id" | indent
    fi
    if sql cyberpanel -e "SHOW TABLES LIKE 'records'" | grep -q .; then
      echo "    orkestrai DNS kaydı sayısı:"; sql cyberpanel -e "SELECT COUNT(*) FROM records WHERE name LIKE '%orkestrai%'" | indent
    fi
  fi
fi

section "4. Siteler, sanal sunucular ve sertifikalar"
echo "  -- /home"; ls -1 /home 2>/dev/null | indent
echo "  -- OLS sanal sunucuları"; ls -1 "$LSWS/conf/vhosts" 2>/dev/null | indent
echo "  -- httpd_config.conf içinde orkestrai/sevkiyat geçen satırlar"
grep -nI -iE 'orkestrai|sevkiyat' "$LSWS/conf/httpd_config.conf" 2>/dev/null | cut -c1-200 | indent
echo "  -- sertifikalar"; ls -1 /etc/letsencrypt/live 2>/dev/null | indent
for d in /root/.acme.sh /etc/letsencrypt/renewal; do [ -d "$d" ] && ls -1 "$d" 2>/dev/null | grep -i orkestrai | sed "s#^#    $d/#"; done
du -sh /home/orkestrai.net 2>/dev/null | indent

section "5. Servisler, süreçler ve portlar"
echo "  -- systemd birimleri"; systemctl list-unit-files --no-pager 2>/dev/null | grep -iE 'orkestrai|sevkiyat|socket-server|ollama|llm|studio' | indent
for u in $(systemctl list-unit-files --no-pager --plain 2>/dev/null | awk '{print $1}' | grep -iE 'orkestrai|sevkiyat|socket|ollama|llm|studio' | grep -E '\.service$'); do
  printf '    %s: %s %s, %s\n' "$u" "$(systemctl is-enabled "$u" 2>/dev/null)" "$(systemctl is-active "$u" 2>/dev/null)" "$(systemctl show "$u" -p FragmentPath -p WorkingDirectory -p User --value 2>/dev/null | tr '\n' ' ')"
done
echo "  -- dinleyen portlar"; ss -ltnpH 2>/dev/null | awk '{print $4, $6}' | sed 's/pid=[0-9]*,//g' | sort -u | indent
echo "  -- pm2 / node süreçleri"
for d in /proc/[0-9]*; do
  c=$(cat "$d/comm" 2>/dev/null) || continue
  case "$c" in node|pm2*|PM2*|ollama) printf '    %s (pid %s, cwd %s)\n' "$c" "${d#/proc/}" "$(readlink "$d/cwd" 2>/dev/null)" ;; esac
done
# pm2 komutu çalışan bir daemon yoksa yenisini başlatır; yalnız daemon zaten varsa sorulur.
if have pm2 && [ -S /root/.pm2/rpc.sock ]; then pm2 ls --no-color 2>/dev/null | indent; fi

section "6. Zamanlanmış görevler"
for f in /var/spool/cron/* /etc/crontab /etc/cron.d/*; do
  [ -f "$f" ] || continue
  grep -nIH -iE 'orkestrai|sevkiyat|ollama|llm|yedek|backup' "$f" 2>/dev/null | sed -E 's/(-p|--password[= ])[^ ]+/\1<gizli>/g' | cut -c1-220 | indent
done
systemctl list-timers --all --no-pager 2>/dev/null | grep -iE 'orkestrai|sevkiyat|llm|ollama' | indent

section "7. orkestrai adını taşıyan dosya ve dizinler (içerik okunmaz)"
for root in /etc /opt /var/lib /var/log /var/backups /var/tmp /root /home /usr/local /srv /backup /backups; do
  [ -d "$root" ] || continue
  find "$root" -maxdepth 4 \( -iname '*orkestrai*' -o -iname '*sevkiyat*' -o -iname '*OrkestraDB*' \) \
    -not -path '/home/orkestrai.net/*' -not -path '/proc/*' 2>/dev/null | head -60 | while read -r p; do
      printf '    %s  %s\n' "$(du -sh "$p" 2>/dev/null | cut -f1)" "$p"; done
done
echo "  -- /etc/orkestrai"; ls -la /etc/orkestrai 2>/dev/null | indent
echo "  -- CyberPanel yedek klasörleri"; for d in /home/backup /home/*/backup /backup; do [ -d "$d" ] && ls -la "$d" 2>/dev/null | head -20 | sed "s#^#    $d: #"; done

section "8. E-posta"
for d in /home/vmail/orkestrai.net /home/vmail/peyrev.com; do
  if [ -d "$d" ]; then printf '    %s: %s kutu, %s\n' "$d" "$(ls -1 "$d" 2>/dev/null | wc -l)" "$(du -sh "$d" 2>/dev/null | cut -f1)"; else printf '    yok: %s\n' "$d"; fi
done
have postconf && printf '    postfix myhostname: %s\n    postfix mydomain: %s\n' "$(postconf -h myhostname 2>/dev/null)" "$(postconf -h mydomain 2>/dev/null)"
grep -rlI -i orkestrai /etc/postfix /etc/dovecot /etc/opendkim* 2>/dev/null | indent

section "9. Peyrev'in orkestrai.net'e dayanması"
printf '    sunucu adı: %s\n' "$(hostname -f 2>/dev/null)"
if [ -r /etc/peyrev/destek.env ]; then
  for k in DESTEK_SMTP_HOST DESTEK_SMTP_USER DESTEK_EPOSTA_GONDEREN DESTEK_EPOSTA_ALICI; do
    v=$(grep -E "^$k=" /etc/peyrev/destek.env | head -1 | cut -d= -f2- | tr -d "\"' ")
    case "$v" in
      '') d='(boş)' ;;
      *@*) d="@${v##*@}" ;;
      *orkestrai*) d='orkestrai.net alanında' ;;
      *peyrev.com*) d='peyrev.com alanında' ;;
      *) d='başka bir alan' ;;
    esac
    printf '    %s: %s\n' "$k" "$d"
  done
else
  echo "    /etc/peyrev/destek.env yok"
fi
echo "  -- /etc/peyrev ve /opt/peyrev içinde orkestrai geçen dosyalar (yalnız adlar)"
grep -rlI -i orkestrai /etc/peyrev /opt/peyrev/current/deploy 2>/dev/null | indent

genel "orkestrai envanteri tamamlandı"
