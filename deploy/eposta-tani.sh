#!/usr/bin/env bash
# deploy/eposta-tani.sh — müşteriye giden e-postanın (indirme bağlantısı) neden ulaşmadığını SALT OKUNUR inceler
# (Sunucu işleri → eposta-tani): son başvuru onayları ve indirme bağlantısı olaylarının e-posta sonucu, uygulama
# günlüğündeki e-posta uyarıları, Postfix günlüğündeki son teslim satırları, kuyruk ve DKIM imzalayıcısı. Hiçbir ayar
# ve kayıt değişmez; bağlantı anahtarları yazdırılmaz (olaylarda yalnız sonuç vardır).

set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8
REL=$(readlink -f /opt/peyrev/current)
echo "-- son onay ve indirme bağlantısı olayları"
runuser -u peyrev -- env PYTHONPATH="$REL/src" PEYREV_SECRETS_DIR=/etc/peyrev "$REL/venv/bin/python" - <<'PY'
from peyrev.config import Settings
from peyrev.db import Database

database = Database(Settings.from_environment().database_url, max_size=1)
with database.transaction() as cur:
    cur.execute("SET TRANSACTION READ ONLY")
    cur.execute("SELECT zaman, tur, hedef, ayrinti FROM yonetim_olaylari WHERE tur IN "
                "('basvuru_onaylandi', 'indirme_baglantisi') ORDER BY id DESC LIMIT 12")
    rows = cur.fetchall()
database.close()
for row in rows:
    detail = {key: value for key, value in (row["ayrinti"] or {}).items() if key in ("eposta", "paket", "giris")}
    print(f"  {row['zaman']:%Y-%m-%d %H:%M:%S} {row['tur']} {row['hedef']} {detail}")
PY
echo "-- uygulama günlüğü (son 6 saat, e-posta satırları)"
journalctl -u peyrev.service --since "-6h" --no-pager 2>/dev/null \
  | grep -iE "e-posta|eposta|smtp|indirme bağlantısı|bildirim" | tail -20 | sed 's/^/  /' || true
echo "-- posta günlüğü (son teslim satırları)"
for f in /var/log/maillog /var/log/mail.log; do
  [ -f "$f" ] || continue
  grep -E "status=|DKIM-Signature|opendkim|reject|milter" "$f" | tail -30 | sed 's/^/  /'
done
echo "-- kuyruk"
postqueue -p 2>/dev/null | tail -15 | sed 's/^/  /' || true
echo "-- imzalayıcı"
systemctl is-active opendkim 2>/dev/null | sed 's/^/  opendkim: /' || echo "  opendkim: yok"
postconf -h smtpd_milters non_smtpd_milters 2>/dev/null | sed 's/^/  postfix milter: /'
echo "GENEL: e-posta gönderim tanılaması bitti (ayrıntılar şifreli raporda)"
