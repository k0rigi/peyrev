"""Ortak model çalıştırıcısı: `python -m peyrev.llm.worker` (ayrı systemd birimi; Gunicorn modeli beklemez).

- Tek örnek: PostgreSQL oturum kilidi alınır; ikinci örnek "Model çalışanı zaten açık." yazıp 1 ile çıkar.
- Tek çıkarım (parallel 1): sıradaki işi alır, Ollama'ya gönderir, sonucu doğrulayıp yazar. Modelin araç çağrıları
  Studio'ya yalnız veri olarak döner; çalıştırıcı hiçbir aracı çalıştırmaz.
- Çalışan işin iptali ve süresi 0,25 sn'de bir denetlenir; iptal edilen ya da süresi dolan işin çıkarımı kesilir, geç
  gelen sonucu atılır. Çıkarımın toplam süresi 90 sn'dir.
- Saniyede bir süresi dolan işleri siler. 2 sn'de bir yaşam işareti yazar; boştayken önce Ollama'yı yoklar (bulut
  erişimi kapalı yerel hizmet), yanıt yoksa işareti kaldırır ve API özelliği kapalı gösterir.
- Başlarken yarım kalan (running) işleri başarısız sayar; çıkarım kendiliğinden tekrarlanmaz.
- Yalnız standart kütüphane ve psycopg kullanır. Çıktıya yalnız sabit satırlar yazar; istek, yanıt, ayar değeri ve
  istisna metni yazılmaz.

Ortam: PEYREV_DATABASE_URL ya da /etc/peyrev/db.env (DATABASE_URL); ayar dosyası PEYREV_SECRETS_DIR/llm.env
(varsayılan /etc/peyrev/llm.env). Ayar kapalıysa ya da güvenilir değilse başlamaz (çıkış kodu 1). Ayar değişince
çalıştırıcı yeniden başlatılmalıdır; o zamana kadar ayar izleri tutmadığından API özelliği kapalı gösterir.
"""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

import psycopg
from psycopg.rows import dict_row

from ..clock import Clock
from ..config import Settings
from . import jobs, settings
from .backend import OllamaBackend
from .protocol import REQUEST_LIMIT, LLMError, loads, validate_request, validate_result

WORKER_LOCK = "peyrev-llm-calistirici"
POLL_SECONDS = 0.25  # çalışan işin iptal ve süre denetimi
IDLE_SECONDS = 0.5  # boş kuyrukta bekleme
PURGE_SECONDS = 1
BEAT_SECONDS = 2
PROBE_SECONDS = 2  # boştayken Ollama durum yoklamasının süresi


class Connection:
    """Çalıştırıcının tek veritabanı bağlantısı (havuz yok); oturum kilidi bağlantı kapanınca kalkar."""

    def __init__(self, url: str):
        self.raw = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        self.raw.execute("SET TIME ZONE 'UTC'")

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Cursor]:
        with self.raw.transaction(), self.raw.cursor() as cursor:
            yield cursor

    def lock(self) -> bool:
        row = self.raw.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS alindi",
                               (WORKER_LOCK,)).fetchone()
        return bool(row["alindi"])

    def close(self) -> None:
        self.raw.close()


