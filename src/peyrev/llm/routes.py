"""Studio'nun ortak model uçları (/api/studio/ekip/llm/*); sözleşme orkestrai `api/ajanlar/llm_routes.py` ile aynıdır.

İstek öncesi sıra ajan uçlarıyla aynıdır: gövde 128 KiB'den büyükse 413 GOVDE_SINIRI; sorguda kimlik alanı varsa
400 GECERSIZ_GIRDI; ardından ajan kimliği (Authorization: Bearer <ajan_erisim>, isteğe bağlı X-Rpa-Ofis). Etkin ajan
yetkisi yeterlidir, ayrıca agents.* yetkisi aranmaz.
- GET  /llm/models           200; kapalıyken enabled:false, models:[] ve varsayılan sınırlar.
- POST /llm/jobs             202 {job}. Önce gövde (JSON türü değilse 400, 64 KiB'den büyükse 413, bozuksa 400),
                             sonra kapalılık (503 LLM_KAPALI), sonra istek doğrulaması ve kuyruk kuralları.
- GET|DELETE /llm/jobs/<id>  200 {job}; kapalılık iş kimliği denetiminden önce gelir.
Özellik ancak ayar dosyası geçerliyse (settings.py) ve çalıştırıcı aynı ayarla çalışıyorsa açıktır.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, g, request

from ..agents import entitlement
from ..common import ApiError, fail, ok
from . import jobs, settings
from .protocol import QUEUE_LIMIT, REQUEST_LIMIT, RESERVE_TOKENS, LLMError, identifier, loads, too_large, unavailable

BODY_LIMIT = 128 * 1024
OFFICE_HEADER = "X-Rpa-Ofis"
IDENTITY_ARGS = ("firma_id", "company_id", "kullanici_id", "user_id")


def configuration(config: settings.LLMConfig | None) -> dict[str, Any]:
    """GET /llm/models gövdesi; `config` None ise özellik kapalıdır."""
    shown = config or settings.LLMConfig()
    return {"models": list(shown.models), "enabled": config is not None, "queue_limit": QUEUE_LIMIT, "parallel": 1,
            "context_tokens": shown.context_tokens, "max_output_tokens": shown.max_output_tokens,
            "reserve_tokens": RESERVE_TOKENS, "estimation": "utf8_bytes"}


def create_blueprint(core) -> Blueprint:
    blueprint = Blueprint("studio_llm", __name__, url_prefix="/api/studio/ekip")

    def ready(cur) -> settings.LLMConfig | None:
        config = settings.load(core.settings.llm_settings_path)
        if config.enabled and jobs.worker_ready(cur, core.clock.now(), config.fingerprint):
            return config
        return None

    def owner() -> tuple[int, str]:
        identity = g.llm_identity
        return int(identity.user["id"]), identifier(identity.claims["oturum"])

    @blueprint.before_request
    def authenticate():
        if request.content_length is not None and request.content_length > BODY_LIMIT:
            raise ApiError("İstek gövdesi çok büyük.", 413, "GOVDE_SINIRI")
        # Identity comes only from the Studio session, never from the query string.
        if any(field in request.args for field in IDENTITY_ARGS):
            raise ApiError("Kimlik yalnız Studio oturumundan alınır.", 400, "GECERSIZ_GIRDI")
        with core.db.transaction() as cur:
            g.llm_identity = entitlement.authenticate(core, cur, request.headers.get("Authorization"),
                                                      request.headers.get(OFFICE_HEADER))
            # Every request removes expired jobs, so content never outlives 300 s even while no worker runs.
            jobs.purge(cur, core.clock.now())

    @blueprint.errorhandler(LLMError)
    def llm_error(error: LLMError):
        return fail(ApiError(str(error), error.status, error.kod))

    @blueprint.get("/llm/models")
    def models():
        with core.db.transaction() as cur:
            config = ready(cur)
        return ok(**configuration(config))

    @blueprint.post("/llm/jobs")
    def submit():
        if not request.is_json:
            raise LLMError()
        if request.content_length is not None and request.content_length > REQUEST_LIMIT:
            raise too_large()
        payload = loads(request.stream.read(REQUEST_LIMIT + 1), REQUEST_LIMIT)
        with core.db.transaction() as cur:
            config = ready(cur)
            if config is None:
                raise unavailable()
            job = jobs.submit(cur, core.clock.now(), owner(), payload, config)
        return ok(202, job=job)

    @blueprint.route("/llm/jobs/<job_id>", methods=["GET", "DELETE"])
    def job(job_id: str):
        with core.db.transaction() as cur:
            if ready(cur) is None:
                raise unavailable()
            operation = jobs.cancel if request.method == "DELETE" else jobs.get
            result = operation(cur, core.clock.now(), owner(), identifier(job_id))
        return ok(job=result)

    return blueprint
