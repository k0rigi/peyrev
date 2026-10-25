#!/usr/bin/env bash
# deploy/kurulum.sh — Peyrev'in ortak sunucudaki kurulumu (Sunucu işleri → kurulum-deneme / kurulum). Tekrar çalıştırılabilir.
#
# KIP=deneme  hiçbir şeyi değiştirmez: planı, dnf işlem özetini, peyrev.com sanal sunucu ayarını ve sertifikayı raporlar.
# KIP=uygula  PostgreSQL 16 (yalnız localhost), peyrev hesabı ve klasörleri, uv + Python 3.12, veritabanları ve parolası,
#             systemd birimi, gece yedeği, www dahil SSL ve OpenLiteSpeed vekili (/api/, /yonetim/ → 127.0.0.1:PORT).
#
# Kurallar: aynı sunucudaki orkestrai.net ve diğer siteler bozulmaz. Her riskli adımdan önce ve sonra sitelerin durum kodları
# karşılaştırılır; fark varsa o adım geri alınır ve betik durur. Mevcut bir paket yükseltilecek/kaldırılacaksa kurulum yapılmaz.
# Sırlar (veritabanı parolası) yalnız /etc/peyrev altındaki 0640 dosyaya yazılır; komut satırına ve çıktıya girmez.

set -Eeuo pipefail
umask 022
export LC_ALL=C.UTF-8 LANG=C.UTF-8
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

KIP=${KIP:-deneme}
case "$KIP" in deneme|uygula) ;; *) echo "KIP deneme ya da uygula olmalı"; exit 1 ;; esac
SITE=peyrev.com
PORT=5101
UV_VERSION=0.12.19
ROOT=/opt/peyrev
SECRETS=/etc/peyrev
BACKUPS=/var/backups/peyrev
LSWS=/usr/local/lsws
VHOST=$LSWS/conf/vhosts/$SITE/vhost.conf
SITES="orkestrai.net altunhukuk.av.tr hmtgross.com mkticaret.com"
MARK_BEGIN="# >>> peyrev vekili (deploy/kurulum.sh yönetir)"
MARK_END="# <<< peyrev vekili"
STEP=başlangıç

genel() { printf 'GENEL: %s\n' "$*"; }
adim() { STEP=$1; printf '\n========== %s ==========\n' "$1"; }
not() { printf '  * %s\n' "$*"; }
trap 'rc=$?; printf "HATA: %s adımında, satır %s (çıkış %s)\n" "$STEP" "$LINENO" "$rc"; genel "kurulum başarısız: $STEP"' ERR

code() {   # code HOST [YOL]: yerel 443 üzerinden durum kodu (DNS'e bağlı değil)
  curl -sk -o /dev/null -w '%{http_code}' --max-time 10 --resolve "$1:443:127.0.0.1" "https://$1${2:-/}" </dev/null || true
}
snapshot() {   # sitelerin durum kodları; orkestrai'nin lisans ucu dahil
  local s out=""
  for s in $SITES; do out="$out $s=$(code "$s")"; done
  printf '%s orkestrai-anahtar=%s' "$out" "$(code orkestrai.net /api/rpa/lisans/anahtar)"
}
require_same() {   # require_same ÖNCEKİ: birkaç denemede de farklıysa 1 (yeniden başlatma sonrası kısa dalgalanmaya izin)
  local now i
  for i in 1 2 3 4; do
    now=$(snapshot)
    [ "$now" = "$1" ] && { printf '  siteler: %s\n' "$now"; return 0; }
    sleep 5
  done
  printf '  siteler (önce): %s\n  siteler (şimdi): %s\n' "$1" "$now"
  return 1
}
ols_restart() {
  "$LSWS/bin/lswsctrl" restart >/dev/null
  sleep 10
}

