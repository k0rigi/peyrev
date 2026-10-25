"""Studio oturumları (`oturumlar`): hesap başına tek bilgisayar, sıra numarası ve kopya tespiti.

- Giriş yeni bir oturum açar; hesabın diğer aktif oturumları kapanır (aynı cihaz YENI_GIRIS, başka cihaz BASKA_CIHAZ).
- Yenileme belirteci oturumun sıra numarasını taşır ve her temiz yenilemede numara bir artar. Bir önceki numara, yanıtın
  Studio'ya ulaşmadığını gösterir: numarayı ilerleten Studio (`ornek`) yeniden sorarsa aynı numara verilir ve sayılmaz,
  başka bir örnekten gelirse sayılır. Daha eski ya da ileri bir numara veya biriken tekrarlar aynı oturumun birden fazla
  yerde çalıştığını gösterir: oturum KOPYA ile kapanır.
- Diğer kapanışlar: Studio'da "Oturumu kapat" CIKIS, yönetici YONETICI. Kapanmış oturumlar 90 gün sonra silinir.

Satır kilidi (FOR UPDATE) işlem sonuna kadar sürer: aynı oturumun eş zamanlı iki yenilemesi sırayla değerlendirilir.
Şifre değişikliği ve hesabın pasifleşmesi satırı kapatmaz; belirteçler şifre izi ve hesap durumu denetimiyle düşer.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg import Cursor

from ..common import LOG

CONCURRENT_SESSIONS = 1
# Başka bir örnekten gelen önceki numara bu kadar birikirse oturum kopya sayılır.
REPEAT_LIMIT = 3
KEEP_CLOSED = timedelta(days=90)
OK, CLOSED = "tamam", "kapali"
SESSION_EXPIRED = "Oturumun süresi doldu. Yeniden giriş yapın."
# Studio bu metinleri giriş ekranında gösterir; listede olmayan nedenler (YENI_GIRIS, CIKIS, YOK, UYUSMUYOR)
# SESSION_EXPIRED alır.
CLOSE_MESSAGES = {
    "BASKA_CIHAZ": ("Bu hesap başka bir bilgisayarda açıldığı için bu bilgisayardaki oturum kapatıldı. "
                    "Devam etmek için yeniden giriş yapın."),
    "KOPYA": "Bu oturum aynı anda birden fazla yerde kullanıldığı için kapatıldı. Yeniden giriş yapın.",
    "YONETICI": "Oturumunuz yönetici tarafından kapatıldı. Yeniden giriş yapın.",
}


def close(cur: Cursor, session_id: str, reason: str, moment: datetime) -> None:
    cur.execute("UPDATE oturumlar SET durum = 'kapandi', kapanma_nedeni = %s, son_gorulme = %s "
                "WHERE id = %s AND durum = 'aktif'", (reason, moment, session_id))


def start(cur: Cursor, user: dict[str, Any], device: str, version: Any, address: str, moment: datetime, *,
          limit: int = CONCURRENT_SESSIONS, instance: str | None = None) -> tuple[str, int]:
    """Girişte yeni oturum (numara 0). Çağıran, kullanıcının giriş kilidini (danışma kilidi) önceden alır."""
    cur.execute("SELECT id, cihaz FROM oturumlar WHERE kullanici_id = %s AND durum = 'aktif' "
                "ORDER BY son_gorulme DESC, id FOR UPDATE", (user["id"],))
    room = max(int(limit), 1) - 1
    for row in cur.fetchall():
        if row["cihaz"] != device and room > 0:
            room -= 1
            continue
        close(cur, row["id"], "YENI_GIRIS" if row["cihaz"] == device else "BASKA_CIHAZ", moment)
    session_id = secrets.token_hex(16)
    cur.execute("INSERT INTO oturumlar (id, kullanici_id, cihaz, sira, ornek, surum, ip, olusturma, son_gorulme) "
                "VALUES (%s, %s, %s, 0, %s, %s, %s, %s, %s)",
                (session_id, user["id"], device, instance, str(version or "")[:20], str(address or "")[:64], moment,
                 moment))
    _forget_closed(cur, moment)
    return session_id, 0


def _forget_closed(cur: Cursor, moment: datetime) -> None:
    try:
        with cur.connection.transaction():
            cur.execute("DELETE FROM oturumlar WHERE durum = 'kapandi' AND son_gorulme < %s", (moment - KEEP_CLOSED,))
    except psycopg.IntegrityError:
        # Another table still refers to an old session: sign-in never fails over housekeeping.
        LOG.warning("Kapanmış eski Studio oturumları silinemedi; başka bir kayıt hâlâ bağlı.")


def renew(cur: Cursor, session_id: str, number: Any, user_id: int, device: str, version: Any, address: str,
          moment: datetime, *, instance: str | None = None) -> tuple[str, Any]:
    """("tamam", yeni belirtecin numarası) ya da ("kapali", neden)."""
    cur.execute("SELECT * FROM oturumlar WHERE id = %s FOR UPDATE", (session_id,))
    row = cur.fetchone()
    if row is None or row["durum"] != "aktif":
        return CLOSED, (row or {}).get("kapanma_nedeni") or "YOK"
    if row["kullanici_id"] != user_id or row["cihaz"] != device or type(number) is not int:
        return CLOSED, "UYUSMUYOR"  # a wrong request never closes the valid session
    current = row["sira"]
    version, address = str(version or "")[:20], str(address or "")[:64]
    if number == current:
        # Without a counted repeat since the last clean renewal the counter resets: one-off problems never add up.
        repeats = row["tekrar"] if row["tekrar_var"] else 0
        cur.execute("UPDATE oturumlar SET sira = %s, ornek = %s, tekrar = %s, tekrar_var = false, son_gorulme = %s, "
                    "surum = %s, ip = %s WHERE id = %s",
                    (current + 1, instance, repeats, moment, version, address, session_id))
        return OK, current + 1
    if number == current - 1:
        if instance and row["ornek"] == instance:
            # The Studio that moved the number on never got the answer and asks again: not counted, no time limit.
            cur.execute("UPDATE oturumlar SET son_gorulme = %s WHERE id = %s", (moment, session_id))
            return OK, current
        repeats = row["tekrar"] + 1
        if repeats < REPEAT_LIMIT:
            cur.execute("UPDATE oturumlar SET ornek = %s, tekrar = %s, tekrar_var = true, son_gorulme = %s, surum = %s, "
                        "ip = %s WHERE id = %s", (instance, repeats, moment, version, address, session_id))
            return OK, current
    close(cur, session_id, "KOPYA", moment)
    return CLOSED, "KOPYA"


def end(cur: Cursor, session_id: str, user_id: int, moment: datetime) -> None:
    """Studio'da "Oturumu kapat": yalnız hesabın kendi oturumu kapanır."""
    cur.execute("UPDATE oturumlar SET durum = 'kapandi', kapanma_nedeni = 'CIKIS', son_gorulme = %s "
                "WHERE id = %s AND kullanici_id = %s AND durum = 'aktif'", (moment, session_id, user_id))


def close_all(cur: Cursor, user_id: int, reason: str, moment: datetime, session_id: str | None = None) -> int:
    """Hesabın aktif oturumlarını (ya da yalnız birini) kapatır; kapanan sayısı."""
    only = " AND id = %s" if session_id is not None else ""
    cur.execute("UPDATE oturumlar SET durum = 'kapandi', kapanma_nedeni = %s, son_gorulme = %s "
                "WHERE kullanici_id = %s AND durum = 'aktif'" + only,
                (reason, moment, user_id) + ((session_id,) if session_id is not None else ()))
    return cur.rowcount
