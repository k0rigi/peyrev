"""Ortak model iş kuyruğu (PostgreSQL): Studio'nun gönderim, durum ve iptal işlemleri ile çalıştırıcının adımları.

Kaynak orkestrai `api/ajanlar/llm_queue.py` (işler orada worker belleğindeydi). Gunicorn birden çok süreçte
çalıştığından kuyruk `llm_isler` tablosundadır; davranış aynıdır, firma kotası kullanıcıya döner:
- Sahip (kullanici_id, oturum_id) ikilisidir; başka sahibin işi 404 alır (403 değil).
- Tekrar koruması: aynı sahip ve request_id aynı içerikle (kanonik JSON özeti) aynı işi döndürür, kuyruk doluyken bile;
  farklı içerik 409 alır.
- Kotalar (biri dolarsa 429): bekleyen iş en çok 4, süresi dolmamış iş en çok 128, kullanıcı başına bekleyen + çalışan
  en çok 2, oturum başına 1. Gönderimler kuyruk kilidiyle sıraya girer; kotalar aynı işlemde denetlenir.
- Çalıştırıcı sıradaki işi (FIFO) `FOR UPDATE SKIP LOCKED` ile alır; istek içeriği o anda silinir. İptalde istek ve
  sonuç silinir; geç gelen sonuç atılır.
- Her iş gönderimden 300 sn sonra, durumu ne olursa olsun silinir (tekrar anahtarı dahil). Süresi dolan satır hiçbir
  sorguda görünmez; API her istekte, çalıştırıcı saniyede bir siler.

Yalnız psycopg imleci kullanır (çalıştırıcı da yükler); zamanlar çağıranın saatindendir (UTC).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Any

from .protocol import (
    FAILURES,
    JOB_LIMIT,
    JOB_TTL,
    NOT_FOUND,
    QUEUE_FULL,
    QUEUE_LIMIT,
    REQUEST_LIMIT,
    RESULT_LIMIT,
    USER_LIMIT,
    LLMError,
    dumps,
    loads,
    validate_request,
    validate_result,
)

QUEUE_LOCK = "peyrev-llm-kuyruk"
# Çalıştırıcı 2 sn'de bir yaşam işareti yazar; bundan eski işaret "çalışmıyor" sayılır.
WORKER_STALE = timedelta(seconds=15)


def _lock(cur) -> None:
    # Same as peyrev.db.advisory_lock, inlined so that the worker does not load the connection pool package.
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (QUEUE_LOCK,))


def purge(cur, now: datetime) -> int:
    """Süresi dolan işleri içerik ve tekrar anahtarıyla birlikte siler; kuyruk kilidi işlem sonuna kadar sürer."""
    _lock(cur)
    cur.execute("DELETE FROM llm_isler WHERE gecerlilik_sonu <= %s", (now,))
    return cur.rowcount


def public(row: dict[str, Any]) -> dict[str, Any]:
    """Studio'nun iş nesnesi: result yalnız succeeded, error/error_code yalnız failed durumunda."""
    job = {"id": row["id"], "status": row["durum"], "expires_at": row["gecerlilik_sonu"].isoformat()}
    if row["durum"] == "succeeded":
        job["result"] = validate_result(loads(bytes(row["sonuc"]), RESULT_LIMIT))
    elif row["durum"] == "failed":
        code = row["hata_kodu"]
        job["error"] = FAILURES.get(code, "Model işi tamamlanamadı.")
        job["error_code"] = code if code in FAILURES else "LLM_CALISMA_HATASI"
    return job


def submit(cur, now: datetime, owner: tuple[int, str], payload: Any, config) -> dict[str, Any]:
    """İsteği doğrular ve sıraya koyar; `config` settings.LLMConfig (izinli modeller ve çıktı sınırı)."""
    user_id, session_id = owner
    normalized = validate_request(payload, config.models, config.max_output_tokens)
    raw = dumps(normalized, REQUEST_LIMIT)
    digest = hashlib.sha256(raw).hexdigest()
    purge(cur, now)
    cur.execute("SELECT * FROM llm_isler WHERE kullanici_id = %s AND oturum_id = %s AND istek_id = %s",
                (user_id, session_id, normalized["request_id"]))
    existing = cur.fetchone()
    if existing is not None:
        if existing["ozet"] != digest:
            raise LLMError("Model istek kimliği farklı içerikle kullanılmış.", 409, "LLM_ISTEK_CAKISMASI")
        return public(existing)
    cur.execute("SELECT count(*) FILTER (WHERE durum = 'queued') AS bekleyen, count(*) AS toplam,"
                " count(*) FILTER (WHERE durum IN ('queued', 'running') AND kullanici_id = %s) AS kullanici,"
                " count(*) FILTER (WHERE durum IN ('queued', 'running') AND kullanici_id = %s AND oturum_id = %s)"
                " AS oturum FROM llm_isler", (user_id, user_id, session_id))
    counts = cur.fetchone()
    if (counts["bekleyen"] >= QUEUE_LIMIT or counts["toplam"] >= JOB_LIMIT or counts["kullanici"] >= USER_LIMIT
            or counts["oturum"]):
        raise LLMError(QUEUE_FULL, 429, "LLM_KUYRUK_DOLU")
    cur.execute("INSERT INTO llm_isler (id, kullanici_id, oturum_id, istek_id, ozet, istek, olusturma, gecerlilik_sonu)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (uuid.uuid4().hex, user_id, session_id, normalized["request_id"], digest, raw, now,
                 now + timedelta(seconds=JOB_TTL)))
    return public(cur.fetchone())


