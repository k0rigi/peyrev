"""Yönetim olayları (yonetim_olaylari): panelin kendi kayıtları ve Olaylar bölümünün sayfalı listesi.

Satırları hesap, destek, ajan ve ortak model modülleri de yazar; burada yalnız panelin kendi türleri yazılır. Ayrıntıda
şifre, özet, kod, gizli anahtar ve yanıt metni bulunmaz.
"""

from __future__ import annotations

import re
from typing import Any

from psycopg import Cursor
from psycopg.types.json import Jsonb

from ..accounts.rules import stamp
from ..common import ApiError

PAGE_SIZE = 50
TARGET = re.compile(r"[a-z_]{1,30}(?::[0-9A-Za-z_-]{1,64})?")
# Bilinen türlerin panelde görünen adları; listede olmayan tür (ör. taşıma aracının kaydı) olduğu gibi gösterilir.
TYPES = {
    "yonetim_girisi": "Panele giriş",
    "yonetim_cikisi": "Panelden çıkış",
    "yonetim_oturumlari_kapatildi": "Panel oturumları kapatıldı",
    "iki_adim_acildi": "İki adımlı doğrulama açıldı",
    "iki_adim_kapatildi": "İki adımlı doğrulama kapatıldı",
    "iki_adim_sifirlandi": "İki adımlı doğrulama sıfırlandı",
    "kullanici_acildi": "Hesap açıldı",
    "bilgiler_degisti": "Hesap bilgileri değişti",
    "rol_degisti": "Rol değişti",
    "durum_degisti": "Hesap durumu değişti",
    "modul_ayarlandi": "Modül hakkı verildi veya değişti",
    "modul_kaldirildi": "Modül hakkı kaldırıldı",
    "sifre_kodu_uretildi": "Şifre kodu üretildi",
    "sifre_belirlendi": "Şifre yönetimden belirlendi",
    "sifre_sunucuda_yenilendi": "Yönetici şifresi sunucuda yenilendi",
    "oturumlar_kapatildi": "Studio oturumları kapatıldı",
    "basvuru_onaylandi": "Başvuru onaylandı",
    "basvuru_reddedildi": "Başvuru reddedildi",
    "uzatma_istegi": "Lisans uzatma isteği (form)",
    "destek_yanit": "Destek talebi yanıtlandı",
    "destek_durum": "Destek talebinin durumu değişti",
    "ajan_ofisi_durumu": "Ajan ofisinin durumu değişti",
    "ajan_ofisi_acildi": "Müşteri ofisi panelden açıldı",
    "ofis_siniri": "Ofis sınırı değişti",
    "llm_kuyruk_bosaltma": "Ortak model kuyruğu boşaltıldı",
    "egitim_eklendi": "Eğitim videosu eklendi",
    "egitim_degisti": "Eğitim videosu değişti",
    "egitim_silindi": "Eğitim videosu silindi",
    "indirme_baglantisi": "İndirme bağlantısı oluşturuldu",
    "kullanici_silindi": "Hesap silindi",
    "topluluk_konu": "Topluluk konusu düzenlendi",
    "topluluk_konu_silindi": "Topluluk konusu silindi",
    "topluluk_yorum": "Topluluk yorumu gizlendi veya açıldı",
    "topluluk_yorum_silindi": "Topluluk yorumu silindi",
    "iletisim_silindi": "İletişim mesajı silindi",
    "robot_komutu": "Robot Paneli komutu",
    "robot_komutu_uye": "Robot Paneli komutu (Hesabım)",
}


def record(core, cur: Cursor, admin_id: int | None, kind: str, target: str, **details: Any) -> None:
    cur.execute("INSERT INTO yonetim_olaylari (yonetici_id, tur, hedef, ayrinti, zaman) VALUES (%s, %s, %s, %s, %s)",
                (admin_id, kind, target, Jsonb(details), core.clock.now()))


def listing(cur: Cursor, *, page: int, kind: str | None = None, target: str | None = None) -> dict[str, Any]:
    """Yeniden eskiye, sayfa başına 50 olay; `kind` tür, `target` hedef ("kullanici:12", "talep:1001") süzgecidir."""
    if kind is not None and (not isinstance(kind, str) or not re.fullmatch(r"[a-z_]{1,40}", kind)):
        raise ApiError("Olay türü geçersiz.")
    if target is not None and (not isinstance(target, str) or not TARGET.fullmatch(target)):
        raise ApiError("Olay hedefi geçersiz.")
    where, values = [], []
    if kind:
        where.append("o.tur = %s")
        values.append(kind)
    if target:
        where.append("o.hedef = %s")
        values.append(target)
    condition = (" WHERE " + " AND ".join(where)) if where else ""
    cur.execute("SELECT count(*) AS n FROM yonetim_olaylari o" + condition, values)
    total = cur.fetchone()["n"]
    cur.execute("SELECT o.id, o.yonetici_id, o.tur, o.hedef, o.ayrinti, o.zaman, k.giris, k.ad_soyad "
                "FROM yonetim_olaylari o LEFT JOIN kullanicilar k ON k.id = o.yonetici_id" + condition
                + " ORDER BY o.zaman DESC, o.id DESC LIMIT %s OFFSET %s", values + [PAGE_SIZE, (page - 1) * PAGE_SIZE])
    rows = [{"id": row["id"], "zaman": stamp(row["zaman"]), "tur": row["tur"], "tur_adi": TYPES.get(row["tur"], row["tur"]),
             "hedef": row["hedef"], "ayrinti": row["ayrinti"],
             "yonetici": None if row["yonetici_id"] is None else {
                 "kullanici_id": row["yonetici_id"], "giris": row["giris"], "ad_soyad": row["ad_soyad"]}}
            for row in cur.fetchall()]
    return {"olaylar": rows, "toplam": total, "sayfa": page, "sayfa_boyu": PAGE_SIZE, "turler": dict(TYPES)}
