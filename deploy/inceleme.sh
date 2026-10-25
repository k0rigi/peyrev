#!/usr/bin/env bash
# deploy/inceleme.sh — Peyrev kurulumu öncesi ortak sunucunun SALT OKUNUR incelemesi (Sunucu işleri → inceleme).
# Çıktı yalnız şifreli rapora gider (.github/scripts/uzak.py); günlükte yalnız "GENEL: " satırları görünür.
# Sözleşme: hiçbir dosya/paket/servis/veritabanı/cron/güvenlik duvarı değiştirilmez; servis yeniden
# başlatılmaz. Kimlik bilgisi dosyalarının içeriği, /proc/*/environ ve /proc/*/cmdline okunmaz.
# IP, e-posta, sunucu adı ve gizli değerler maskelenir. Veritabanına bağlanılmaz. Hassas web yollarına
# yalnız HEAD gönderilir. Tek yan etki: dnf önbellekten (-C) sorgulanır, günlüğü geçici dizine yazılır;
# DNF_YENILE=1 verilirse depo üstverisi /var/cache/dnf altına inebilir. DIS_BAGLANTI=0 dış denetimi atlar.

set +e
export LC_ALL=C LANG=C
umask 077
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH
export PATH

SITE=${SITE:-orkestrai.net}
SITE_HOME=/home/$SITE
WEBROOT=$SITE_HOME/public_html
APP=$WEBROOT/api
LSWS=/usr/local/lsws
PINNED_HOSTKEY='SHA256:E6ZdzQjnBK7a+kOE8/OBFFBlHZpVJz7sdRsC3Y75Ng4'

TMPD=$(mktemp -d "${TMPDIR:-/tmp}/sunucu-inceleme.XXXXXX" 2>/dev/null) || TMPD=
cleanup() { if [ -n "$TMPD" ] && [ -d "$TMPD" ]; then rm -rf -- "$TMPD"; fi; }
trap cleanup EXIT

PYBIN=
for c in /usr/libexec/platform-python /usr/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1; then PYBIN=$(command -v "$c"); break; fi
done

MASK_HOSTS=
for h in "$(hostname -f 2>/dev/null)" "$(hostname -s 2>/dev/null)" "$(hostname 2>/dev/null)"; do
  case "$h" in
    ''|localhost|localhost.*|"$SITE"|www."$SITE") ;;
    *) if [ "${#h}" -ge 4 ]; then MASK_HOSTS="$MASK_HOSTS $h"; fi ;;
  esac
done
export MASK_HOSTS

# Heredoc komut ikamesi içinde değil: eski bash sürümleri $(...) içindeki heredoc tırnaklarını yanlış çözer.
MASK_PY=
IFS= read -r -d '' MASK_PY <<'PY'
import os, re, sys
mode = sys.argv[1] if len(sys.argv) > 1 else 'genel'
hosts = sorted(set(h for h in os.environ.get('MASK_HOSTS', '').split() if len(h) >= 4), key=len, reverse=True)
HOSTS = [re.compile(r'(?<![\w.-])' + re.escape(h) + r'(?![\w-]|\.[A-Za-z0-9])', re.I) for h in hosts]
SENS = (r'(?:pass(?:word|wd|phrase)?|pwd|pw|secret|token|key|credential|auth(?:orization)?|cookie|'
        r'salt|dsn|sig(?:nature)?|anahtar|sifre|\u015fifre|parola|gizli|belirte[c\u00e7]|tohum)')
KV = re.compile(r'(?i)\b([\w.-]*' + SENS + r'[\w.-]*)(["\']?\s*[=:]\s*)("[^"]*"|\'[^\']*\'|[^\s,;&]+)')
SPACED = re.compile(r'(?i)^(\s*[\w.-]*' + SENS + r'[\w.-]*\s+)(\S.*)$')
ENV = re.compile(r'(?i)^(\s*(?:Environment=|env\s+|export\s+|setenv\s+))(.*)$')
ENVNAME = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=')
UPPER = re.compile(r'(?<![\w$])([A-Z][A-Z0-9_]{2,})=("[^"]*"|\'[^\']*\'|\S+)')
SAFE_UPPER = set(['PATH', 'SHELL', 'HOME', 'LANG', 'LC_ALL', 'TZ', 'CRON_TZ', 'USER', 'LOGNAME'])
URLCRED = re.compile(r'(?i)([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]*@')
BEARER = re.compile(r'(?i)\b(bearer|basic|token)\s+[A-Za-z0-9+/=._~-]{8,}')
CRONARG = re.compile(r'(\s(?:-u|--user|-p|--password|--pass|-P|--token|-k|--key|-H|--header)[\s=]?)("[^"]*"|\'[^\']*\'|\S+)')
MYSQLP = re.compile(r'(\s-p)(?=\S)(\S+)')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')
IPV4D = re.compile(r'(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\d|\.\d)')
IPV4H = re.compile(r'(?<![\d.])(\d{1,3})-(\d{1,3})-(\d{1,3})-(\d{1,3})(?![\d-])')
IPV6 = re.compile(r'(?i)(?<![0-9a-f:])(?:[23][0-9a-f]{3}|fe80):[0-9a-f:]*[0-9a-f](?![0-9a-f:])')
LONG = re.compile(r'(?<![A-Za-z0-9+=_-])(?=[A-Za-z0-9+=_-]*\d)(?=[A-Za-z0-9+=_-]*[A-Za-z])[A-Za-z0-9+=_-]{40,}')

