"""Ortak model çalıştırıcısı: yerel iş parçacığındaki sahte Ollama HTTP sunucusuyla uçtan uca iş, araç çağrılarının
yalnız veri olarak dönmesi, iptal, süre aşımı, yaşam işareti, tek örnek kuralı, yarım işlerin kurtarılması ve
`python -m peyrev.llm.worker` komutu."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import make_user
from test_llm import chat, payload, result, settings_text, tool, write_settings
from test_llm_api import PREFIX, FakeBackend, agent, cancel, count, enable, get, row, submit

from peyrev.clock import Clock
from peyrev.llm import jobs, settings
from peyrev.llm import worker as worker_command
from peyrev.llm.backend import OllamaBackend
from peyrev.llm.protocol import JOB_TTL, LLMError
from peyrev.llm.worker import Connection, Worker

SRC = Path(__file__).resolve().parents[1] / "src"


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # a client that gave up (timeout test) must not print a traceback


class FakeOllama:
    """Sahte Ollama HTTP API'si: istekleri kaydeder; `hold` kurulursa sohbet yanıtını o olay gelene kadar bekletir."""

    def __init__(self):
        self.requests: list[tuple[str, str, object]] = []
        self.replies = {"/api/status": {"cloud": {"disabled": True}},
                        "/api/show": {"model_info": {"general.architecture": "test"}, "capabilities": ["tools"]},
                        "/api/chat": chat()}
        self.hold: threading.Event | None = None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.reply(None)

            def do_POST(self):
                self.reply(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

            def reply(self, body):
                fake.requests.append((self.command, self.path, body))
                if self.path == "/api/chat" and fake.hold is not None:
                    fake.hold.wait(5)
                raw = json.dumps(fake.replies[self.path]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = QuietServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.closed = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        if self.hold is not None:
            self.hold.set()
        if not self.closed:
            self.closed = True
            self.server.shutdown()
            self.server.server_close()


@pytest.fixture
def ollama():
    server = FakeOllama()
    yield server
    server.close()


class BlockingBackend(FakeBackend):
    """Çıkarım `finish` gelene kadar sürer; çalıştırıcının verdiği iptal olayı `seen` içinde görülür."""

    def __init__(self):
        super().__init__()
        self.entered, self.finish, self.seen = threading.Event(), threading.Event(), []

    def generate(self, request, cancelled):
        self.seen.append(cancelled)
        self.entered.set()
        assert self.finish.wait(5)
        return result()


def test_worker_runs_api_jobs_against_local_ollama_and_returns_tool_calls_as_data(core, client, ollama):
    ollama.replies["/api/chat"] = dict(chat(), message={"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "run_workflow", "arguments": {"workflow_id": "abc"}}}]})
    config = enable(core, OLLAMA_URL=ollama.url)
    _user, _session, headers = agent(core)
    answer = client.post(PREFIX + "/jobs", json=payload(tools=[tool()]), headers=headers)
    assert answer.status_code == 202
    job_id = answer.get_json()["job"]["id"]
    assert row(core, job_id)["istek"] is not None
    runner = Worker(core.db.transaction, config, clock=core.clock)
    assert runner.run_once()
    job = client.get(PREFIX + "/jobs/" + job_id, headers=headers).get_json()["job"]
    assert job["status"] == "succeeded" and job["result"]["text"] == ""
    call = job["result"]["tool_calls"][0]
    assert (call["name"], call["arguments"], len(call["id"])) == ("run_workflow", {"workflow_id": "abc"}, 32)
    assert job["result"]["usage"] == {"input_tokens": 11, "output_tokens": 3, "total_tokens": 14}
    # Locality checks and a single inference only: the tool call is returned to Studio, never executed here.
    assert [(method, path) for method, path, _body in ollama.requests] == [
        ("GET", "/api/status"), ("POST", "/api/show"), ("POST", "/api/chat")]
    sent = ollama.requests[-1][2]
    assert sent["stream"] is False and sent["options"] == {"num_ctx": 8192, "num_thread": 4, "num_predict": 128}
    assert sent["messages"] == [{"role": "user", "content": "görev-özel-veri"}] and sent["model"] == "yerel-deneme:1"
    stored = row(core, job_id)
    assert stored["istek"] is None and stored["baslama"] is not None and stored["bitis"] is not None
    assert not runner.run_once()


def test_worker_cancels_a_running_inference_and_discards_the_late_result(core):
    config = enable(core)
    owner, other = (make_user(core, "a@x.test")["id"], "1" * 32), (make_user(core, "b@x.test")["id"], "2" * 32)
    first, second = submit(core, owner, payload(), config), submit(core, other, payload(), config)
    backend = BlockingBackend()
    thread = threading.Thread(target=Worker(core.db.transaction, config, backend=backend, clock=core.clock).run_once)
    thread.start()
    try:
        assert backend.entered.wait(5)
        assert get(core, owner, first["id"])["status"] == "running"
        assert get(core, other, second["id"])["status"] == "queued"  # a single inference at a time
        cancel(core, owner, first["id"])
        assert backend.seen[0].wait(5)  # the worker saw the cancellation and interrupts the inference
    finally:
        backend.finish.set()
        thread.join(5)
    assert not thread.is_alive()
    assert get(core, owner, first["id"]) == dict(first, status="cancelled")
    assert row(core, first["id"])["sonuc"] is None


def test_a_job_that_expires_while_running_is_interrupted(core):
    config = enable(core)
    owner = (make_user(core, "a@x.test")["id"], "1" * 32)
    job = submit(core, owner, payload(), config)
    backend = BlockingBackend()
    thread = threading.Thread(target=Worker(core.db.transaction, config, backend=backend, clock=core.clock).run_once)
    thread.start()
    try:
        assert backend.entered.wait(5)
        core.clock.advance(seconds=JOB_TTL)
        assert backend.seen[0].wait(5)
    finally:
        backend.finish.set()
        thread.join(5)
    with pytest.raises(LLMError) as error:
        get(core, owner, job["id"])
    assert error.value.kod == "LLM_IS_BULUNAMADI"
    stored = row(core, job["id"])
    assert stored is None or (stored["durum"] == "running" and stored["sonuc"] is None)


def test_backend_errors_and_invalid_usage_are_neither_stored_nor_logged(core, capsys):
    class BrokenBackend(FakeBackend):
        def generate(self, request, cancelled):
            raise RuntimeError("secret-input-test")

    class ExitingBackend(FakeBackend):
        def generate(self, request, cancelled):
            raise SystemExit("secret-input-test")  # not even this may end the thread with a traceback

    zero = {"text": "deneme", "tool_calls": [], "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
    config = enable(core)
    for index, (backend, code) in enumerate([(BrokenBackend(), "LLM_CALISMA_HATASI"),
                                             (ExitingBackend(), "LLM_CALISMA_HATASI"),
                                             (FakeBackend(zero), "LLM_KULLANIM_EKSIK")]):
        owner = (make_user(core, f"k{index}@x.test")["id"], f"{index + 1:032x}")
        job = submit(core, owner, payload(), config)
        assert Worker(core.db.transaction, config, backend=backend, clock=core.clock).run_once()
        failed = get(core, owner, job["id"])
        assert failed["status"] == "failed" and failed["error_code"] == code and "result" not in failed
        assert "secret" not in json.dumps(failed) and row(core, job["id"])["istek"] is None
    assert capsys.readouterr() == ("", "")


def test_inference_time_limit_fails_the_job_with_a_fixed_text(core, client, ollama):
    ollama.hold = threading.Event()
    config = enable(core, OLLAMA_URL=ollama.url)
    _user, _session, headers = agent(core)
    job_id = client.post(PREFIX + "/jobs", json=payload(), headers=headers).get_json()["job"]["id"]
    runner = Worker(core.db.transaction, config, backend=OllamaBackend(ollama.url, timeout=1), clock=core.clock)
    started = time.monotonic()
    assert runner.run_once()
    assert time.monotonic() - started < 4
    job = client.get(PREFIX + "/jobs/" + job_id, headers=headers).get_json()["job"]
    assert job == {"id": job_id, "status": "failed", "expires_at": job["expires_at"],
                   "error": "Model yanıt süresi doldu.", "error_code": "LLM_ZAMAN_ASIMI"}
    assert [path for _method, path, _body in ollama.requests].count("/api/chat") == 1  # never retried


def test_heartbeat_requires_a_local_ollama_with_cloud_access_disabled(core, client, ollama):
    config = enable(core, beat=False, OLLAMA_URL=ollama.url)
    _user, _session, headers = agent(core)

    def enabled():
        return client.get(PREFIX + "/models", headers=headers).get_json()["enabled"]

    runner = Worker(core.db.transaction, config, clock=core.clock)
    assert enabled() is False
    assert runner.beat() and enabled() is True
    ollama.replies["/api/status"] = {"cloud": {"disabled": False}}
    assert not runner.beat() and enabled() is False
    ollama.replies["/api/status"] = {"cloud": {"disabled": True}}
    assert runner.beat() and enabled() is True
    ollama.close()
    assert not runner.beat() and enabled() is False
    assert runner.beat(busy=True) and enabled() is True  # a running job proves the worker is alive


def test_only_one_worker_holds_the_queue(db):
    first, second = Connection(db.url), Connection(db.url)
    try:
        assert first.lock() and not second.lock()
        first.close()
        assert second.lock()
    finally:
        first.close()
        second.close()


def test_worker_start_fails_orphaned_jobs_and_purges_expired_ones(core):
    config = enable(core)
    owner, other = (make_user(core, "a@x.test")["id"], "1" * 32), (make_user(core, "b@x.test")["id"], "2" * 32)
    orphan, waiting = submit(core, owner, payload(), config), submit(core, other, payload(), config)
    with core.db.transaction() as cur:
        assert jobs.claim(cur, core.clock.now())[0] == orphan["id"]  # a worker died after taking it
    with core.db.transaction() as cur:
        assert jobs.recover(cur, core.clock.now()) == 1
    assert get(core, owner, orphan["id"]) == dict(orphan, status="failed", error="Model işi tamamlanamadı.",
                                                  error_code="LLM_CALISMA_HATASI")
    assert get(core, other, waiting["id"])["status"] == "queued"  # waiting jobs survive a restart
    core.clock.advance(seconds=JOB_TTL)
    Worker(core.db.transaction, config, backend=FakeBackend(), clock=core.clock).maintain()
    assert count(core) == 0


def test_worker_loads_only_the_standard_library_and_psycopg():
    code = ("import json, sys, peyrev.llm.worker\n"
            "names = {name.split('.')[0] for name in sys.modules} - set(sys.stdlib_module_names)\n"
            "names = [name for name in names if not name.startswith('_') and name != 'cython_runtime']\n"
            "print(json.dumps(sorted(names)))")
    output = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, PYTHONPATH=str(SRC)),
                            capture_output=True, text=True, check=True).stdout
    assert set(json.loads(output)) <= {"peyrev", "psycopg", "psycopg_binary", "psycopg_c", "typing_extensions"}


def test_worker_command_serves_jobs_and_stops_cleanly(core, tmp_path, ollama):
    secrets = tmp_path / "gizli"
    secrets.mkdir(mode=0o700)
    config = settings.load(write_settings(secrets / "llm.env", settings_text(OLLAMA_URL=ollama.url)))
    owner = (make_user(core, "a@x.test")["id"], "1" * 32)
    real = Clock()  # the command runs on the real clock
    with core.db.transaction() as cur:
        job = jobs.submit(cur, real.now(), owner, payload(), config)
    environment = dict(os.environ, PYTHONPATH=str(SRC), PEYREV_DATABASE_URL=core.db.url,
                       PEYREV_SECRETS_DIR=str(secrets))
    process = subprocess.Popen([sys.executable, "-B", "-m", "peyrev.llm.worker"], cwd=tmp_path, env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline, state, ready = time.monotonic() + 20, None, False
        while time.monotonic() < deadline and not (ready and state and state["status"] == "succeeded"):
            time.sleep(0.05)
            with core.db.transaction() as cur:
                state = jobs.get(cur, real.now(), owner, job["id"])
                ready = jobs.worker_ready(cur, real.now(), config.fingerprint)
        assert state == dict(job, status="succeeded", result=result()) and ready
    finally:
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=10)
    assert process.returncode == 0 and err == ""
    assert out.splitlines() == ["Ortak model çalıştırıcısı başladı.", "Ortak model çalıştırıcısı durdu."]
    with core.db.transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM llm_calistirici")
        assert cur.fetchone()["n"] == 0


def test_worker_refuses_to_start_without_trusted_settings_or_twice(core, tmp_path, monkeypatch, capsys):
    secrets = tmp_path / "gizli"
    secrets.mkdir(mode=0o700)
    monkeypatch.setenv("PEYREV_DATABASE_URL", core.db.url)
    monkeypatch.setenv("PEYREV_SECRETS_DIR", str(secrets))
    off = "Ortak model kapalı: ayar dosyası yok, güvenilir değil ya da geçersiz; çalıştırıcı başlatılmadı.\n"
    assert worker_command.main() == 1 and capsys.readouterr().out == off
    write_settings(secrets / "llm.env", settings_text(), mode=0o644)
    assert worker_command.main() == 1 and capsys.readouterr().out == off
    (secrets / "llm.env").chmod(0o600)
    holder = Connection(core.db.url)
    try:
        assert holder.lock()
        assert worker_command.main() == 1 and capsys.readouterr().out == "Model çalışanı zaten açık.\n"
    finally:
        holder.close()
    monkeypatch.setenv("PEYREV_DATABASE_URL", "host=127.0.0.1 port=1 dbname=yok user=gizli-hesap connect_timeout=2")
    assert worker_command.main() == 1
    printed = capsys.readouterr()
    assert printed.out == ("Ortak model çalıştırıcısı başlatılamadı ya da durdu; ayar dosyasını ve veritabanını"
                           " denetleyin.\n") and "gizli" not in printed.out + printed.err