# ---------------------------------------------------------------------------------------------------------------------
adim "Ön denetim"
[ "$(id -u)" = 0 ] || { echo "root gerekli"; exit 1; }
grep -q '^PLATFORM_ID="platform:el8"' /etc/os-release || { echo "Beklenmeyen işletim sistemi"; exit 1; }
BEFORE=$(snapshot)
printf '  başlangıç: %s\n' "$BEFORE"
case "$BEFORE" in *"orkestrai.net=200"*"orkestrai-anahtar=200"*) ;; *) echo "orkestrai.net sağlıklı değil; hiçbir şey yapılmadı."; genel "orkestrai.net sağlıklı değil; kurulum yapılmadı"; exit 1 ;; esac
genel "orkestrai.net / ve /api/rpa/lisans/anahtar -> 200 (başlangıç)"
for p in "$PORT" 5432; do
  owner=$(ss -H -ltnp "sport = :$p" 2>/dev/null | sed -E 's/.*users:\(\("([^"]+)".*/\1/' | head -1)
  printf '  port %s: %s\n' "$p" "${owner:-boş}"
  case "$p:$owner" in "$PORT:"|"$PORT:gunicorn"|"5432:"|"5432:postgres"|"5432:postmaster") ;; *) echo "Port $p başka bir süreçte."; exit 1 ;; esac
done
[ -f "$VHOST" ] || { echo "peyrev.com sanal sunucusu yok (CyberPanel'de site açılmalı)."; exit 1; }
SITE_USER=$(stat -c %U "/home/$SITE")
printf '  site hesabı: %s\n' "$SITE_USER"

# ---------------------------------------------------------------------------------------------------------------------
adim "PostgreSQL 16 paketi"
if rpm -q postgresql-server >/dev/null 2>&1; then
  printf '  kurulu: %s\n' "$(rpm -q postgresql-server)"
  rpm -q --qf '%{VERSION}' postgresql-server | grep -q '^16\.' || { echo "Başka sürüm kurulu; dokunulmadı."; exit 1; }
else
  dnf -q module list postgresql 2>/dev/null | grep -E '^postgresql +16 ' || { echo "postgresql:16 akışı yok"; exit 1; }
  # Dry run first: the module transaction must only add packages (no upgrade, downgrade or removal of anything installed).
  plan=$(dnf module install --assumeno --setopt=install_weak_deps=False postgresql:16/server 2>&1 || true)
  printf '%s\n' "$plan" | sed 's/^/    /'
  if printf '%s\n' "$plan" | grep -Eq '^(Upgrading|Downgrading|Removing|Obsoleting|Reinstalling|Switching module streams)'; then
    echo "Kurulum mevcut paketleri değiştirecekti; durduruldu."; genel "PostgreSQL kurulumu mevcut paketleri değiştirecekti; durduruldu"; exit 1
  fi
  if [ "$KIP" = uygula ]; then
    dnf -y -q module install --setopt=install_weak_deps=False postgresql:16/server
    printf '  kuruldu: %s\n' "$(rpm -q postgresql-server postgresql)"
  fi
fi

if [ "$KIP" = uygula ]; then
  adim "PostgreSQL yapılandırması"
  if [ ! -f /var/lib/pgsql/data/PG_VERSION ]; then
    PGSETUP_INITDB_OPTIONS="--encoding=UTF8 --locale=C.UTF-8 --auth-local=peer --auth-host=scram-sha-256" postgresql-setup --initdb
  fi
  conf=/var/lib/pgsql/data/postgresql.conf
  if ! grep -qF "$MARK_BEGIN" "$conf"; then
    cat >> "$conf" <<EOF

