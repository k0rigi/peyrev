"""Dijital İkiz firma adresi (<ad>.peyrev.com): panelden açma, değiştirme ve kaldırma.

Adresi Dijital İkiz servisi tutar; panel onun iç yönetim API'sine vekillik eder (kök `settings.ikiz_internal_base`,
varsayılan http://127.0.0.1:5102; yalnız döngü arayüzü kabul edilir):

- GET    {kök}/api/yonetim/hesaplar/<hesap>/alan → {"alt_alan", "durum": yok|bekliyor|hazir|hata, "mesaj", "adres"}
- PUT    aynı yol, {"ad": "<ad>", "firma": "<görünen ad>"} → aynı biçim ("bekliyor"); 400 GECERSIZ, 409 KULLANILIYOR
- DELETE aynı yol → aynı biçim ("yok")

Kimlik `Authorization: Bearer <belirteç>`dir: belirteç accounts.ikiz.issue ile isteği yapan yöneticinin kendisi için
her çağrıda yeniden üretilir (`yonetici: true`, 720 sn, tek kullanımlık jti) ve hiçbir yere yazılmaz. Sertifikayı ve
ad eşlemesini servisin sunucudaki yardımcısı kurar; panel "bekliyor" durumunu 10 sn'de bir sorar.

Adres yalnız etkin MOD_IKIZ hakkı olan hesaba açılır (users.module_state; yönetici rolü satırsız açar), yoksa 409
IKIZ_HAKKI_YOK. Durum okuma ve kaldırma haksız hesapta da çalışır (hakkı biten firmanın adresi kapatılabilsin). Servise ulaşılamazsa 502 IKIZ_ERISIM; servisin 400/409 iletisi olduğu gibi (metin olarak) geçer.
Açma ve kaldırma yonetim_olaylari'na yazılır (ikiz_adresi, ikiz_adresi_kaldirildi).
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .. import users
from ..accounts import ikiz as twin_tokens
from ..common import LOG, ApiError
from . import events, service

NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?")
RESERVED = frozenset({"www", "mail", "ikiz", "demo", "api", "yonetim", "panel", "admin", "destek", "indir", "ftp",
                      "smtp", "imap", "pop", "cdn", "static", "test", "peyrev"})
STATES = ("yok", "bekliyor", "hazir", "hata")
TIMEOUT = 5
RESPONSE_LIMIT = 16 * 1024
MESSAGE_LIMIT = 300
LOOPBACK = ("127.0.0.1", "::1")

INVALID_NAME = "Adres 1-40 karakter olmalı: küçük harf (a-z), rakam ve tire; tire başta ya da sonda olamaz."
RESERVED_NAME = "Bu ad ayrılmış; başka bir ad seçin."
NO_RIGHT = "Önce Dijital İkiz hakkı verin."
UNREACHABLE = "Dijital İkiz servisine ulaşılamadı."
REFUSED = "Dijital İkiz servisi yönetici kimliğini kabul etmedi."
BAD_ANSWER = "Dijital İkiz servisinden geçersiz yanıt geldi."


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "Yönlendirme engellendi.", headers, None)


_OPENER = build_opener(ProxyHandler({}), _NoRedirect())


def check_name(value: Any) -> str:
    """Firma adresinin adı (küçük harfe çevrilir); servis de aynı kuralı uygular ve son sözü odur."""
    if not isinstance(value, str):
        raise ApiError(INVALID_NAME)
    name = value.strip().lower()
    if not NAME.fullmatch(name):
        raise ApiError(INVALID_NAME)
    if name in RESERVED:
        raise ApiError(RESERVED_NAME)
    return name


def _base(core) -> str:
    value = core.settings.ikiz_internal_base
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname not in LOOPBACK or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ("", "/") or parsed.port is None):
            raise ValueError("adres")
    except (TypeError, ValueError, AttributeError):
        LOG.error("Dijital İkiz iç adresi geçersiz; yalnız http://127.0.0.1:<port> kullanılabilir.")
        raise ApiError(UNREACHABLE, 502, "IKIZ_ERISIM") from None
    return "http://" + parsed.netloc


def _text(value: Any) -> str | None:
    """Servisten gelen kısa metin: denetim karakterleri atılır, en çok 300 karakter."""
    if not isinstance(value, str):
        return None
    cleaned = "".join(char for char in value if char.isprintable()).strip()
    return cleaned[:MESSAGE_LIMIT] or None


def _area(value: Any) -> dict[str, Any]:
    """Servisin yanıtı yalnız sözleşmedeki alanlarla geçer; adres yalnız https ise gösterilir."""
    if not isinstance(value, dict) or value.get("durum") not in STATES:
        raise ApiError(BAD_ANSWER, 502, "IKIZ_ERISIM")
    name = value.get("alt_alan")
    if name is not None and (not isinstance(name, str) or not NAME.fullmatch(name)):
        raise ApiError(BAD_ANSWER, 502, "IKIZ_ERISIM")
    address = _text(value.get("adres"))
    if address is not None and not re.fullmatch(r"https://[a-z0-9.-]{1,200}/?", address):
        address = None
    return {"alt_alan": name, "durum": value["durum"], "mesaj": _text(value.get("mesaj")), "adres": address}


def _call(core, admin: dict[str, Any], method: str, user_id: int, body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{_base(core)}/api/yonetim/hesaplar/{int(user_id)}/alan"
    headers = {"Accept": "application/json", "Authorization": "Bearer " + twin_tokens.issue(core, admin)}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=TIMEOUT) as response:
            if response.status != 200:
                raise ApiError(UNREACHABLE, 502, "IKIZ_ERISIM")
            raw = response.read(RESPONSE_LIMIT + 1)
    except ApiError:
        raise
    except HTTPError as error:
        status = error.code
        if status in (400, 409):
            try:
                answer = json.loads(error.read(RESPONSE_LIMIT))
            except (OSError, ValueError):
                answer = {}
            answer = answer if isinstance(answer, dict) else {}
            kod = "KULLANILIYOR" if status == 409 else "GECERSIZ"
            fallback = "Bu ad başka bir firmada kullanılıyor." if status == 409 else INVALID_NAME
            raise ApiError(_text(answer.get("mesaj")) or fallback, status, kod) from None
        # Only the status is written down: never the request, its headers or the token.
        LOG.warning("Dijital İkiz servisi %s isteğine %s döndü.", method, status)
        if status in (401, 403):
            raise ApiError(REFUSED, 502, "IKIZ_ERISIM") from None
        raise ApiError(UNREACHABLE, 502, "IKIZ_ERISIM") from None
    except (URLError, OSError, ValueError):
        LOG.warning("Dijital İkiz servisine ulaşılamadı (%s).", method)
        raise ApiError(UNREACHABLE, 502, "IKIZ_ERISIM") from None
    try:
        answer = json.loads(raw) if len(raw) <= RESPONSE_LIMIT else None
    except ValueError:
        answer = None
    return _area(answer)


def _prepare(core, admin_id: int, user_id: int, *, right: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """(yönetici, hesap): yönetici yeniden okunur; right ise hesabın etkin MOD_IKIZ hakkı denetlenir. Servis çağrısı
    işlemin dışında yapılır; veritabanı bağlantısı ağ beklerken tutulmaz."""
    with core.db.transaction() as cur:
        admin = service.require_admin(cur, admin_id)
        user = users.get(cur, user_id) if type(user_id) is int and user_id >= 1 else None
        if user is None:
            raise ApiError("Hesap bulunamadı.", 404, "BULUNAMADI")
        if right and users.module_state(cur, user, users.IKIZ, core.clock.today_tr())[0] != users.ACTIVE:
            raise ApiError(NO_RIGHT, 409, "IKIZ_HAKKI_YOK")
    return admin, user


def status(core, admin_id: int, user_id: int) -> dict[str, Any]:
    admin, _user = _prepare(core, admin_id, user_id)
    return _call(core, admin, "GET", user_id)


def assign(core, admin_id: int, user_id: int, ad: Any) -> dict[str, Any]:
    name = check_name(ad)
    admin, user = _prepare(core, admin_id, user_id, right=True)
    area = _call(core, admin, "PUT", user_id, {"ad": name, "firma": twin_tokens.name(user)})
    with core.db.transaction() as cur:
        events.record(core, cur, admin["id"], "ikiz_adresi", f"kullanici:{user_id}", ad=name)
    return area


def remove(core, admin_id: int, user_id: int) -> dict[str, Any]:
    admin, _user = _prepare(core, admin_id, user_id)
    area = _call(core, admin, "DELETE", user_id)
    with core.db.transaction() as cur:
        events.record(core, cur, admin["id"], "ikiz_adresi_kaldirildi", f"kullanici:{user_id}")
    return area
