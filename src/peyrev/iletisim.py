"""İletişim formu: POST /api/iletisim (peyrev.com/iletisim/, oturumsuz).

Mesaj iletisim_mesajlari'na yazılır ve yöneticiye bildirilir (Telegram, e-posta); bildirimde mesajın kendisi yoktur, ad,
e-posta ve konu vardır, mesaj panelde okunur. Gönderen adrese otomatik e-posta gitmez: form başkası adına e-posta
göndermenin aracı olamaz. Bal küpü alanı doluysa aynı yanıt verilir, bir şey yazılmaz. Adres başına saatte ve günde,
hizmetin tamamında günde sınır vardır (hesap_izleri, tür "iletisim"). 365 günden eski mesajlar silinir.
"""

from __future__ import annotations

import re
from datetime import timedelta

from flask import Blueprint
from psycopg import Cursor

from .accounts import rules
from .common import ApiError, client_ip, json_body, ok
from .support import notify

TOPICS = {"satis": "Satış ve fiyat", "teknik": "Teknik soru", "diger": "Diğer"}
PER_HOUR, PER_DAY, TOTAL_PER_DAY = 3, 10, 200
KEEP = timedelta(days=365)
RECEIVED = "Mesajınız bize ulaştı. Yazdığınız e-posta adresine en kısa sürede yanıt veririz."


def message_text(value: object) -> str:
    """Satırlar korunur (en çok bir boş satır); satır içi boşluklar teke iner, denetim karakteri reddedilir
    (panel mesajı metin olarak gösterir)."""
    if not isinstance(value, str) or not 10 <= len(value.strip()) <= 3000:
        raise ApiError("Mesaj 10-3000 karakter olmalıdır.")
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(" ".join(line.split()) for line in value.strip().splitlines()).strip())
    if any(ord(char) < 32 and char != "\n" for char in text):
        raise ApiError("Mesaj geçersiz karakter içeriyor.")
    return text


def _store(core, cur: Cursor, values: dict, address: str) -> int:
    moment, key = core.clock.now(), "adres:" + address
    if (rules.count(cur, "iletisim", key, moment - timedelta(hours=1)) >= PER_HOUR
            or rules.count(cur, "iletisim", key, moment - timedelta(days=1)) >= PER_DAY):
        raise ApiError("Bu bağlantıdan çok fazla mesaj gönderildi. Daha sonra tekrar deneyin ya da info@peyrev.com "
                       "adresine yazın.", 429, "COK_DENEME")
    cur.execute("SELECT count(*) AS n FROM hesap_izleri WHERE tur = 'iletisim' AND zaman > %s", (moment - timedelta(days=1),))
    if cur.fetchone()["n"] >= TOTAL_PER_DAY:
        raise ApiError("Mesajlar şu anda alınamıyor. Lütfen info@peyrev.com adresine yazın.", 503, "YOGUNLUK")
    rules.trace(cur, "iletisim", key, moment)
    rules.forget_old_traces(cur, moment)
    cur.execute("DELETE FROM iletisim_mesajlari WHERE olusturma < %s", (moment - KEEP,))
    cur.execute("INSERT INTO iletisim_mesajlari (ad, eposta, konu, mesaj, ip, olusturma) VALUES (%(ad)s, %(eposta)s, "
                "%(konu)s, %(mesaj)s, %(ip)s, %(an)s) RETURNING id", {**values, "ip": address, "an": moment})
    return cur.fetchone()["id"]


def create_blueprint(core) -> Blueprint:
    blueprint = Blueprint("iletisim", __name__)

    @blueprint.post("/api/iletisim")
    def send():
        data = json_body({"ad", "eposta", "konu", "mesaj", "web_sitesi"}, {"ad", "eposta", "konu", "mesaj"},
                         limit=32 * 1024)
        if data.get("web_sitesi"):
            return ok(201, message=RECEIVED)  # a filled hidden field is a bot: same answer, nothing stored
        topic = data["konu"]
        if not isinstance(topic, str) or topic not in TOPICS:
            raise ApiError("Konu seçin.")
        values = {"ad": rules.text(data["ad"], "Ad", 2, 120), "eposta": rules.email(data["eposta"]), "konu": topic,
                  "mesaj": message_text(data["mesaj"])}
        with core.db.transaction() as cur:
            number = _store(core, cur, values, client_ip())
        notify.deliver(core, "İletişim formu: " + TOPICS[topic], f"{values['ad']} · {values['eposta']}\n"
                       f"Mesaj #{number}: {core.settings.public_base.rstrip('/')}/yonetim/#iletisim")
        return ok(201, message=RECEIVED)

    return blueprint