def v4(m):
    parts = [int(x) for x in m.groups()]
    if max(parts) > 255 or parts[0] in (0, 127, 255):
        return m.group(0)
    a, b = parts[0], parts[1]
    if a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) or (a == 100 and 64 <= b <= 127):
        return '<ipv4-ozel>'
    return '<ipv4-genel>'

def env_mask(m):
    names = ENVNAME.findall(m.group(2))
    if not names:
        return m.group(1) + '<gizli>'
    return m.group(1) + ' '.join(n + '=<gizli>' for n in names)

def upper_mask(m):
    return m.group(0) if m.group(1) in SAFE_UPPER else m.group(1) + '=<gizli>'

out = sys.stdout.buffer
for raw in sys.stdin.buffer:
    line = raw.decode('utf-8', 'replace').rstrip('\r\n')
    for rx in HOSTS:
        line = rx.sub('<sunucu-adi>', line)
    if ENV.match(line):
        line = ENV.sub(env_mask, line)
    line = URLCRED.sub(r'\1<gizli>@', line)
    line = BEARER.sub(r'\1 <gizli>', line)
    if re.search(r'(?i)mysql|mariadb|sshpass', line):
        line = MYSQLP.sub(r'\1<gizli>', line)
    if mode == 'cron':
        line = CRONARG.sub(r'\1<gizli>', line)
    line = KV.sub(lambda m: m.group(1) + m.group(2) + '<gizli>', line)
    line = UPPER.sub(upper_mask, line)
    if mode == 'conf':
        mm = SPACED.match(line)
        if mm and not mm.group(2).startswith(('/', '$', '<', '{')):
            line = mm.group(1) + '<gizli>'
    line = EMAIL.sub('<eposta>', line)
    line = IPV4D.sub(v4, line)
    line = IPV4H.sub(v4, line)
    line = IPV6.sub('<ipv6>', line)
    line = LONG.sub('<uzun-deger>', line)
    out.write((line + '\n').encode('utf-8', 'replace'))
out.flush()
PY

mask() {
  if [ -n "$PYBIN" ]; then
    "$PYBIN" -I -c "$MASK_PY" "${1:-genel}"
  else
    sed -E -e 's/[0-9]{1,3}([.-][0-9]{1,3}){3}/<ipv4>/g' \
      -e 's/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/<eposta>/g' \
      -e 's/((pass|pwd|secret|token|key|auth|anahtar|sifre|parola)[A-Za-z0-9_.-]*["'"'"']?[[:space:]]*[=:][[:space:]]*)[^[:space:]]+/\1<gizli>/gI' \
      -e 's/^([[:space:]]*(Environment=|env[[:space:]]+|export[[:space:]]+)).*/\1<gizli>/I'
  fi
}

section() { printf '\n========== %s ==========\n' "$*"; }
note() { printf '  * %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
run() {   # run [-t saniye] komut...: stdin kapalı, stderr birleşik, süre sınırlı
  local t=25
  if [ "${1:-}" = "-t" ]; then t=$2; shift 2; fi
  if have timeout; then timeout -k 5 "$t" "$@" </dev/null 2>&1; else "$@" </dev/null 2>&1; fi
}
show() { run "$@" | mask; }
indent() { sed 's/^/    /'; }
fileinfo() {   # yalnız meta veri; içerik okunmaz
  local f
  for f in "$@"; do
    if [ -e "$f" ] || [ -L "$f" ]; then
      stat -c '  %A %U(%u):%G(%g) %s bayt %y  %n' -- "$f" 2>/dev/null | mask
    else
      printf '  yok: %s\n' "$f"
    fi
  done
}
svc_state() {
  local u a e
  for u in "$@"; do
    a=$(systemctl is-active "$u" 2>/dev/null); e=$(systemctl is-enabled "$u" 2>/dev/null)
    printf '  %-30s aktif=%s etkin=%s\n' "$u" "${a:--}" "${e:--}"
  done
}
dnfq() {
  local opts
  if ! have dnf; then note "dnf yok"; return 0; fi
  opts="-q"
  if [ -n "$TMPD" ]; then opts="$opts --setopt=logdir=$TMPD"; fi
  if [ "${DNF_YENILE:-0}" != 1 ]; then opts="$opts -C"; fi
  run -t 120 dnf $opts "$@" | head -80 | mask
}
http_code() {   # http_code URL [curl seçenekleri...]: yalnız durum kodu, gövde yok (HEAD)
  local u=$1; shift
  curl -sk -I -o /dev/null -w '%{http_code}' --max-time 8 "$@" "$u" </dev/null 2>/dev/null
}

section "0. Çalıştırma"
printf '  betik sürümü: 1\n  zaman (UTC): %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
printf '  etkin kullanıcı: %s (uid=%s)\n' "$(id -un 2>/dev/null)" "$(id -u 2>/dev/null)"
if [ "$(id -u 2>/dev/null)" != 0 ]; then note "root değil: bazı denetimler eksik kalacak"; fi
printf '  maskeleme: %s\n' "${PYBIN:-sed (python yok)}"

section "1. İşletim sistemi"
if [ -r /etc/os-release ]; then grep -E '^(NAME|ID|ID_LIKE|VERSION_ID|PRETTY_NAME|PLATFORM_ID)=' /etc/os-release | indent; fi
if [ -r /etc/redhat-release ]; then printf '    redhat-release: %s\n' "$(head -1 /etc/redhat-release)"; fi
printf '    çekirdek: %s  mimari: %s\n' "$(uname -r)" "$(uname -m)"
printf '    systemd: %s\n' "$(run systemctl --version | head -1)"
printf '    sanallaştırma: %s\n' "$(run systemd-detect-virt | head -1)"
printf '    cgroup kökü: %s (cgroup2fs=v2, tmpfs=v1/karma)\n' "$(stat -fc %T /sys/fs/cgroup 2>/dev/null)"
printf '    SELinux: %s\n' "$(run getenforce | head -1)"
run timedatectl status | grep -E 'Time zone|synchronized|NTP service' | indent
printf '    UTC farkı: %s  açık kalma: %s  yük: %s\n' "$(date +%z)" "$(run uptime -p)" "$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"

section "2. İşlemci, bellek, disk"
printf '  mantıksal işlemci: %s\n' "$(getconf _NPROCESSORS_ONLN 2>/dev/null)"
FLAGS=" $(awk -F: '/^flags[[:space:]]*:/ {print $2; exit}' /proc/cpuinfo 2>/dev/null) "
hasf() { case "$FLAGS" in *" $1 "*) return 0 ;; esac; return 1; }
for f in sse2 pni ssse3 sse4_1 sse4_2 popcnt cx16 lahf_lm avx avx2 bmi1 bmi2 fma f16c abm movbe xsave aes avx512f; do
  if hasf "$f"; then v=evet; else v=hayir; fi
  printf '    %-8s %s\n' "$f" "$v"
