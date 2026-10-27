#!/usr/bin/env bash
# deploy/destek-tani.sh — destek e-postasının neden gönderilemediğini SALT OKUNUR inceler (Sunucu işleri → destek-tani):
# destek.env'deki SMTP şifresinin biçimi (uzunluk, tırnak, boşluk; değer yazılmaz), aynı bilgilerle SMTP ve IMAP girişi,
# CyberPanel'deki kutu kaydı ve posta günlüğündeki son kimlik doğrulama satırları. Hiçbir ayar değişmez.

set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8
REL=$(readlink -f /opt/peyrev/current)
echo "-- destek.env biçimi ve giriş denemeleri"
PYTHONPATH="$REL/src" "$REL/venv/bin/python" -B - <<'PY'
import imaplib, smtplib, ssl
from peyrev.support import notify
s = notify.read_settings("/etc/peyrev/destek.env", owner=0)
pw = s.get("DESTEK_SMTP_PASSWORD", "")
print("  anahtarlar:", sorted(s))
print("  kullanıcı:", s.get("DESTEK_SMTP_USER"), "| sunucu:", s.get("DESTEK_SMTP_HOST"), "| port:", s.get("DESTEK_SMTP_PORT"))
print("  şifre: uzunluk", len(pw), "| tırnak var:", any(c in pw for c in "\"'"), "| boşluk var:", pw != pw.strip(),
      "| harf+rakam dışı:", any(not c.isalnum() for c in pw))
for name, attempt in (("smtp", lambda: smtplib.SMTP_SSL(s["DESTEK_SMTP_HOST"], 465, timeout=15, context=ssl.create_default_context())),):
    try:
        connection = attempt(); connection.login(s["DESTEK_SMTP_USER"], pw); connection.quit(); print("  smtp girişi: başarılı")
    except Exception as error:
        print("  smtp girişi:", type(error).__name__, getattr(error, "smtp_code", ""))
try:
    imap = imaplib.IMAP4_SSL(s["DESTEK_SMTP_HOST"], 993, ssl_context=ssl.create_default_context(), timeout=15)
    imap.login(s["DESTEK_SMTP_USER"], pw); imap.logout(); print("  imap girişi: başarılı")
except Exception as error:
    print("  imap girişi:", type(error).__name__)
PY
echo "-- CyberPanel kutu kaydı (şifre özetinin yalnız biçimi)"
mysql --defaults-file=/root/.my.cnf -N -B cyberpanel -e "SELECT email, LEFT(password, 12), LENGTH(password), mail FROM e_users WHERE email='info@peyrev.com'" | sed 's/^/  /'
echo "-- posta günlüğü (son kimlik doğrulama satırları)"
for f in /var/log/maillog /var/log/mail.log /var/log/dovecot.log; do
  [ -f "$f" ] && grep -E "info@peyrev.com|authentication fail|auth failed|SASL" "$f" | tail -8 | sed 's/^/  /'
done
echo "-- cyberpanel aracında e-posta şifresi komutu"
cyberpanel -h 2>&1 | grep -i -o "[a-zA-Z]*[Ee]mail[a-zA-Z]*" | sort -u | sed 's/^/  /'
echo "GENEL: destek e-postası tanılaması bitti (ayrıntılar şifreli raporda)"
