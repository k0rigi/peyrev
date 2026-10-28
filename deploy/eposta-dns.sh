#!/usr/bin/env bash
# deploy/eposta-dns.sh — peyrev.com'dan giden e-postanın kimlik kayıtlarını SALT OKUNUR inceler (Sunucu işleri →
# eposta-dns): Postfix'in DKIM imzalayıcısı (OpenDKIM milter) açık mı, peyrev.com için DKIM anahtarı var mı ve DNS'e
# girilecek açık anahtar kaydı nedir. Gizli anahtar yazdırılmaz; açık anahtar zaten DNS'te yayımlanacak bir değerdir.

set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8
DOMAIN=peyrev.com
echo "-- imzalayıcı"
systemctl is-active opendkim 2>/dev/null | sed 's/^/  opendkim: /' || echo "  opendkim: yok"
postconf -h smtpd_milters non_smtpd_milters 2>/dev/null | sed 's/^/  postfix milter: /'
echo "-- peyrev.com anahtarları"
found=0
for dir in /etc/opendkim/keys/$DOMAIN /etc/opendkim/keys /etc/dkim /var/lib/opendkim; do
  [ -d "$dir" ] || continue
  for txt in "$dir"/*.txt; do
    [ -f "$txt" ] || continue
    case "$txt" in *"$DOMAIN"*|"/etc/opendkim/keys/$DOMAIN/"*) ;; *) continue ;; esac
    found=1
    echo "  dosya: $txt"
    # The .txt file is the public DNS record (selector._domainkey IN TXT "v=DKIM1; k=rsa; p=...").
    tr -d '\n\t"' < "$txt" | sed 's/  */ /g; s/^/  kayıt: /'; echo
  done
done
[ "$found" = 1 ] || echo "  peyrev.com için DKIM anahtar dosyası bulunamadı"
for table in /etc/opendkim/KeyTable /etc/opendkim/SigningTable; do
  [ -f "$table" ] && grep -n "$DOMAIN" "$table" | sed "s|^|  ${table##*/}: |" || true
done
echo "-- DNS'te şu an"
for name in "$DOMAIN" "_dmarc.$DOMAIN" "default._domainkey.$DOMAIN"; do
  printf '  %s TXT: %s\n' "$name" "$(dig +short TXT "$name" 2>/dev/null | tr '\n' ' ')"
done
echo "GENEL: e-posta DNS incelemesi bitti (ayrıntılar şifreli raporda)"
