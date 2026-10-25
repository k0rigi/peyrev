"""Ortak model uçları ve PostgreSQL kuyruğu: denetim sırası, kapalı durum, ayar ve çalıştırıcı kapısı, sahiplik,
tekrar koruması, kotalar (kullanıcı başına), iptal, saklama süresi, sabit hata metinleri ve yönetim işlemleri."""

from __future__ import annotations

import copy
import json
import threading
from datetime import timedelta

import pytest
from conftest import DEVICE, START, make_user, open_session
from test_llm import MODEL, payload, result, settings_text, tool, write_settings

from peyrev import tokens, users
from peyrev.llm import admin, jobs, settings
from peyrev.llm.protocol import JOB_LIMIT, JOB_TTL, REQUEST_LIMIT, LLMError, validate_request
from peyrev.llm.worker import Worker

PREFIX = "/api/studio/ekip/llm"
OFF = {"success": True, "models": [], "enabled": False, "queue_limit": 4, "parallel": 1, "context_tokens": 8192,
       "max_output_tokens": 1024, "reserve_tokens": 512, "estimation": "utf8_bytes"}


class FakeBackend:
    """Ollama yerine: isteği kaydeder ve sabit sonucu döndürür."""

    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer or result()

    def generate(self, request, cancelled):
        self.calls.append(copy.deepcopy(request))
        assert not cancelled.is_set()
        return copy.deepcopy(self.answer)

    def check_cloud(self, cancelled, deadline):
        pass


def agent(core, giris="ajan@x.test", *, session_id=None, user=None):
    """Ajan lisanslı kullanıcı ve Studio oturumu: (kullanıcı, oturum, istek başlıkları)."""
    user = user or make_user(core, giris, modules={users.RPA: None, users.AGENTS: None})
    session = open_session(core, user, session_id=session_id)
    return user, session, {"Authorization": "Bearer " + tokens.issue(core, "ajan", user, DEVICE, session)}


def enable(core, *, beat=True, **values) -> settings.LLMConfig:
    """Geçerli ayar dosyası ve (istenirse) aynı ayarla çalışan bir çalıştırıcının taze işareti."""
    write_settings(core.settings.llm_settings_path, settings_text(**values))
    config = settings.load(core.settings.llm_settings_path)
    assert config.enabled
    if beat:
        heartbeat(core, config)
    return config


def heartbeat(core, config):
    with core.db.transaction() as cur:
        jobs.heartbeat(cur, core.clock.now(), config.fingerprint, core.clock.now())


def submit(core, owner, request, config):
    with core.db.transaction() as cur:
        return jobs.submit(cur, core.clock.now(), owner, request, config)


def get(core, owner, job_id):
    with core.db.transaction() as cur:
        return jobs.get(cur, core.clock.now(), owner, job_id)


def cancel(core, owner, job_id):
    with core.db.transaction() as cur:
        return jobs.cancel(cur, core.clock.now(), owner, job_id)


def row(core, job_id):
    with core.db.transaction() as cur:
        cur.execute("SELECT * FROM llm_isler WHERE id = %s", (job_id,))
        return cur.fetchone()


def count(core, where="true"):
    with core.db.transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM llm_isler WHERE " + where)
        return cur.fetchone()["n"]