done
LEVEL=1
if hasf cx16 && hasf lahf_lm && hasf popcnt && hasf pni && hasf ssse3 && hasf sse4_1 && hasf sse4_2; then
  LEVEL=2
  if hasf avx && hasf avx2 && hasf bmi1 && hasf bmi2 && hasf f16c && hasf fma && hasf abm && hasf movbe && hasf xsave; then LEVEL=3; fi
fi
printf '  x86-64 mikromimari seviyesi: v%s (AlmaLinux/RHEL 9 en az v2 ister)\n' "$LEVEL"
grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo 2>/dev/null | indent
show swapon --show
show df -hPT / /home /var /var/lib /opt /tmp /usr/local
show df -iP / /home
show lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT

section "3. Dinlenen portlar"
echo "  -- TCP (ss -ltnp)"; run ss -H -ltnp | indent | mask
echo "  -- UDP (ss -lunp)"; run ss -H -lunp | indent | mask
USED=$(ss -H -ltn 2>/dev/null | awk '{print $4}' | sed -E 's/.*:([0-9]+)$/\1/' | sort -un)
FREE=; NFREE=0; p=5100
while [ "$p" -le 5199 ]; do
  if ! printf '%s\n' "$USED" | grep -qx "$p"; then FREE="$FREE $p"; NFREE=$((NFREE + 1)); fi
  p=$((p + 1))
done
printf '  5100-5199 arasında dinlenmeyen TCP port sayısı: %s\n' "$NFREE"
printf '  ilk boş portlar:%s\n' "$(printf '%s' "$FREE" | cut -d' ' -f1-21)"
printf '  aralıkta dolu olanlar: %s\n' "$(printf '%s\n' "$USED" | awk '$1 >= 5100 && $1 <= 5199' | tr '\n' ' ')"
for p in 22 80 443 3000 3306 5001 5432 6432 7080 8090 11434; do
  if printf '%s\n' "$USED" | grep -qx "$p"; then s=dinleniyor; else s=bos; fi
  printf '    port %-6s %s\n' "$p" "$s"
done
printf '  geçici port aralığı: %s\n' "$(cat /proc/sys/net/ipv4/ip_local_port_range 2>/dev/null | tr '\t' ' ')"

section "4. Güvenlik duvarı"
svc_state firewalld csf lfd nftables iptables fail2ban
if have firewall-cmd; then
  printf '  firewalld: %s\n' "$(run -t 10 firewall-cmd --state | head -1)"
  run -t 10 firewall-cmd --get-active-zones | indent | mask
  run -t 10 firewall-cmd --list-all | indent | mask
fi
if [ -r /etc/csf/csf.conf ]; then
  grep -E '^[[:space:]]*(TESTING|TCP_IN|TCP_OUT|UDP_IN|UDP_OUT|TCP6_IN|UDP6_IN|IPV6)[[:space:]]*=' /etc/csf/csf.conf | indent | mask
  if have csf; then printf '  csf: %s\n' "$(run csf -v | head -1)"; fi
fi
if have nft; then echo "  -- nft list ruleset (ilk 150 satır)"; run nft list ruleset | head -150 | indent | mask; fi
if have iptables; then echo "  -- iptables -S (ilk 80 satır)"; run iptables -S | head -80 | indent | mask; fi

section "5. OpenLiteSpeed"
svc_state lsws lshttpd openlitespeed
if [ -r "$LSWS/VERSION" ]; then printf '  VERSION: %s\n' "$(head -1 "$LSWS/VERSION")"; fi
for b in "$LSWS/bin/openlitespeed" "$LSWS/bin/lshttpd" "$LSWS/bin/litespeed"; do
  if [ -x "$b" ]; then printf '  %s -v:\n' "$b"; run -t 10 "$b" -v | head -5 | indent | mask; break; fi
