"""Studio ajan merkezi uçları (/api/studio/ekip). Yollar, alanlar ve yanıt biçimleri kurulu Studio sürümleriyle uyumlu kalır.

İstek sırası: 128 KiB'tan büyük gövde (413) ve sorgudaki kimlik alanı (400) kimlik doğrulamasından önce reddedilir;
ardından ajan erişim belirteci ve X-Rpa-Ofis doğrulanır (401/403/400). Kimliksiz `GET /ben` tam olarak
{"success": false, "kod": "OTURUM_GEREKLI", "message": "Studio oturumu gerekli."} döner (yeniden başlatma denetimi).
Firma kavramına ait uçlar yoktur ve 404 BULUNAMADI döner: POST /ofisim/uyeler, POST /ofisim/uyeler/<id>/cikar,
POST /ofisim/ayril ve göreve başkasını atayan POST /gorevler/<id>/ata.
"""

from __future__ import annotations

import math

from flask import Blueprint, g, request

from ..common import INVALID_FIELDS, ApiError, json_body, ok
from . import records
from .entitlement import OFFICE_HEADER, authenticate

PREFIX = "/api/studio/ekip"
BODY_LIMIT = 128 * 1024
IDENTITY_FIELDS = ("firma_id", "company_id", "kullanici_id", "user_id")


def _storable(value) -> bool:
    """Finite numbers and text that UTF-8 can hold: 1e999 turns into infinity, a lone "\\ud800" cannot be stored."""
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return True
    if isinstance(value, dict):
        return all(_storable(key) and _storable(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_storable(item) for item in value)
    return True


def body(allowed, required=()) -> dict:
    """Sıkı JSON gövdesi (sonsuza taşan sayı ve saklanamayan karakter de reddedilir); metinler aynen kalır."""
    if not request.is_json:
        raise ApiError("JSON gövdesi gerekli.", 400, "GECERSIZ_GIRDI")
    try:
        data = json_body(allowed, required, limit=BODY_LIMIT)
        if not _storable(data):
            raise ApiError(INVALID_FIELDS)
    except ApiError as error:
        # The agent API names every field error GECERSIZ_GIRDI; a too large body stays 413 GOVDE_SINIRI.
        if error.status != 400:
            raise
        raise ApiError(INVALID_FIELDS, 400, "GECERSIZ_GIRDI") from None
    return data


def create_blueprint(core) -> Blueprint:
    blueprint = Blueprint("agents", __name__, url_prefix=PREFIX)

    @blueprint.before_request
    def identify():
        if request.content_length is not None and request.content_length > BODY_LIMIT:
            raise ApiError("İstek gövdesi çok büyük.", 413, "GOVDE_SINIRI")
        # Identity is never accepted from the query or the body, including for platform admins.
        if any(field in request.args for field in IDENTITY_FIELDS):
            raise ApiError("Kimlik yalnız Studio oturumundan alınır.", 400, "GECERSIZ_GIRDI")
        with core.db.transaction() as cur:
            g.agent_identity = authenticate(core, cur, request.headers.get("Authorization"),
                                            request.headers.get(OFFICE_HEADER))

    @blueprint.get("/ben")
    def me():
        identity = g.agent_identity
        return ok(kullanici_id=identity.user["id"], firma_id=identity.entitlement["firma_id"],
                  ad_soyad=identity.user["ad_soyad"], ajan=identity.entitlement)

    @blueprint.get("/uyeler")
    def members():
        return ok(uyeler=records.members(g.agent_identity))

    @blueprint.get("/ofis")
    def office():
        if request.args:
            raise ApiError("Ofis görünümü sorgu parametresi kabul etmez.", 400, "GECERSIZ_GIRDI")
        return ok(people=records.office_people(core, g.agent_identity))

    @blueprint.route("/ofisim", methods=["GET", "POST"])
    def my_office():
        if request.method == "POST":
            data = body({"name"}, {"name"})
            return ok(201, office=records.create_office(core, g.agent_identity, data["name"]))
        return ok(**records.my_office(core, g.agent_identity))

    @blueprint.get("/ofislerim")
    def my_offices():
        if request.args:
            raise ApiError("Ofis özeti sorgu parametresi kabul etmez.", 400, "GECERSIZ_GIRDI")
        return ok(**records.my_offices(core, g.agent_identity))

    @blueprint.post("/ofisim/ad")
    def office_name():
        data = body({"name"}, {"name"})
        return ok(office=records.rename_office(core, g.agent_identity, data["name"]))

    @blueprint.post("/ofisim/sil")
    def office_delete():
        data = body({"name"}, {"name"})
        return ok(office=records.delete_office(core, g.agent_identity, data["name"]))

    @blueprint.get("/metrics")
    def task_metrics():
        if set(request.args) != {"start", "end"} or any(len(request.args.getlist(key)) != 1 for key in request.args):
            raise ApiError("Yalnız tek başlangıç ve bitiş tarihi gerekli.", 400, "TARIH_ARALIGI")
        return ok(metrics=records.task_metrics(core, g.agent_identity, request.args["start"], request.args["end"]))

    @blueprint.route("/gorevler", methods=["GET", "POST"])
    def tasks():
        if request.method == "POST":
            fields = {"request_id", "title", "instructions", "assignee_user_id", "priority"}
            data = body(fields, fields - {"priority"})
            return ok(201, task=records.create_task(core, g.agent_identity, data))
        return ok(tasks=records.list_tasks(core, g.agent_identity, request.args.get("status")))

    @blueprint.get("/gorevler/<task_id>")
    def task(task_id):
        return ok(task=records.get_task(core, g.agent_identity, task_id))

    @blueprint.post("/gorevler/<task_id>/durum")
    def task_status(task_id):
        data = body({"status", "expected_version", "summary"}, {"status", "expected_version"})
        return ok(task=records.transition_task(core, g.agent_identity, task_id, data["status"],
                                               data["expected_version"], data.get("summary", "")))

    @blueprint.post("/gorevler/<task_id>/sil")
    def task_delete(task_id):
        data = body({"expected_version"}, {"expected_version"})
        return ok(task=records.delete_task(core, g.agent_identity, task_id, data["expected_version"]))

    @blueprint.route("/onaylar", methods=["GET", "POST"])
    def approvals():
        if request.method == "POST":
            fields = {"request_id", "run_id", "workflow_id", "workflow_digest", "inputs_digest", "preview", "reason",
                      "attempt", "expires_in"}
            data = body(fields, fields - {"expires_in"})
            return ok(201, approval=records.request_approval(core, g.agent_identity, data))
        return ok(approvals=records.list_approvals(core, g.agent_identity))

    @blueprint.get("/onaylar/<approval_id>")
    def approval(approval_id):
        return ok(approval=records.get_approval(core, g.agent_identity, approval_id))

    @blueprint.post("/onaylar/<approval_id>/karar")
    def approval_decide(approval_id):
        data = body({"decision", "expected_version"}, {"decision", "expected_version"})
        return ok(approval=records.decide_approval(core, g.agent_identity, approval_id, data["decision"],
                                                   data["expected_version"]))

    @blueprint.post("/onaylar/<approval_id>/tuket")
    def approval_consume(approval_id):
        fields = {"run_id", "workflow_id", "workflow_digest", "inputs_digest", "preview_digest", "attempt"}
        return ok(approval=records.consume_approval(core, g.agent_identity, approval_id, body(fields, fields)))

    @blueprint.post("/onaylar/<approval_id>/iptal")
    def approval_cancel(approval_id):
        body(set())
        return ok(approval=records.cancel_approval(core, g.agent_identity, approval_id))

    return blueprint