def test_feature_is_off_by_default_and_checks_keep_their_order(core, client):
    _user, _session, headers = agent(core)
    # Size and identity-in-query checks come before authentication.
    big = client.post(PREFIX + "/jobs", data=b" " * (128 * 1024 + 1), content_type="application/json")
    assert big.status_code == 413 and big.get_json()["kod"] == "GOVDE_SINIRI"
    query = client.get(PREFIX + "/models?firma_id=8")
    assert query.status_code == 400 and query.get_json() == {
        "success": False, "kod": "GECERSIZ_GIRDI", "message": "Kimlik yalnız Studio oturumundan alınır."}
    assert client.get(PREFIX + "/models?user_id=8", headers=headers).status_code == 400
    anonymous = client.get(PREFIX + "/models")
    assert anonymous.status_code == 401 and anonymous.get_json()["kod"] == "OTURUM_GEREKLI"
    office = client.get(PREFIX + "/models", headers=dict(headers, **{"X-Rpa-Ofis": "ofis"}))
    assert office.status_code == 400 and office.get_json()["kod"] == "GECERSIZ_GIRDI"
    models = client.get(PREFIX + "/models", headers=headers)
    assert models.status_code == 200 and models.get_json() == OFF and models.headers["Cache-Control"] == "no-store"

    def post(**options):
        return client.post(PREFIX + "/jobs", headers=headers, **options)

    # On submit the body checks come before the feature check; on GET/DELETE the feature check precedes the id.
    cases = [(post(data="x", content_type="text/plain"), 400, "LLM_GECERSIZ_GIRDI"),
             (post(data=b" " * (REQUEST_LIMIT + 1), content_type="application/json"), 413, "LLM_BOYUT_SINIRI"),
             (post(data=b'{"a":1,"a":2}', content_type="application/json"), 400, "LLM_GECERSIZ_GIRDI"),
             (post(json=payload()), 503, "LLM_KAPALI"), (post(json={"bilinmeyen": 1}), 503, "LLM_KAPALI"),
             (client.get(PREFIX + "/jobs/" + "a" * 32, headers=headers), 503, "LLM_KAPALI"),
             (client.get(PREFIX + "/jobs/gecersiz", headers=headers), 503, "LLM_KAPALI"),
             (client.delete(PREFIX + "/jobs/" + "a" * 32, headers=headers), 503, "LLM_KAPALI")]
    for answer, status, kod in cases:
        assert (answer.status_code, answer.get_json()["kod"]) == (status, kod)
        assert answer.headers["Cache-Control"] == "no-store"
    assert post(json=payload()).get_json() == {"success": False, "kod": "LLM_KAPALI", "message":
                                               "Ortak model hizmeti henüz etkin değil veya şu anda kullanılamıyor."}
    assert client.put(PREFIX + "/jobs", headers=headers).status_code == 405


def test_feature_follows_the_settings_file_and_the_worker_on_every_request(core, client):
    _user, _session, headers = agent(core)

    def models():
        return client.get(PREFIX + "/models", headers=headers).get_json()

    config = enable(core, beat=False, LLM_CONTEXT_TOKENS="16384", LLM_MAX_OUTPUT_TOKENS="2048")
    assert models() == OFF  # valid settings, but no worker
    heartbeat(core, config)
    assert models() == dict(OFF, models=[MODEL], enabled=True, context_tokens=16384, max_output_tokens=2048)
    core.clock.advance(seconds=16)
    assert models()["enabled"] is False  # stale heartbeat
    heartbeat(core, config)
    assert models()["enabled"] is True
    path = core.settings.llm_settings_path
    write_settings(path, path.read_text(encoding="utf-8").replace("2048", "2047"))
    assert models() == OFF  # the worker still runs with the previous settings
    heartbeat(core, settings.load(path))
    assert models()["enabled"] is True
    path.chmod(0o644)
    assert models() == OFF
    path.unlink()
    assert models() == OFF
    assert client.post(PREFIX + "/jobs", json=payload(), headers=headers).status_code == 503


def test_job_round_trip_ownership_size_limit_and_revocation(core, client):
    user, _session, headers = agent(core)
    enable(core)
    answer = client.post(PREFIX + "/jobs", json=payload(), headers=headers)
    assert answer.status_code == 202 and answer.headers["Cache-Control"] == "no-store"
    job = answer.get_json()["job"]
    assert job == {"id": job["id"], "status": "queued", "expires_at": (START + timedelta(seconds=JOB_TTL)).isoformat()}
    assert "görev-özel-veri" not in answer.get_data(as_text=True)
    _other, _session, outsider = agent(core, "baska@x.test")
    _same, _session, second_session = agent(core, session_id="b" * 32, user=user)
    for headers_of in (outsider, second_session):
        missing = client.get(PREFIX + "/jobs/" + job["id"], headers=headers_of)
        assert missing.status_code == 404 and missing.get_json() == {
            "success": False, "kod": "LLM_IS_BULUNAMADI", "message": "Model işi bulunamadı veya saklama süresi doldu."}
        assert client.delete(PREFIX + "/jobs/" + job["id"], headers=headers_of).status_code == 404
    assert client.get(PREFIX + "/jobs/" + job["id"], headers=headers).get_json() == {"success": True, "job": job}
    too_big = client.post(PREFIX + "/jobs", data=b" " * (REQUEST_LIMIT + 1), content_type="application/json",
                          headers=headers)
    assert too_big.status_code == 413 and too_big.get_json()["kod"] == "LLM_BOYUT_SINIRI"
    cancelled = client.delete(PREFIX + "/jobs/" + job["id"], headers=headers)
    assert cancelled.status_code == 200 and cancelled.get_json()["job"] == dict(job, status="cancelled")
    again = client.delete(PREFIX + "/jobs/" + job["id"], headers=headers)
    assert again.get_json()["job"] == dict(job, status="cancelled")
    with core.db.transaction() as cur:
        cur.execute("DELETE FROM kullanici_modulleri WHERE kullanici_id = %s AND modul = %s",
                    (user["id"], users.AGENTS))
    revoked = client.get(PREFIX + "/jobs/" + job["id"], headers=headers)
    assert revoked.status_code == 403 and revoked.get_json()["kod"] == "AJAN_YETKISI_YOK"
    with core.db.transaction() as cur:
        cur.execute("UPDATE oturumlar SET durum = 'kapandi'")
    assert client.get(PREFIX + "/models", headers=second_session).status_code == 401