done
echo "  -- sanal sunucular (ad ve docRoot)"
for v in "$LSWS"/conf/vhosts/*/; do
  [ -d "$v" ] || continue
  n=$(basename "$v"); d=-
  if [ -r "$v/vhost.conf" ]; then d=$(awk '$1 == "docRoot" {print $2; exit}' "$v/vhost.conf"); fi
  printf '    %-32s docRoot=%s\n' "$n" "${d:--}"
done
if [ -r "$LSWS/conf/httpd_config.conf" ]; then
  echo "  -- $LSWS/conf/httpd_config.conf (gizli değerler maskeli, ilk 400 satır)"
  head -400 "$LSWS/conf/httpd_config.conf" | indent | mask conf
fi
VH="$LSWS/conf/vhosts/$SITE"
echo "  -- $VH (yalnız adlar)"; ls -la "$VH" 2>/dev/null | indent | mask
if [ -r "$VH/vhost.conf" ]; then
  echo "  -- $VH/vhost.conf (gizli değerler maskeli, ilk 400 satır)"
  head -400 "$VH/vhost.conf" | indent | mask conf
  echo "  -- vekil / yeniden yazma / başlık satırları"
  grep -nE 'extprocessor|context|handler|address|RewriteRule|RewriteCond|\[P|autoLoadHtaccess|Access-Control|extraHeaders|[Hh]eader|vhDomain|vhAliases|keyFile|certFile' "$VH/vhost.conf" | indent | mask conf
else
  note "$VH/vhost.conf okunamadı"
fi
echo "  -- tüm OLS yapılandırmasında Access-Control ve vekil hedefleri"
grep -rnI 'Access-Control' "$LSWS/conf" 2>/dev/null | head -20 | indent | mask conf
grep -rnIE '^[[:space:]]*(extprocessor|type[[:space:]]+proxy|address[[:space:]]+[0-9*\[])' "$LSWS/conf" 2>/dev/null | head -80 | indent | mask conf

section "6. CyberPanel"
if have cyberpanel; then
  printf '  cyberpanel CLI: %s (alt komut çalıştırılmadı)\n' "$(command -v cyberpanel)"
  printf '  başlık satırı: %s\n' "$(head -1 "$(command -v cyberpanel)" 2>/dev/null)"
else
  note "cyberpanel CLI bulunamadı"
fi
svc_state lscpd
if [ -r /usr/local/CyberCP/version.txt ]; then printf '  version.txt: %s\n' "$(head -c 200 /usr/local/CyberCP/version.txt | tr '\n' ' ')"; fi
if [ -r /usr/local/CyberCP/baseTemplate/views.py ]; then grep -E '^(VERSION|BUILD)[[:space:]]*=' /usr/local/CyberCP/baseTemplate/views.py | head -2 | indent; fi
echo "  -- /home altındaki dizinler"; ls -1 /home 2>/dev/null | indent
for d in /home/peyrev.com "$LSWS/conf/vhosts/peyrev.com" /etc/letsencrypt/live/peyrev.com; do
  if [ -e "$d" ]; then printf '  VAR: %s\n' "$d"; else printf '  yok: %s\n' "$d"; fi
done
for d in /home/rotarazzi_api /home/rotarazzi.online /home/rotarazzi.com "$LSWS/conf/vhosts/rotarazzi.online" "$LSWS/conf/vhosts/rotarazzi.com"; do
  if [ -e "$d" ]; then printf '  KALINTI: %s\n' "$d"; fi
done

section "7. orkestrai.net dosya düzeni ve Python"
fileinfo "$SITE_HOME" "$WEBROOT" "$APP" "$APP/venv" "$WEBROOT/rpa" "$WEBROOT/.htaccess" "$WEBROOT/index.html" "$APP/app.py" "$APP/db.py" "$APP/rpa_lisans.py"
echo "  -- $SITE_HOME (yalnız ad/izin; içerik okunmaz)"; ls -la "$SITE_HOME" 2>/dev/null | indent | mask
echo "  -- web kökündeki nokta dizinleri ve git iç dosyaları"
for n in .git .github .agents .clinerules .claude .testler .vscode HEAD FETCH_HEAD ORIG_HEAD config description index shallow packed-refs hooks info logs objects refs workflows node_modules; do
  if [ -e "$WEBROOT/$n" ]; then printf '    var: %s\n' "$n"; fi
done
echo "  -- 2026-09-09 strip_components dağıtımından kalmış olabilecek dosyalar"
for n in server.js db.py app.py db_migrate.py migrate.py route_optimizer.py requirements.txt gunicorn.log api.log settings.json "kontrol listesi.md" deploy.yml exclude rampaac.html rampakapat.html ._app.py ._db.py ._route_optimizer.py; do
  if [ -e "$WEBROOT/$n" ]; then stat -c '    var: %n  %s bayt  %y' -- "$WEBROOT/$n" 2>/dev/null | sed 's#^\(    var: \).*/public_html/#\1#'; fi
done
ls -1 "$WEBROOT" 2>/dev/null | grep -E '\.sample$' | head -20 | sed 's/^/    var (git hook örneği): /'
printf '  web kökü girdi sayısı: %s  boyut: %s\n' "$(ls -1A "$WEBROOT" 2>/dev/null | wc -l)" "$(run -t 60 du -sh "$WEBROOT" | awk 'NR==1 {print $1}')"
echo "  -- API Python ortamı"
if [ -r "$APP/venv/pyvenv.cfg" ]; then grep -E '^(home|version|include-system-site-packages)' "$APP/venv/pyvenv.cfg" | indent; fi
if [ -x "$APP/venv/bin/python" ]; then
  printf '    venv python: %s\n' "$(run "$APP/venv/bin/python" -V)"
  run -t 60 "$APP/venv/bin/python" -m pip list --format=freeze --disable-pip-version-check | indent | mask
