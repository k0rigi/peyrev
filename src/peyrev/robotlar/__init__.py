"""Robot Paneli: Studio kurulumlarının durum bildirimi ve uzaktan komutları.

Studio ucu (kimlik: lisans yanıtındaki robot belirteci, Authorization: Bearer; her istekte hesap, şifre izi, Studio
oturumu ve MOD_RPA hakkı yeniden denetlenir):
  POST /api/rpa/robot/durum   durum bildirimi ve önceki komutların sonuçları → {komutlar, aralik}
Panel (/api/yonetim/robotlar) ve Hesabım (/api/uye/robotlar) uçları kendi modüllerindedir; ikisi de service.py'yi
çağırır. Sessizlik denetimi: `python -m peyrev.robotlar` (ayrıca bildirimlerin arasında kendiliğinden yapılır).
"""

from __future__ import annotations

from flask import Blueprint, request

from ..common import json_body, ok
from ..support import notify
from . import service


def create_blueprint(core) -> Blueprint:
    blueprint = Blueprint("robotlar", __name__)

    def studio(work):
        with core.db.transaction() as cur:
            user, claims = service.studio_user(core, cur, request.headers.get("Authorization"))
            return work(cur, user, claims)

    @blueprint.post("/api/rpa/robot/durum")
    def heartbeat():
        # The caller is identified before the body is read; the identity is checked again with the write.
        studio(lambda cur, user, claims: None)
        data = json_body(service.TOP, {"surum", "kurulum"}, limit=service.BODY_LIMIT, message=service.INVALID)
        commands, notice = studio(lambda cur, user, claims: service.heartbeat(core, cur, user, claims, data))
        if notice is not None:
            notify.deliver(core, *notice)
        service.maybe_check_silence(core)
        return ok(komutlar=commands, aralik=service.INTERVAL)

    return blueprint