$MARK_BEGIN
listen_addresses = 'localhost'
port = 5432
password_encryption = 'scram-sha-256'
timezone = 'UTC'
log_timezone = 'Europe/Istanbul'
$MARK_END
EOF
  fi
  grep -Ev '^\s*(#|$)' /var/lib/pgsql/data/pg_hba.conf | sed 's/^/    hba: /'
  systemctl enable --now postgresql >/dev/null 2>&1
  systemctl restart postgresql
  for _ in $(seq 1 30); do runuser -u postgres -- pg_isready -q && break; sleep 1; done
  runuser -u postgres -- pg_isready
  ss -H -ltn 'sport = :5432' | awk '{print "    dinleme: " $4}'
  if ss -H -ltn 'sport = :5432' | awk '{print $4}' | grep -Ev '^(127\.0\.0\.1|\[::1\]):5432$' | grep -q .; then
    echo "PostgreSQL yalnız localhost'u dinlemiyor."; exit 1
  fi
  genel "PostgreSQL 16 çalışıyor (yalnız localhost)"
fi

# ---------------------------------------------------------------------------------------------------------------------
adim "Hesap ve klasörler"
if [ "$KIP" = uygula ]; then
  id peyrev >/dev/null 2>&1 || useradd --system --home-dir /var/lib/peyrev --create-home --shell /sbin/nologin peyrev
  install -d -o root -g root -m 0755 "$ROOT" "$ROOT/bin" "$ROOT/python" "$ROOT/releases"
  install -d -o root -g peyrev -m 0750 "$SECRETS"
  install -d -o root -g root -m 0700 "$BACKUPS"
fi
printf '  peyrev hesabı: %s\n' "$(id peyrev 2>/dev/null || echo yok)"
ls -ld "$ROOT" "$SECRETS" "$BACKUPS" 2>/dev/null | sed 's/^/    /' || true

# ---------------------------------------------------------------------------------------------------------------------
adim "uv ve Python 3.12"
if [ "$KIP" = uygula ]; then
  if ! "$ROOT/bin/uv" --version 2>/dev/null | grep -q "^uv $UV_VERSION"; then
    tmp=$(mktemp -d)
    base="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz"
    curl -fsSL --max-time 120 -o "$tmp/uv.tar.gz" "$base"
    curl -fsSL --max-time 60 -o "$tmp/uv.tar.gz.sha256" "$base.sha256"
    (cd "$tmp" && awk '{print $1 "  uv.tar.gz"}' uv.tar.gz.sha256 | sha256sum -c -)
    tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
    install -o root -g root -m 0755 "$tmp/uv-x86_64-unknown-linux-gnu/uv" "$ROOT/bin/uv"
    rm -rf -- "$tmp"
  fi
  export UV_PYTHON_INSTALL_DIR=$ROOT/python UV_NO_PROGRESS=1
  "$ROOT/bin/uv" python install 3.12 >/dev/null
  chmod -R a+rX "$ROOT/python"
  printf '  %s; %s\n' "$("$ROOT/bin/uv" --version)" "$("$ROOT/bin/uv" python find 3.12)"
  "$("$ROOT/bin/uv" python find 3.12)" -c 'import ssl, sys; print("  python", sys.version.split()[0], ssl.OPENSSL_VERSION)'
else
  printf '  uv: %s\n' "$("$ROOT/bin/uv" --version 2>/dev/null || echo 'kurulu değil')"
fi

# ---------------------------------------------------------------------------------------------------------------------
adim "Veritabanları"
if [ "$KIP" = uygula ]; then
  if [ ! -s "$SECRETS/db.env" ]; then
    PW=$("$("$ROOT/bin/uv" python find 3.12)" -c 'import secrets; print(secrets.token_urlsafe(32))')
    # The password reaches psql through stdin (printf is a builtin), never through a command line.
    { printf '\\set pw %s\n' "$PW"; cat <<'SQL'; } | runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1
SELECT 'CREATE ROLE peyrev LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'peyrev') \gexec
SELECT format('ALTER ROLE peyrev WITH LOGIN PASSWORD %L', :'pw') \gexec
SQL
    tmpenv=$(mktemp "$SECRETS/.db.env.XXXXXX")
    printf 'DATABASE_URL=postgresql://peyrev:%s@127.0.0.1:5432/peyrev\nPROVA_DATABASE_URL=postgresql://peyrev:%s@127.0.0.1:5432/peyrev_prova\n' "$PW" "$PW" > "$tmpenv"
    chown root:peyrev "$tmpenv"; chmod 0640 "$tmpenv"; mv -f "$tmpenv" "$SECRETS/db.env"
    unset PW
  fi
  runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 <<'SQL'