fi
for py in /usr/bin/python3 /usr/libexec/platform-python; do
  if [ -x "$py" ]; then printf '    %s: %s\n' "$py" "$(run "$py" -V)"; fi
done
if [ -x /usr/bin/python3 ]; then
  if run /usr/bin/python3 -I -c 'import cryptography' >/dev/null 2>&1; then c=var; else c=yok; fi
  printf '    sistem python3 içinde cryptography: %s\n' "$c"
fi

section "8. orkestrai_api.service ve Gunicorn"
run systemctl show orkestrai_api.service -p Id -p LoadState -p ActiveState -p SubState -p UnitFileState -p User -p Group \
  -p WorkingDirectory -p FragmentPath -p DropInPaths -p MainPID -p Restart -p KillMode -p TimeoutStopUSec \
  -p ExecMainStartTimestamp -p EnvironmentFiles | indent | mask
echo "  -- birim dosyası (Environment= değerleri maskeli)"; run systemctl cat orkestrai_api.service | indent | mask
echo "  -- /etc/systemd/system altındaki özel birimler"; ls -1 /etc/systemd/system/*.service 2>/dev/null | sed 's#^/etc/systemd/system/#    #'
echo "  -- ilgili süreçler (/proc; komut satırı ve ortam okunmaz)"
for d in /proc/[0-9]*; do
  c=$(cat "$d/comm" 2>/dev/null) || continue
  case "$c" in gunicorn|node|ollama|postgres|postmaster|mysqld|mariadbd|litespeed|lshttpd|openlitespeed|lscgid|lscpd|uv) ;; *) continue ;; esac
  pp=$(awk '/^PPid:/ {print $2}' "$d/status" 2>/dev/null)
  uid=$(awk '/^Uid:/ {print $2}' "$d/status" 2>/dev/null)
  usr=$(getent passwd "$uid" 2>/dev/null | cut -d: -f1)
  cwd=$(readlink "$d/cwd" 2>/dev/null)
  printf '    pid=%s ppid=%s ad=%s kullanici=%s(%s) cwd=%s\n' "${d#/proc/}" "$pp" "$c" "${usr:-?}" "$uid" "${cwd:--}"
done | head -80 | mask
masters=0; workers=0; app_masters=0
for d in /proc/[0-9]*; do
  [ "$(cat "$d/comm" 2>/dev/null)" = gunicorn ] || continue
  pp=$(awk '/^PPid:/ {print $2}' "$d/status" 2>/dev/null)
  if [ "$(cat "/proc/$pp/comm" 2>/dev/null)" = gunicorn ]; then
    workers=$((workers + 1))
  else
    masters=$((masters + 1))
    if [ "$(readlink "$d/cwd" 2>/dev/null)" = "$APP" ]; then app_masters=$((app_masters + 1)); fi
  fi
done
printf '  gunicorn ana süreç: %s (cwd=%s olan: %s), işçi: %s\n' "$masters" "$APP" "$app_masters" "$workers"
if have curl; then
  printf '  GET 127.0.0.1:5001/api/studio/ekip/ben -> %s (beklenen 401)\n' \
    "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:5001/api/studio/ekip/ben </dev/null)"
  if [ -n "$PYBIN" ]; then
    curl -s --max-time 5 http://127.0.0.1:5001/api/rpa/lisans/anahtar </dev/null | "$PYBIN" -I -c '
import sys, json, hashlib
try:
    d = json.loads(sys.stdin.read() or "{}")
except Exception:
    print("  anahtar ucu: JSON değil"); sys.exit(0)
k = d.get("anahtar") or ""
print("  anahtar ucu: success=%s protokol=%s asgari_surum=%s anahtar_sha256_ilk16=%s" % (
    d.get("success"), d.get("protokol"), d.get("asgari_surum"),
    hashlib.sha256(k.encode("utf-8")).hexdigest()[:16] if k else "-"))'
  fi
fi

section "9. OLS üzerinden /api vekili ve web erişimi (yalnız durum kodları)"
if have curl; then
  RESOLVE="$SITE:443:127.0.0.1"
  if [ "$(http_code "https://$SITE/" --resolve "$RESOLVE")" = 000 ]; then
    ip1=$(hostname -I 2>/dev/null | awk '{print $1}')
    if [ -n "$ip1" ]; then RESOLVE="$SITE:443:$ip1"; note "443 loopback'te yanıtsız; sunucunun ilk adresi denendi (yazdırılmadı)"; fi
  fi
  for path in / /rpa/ /rpa/stable.manifest /api/rpa/lisans/anahtar /api/studio/ekip/ben /api/yok-boyle-bir-uc \
      /.htaccess /.git/config /.git/HEAD /.github/workflows/deploy.yml /.agents/rules/orkestrai.md \
      "/.clinerules/kontrol%20listesi.md" /config /HEAD /objects/ /workflows/deploy.yml \
      /server.js /db.py /app.py "/kontrol%20listesi.md" /deploy.yml /settings.json /requirements.txt /gunicorn.log \
      /api/db.py /api/app.py /api/venv/pyvenv.cfg /api/gunicorn.log /socket-server/server.js /backend/server.js \
      /app.src.js /rpa-yonetim.src.js /uploads/; do
    printf '    HEAD %-38s %s\n' "$path" "$(http_code "https://$SITE$path" --resolve "$RESOLVE")"
  done
  printf '    HTTP 80 / -> %s\n' "$(http_code "http://$SITE/" --resolve "$SITE:80:127.0.0.1")"
  echo "  -- seçili yanıt başlıkları (Origin: https://$SITE)"
  for path in /api/rpa/lisans/anahtar / /rpa/stable.manifest; do
    echo "    [$path]"
    curl -sk -D - -o /dev/null --max-time 8 --resolve "$RESOLVE" -H "Origin: https://$SITE" "https://$SITE$path" </dev/null 2>/dev/null \
      | tr -d '\r' \
      | grep -iE '^(HTTP/|server:|access-control-allow-|vary:|content-type:|x-frame-options:|x-content-type-options:|referrer-policy:|strict-transport-security:|content-security-policy:|x-litespeed-|alt-svc:)' \
      | sed 's/^/      /' | mask
  done
  printf '  /api yanıtındaki Access-Control-Allow-Origin satırı sayısı: %s (beklenen 1)\n' \
    "$(curl -sk -D - -o /dev/null --max-time 8 --resolve "$RESOLVE" -H "Origin: https://$SITE" "https://$SITE/api/rpa/lisans/anahtar" </dev/null 2>/dev/null | tr -d '\r' | grep -ci '^access-control-allow-origin:')"
else
  note "curl yok"
fi

section "10. /rpa yayın dizini (publish-rpa)"
ls -la "$WEBROOT/rpa" 2>/dev/null | indent | mask
ls -la "$WEBROOT/rpa/releases" 2>/dev/null | indent | mask
for d in "$WEBROOT"/rpa/releases/*/; do
  if [ -d "$d" ]; then echo "    [$(basename "$d")]"; ls -la "$d" 2>/dev/null | sed 's/^/      /'; fi