def test_request_errors_keep_their_codes_and_nothing_is_queued(core, client):
    _user, _session, headers = agent(core)
    enable(core)
    unmatched = payload(tools=[tool()], messages=[
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "run_workflow", "arguments": {}}]},
        {"role": "tool", "content": "{}", "tool_call_id": "c2", "name": "run_workflow"}])
    cases = [(payload(model="baska:1"), 400, "LLM_MODEL_IZNI", "Seçilen model ortak sunucuda izinli değil."),
             (payload(max_tokens=1025), 400, "LLM_GECERSIZ_GIRDI", "Model isteği geçersiz."),
             (dict(payload(), fazla=True), 400, "LLM_GECERSIZ_GIRDI", "Model isteği geçersiz."),
             (unmatched, 400, "LLM_GECERSIZ_GIRDI", "Araç sonucu çağrı kimliğiyle eşleşmiyor."),
             (payload(messages=[{"role": "user", "content": "\ud800"}]), 400, "LLM_GECERSIZ_GIRDI",
              "Model isteği geçersiz.")]
    for body, status, kod, message in cases:
        # json.dumps escapes the lone surrogate; Studio's canonical JSON could send the same escape.
        answer = client.post(PREFIX + "/jobs", data=json.dumps(body), content_type="application/json", headers=headers)
        assert (answer.status_code, answer.get_json()) == (status, {"success": False, "kod": kod, "message": message})
    for job_id in ("gecersiz", "A" * 32, "a" * 31):
        assert client.get(PREFIX + "/jobs/" + job_id, headers=headers).get_json()["kod"] == "LLM_GECERSIZ_GIRDI"
    assert client.get(PREFIX + "/jobs/" + "a" * 32, headers=headers).status_code == 404
    assert count(core) == 0


def test_queue_idempotence_bound_to_payload_and_scoped_to_session(core):
    config = enable(core)
    user, other = make_user(core, "a@x.test"), make_user(core, "b@x.test")
    owner = (user["id"], "1" * 32)
    request = payload()
    first = submit(core, owner, request, config)
    assert submit(core, owner, copy.deepcopy(request), config) == first
    with pytest.raises(LLMError) as error:
        submit(core, owner, dict(request, max_tokens=256), config)
    assert (error.value.status, error.value.kod) == (409, "LLM_ISTEK_CAKISMASI")
    for outsider in ((other["id"], "1" * 32), (user["id"], "f" * 32)):
        for operation in (get, cancel):
            with pytest.raises(LLMError) as error:
                operation(core, outsider, first["id"])
            assert (error.value.status, error.value.kod) == (404, "LLM_IS_BULUNAMADI")
    backend = FakeBackend()
    assert Worker(core.db.transaction, config, backend=backend, clock=core.clock).run_once()
    complete = get(core, owner, first["id"])
    assert complete == dict(first, status="succeeded", result=result())
    complete["result"]["text"] = "caller modification"
    assert get(core, owner, first["id"])["result"]["text"] == "Tamamlandı."
    assert backend.calls == [validate_request(request, (MODEL,))]
    assert submit(core, owner, request, config)["status"] == "succeeded"  # a repeat after the result is the same job