SELECT 'CREATE DATABASE peyrev OWNER peyrev ENCODING ''UTF8'' TEMPLATE template0'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'peyrev') \gexec
SELECT 'CREATE DATABASE peyrev_prova OWNER peyrev ENCODING ''UTF8'' TEMPLATE template0'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'peyrev_prova') \gexec
REVOKE ALL ON DATABASE peyrev FROM PUBLIC;
REVOKE ALL ON DATABASE peyrev_prova FROM PUBLIC;
SQL
  # Login check with the stored password, read by the peyrev account itself.
  # The password goes to psql through its environment (readable only by that account), not its command line.
  runuser -u peyrev -- bash -c '. /etc/peyrev/db.env; PGPASSWORD=$(printf %s "$DATABASE_URL" | sed -E "s#^postgresql://[^:]+:([^@]+)@.*#\1#") psql -h 127.0.0.1 -U peyrev -d peyrev -X -A -t -c "select current_user, current_database()"' | sed 's/^/    giriş: /'
  ls -l "$SECRETS" | sed 's/^/    /'
  genel "veritabanları peyrev ve peyrev_prova hazır"
fi

# ---------------------------------------------------------------------------------------------------------------------
adim "systemd birimi"
UNIT=/etc/systemd/system/peyrev.service
read -r -d '' UNIT_TEXT <<EOF || true
[Unit]
Description=Peyrev API (peyrev.com)
After=network-online.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=peyrev
Group=peyrev
WorkingDirectory=$ROOT/current
Environment=PEYREV_SECRETS_DIR=$SECRETS
Environment=PYTHONUNBUFFERED=1
ExecStart=$ROOT/current/venv/bin/gunicorn --bind 127.0.0.1:$PORT --workers 3 --timeout 60 --graceful-timeout 30 --max-requests 2000 --max-requests-jitter 200 --access-logfile - --error-logfile - peyrev.wsgi:app
Restart=always
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictRealtime=yes

[Install]
WantedBy=multi-user.target
EOF
if [ "$KIP" = uygula ]; then
  if [ ! -f "$UNIT" ] || [ "$(cat "$UNIT")" != "$UNIT_TEXT" ]; then
    printf '%s\n' "$UNIT_TEXT" > "$UNIT"; chmod 0644 "$UNIT"; systemctl daemon-reload
    not "birim yazıldı (uygulama ilk dağıtımla başlar)"
  fi
  systemctl enable peyrev.service >/dev/null 2>&1 || true
fi
printf '%s\n' "$UNIT_TEXT" | sed 's/^/    /'

# ---------------------------------------------------------------------------------------------------------------------
adim "Gece yedeği"
if [ "$KIP" = uygula ]; then
  cat > "$ROOT/bin/yedek.sh" <<'EOF'
#!/usr/bin/env bash
# Peyrev gece yedeği: veritabanı (pg_dump -Fc) ve /etc/peyrev sırları; 14 gün saklanır. /etc/cron.d/peyrev-yedek çalıştırır.
set -euo pipefail
umask 077
D=/var/backups/peyrev
T=$(date +%Y%m%d-%H%M%S)
runuser -u postgres -- pg_dump -Fc peyrev > "$D/.peyrev-$T.dump"
mv "$D/.peyrev-$T.dump" "$D/peyrev-$T.dump"
tar -C /etc -czf "$D/.sirlar-$T.tar.gz" peyrev
mv "$D/.sirlar-$T.tar.gz" "$D/sirlar-$T.tar.gz"
find "$D" -maxdepth 1 -type f \( -name 'peyrev-*.dump' -o -name 'sirlar-*.tar.gz' \) -mtime +13 -delete
EOF
  chmod 0700 "$ROOT/bin/yedek.sh"
  printf '30 3 * * * root %s/bin/yedek.sh >>/var/log/peyrev-yedek.log 2>&1\n' "$ROOT" > /etc/cron.d/peyrev-yedek
  chmod 0644 /etc/cron.d/peyrev-yedek
  "$ROOT/bin/yedek.sh"
  last=$(ls -1t "$BACKUPS"/peyrev-*.dump | head -1)
  runuser -u postgres -- pg_restore --list < "$last" >/dev/null
  ls -l "$BACKUPS" | sed 's/^/    /'
  genel "gece yedeği kuruldu ve bir kez denendi (03:30, 14 gün)"