def _owned(cur, now: datetime, owner: tuple[int, str], job_id: str, *, lock: bool = False) -> dict[str, Any]:
    cur.execute("SELECT * FROM llm_isler WHERE id = %s AND kullanici_id = %s AND oturum_id = %s"
                " AND gecerlilik_sonu > %s" + (" FOR UPDATE" if lock else ""), (job_id, *owner, now))
    row = cur.fetchone()
    if row is None:
        raise LLMError(NOT_FOUND, 404, "LLM_IS_BULUNAMADI")
    return row


def get(cur, now: datetime, owner: tuple[int, str], job_id: str) -> dict[str, Any]:
    return public(_owned(cur, now, owner, job_id))


def cancel(cur, now: datetime, owner: tuple[int, str], job_id: str) -> dict[str, Any]:
    """Durum cancelled olur, istek ve sonuç silinir; çalışan çıkarımı çalıştırıcı keser. Tekrarı aynı nesneyi verir."""
    row = _owned(cur, now, owner, job_id, lock=True)
    if row["durum"] != "cancelled":
        cur.execute("UPDATE llm_isler SET durum = 'cancelled', istek = NULL, sonuc = NULL, hata_kodu = NULL, bitis = %s"
                    " WHERE id = %s RETURNING *", (now, job_id))
        row = cur.fetchone()
    return public(row)


def claim(cur, now: datetime) -> tuple[str, bytes] | None:
    """Çalıştırıcı: sıradaki işi alır; durum running olur ve istek içeriği tablodan silinir."""
    cur.execute("SELECT id, istek FROM llm_isler WHERE durum = 'queued' AND gecerlilik_sonu > %s"
                " ORDER BY sira LIMIT 1 FOR UPDATE SKIP LOCKED", (now,))
    row = cur.fetchone()
    if row is None:
        return None
    cur.execute("UPDATE llm_isler SET durum = 'running', istek = NULL, baslama = %s WHERE id = %s", (now, row["id"]))
    return row["id"], bytes(row["istek"])


def still_running(cur, now: datetime, job_id: str) -> bool:
    cur.execute("SELECT 1 FROM llm_isler WHERE id = %s AND durum = 'running' AND gecerlilik_sonu > %s", (job_id, now))
    return cur.fetchone() is not None


def finish(cur, now: datetime, job_id: str, *, result: dict | None = None, error: str | None = None) -> bool:
    """Sonucu yalnız hâlâ çalışan işe yazar; iptal edilen ya da süresi dolan işin geç gelen sonucu atılır."""
    sonuc = None if error else dumps(result, RESULT_LIMIT)
    cur.execute("UPDATE llm_isler SET durum = %s, sonuc = %s, hata_kodu = %s, bitis = %s"
                " WHERE id = %s AND durum = 'running' AND gecerlilik_sonu > %s",
                ("failed" if error else "succeeded", sonuc, error, now, job_id, now))
    return cur.rowcount == 1


def recover(cur, now: datetime) -> int:
    """Önceki çalıştırıcıdan yarım kalan işler başarısız sayılır; çıkarım hiçbir zaman kendiliğinden tekrarlanmaz."""
    cur.execute("UPDATE llm_isler SET durum = 'failed', hata_kodu = 'LLM_CALISMA_HATASI', bitis = %s"
                " WHERE durum = 'running'", (now,))
    return cur.rowcount


def heartbeat(cur, now: datetime, fingerprint: str, started: datetime) -> None:
    cur.execute("INSERT INTO llm_calistirici (id, ayar_izi, baslama, son_sinyal) VALUES (1, %s, %s, %s)"
                " ON CONFLICT (id) DO UPDATE SET ayar_izi = EXCLUDED.ayar_izi, baslama = EXCLUDED.baslama,"
                " son_sinyal = EXCLUDED.son_sinyal", (fingerprint, started, now))


def withdraw(cur) -> None:
    cur.execute("DELETE FROM llm_calistirici")


def worker_ready(cur, now: datetime, fingerprint: str) -> bool:
    """Çalıştırıcı aynı ayarla çalışıyor ve yaşam işareti taze (orkestrai'de soket + ready.json kapısı)."""
    cur.execute("SELECT 1 FROM llm_calistirici WHERE id = 1 AND ayar_izi = %s AND son_sinyal > %s",
                (fingerprint, now - WORKER_STALE))
    return cur.fetchone() is not None
