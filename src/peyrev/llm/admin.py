"""Yönetim paneli için ortak model işlemleri (uç değil, servis fonksiyonu). İstek ve yanıt içeriği hiçbir zaman dönmez.

status(core) -> dict
    {"enabled": bool,                       Studio'nun gördüğü durum (ayar geçerli ve çalıştırıcı aynı ayarla çalışıyor)
     "ayar": {"dosya": bool, "gecerli": bool, "modeller": [...], "context_tokens": int, "max_output_tokens": int,
              "ollama_url": str},
     "calistirici": {"calisiyor": bool, "ayar_uyumlu": bool, "baslama": iso | None, "son_sinyal": iso | None},
     "isler": {"queued": n, "running": n, "succeeded": n, "failed": n, "cancelled": n}}   süresi dolmamış işler
clear_queue(core, yonetici_id) -> int
    Bekleyen ve çalışan bütün işleri iptal eder (istek ve sonuç silinir, çalışan çıkarım kesilir), yonetim_olaylari'na
    tur='llm_kuyruk_bosaltma', hedef='llm_isler', ayrinti={"adet": n} yazar ve iptal edilen iş sayısını döndürür.
"""

from __future__ import annotations

import os
from typing import Any

from psycopg.types.json import Jsonb

from . import jobs, settings

STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")


def status(core) -> dict[str, Any]:
    path = core.settings.llm_settings_path
    config = settings.load(path)
    now = core.clock.now()
    with core.db.transaction() as cur:
        cur.execute("SELECT ayar_izi, baslama, son_sinyal FROM llm_calistirici WHERE id = 1")
        worker = cur.fetchone()
        enabled = config.enabled and jobs.worker_ready(cur, now, config.fingerprint)
        cur.execute("SELECT durum, count(*) AS adet FROM llm_isler WHERE gecerlilik_sonu > %s GROUP BY durum", (now,))
        counts = {row["durum"]: row["adet"] for row in cur.fetchall()}
    return {"enabled": enabled,
            "ayar": {"dosya": os.path.lexists(path), "gecerli": config.enabled, "modeller": list(config.models),
                     "context_tokens": config.context_tokens, "max_output_tokens": config.max_output_tokens,
                     "ollama_url": config.ollama_url},
            "calistirici": {"calisiyor": worker is not None and worker["son_sinyal"] > now - jobs.WORKER_STALE,
                            "ayar_uyumlu": worker is not None and worker["ayar_izi"] == config.fingerprint,
                            "baslama": worker["baslama"].isoformat() if worker else None,
                            "son_sinyal": worker["son_sinyal"].isoformat() if worker else None},
            "isler": {name: counts.get(name, 0) for name in STATUSES}}


def clear_queue(core, yonetici_id: int) -> int:
    now = core.clock.now()
    with core.db.transaction() as cur:
        jobs.purge(cur, now)
        cur.execute("UPDATE llm_isler SET durum = 'cancelled', istek = NULL, sonuc = NULL, hata_kodu = NULL, bitis = %s"
                    " WHERE durum IN ('queued', 'running')", (now,))
        count = cur.rowcount
        cur.execute("INSERT INTO yonetim_olaylari (yonetici_id, tur, hedef, ayrinti, zaman)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (yonetici_id, "llm_kuyruk_bosaltma", "llm_isler", Jsonb({"adet": count}), now))
    return count
