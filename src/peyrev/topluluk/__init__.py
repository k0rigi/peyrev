"""Topluluk API'si (/api/topluluk/*): yalnız aboneliği devam eden Peyrev hesaplarına açık konular, yorumlar ve akış
paylaşımı. Giriş ve oturum kuralları auth.py'de, içerik kuralları service.py'dedir; moderasyon yönetim panelindedir.
"""

from __future__ import annotations

import re
from typing import Any

from flask import Blueprint, Response, g, request

from ..common import LOG, ApiError, client_ip, json_body, ok
from ..support import notify
from . import auth, service

POLICY = "default-src 'none'; frame-ancestors 'none'"
PAGE = re.compile(r"[0-9]{1,5}")
BODY_LIMIT = 2 * 1024 * 1024  # 1 MB akış dosyası ve metin, JSON kaçışlarıyla


def _committed(core, work):
    with core.db.transaction() as cur:
        result = work(cur)
    if isinstance(result, ApiError):
        raise result
    return result


def create_blueprint(core) -> Blueprint:
    blueprint = Blueprint("topluluk", __name__, url_prefix=auth.PATH)

    @blueprint.before_request
    def guard():
        auth.check_request()
        g.member = None
        if request.endpoint not in ("topluluk.sign_in", "topluluk.sign_out"):
            g.member = _committed(core, lambda cur: auth.verify(core, cur, request.cookies.get(auth.COOKIE)))

    @blueprint.after_request
    def headers(response: Response) -> Response:
        response.headers.setdefault("Content-Security-Policy", POLICY)
        response.headers["Referrer-Policy"] = "no-referrer"
        if response.status_code == 401 and auth.COOKIE in request.cookies:
            auth.forget_cookie(response)
        return response

    @blueprint.post("/giris")
    def sign_in():
        data = json_body({"kullanici", "sifre"}, {"kullanici", "sifre"})
        login, password = data["kullanici"], data["sifre"]
        if (not isinstance(login, str) or not login.strip() or len(login) > 150 or not isinstance(password, str)
                or not password or len(password) > 256):
            raise ApiError("Kullanıcı adınızı ve şifrenizi yazın.", 400, "EKSIK")
        token, user = _committed(core, lambda cur: auth.sign_in(core, cur, login.strip(), password, client_ip(),
                                                                request.headers.get("User-Agent")))
        response, status = ok(uye=auth.profile(user))
        auth.set_cookie(response, token)
        return response, status

    @blueprint.post("/cikis")
    def sign_out():
        json_body(set())
        with core.db.transaction() as cur:
            auth.sign_out(cur, request.cookies.get(auth.COOKIE))
        response, status = ok()
        auth.forget_cookie(response)
        return response, status

    @blueprint.get("/ben")
    def me():
        return ok(uye=auth.profile(g.member), kategoriler=service.CATEGORIES)

    @blueprint.get("/konular")
    def topics():
        page = request.args.get("sayfa") or "1"
        if not PAGE.fullmatch(page) or int(page) < 1:
            raise ApiError("Sayfa geçersiz.")
        with core.db.transaction() as cur:
            return ok(**service.listing(cur, kategori=request.args.get("kategori"), ara=request.args.get("ara"),
                                        sayfa=int(page)))

    @blueprint.post("/konular")
    def topic_create():
        data = json_body({"kategori", "baslik", "metin", "akis"}, {"kategori", "baslik", "metin"}, limit=BODY_LIMIT)
        with core.db.transaction() as cur:
            topic_id = service.create_topic(core, cur, g.member, data)
            result = service.read_topic(cur, topic_id, g.member)
        _announce(core, result["konu"])
        return ok(201, **result)

    @blueprint.get("/konular/<int:topic_id>")
    def topic(topic_id: int):
        with core.db.transaction() as cur:
            return ok(**service.read_topic(cur, topic_id, g.member))

    @blueprint.post("/konular/<int:topic_id>/yorumlar")
    def comment(topic_id: int):
        data = json_body({"metin"}, {"metin"})
        with core.db.transaction() as cur:
            service.add_comment(core, cur, g.member, topic_id, data)
            return ok(201, yorumlar=service.comments(cur, topic_id, g.member))

    @blueprint.post("/konular/<int:topic_id>/sil")
    def topic_delete(topic_id: int):
        json_body(set())
        with core.db.transaction() as cur:
            service.delete_own_topic(cur, g.member, topic_id)
        return ok(silindi=True)

    @blueprint.post("/yorumlar/<int:comment_id>/sil")
    def comment_delete(comment_id: int):
        json_body(set())
        with core.db.transaction() as cur:
            service.delete_comment(cur, comment_id, user=g.member)
        return ok(silindi=True)

    @blueprint.get("/konular/<int:topic_id>/akis")
    def flow(topic_id: int):
        with core.db.transaction() as cur:
            name, content = service.flow_file(cur, topic_id)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name.translate(str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU"))).strip("-")
        response = Response(content, mimetype="application/json")
        response.headers["Content-Disposition"] = f'attachment; filename="{(safe or "akis")[:80]}.json"'
        return response

    return blueprint


def _announce(core, topic: dict[str, Any]) -> None:
    """Yeni konu yöneticiye bildirilir (Telegram, e-posta); bildirim hatası konuyu etkilemez."""
    try:
        notify.deliver(core, "Toplulukta yeni konu", f"{topic['yazar']['ad']} · {service.CATEGORIES[topic['kategori']]}\n"
                       f"{topic['baslik']}\n{core.settings.public_base.rstrip('/')}/topluluk/#konu-{topic['id']}")
    except Exception:  # noqa: BLE001
        LOG.exception("Topluluk bildirimi gönderilemedi.")
