"""Flask uygulama fabrikası. Modüller kendi Blueprint'lerini `create_blueprint(core)` ile verir.

Uç haritası (yollar kurulu Studio sürümleriyle uyumlu kalır):
  /api/saglik                      sağlık (veritabanına bağlanır)
  /api/rpa/lisans/*                lisans ve Studio oturumu          → peyrev.accounts
  /api/rpa/hesap/*, /api/rpa/basvuru  şifre işlemleri ve başvuru     → peyrev.accounts
  /api/rpa/destek/*                Studio destek talepleri            → peyrev.support
  /api/studio/ekip/*               ajan ofisleri, görevler, onaylar   → peyrev.agents
  /api/studio/ekip/llm/*           ortak model (kapalıysa enabled:false) → peyrev.llm
  /api/yonetim/*                   yönetim paneli (yönetici oturumu)   → peyrev.admin
"""

from __future__ import annotations

import logging

from flask import Flask

from . import __version__, common
from .clock import Clock
from .config import Settings
from .core import Core
from .db import Database
from .keys import Keys

BODY_LIMIT = 8 * 1024 * 1024  # destek talebinin görselleri dahil üst sınır; uçlar kendi sınırlarını ayrıca uygular


def create_app(settings: Settings | None = None, *, core: Core | None = None) -> Flask:
    if core is None:
        settings = settings or Settings.from_environment()
        core = Core(settings=settings, db=Database(settings.database_url),
                    keys=Keys(settings.secrets_dir, settings.allow_generate), clock=Clock())
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=BODY_LIMIT, JSON_AS_ASCII=False, SECRET_KEY=core.keys.derive("peyrev-flask"))
    app.json.ensure_ascii = False
    app.json.sort_keys = False
    app.extensions["peyrev"] = core
    common.register(app)
    logging.getLogger("peyrev").setLevel(logging.INFO)

    from . import egitim, health, iletisim, indirme, uye, ziyaret
    from .accounts import create_blueprint as accounts
    from .admin import create_blueprint as admin
    from .agents import create_blueprint as agents
    from .llm import create_blueprint as llm
    from .support import create_blueprint as support
    from .topluluk import create_blueprint as community

    for blueprint in (health.create_blueprint(core), egitim.create_blueprint(core), indirme.create_blueprint(core),
                      accounts(core), support(core), iletisim.create_blueprint(core), ziyaret.create_blueprint(core),
                      uye.create_blueprint(core), community(core), agents(core),
                      llm(core), admin(core)):
        app.register_blueprint(blueprint)
    app.config["PEYREV_VERSION"] = __version__
    return app