class Worker:
    """İşleri sırayla yürütür. `transaction`: db.transaction() gibi imleç veren işlem bloğu (testte havuz, üretimde
    tek bağlantı); `backend`: OllamaBackend ya da testte aynı arayüzü sunan taklit."""

    def __init__(self, transaction: Callable[[], AbstractContextManager], config: settings.LLMConfig, *,
                 backend=None, clock: Clock | None = None):
        self.transaction = transaction
        self.config = config
        self.backend = backend or OllamaBackend(config.ollama_url, context_tokens=config.context_tokens)
        self.clock = clock or Clock()
        self.started = self.clock.now()
        self.stopping = threading.Event()
        self._purge_at = self._beat_at = 0.0

    def serve(self) -> None:
        while not self.stopping.is_set():
            self.maintain()
            if not self.run_once():
                self.stopping.wait(IDLE_SECONDS)

    def maintain(self, busy: bool = False) -> None:
        moment = time.monotonic()
        if moment >= self._purge_at:
            self._purge_at = moment + PURGE_SECONDS
            with self.transaction() as cur:
                jobs.purge(cur, self.clock.now())
        if moment >= self._beat_at:
            self._beat_at = moment + BEAT_SECONDS
            self.beat(busy)

    def beat(self, busy: bool = False) -> bool:
        """Yaşam işareti. Çalışan iş canlılığı zaten gösterir; boştayken Ollama hazır değilse işaret kaldırılır."""
        ready = busy or self.ollama_ready()
        with self.transaction() as cur:
            if ready:
                jobs.heartbeat(cur, self.clock.now(), self.config.fingerprint, self.started)
            else:
                jobs.withdraw(cur)
        return ready

    def ollama_ready(self) -> bool:
        try:
            self.backend.check_cloud(threading.Event(), time.monotonic() + PROBE_SECONDS)
        except LLMError:
            return False
        return True

    def run_once(self) -> bool:
        """Sıradaki işi alıp bitirir; kuyruk boşsa False."""
        with self.transaction() as cur:
            claimed = jobs.claim(cur, self.clock.now())
        if claimed is None:
            return False
        job_id, raw = claimed
        result, error = self._execute(job_id, raw)
        with self.transaction() as cur:
            jobs.finish(cur, self.clock.now(), job_id, result=result, error=error)
        return True

    def _execute(self, job_id: str, raw: bytes) -> tuple[dict | None, str | None]:
        cancelled, outcome = threading.Event(), {}

        def infer():
            try:
                request = validate_request(loads(raw, REQUEST_LIMIT), self.config.models,
                                           self.config.max_output_tokens)
                outcome["result"] = validate_result(self.backend.generate(request, cancelled))
            except LLMError as error:
                outcome["error"] = error.kod
            except BaseException:  # noqa: BLE001 - provider data must never reach a thread traceback or the log
                outcome["error"] = "LLM_CALISMA_HATASI"

        thread = threading.Thread(target=infer, daemon=True)
        thread.start()
        while True:
            thread.join(POLL_SECONDS)
            if not thread.is_alive():
                break
            if self.stopping.is_set():
                cancelled.set()
                return None, "LLM_CALISMA_HATASI"
            self.maintain(busy=True)
            with self.transaction() as cur:
                if not jobs.still_running(cur, self.clock.now(), job_id):
                    cancelled.set()
        if "result" in outcome:
            return outcome["result"], None
        return None, outcome.get("error", "LLM_CALISMA_HATASI")


def main() -> int:
    connection = None
    try:
        environment = Settings.from_environment()
        config = settings.load(environment.llm_settings_path)
        if not config.enabled:
            print("Ortak model kapalı: ayar dosyası yok, güvenilir değil ya da geçersiz; çalıştırıcı başlatılmadı.",
                  flush=True)
            return 1
        connection = Connection(environment.database_url)
        if not connection.lock():
            print("Model çalışanı zaten açık.", flush=True)
            return 1
        worker = Worker(connection.transaction, config)
        with connection.transaction() as cur:
            jobs.withdraw(cur)
            jobs.recover(cur, worker.clock.now())
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: worker.stopping.set())
        print("Ortak model çalıştırıcısı başladı.", flush=True)
        worker.serve()
        with connection.transaction() as cur:
            jobs.withdraw(cur)
        print("Ortak model çalıştırıcısı durdu.", flush=True)
        return 0
    except Exception:  # noqa: BLE001 - connection strings, settings and provider errors stay out of the log
        print("Ortak model çalıştırıcısı başlatılamadı ya da durdu; ayar dosyasını ve veritabanını denetleyin.",
              flush=True)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