done
if [ -r "$WEBROOT/rpa/stable.manifest" ] && [ -n "$PYBIN" ]; then
  "$PYBIN" -I -c '
import sys, json, base64
try:
    env = json.load(open(sys.argv[1]))
    p = json.loads(base64.b64decode(env["payload"]).decode("utf-8"))
    print("    stable.manifest: schema=%s channel=%s version=%s published_at=%s expires_at=%s assets=%s" % (
        p.get("schema"), p.get("channel"), p.get("version"), p.get("published_at"), p.get("expires_at"),
        ",".join(sorted((p.get("assets") or {}).keys()))))
except Exception as error:
    print("    stable.manifest okunamadı: %s" % type(error).__name__)' "$WEBROOT/rpa/stable.manifest"
fi
find "$WEBROOT/rpa" -maxdepth 1 \( -name '.upload-*' -o -name '.publish-*' \) -printf '    kalıntı: %f\n' 2>/dev/null

section "11. Zamanlanmış görevler (gizli değerler maskeli)"
echo "  -- root crontab"; run crontab -l -u root | grep -vE '^[[:space:]]*(#|$)' | indent | mask cron
echo "  -- /var/spool/cron"; ls -la /var/spool/cron 2>/dev/null | indent | mask
for f in /var/spool/cron/*; do
  [ -f "$f" ] || continue
  u=$(basename "$f"); [ "$u" = root ] && continue
  echo "  -- crontab: $u"; grep -vE '^[[:space:]]*(#|$)' "$f" 2>/dev/null | indent | mask cron
done
echo "  -- /etc/crontab"; grep -vE '^[[:space:]]*(#|$)' /etc/crontab 2>/dev/null | indent | mask cron
for f in /etc/cron.d/*; do
  [ -f "$f" ] || continue
  echo "  -- $f"; grep -vE '^[[:space:]]*(#|$)' "$f" 2>/dev/null | indent | mask cron
done
for d in /etc/cron.hourly /etc/cron.daily /etc/cron.weekly /etc/cron.monthly; do
  printf '  %s: %s\n' "$d" "$(ls -1 "$d" 2>/dev/null | tr '\n' ' ')"
done
echo "  -- systemd zamanlayıcıları"; run systemctl list-timers --all --no-pager | head -40 | indent | mask
if [ -e "$APP/stok_snapshot_cron.py" ]; then note "api/stok_snapshot_cron.py VAR (depoda hiç olmadı)"; else note "api/stok_snapshot_cron.py yok"; fi

section "12. MySQL/MariaDB (bağlantı kurulmaz)"
svc_state mariadb mysqld mysql
for b in /usr/sbin/mariadbd /usr/sbin/mysqld /usr/libexec/mysqld; do
  if [ -x "$b" ]; then printf '  %s --version: %s\n' "$b" "$(run "$b" --version | head -1)"; break; fi
done
if have mysql; then printf '  istemci: %s\n' "$(run mysql --version | head -1)"; fi
run rpm -qa 'MariaDB*' 'mariadb*' 'mysql*' | sort | indent
echo "  -- my.cnf ağ/karakter ayarları"
grep -rhE '^[[:space:]]*(bind-address|bind_address|port|skip-networking|socket|datadir|character-set-server|collation-server|default-time-zone|lower_case_table_names|max_connections|innodb_buffer_pool_size)[[:space:]]*=' /etc/my.cnf /etc/my.cnf.d 2>/dev/null | indent | mask
echo "  -- veri dizinindeki veritabanları (yalnız adlar)"
for d in /var/lib/mysql/*/; do
  [ -d "$d" ] || continue
  n=$(basename "$d")
  case "$n" in mysql|performance_schema|sys|'#'*) ;; *) printf '    %s\n' "$n" ;; esac