def test_queue_enforces_session_user_and_global_quotas(core):
    config = enable(core)
    people = [make_user(core, f"k{index}@x.test") for index in range(4)]
    first = (people[0]["id"], "1" * 32)
    submit(core, first, payload(), config)
    with pytest.raises(LLMError) as error:
        submit(core, first, payload(), config)  # one pending job per session
    assert (error.value.status, error.value.kod, str(error.value)) == (
        429, "LLM_KUYRUK_DOLU", "Ortak model kuyruğu dolu; mevcut işin bitmesini bekleyin.")
    submit(core, (people[0]["id"], "2" * 32), payload(), config)
    with pytest.raises(LLMError):
        submit(core, (people[0]["id"], "3" * 32), payload(), config)  # two pending jobs per user
    submit(core, (people[1]["id"], "4" * 32), payload(), config)
    submit(core, (people[2]["id"], "5" * 32), payload(), config)
    with pytest.raises(LLMError):
        submit(core, (people[3]["id"], "6" * 32), payload(), config)  # four waiting jobs in all
    assert count(core, "durum = 'queued'") == 4
    with core.db.transaction() as cur:
        assert jobs.claim(cur, core.clock.now()) is not None
    # The running job leaves its waiting place: one running and four waiting.
    submit(core, (people[3]["id"], "6" * 32), payload(), config)
    assert count(core, "durum = 'queued'") == 4


def test_finished_jobs_count_towards_the_total_until_they_expire(core):
    config = enable(core)
    user = make_user(core, "a@x.test")
    now = core.clock.now()
    with core.db.transaction() as cur:
        for index in range(JOB_LIMIT):
            cur.execute("INSERT INTO llm_isler (id, kullanici_id, oturum_id, istek_id, ozet, durum, olusturma,"
                        " gecerlilik_sonu) VALUES (%s, %s, %s, %s, %s, 'cancelled', %s, %s)",
                        (f"{index:032x}", user["id"], "1" * 32, f"{index:032x}", "0" * 64, now,
                         now + timedelta(seconds=JOB_TTL)))
    with pytest.raises(LLMError) as error:
        submit(core, (user["id"], "2" * 32), payload(), config)
    assert error.value.kod == "LLM_KUYRUK_DOLU"
    core.clock.advance(seconds=JOB_TTL)
    assert submit(core, (user["id"], "2" * 32), payload(), config)["status"] == "queued"
    assert count(core) == 1


def test_parallel_submits_from_several_api_processes_keep_the_queue_limit(core):
    config = enable(core)
    people = [make_user(core, f"p{index}@x.test") for index in range(8)]
    barrier, outcomes = threading.Barrier(len(people)), []

    def send(person):
        barrier.wait()
        try:
            outcomes.append(submit(core, (person["id"], f"{person['id']:032x}"), payload(), config)["status"])
        except LLMError as error:
            outcomes.append(error.kod)

    threads = [threading.Thread(target=send, args=(person,)) for person in people]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert sorted(outcomes) == ["LLM_KUYRUK_DOLU"] * 4 + ["queued"] * 4
    assert count(core, "durum = 'queued'") == 4


def test_ttl_removes_prompts_results_and_dedup_keys_even_without_a_worker(core, client):
    _user, _session, headers = agent(core)
    config = enable(core)
    request = payload()
    first = client.post(PREFIX + "/jobs", json=request, headers=headers).get_json()["job"]
    core.clock.advance(seconds=JOB_TTL)
    heartbeat(core, config)
    missing = client.get(PREFIX + "/jobs/" + first["id"], headers=headers)
    assert missing.status_code == 404 and count(core) == 0  # the request itself removed the row
    again = client.post(PREFIX + "/jobs", json=request, headers=headers)
    assert again.status_code == 202 and again.get_json()["job"]["id"] != first["id"]
    # With the feature off (no settings), any authenticated request still removes expired content.
    core.settings.llm_settings_path.unlink()
    core.clock.advance(seconds=JOB_TTL)
    assert client.get(PREFIX + "/models", headers=headers).get_json() == OFF
    assert count(core) == 0


def test_cancel_queued_job_never_reaches_backend(core):
    config = enable(core)
    owner = (make_user(core, "a@x.test")["id"], "1" * 32)
    job = submit(core, owner, payload(), config)
    cancelled = cancel(core, owner, job["id"])
    assert cancelled == dict(job, status="cancelled")
    assert cancel(core, owner, job["id"]) == cancelled
    backend = FakeBackend()
    assert not Worker(core.db.transaction, config, backend=backend, clock=core.clock).run_once()
    assert backend.calls == []
    stored = row(core, job["id"])
    assert stored["istek"] is None and stored["sonuc"] is None and stored["durum"] == "cancelled"