fi

# ---------------------------------------------------------------------------------------------------------------------
adim "SSL (peyrev.com + www)"
CERT=/etc/letsencrypt/live/$SITE/fullchain.pem
sans() { openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null | tr ',' '\n' | sed -n 's/.*DNS://p' | sort | tr '\n' ' '; }
printf '  sertifika: %s | %s\n' "$(sans)" "$(openssl x509 -in "$CERT" -noout -enddate 2>/dev/null)"
if [ "$KIP" = uygula ] && ! sans | grep -qw "www.$SITE"; then
  pre=$(snapshot)
  cp -a "$VHOST" "$BACKUPS/vhost.conf.$(date +%Y%m%d-%H%M%S).ssl"
  cyberpanel issueSSL --domainName "$SITE" 2>&1 | tail -5 | sed 's/^/    /' || true
  sleep 8
  printf '  yeni sertifika: %s | %s\n' "$(sans)" "$(openssl x509 -in "$CERT" -noout -enddate 2>/dev/null)"
  if ! require_same "$pre"; then echo "SSL sonrası site durumları değişti."; genel "SSL sonrası site durumları değişti"; exit 1; fi
  sans | grep -qw "www.$SITE" || { echo "www için sertifika alınamadı."; genel "www için sertifika alınamadı"; exit 1; }
  genel "SSL: peyrev.com ve www.peyrev.com"
fi

# ---------------------------------------------------------------------------------------------------------------------
adim "OpenLiteSpeed vekili"
sed 's/^/    /' "$VHOST"
if [ "$KIP" = uygula ] && ! grep -qF "$MARK_BEGIN" "$VHOST"; then
  pre=$(snapshot)
  backup="$BACKUPS/vhost.conf.$(date +%Y%m%d-%H%M%S)"
  cp -a "$VHOST" "$backup"
  cat >> "$VHOST" <<EOF

$MARK_BEGIN
extprocessor peyrev_api {
  type proxy
  address 127.0.0.1:$PORT
  maxConns 100
  initTimeout 60
  retryTimeout 0
  respBuffer 0
}

context /api/ {
  type proxy
  handler peyrev_api
  addDefaultCharset off
}

context /yonetim/ {
  type proxy
  handler peyrev_api
  addDefaultCharset off
}
$MARK_END
EOF
  ols_restart
  if ! require_same "$pre"; then
    cp -a "$backup" "$VHOST"; ols_restart
    echo "Vekil sonrası site durumları değişti; eski ayar geri yüklendi."; genel "vekil geri alındı: site durumları değişti"; exit 1
  fi
  printf '  peyrev.com/api/saglik (uygulama dağıtılınca 200): %s\n' "$(code "$SITE" /api/saglik)"
  genel "OpenLiteSpeed: /api/ ve /yonetim/ -> 127.0.0.1:$PORT"
fi

# ---------------------------------------------------------------------------------------------------------------------
adim "Son denetim"
if require_same "$BEFORE"; then
  genel "diğer siteler ve orkestrai.net değişmedi: $(snapshot | tr -s ' ')"
else
  echo "Site durumları başlangıçtan farklı."; genel "site durumları başlangıçtan farklı"; exit 1
fi
genel "kurulum ($KIP) tamamlandı"