done
printf '  /var/lib/mysql boyutu: %s\n' "$(run -t 60 du -sh /var/lib/mysql | awk 'NR==1 {print $1}')"
echo "  -- kimlik dosyaları (yalnız var/yok ve izin)"; fileinfo /etc/cyberpanel/mysqlPassword /root/.my.cnf

section "13. PostgreSQL"
run rpm -qa | grep -iE '^(postgresql|libpq|pgdg|postgis|timescale)' | sort | indent
for c in psql pg_ctl postgres pg_config pg_dump; do if have "$c"; then printf '    %s: %s\n' "$c" "$(command -v "$c")"; fi; done
ls -d /usr/pgsql-* /var/lib/pgsql* 2>/dev/null | sed 's/^/    dizin: /'
run systemctl list-unit-files 'postgresql*' --no-legend --no-pager | indent
ls -1 /etc/yum.repos.d 2>/dev/null | sed 's/^/    repo dosyası: /'
grep -lis pgdg /etc/yum.repos.d/*.repo 2>/dev/null | sed 's/^/    PGDG deposu: /'
echo "  -- dnf modül akışları (önbellek)"; dnfq module list postgresql
echo "  -- dnf kullanılabilir sunucu paketleri (önbellek)"; dnfq list --available 'postgresql-server' 'postgresql1*-server'
echo "  -- dnf depo listesi (önbellek)"; dnfq repolist --all

section "14. Python ve uv"
ls -1 /usr/bin/python3* /usr/local/bin/python3* 2>/dev/null | indent
for c in python3 python3.6 python3.8 python3.9 python3.11 python3.12 python3.13; do
  if have "$c"; then printf '    %s -> %s (%s)\n' "$c" "$(command -v "$c")" "$(run "$c" -V | head -1)"; fi
done
run alternatives --display python3 | head -4 | indent
for c in uv uvx /root/.local/bin/uv /root/.cargo/bin/uv /usr/local/bin/uv /usr/bin/uv; do
  if [ -x "$c" ] || have "$c"; then printf '    %s: %s\n' "$c" "$(run "$c" --version | head -1)"; fi
done
if have pipx; then printf '    pipx: %s\n' "$(run pipx --version | head -1)"; fi
run rpm -q python3.12 python3.11 python36 python3-libs | indent
echo "  -- dnf kullanılabilir Python paketleri (önbellek)"; dnfq list --available 'python3.11' 'python3.12' 'python3.12-devel' 'python3.12-pip'

section "15. glibc, OpenSSL ve araçlar"
printf '  getconf: %s\n' "$(getconf GNU_LIBC_VERSION 2>/dev/null)"
printf '  ldd: %s\n' "$(ldd --version 2>/dev/null | head -1)"
run rpm -q glibc openssl-libs openssl ca-certificates | indent
if have openssl; then printf '  openssl: %s\n' "$(run openssl version | head -1)"; fi
for c in gcc make git curl wget tar zstd xz unzip rsync node npm pm2 docker podman nginx haproxy redis-server memcached jq; do
  if have "$c"; then v=var; else v=yok; fi
  printf '    %-12s %s\n' "$c" "$v"
done
if have node; then printf '  node: %s\n' "$(run node -v | head -1)"; fi

section "16. Ortak model (Ollama) birimleri ve kalıntıları"
for u in studio-ollama.service studio-llm.service ollama.service; do
  printf '    %s\n' "$(run systemctl show "$u" -p Id -p LoadState -p ActiveState -p UnitFileState | tr '\n' ' ')"
done
for a in studio-ollama studio-llm; do
  if id "$a" >/dev/null 2>&1; then
    printf '    hesap %s: uid=%s gid=%s kabuk=%s\n' "$a" "$(id -u "$a")" "$(id -g "$a")" "$(getent passwd "$a" | cut -d: -f7)"
  else
    printf '    hesap %s: yok\n' "$a"
  fi
done
fileinfo /opt/orkestrai-ollama /opt/orkestrai-ollama/0.40.3 /var/lib/orkestrai-ollama /var/lib/orkestrai-llm-management \
  /var/lib/orkestrai-llm-management/state.json /var/lib/orkestrai-llm-management/ready.json /var/lib/studio-llm /run/studio-llm \
  /etc/systemd/system/studio-ollama.service /etc/systemd/system/studio-llm.service
run -t 60 du -sh /opt/orkestrai-ollama /var/lib/orkestrai-ollama | grep -v '^du:' | indent
if [ -r /var/lib/orkestrai-llm-management/state.json ] && [ -n "$PYBIN" ]; then
  "$PYBIN" -I -c '
import sys, json
try:
    d = json.load(open(sys.argv[1]))
    print("    state.json: status=%s version=%s model=%s" % (d.get("status"), d.get("version"), d.get("model")))
except Exception as error:
    print("    state.json okunamadı: %s" % type(error).__name__)' /var/lib/orkestrai-llm-management/state.json
fi
run rpm -q zstd | indent

section "17. /etc/orkestrai ve sır dosyaları (yalnız ad/sahip/izin/boyut)"
if [ -d /etc/orkestrai ]; then ls -la /etc/orkestrai | indent | mask; else note "/etc/orkestrai yok"; fi
fileinfo "$SITE_HOME/.orkestrai_jwt_secret" "$SITE_HOME/.orkestrai_rpa_lisans_ed25519" /etc/orkestrai/destek.env /etc/orkestrai/studio-llm.env

section "18. TLS sertifikaları"
ls -1 /etc/letsencrypt/live 2>/dev/null | sed 's/^/    live: /'
CERT=/etc/letsencrypt/live/$SITE/fullchain.pem
if [ -r "$CERT" ] && have openssl; then
  run openssl x509 -in "$CERT" -noout -subject -issuer -enddate | indent | mask
  run openssl x509 -in "$CERT" -noout -ext subjectAltName | indent | mask
fi
if [ -d /root/.acme.sh ]; then
  printf '  acme.sh: var; alan adı dizinleri: %s\n' "$(ls -1 /root/.acme.sh 2>/dev/null | grep -E '^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(_ecc)?$' | tr '\n' ' ')"
fi

section "19. Node socket.io sunucusu (3000)"
run ss -H -ltnp | grep -E ':3000[[:space:]]' | indent | mask
if [ -d /root/.pm2 ]; then note "/root/.pm2 var (içerik okunmadı)"; fi

section "20. SSH ve hesaplar"
if have sshd; then
  run sshd -T | grep -Ei '^(port|permitrootlogin|passwordauthentication|pubkeyauthentication|kbdinteractiveauthentication|challengeresponseauthentication|maxauthtries|allowusers|allowgroups|denyusers) ' | indent | mask
fi
if [ -r /etc/ssh/ssh_host_ed25519_key.pub ]; then
  fp=$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub 2>/dev/null | awk '{print $2}')
  if [ "$fp" = "$PINNED_HOSTKEY" ]; then m=evet; else m=hayir; fi
  printf '    ed25519 sunucu anahtarı publish-rpa.py:28 sabitlemesiyle eşleşiyor: %s\n' "$m"
fi
if [ -r /root/.ssh/authorized_keys ]; then
  printf '    root authorized_keys anahtar sayısı: %s (içerik yazılmadı)\n' "$(grep -cvE '^[[:space:]]*(#|$)' /root/.ssh/authorized_keys)"
else
  note "root authorized_keys yok"
fi
echo "  -- uid 0 ve uid >= 1000 hesaplar (ad:uid:ev:kabuk)"
getent passwd | awk -F: '$3 == 0 || $3 >= 1000 {print "    " $1 ":" $3 ":" $6 ":" $7}' | head -60

section "21. Diğer servisler"
svc_state postfix dovecot pdns pure-ftpd redis memcached docker crond chronyd
echo "  -- çalışan servisler"
run systemctl list-units --type=service --state=running --no-legend --no-pager --plain | awk '{print "    " $1}' | head -80

section "22. Alan adları (IP yazılmaz)"
MYIPS=" $(hostname -I 2>/dev/null) "
PUB=hayir
for i in $MYIPS; do
  case "$i" in 10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*|127.*|*:*) ;; *) PUB=evet ;; esac
done
printf '  sunucu arayüzlerinde genel IPv4 var: %s\n' "$PUB"
for n in peyrev.com www.peyrev.com api.peyrev.com orkestrai.net; do
  r=$(getent ahostsv4 "$n" 2>/dev/null | awk '{print $1}' | sort -u)
  if [ -z "$r" ]; then printf '    %-18s çözümlenmedi\n' "$n"; continue; fi
  same=hayir
  for i in $r; do case "$MYIPS" in *" $i "*) same=evet ;; esac; done
  printf '    %-18s çözümlendi; bu sunucuyu gösteriyor: %s\n' "$n" "$same"
done

section "23. Erişim günlüğü sayaçları (IP yazılmaz)"
AL=$SITE_HOME/logs/$SITE.access_log
if [ -r "$AL" ]; then
  printf '  günlük boyutu: %s\n' "$(du -h "$AL" 2>/dev/null | awk '{print $1}')"
  for pat in '/api/rpa/lisans/' '/api/studio/ekip/' '/api/rpa/destek' '/rpa/stable.manifest' '/rpa/releases/' \
      ' /server.js ' '/kontrol%20listesi.md' ' /config ' ' /db.py ' '/.git/' '/.agents/'; do
    printf '    %-26s %s\n' "$pat" "$(grep -cF -- "$pat" "$AL" 2>/dev/null)"
  done
else
  note "$AL okunamadı"; ls -1 "$SITE_HOME/logs" 2>/dev/null | indent
fi

section "24. Dış bağlantı (yalnız HTTP durum kodu)"
if [ "${DIS_BAGLANTI:-1}" != 0 ] && have curl; then
  for u in https://github.com https://api.github.com https://pypi.org https://files.pythonhosted.org \
      https://download.postgresql.org/pub/repos/yum/ https://repo.almalinux.org https://api.telegram.org; do
    printf '    %-48s %s\n' "$u" "$(http_code "$u")"
  done
else
  note "atlandı"
fi

section "Bitti"
echo "  İnceleme tamamlandı; hiçbir ayar değiştirilmedi."
if have curl; then
  printf 'GENEL: orkestrai.net / -> %s\n' "$(http_code "https://$SITE/" --resolve "$RESOLVE")"
  printf 'GENEL: orkestrai.net /api/rpa/lisans/anahtar -> %s\n' "$(curl -sk -o /dev/null -w '%{http_code}' --max-time 8 --resolve "$RESOLVE" "https://$SITE/api/rpa/lisans/anahtar" </dev/null)"
fi