def test_cancel_removes_a_finished_result(core):
    config = enable(core)
    owner = (make_user(core, "a@x.test")["id"], "1" * 32)
    job = submit(core, owner, payload(), config)
    Worker(core.db.transaction, config, backend=FakeBackend(), clock=core.clock).run_once()
    assert row(core, job["id"])["sonuc"] is not None
    assert cancel(core, owner, job["id"]) == dict(job, status="cancelled")
    assert row(core, job["id"])["sonuc"] is None


@pytest.mark.parametrize("code,text,public_code", [
    ("LLM_ZAMAN_ASIMI", "Model yanıt süresi doldu.", "LLM_ZAMAN_ASIMI"),
    ("LLM_BAGLAM_SINIRI", "İstek modelin bağlam sınırını aşıyor; konuşmayı kısaltın.", "LLM_BAGLAM_SINIRI"),
    ("LLM_YANIT_GECERSIZ", "Model yanıtı doğrulanamadı.", "LLM_YANIT_GECERSIZ"),
    ("LLM_YEREL_GEREKLI", "Ortak modelin yerel kurulumu ve kapalı bulut erişimi doğrulanamadı.", "LLM_YEREL_GEREKLI"),
    ("LLM_ARAC_DESTEGI", "Seçilen yerel model araç çağrılarını desteklemiyor.", "LLM_ARAC_DESTEGI"),
    ("LLM_KULLANIM_EKSIK", "Model kullanım bilgisi eksik.", "LLM_KULLANIM_EKSIK"),
    ("LLM_BOYUT_SINIRI", "Model içeriği izin verilen boyutu aşıyor.", "LLM_BOYUT_SINIRI"),
    ("LLM_ERISIM", "Yerel model hizmeti yanıt vermedi.", "LLM_ERISIM"),
    ("LLM_IPTAL", "Model işi tamamlanamadı.", "LLM_CALISMA_HATASI"),
    ("LLM_GECERSIZ_GIRDI", "Model işi tamamlanamadı.", "LLM_CALISMA_HATASI")])
def test_failed_jobs_carry_only_fixed_texts(core, code, text, public_code):
    config = enable(core)
    owner = (make_user(core, "a@x.test")["id"], "1" * 32)
    job = submit(core, owner, payload(), config)
    with core.db.transaction() as cur:
        jobs.claim(cur, core.clock.now())
        assert jobs.finish(cur, core.clock.now(), job["id"], error=code)
    assert get(core, owner, job["id"]) == dict(job, status="failed", error=text, error_code=public_code)


def test_admin_status_and_queue_clear_are_logged_without_content(core):
    assert admin.status(core) == {
        "enabled": False,
        "ayar": {"dosya": False, "gecerli": False, "modeller": [], "context_tokens": 8192, "max_output_tokens": 1024,
                 "ollama_url": "http://127.0.0.1:11434"},
        "calistirici": {"calisiyor": False, "ayar_uyumlu": False, "baslama": None, "son_sinyal": None},
        "isler": {"queued": 0, "running": 0, "succeeded": 0, "failed": 0, "cancelled": 0}}
    config = enable(core)
    manager = make_user(core, "yonetici@x.test", rol="yonetici")
    for index in range(3):
        submit(core, (make_user(core, f"k{index}@x.test")["id"], f"{index + 1:032x}"), payload(), config)
    with core.db.transaction() as cur:
        jobs.claim(cur, core.clock.now())
    state = admin.status(core)
    assert state["enabled"] is True and state["ayar"]["dosya"] is True and state["ayar"]["modeller"] == [MODEL]
    assert state["calistirici"] == {"calisiyor": True, "ayar_uyumlu": True, "baslama": START.isoformat(),
                                    "son_sinyal": START.isoformat()}
    assert state["isler"] == {"queued": 2, "running": 1, "succeeded": 0, "failed": 0, "cancelled": 0}
    assert admin.clear_queue(core, manager["id"]) == 3
    assert admin.status(core)["isler"]["cancelled"] == 3
    assert count(core, "istek IS NOT NULL OR sonuc IS NOT NULL") == 0
    with core.db.transaction() as cur:
        cur.execute("SELECT yonetici_id, tur, hedef, ayrinti, zaman FROM yonetim_olaylari")
        assert cur.fetchall() == [{"yonetici_id": manager["id"], "tur": "llm_kuyruk_bosaltma", "hedef": "llm_isler",
                                   "ayrinti": {"adet": 3}, "zaman": START}]
    core.settings.llm_settings_path.chmod(0o644)
    state = admin.status(core)
    assert state["enabled"] is False and state["ayar"]["dosya"] is True and state["ayar"]["gecerli"] is False
