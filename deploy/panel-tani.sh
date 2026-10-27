#!/usr/bin/env bash
# deploy/panel-tani.sh — panel girişinin CSRF denetimini SALT OKUNUR inceler (Sunucu işleri → panel-tani): aynı deneme
# isteği (var olmayan "tani.deneme" adı, şifre yok) uygulamaya doğrudan ve OpenLiteSpeed vekili üzerinden gönderilir;
# vekilin uygulamaya ilettiği istek, geri döngü arayüzünde birkaç saniye dinlenerek yalnız Origin, Referer, Host ve
# Sec-Fetch-* satırları yazdırılır. Hiçbir ayar ve servis değişmez.

set -Eeuo pipefail
export LC_ALL=C.UTF-8 LANG=C.UTF-8
PORT=5101
BODY='{"giris":"tani.deneme","sifre":"x"}'
ask() { curl -s --max-time 10 -X POST "$1" -H 'Origin: https://peyrev.com' -H 'Sec-Fetch-Site: same-origin' \
  -H 'X-Peyrev-Yonetim: 1' -H 'Content-Type: application/json' "${@:2}" -d "$BODY" </dev/null | head -c 120; echo; }

echo "-- doğrudan uygulamaya (127.0.0.1:$PORT)"
ask "http://127.0.0.1:$PORT/api/yonetim/giris" -H 'X-Forwarded-Proto: https'
echo "-- vekil üzerinden (OpenLiteSpeed) ve uygulamaya ulaşan başlıklar"
python3 - "$PORT" <<'PY' &
import socket, sys, time
port = int(sys.argv[1])
sniff = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3))
sniff.bind(("lo", 0))
sniff.settimeout(1)
end = time.time() + 6
seen = set()
while time.time() < end:
    try:
        frame = sniff.recv(65535)
    except socket.timeout:
        continue
    ip = frame[14:]
    if len(ip) < 20 or ip[9] != 6:
        continue
    tcp = ip[(ip[0] & 15) * 4:]
    if int.from_bytes(tcp[2:4], "big") != port:
        continue
    payload = tcp[(tcp[12] >> 4) * 4:]
    if b"tani.deneme" not in payload and b"/api/yonetim/giris" not in payload:
        continue
    for line in payload.split(b"\r\n"):
        name = line.split(b":", 1)[0].lower()
        if name in (b"origin", b"referer", b"host", b"sec-fetch-site", b"x-peyrev-yonetim") or line.startswith(b"POST "):
            text = "  " + repr(line.decode("latin-1"))
            if text not in seen:
                seen.add(text)
                print(text, flush=True)
PY
sleep 1
ask "https://peyrev.com/api/yonetim/giris" --resolve peyrev.com:443:127.0.0.1
wait
echo "-- vekil ayarındaki başlık yönergeleri"
grep -rn -i "origin\|extraHeaders\|setHeader\|headerOps" /usr/local/lsws/conf/vhosts/peyrev.com/ 2>/dev/null | head -20 || true
echo "GENEL: panel girişi tanılaması bitti (ayrıntılar şifreli raporda)"
