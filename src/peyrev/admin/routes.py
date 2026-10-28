"""Yönetim paneli API'si (/api/yonetim/*). Giriş dışındaki her uç yönetici oturumu ister; durum değiştiren her istek CSRF
kuralından geçer (auth.py). Uçlar birinci dalga modüllerinin servis işlevlerini çağırır; panelin kendi işlemleri
service.py'dedir. Gövdeler sıkı okunur (bilinmeyen ya da eksik alan 400); boş gövdeli POST'lar "{}" gönderir.
"""

from __future__ import annotations

import re
from typing import Any

from flask import Blueprint, Response, g, request

from ..accounts import admin as accounts_admin
from ..agents import admin as agents_admin
from ..common import LOG, ApiError, client_ip, json_body, ok
from ..llm import admin as llm_admin
from ..support import answer_ticket, file_response, list_tickets, open_ticket, ticket_attachment
from . import auth, service
from . import egitim as training
from . import indirme as downloads

PREFIX = "/api/yonetim"
USERS_PAGE = 50
REPLY_LIMIT = 32 * 1024  # 5000 karakterlik yanıt, çok baytlı harflerle
API_POLICY = "default-src 'none'; frame-ancestors 'none'"
PAGE = re.compile(r"[0-9]{1,6}")
ROLE_NOT_SET = ("Hesap açıldı ama yönetici rolü verilemedi; rolü hesabın ayrıntısından verin. İlk şifre kodu aşağıdadır "
                "ve geçerlidir.")


def _optional(value: Any) -> Any:
    """Formdan gelen boş değer "verilmedi" demektir (ör. bitiş boş = süresiz)."""
    return None if value in (None, "") else value


def _page(value: str | None) -> int:
    if value in (None, ""):
        return 1
    # ASCII digits only: str.isdigit() also accepts "²", which int() refuses.
    if not PAGE.fullmatch(value) or not 1 <= int(value) <= 100000:
        raise ApiError("Sayfa geçersiz.")
    return int(value)


def _committed(core, work):
    """İş tek işlemde yapılır; iş hatalı denemeyi yazıp ret döndürdüyse ret işlem kaydedildikten sonra fırlatılır."""
    with core.db.transaction() as cur:
        result = work(cur)
    if isinstance(result, ApiError):
        raise result
    return result


def _headers(response: Response) -> Response:
    response.headers.setdefault("Content-Security-Policy", API_POLICY)  # ekler kendi kuralını taşır
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def sign_in_blueprint(core) -> Blueprint:
    blueprint = Blueprint("yonetim_giris", __name__, url_prefix=PREFIX)
    blueprint.before_request(lambda: auth.check_request(core))
    blueprint.after_request(_headers)

    @blueprint.post("/giris")
    def sign_in():
        data = json_body({"giris", "sifre", "kod"}, {"giris", "sifre"})
        login, password, code = data["giris"], data["sifre"], data.get("kod")
        if (not isinstance(login, str) or not login.strip() or len(login) > 150 or not isinstance(password, str)
                or not password or len(password) > 256 or not (code is None or isinstance(code, str))):
            raise ApiError("Kullanıcı adı ve şifre gereklidir.", 400, "EKSIK")
        token, admin, session = _committed(core, lambda cur: auth.sign_in(
            core, cur, login.strip(), password, code, client_ip(), request.headers.get("User-Agent"),
            previous=request.cookies.get(auth.COOKIE)))
        response, status = ok(yonetici=auth.profile(admin), oturum=auth.session_view(session))
        auth.set_cookie(response, token)
        return response, status

    return blueprint


def api_blueprint(core) -> Blueprint:
    blueprint = Blueprint("yonetim", __name__, url_prefix=PREFIX)

    @blueprint.before_request
    def guard():
        auth.check_request(core)
        g.admin, g.panel = _committed(core, lambda cur: auth.verify(core, cur, request.cookies.get(auth.COOKIE)))

    @blueprint.after_request
    def headers(response: Response) -> Response:
        if response.status_code == 401 and auth.COOKIE in request.cookies:
            auth.forget_cookie(response)  # the session is gone: the browser forgets its cookie too
        return _headers(response)

    def me() -> int:
        return g.admin["id"]

    # ----- oturum ve Hesabım -----------------------------------------------------------------------
    @blueprint.post("/cikis")
    def sign_out():
        json_body(set())
        with core.db.transaction() as cur:
            auth.sign_out(core, cur, me(), g.panel["id"])
        response, status = ok()
        auth.forget_cookie(response)
        return response, status

    @blueprint.get("/ben")
    def whoami():
        with core.db.transaction() as cur:
            listed = auth.sessions_of(cur, me(), g.panel["id"])
        return ok(yonetici=auth.profile(g.admin), oturum=auth.session_view(g.panel), oturumlar=listed)

    @blueprint.post("/oturumlar/digerlerini-kapat")
    def close_other_sessions():
        json_body(set())
        with core.db.transaction() as cur:
            return ok(kapatilan=auth.close_others(core, cur, me(), g.panel["id"]))

    @blueprint.post("/iki-adim/baslat")
    def two_factor_start():
        data = json_body({"sifre"}, {"sifre"})
        return ok(**_committed(core, lambda cur: auth.start_two_factor(core, cur, me(), g.panel["id"], data["sifre"],
                                                                        client_ip())))

    @blueprint.post("/iki-adim/onayla")
    def two_factor_confirm():
        data = json_body({"kod"}, {"kod"})
        return ok(**_committed(core, lambda cur: auth.confirm_two_factor(core, cur, me(), g.panel["id"], data["kod"],
                                                                          client_ip())))

    @blueprint.post("/iki-adim/kapat")
    def two_factor_stop():
        data = json_body({"sifre", "kod"}, {"sifre", "kod"})
        return ok(**_committed(core, lambda cur: auth.stop_two_factor(core, cur, me(), data["sifre"], data["kod"],
                                                                       client_ip())))

    # ----- özet ------------------------------------------------------------------------------------
    @blueprint.get("/ozet")
    def overview():
        with core.db.transaction() as cur:
            return ok(**service.summary(core, cur, me()))

    @blueprint.get("/ortak-model")
    def shared_model():
        return ok(**llm_admin.status(core))

    @blueprint.post("/ortak-model/kuyrugu-bosalt")
    def shared_model_clear():
        json_body(set())
        return ok(iptal=llm_admin.clear_queue(core, me()))

    # ----- kullanıcılar ----------------------------------------------------------------------------
    @blueprint.get("/kullanicilar")
    def users_list():
        page = _page(request.args.get("sayfa"))
        found = accounts_admin.list_users(core, me(), ara=_optional(request.args.get("ara")),
                                          durum=_optional(request.args.get("durum")), limit=USERS_PAGE,
                                          offset=(page - 1) * USERS_PAGE)
        return ok(**found, sayfa=page, sayfa_boyu=USERS_PAGE)

    @blueprint.post("/kullanicilar")
    def user_create():
        data = json_body({"giris", "ad_soyad", "telefon", "rol", "baslangic", "bitis", "ajan", "sifre"},
                         {"giris", "ad_soyad", "baslangic", "bitis"})
        role = data.get("rol", "kullanici")
        if not isinstance(role, str) or role not in service.ROLES:
            raise ApiError("Rol geçersiz.")
        created = accounts_admin.create_user(core, me(), giris=data["giris"], ad_soyad=data["ad_soyad"],
                                             baslangic=data["baslangic"], bitis=data["bitis"],
                                             ajan=data.get("ajan", False), telefon=data.get("telefon"),
                                             sifre=data.get("sifre") or None)
        warning = None
        if role != "kullanici":
            # Accounts are opened as users; the role follows in its own recorded step. The account and its one-time
            # code already exist, so a failure here never hides the code: the panel says to set the role by hand.
            try:
                service.set_role(core, me(), created["kullanici_id"], role)
            except Exception:  # noqa: BLE001 - the code is shown either way
                LOG.exception("Yeni hesaba yönetici rolü verilemedi (kullanıcı %s).", created["kullanici_id"])
                role, warning = "kullanici", ROLE_NOT_SET
        code = {"kod": {"kod": created["kod"], "gecerlilik": created["gecerlilik"]}} if created["kod"] else {}
        return ok(201, kullanici={"kullanici_id": created["kullanici_id"], "giris": created["giris"], "rol": role},
                  **code, **({"uyari": warning} if warning else {}))

    @blueprint.get("/kullanicilar/<int:user_id>")
    def user_detail(user_id: int):
        return ok(**service.user_detail(core, me(), user_id))

    @blueprint.post("/kullanicilar/<int:user_id>/bilgiler")
    def user_details(user_id: int):
        data = json_body({"ad_soyad", "telefon", "giris"}, {"ad_soyad"})
        return ok(kullanici=service.update_details(core, me(), user_id, ad_soyad=data["ad_soyad"],
                                                   telefon=data.get("telefon"), giris=data.get("giris")))

    @blueprint.post("/kullanicilar/<int:user_id>/rol")
    def user_role(user_id: int):
        data = json_body({"rol"}, {"rol"})
        return ok(kullanici=service.set_role(core, me(), user_id, data["rol"]))

    @blueprint.post("/kullanicilar/<int:user_id>/durum")
    def user_status(user_id: int):
        data = json_body({"durum"}, {"durum"})
        result = accounts_admin.set_status(core, me(), user_id, data["durum"])
        if result["durum"] == "pasif":
            # A passive account fails every panel request anyway; this only removes its rows and records it.
            result["kapatilan_panel_oturumu"] = service.end_panel_sessions(core, me(), user_id)
        return ok(kullanici=result)

    @blueprint.post("/kullanicilar/<int:user_id>/moduller/<module>")
    def user_module(user_id: int, module: str):
        data = json_body({"baslangic", "bitis"})
        return ok(modul=accounts_admin.set_module(core, me(), user_id, module, bitis=_optional(data.get("bitis")),
                                                  baslangic=_optional(data.get("baslangic"))))

    @blueprint.post("/kullanicilar/<int:user_id>/moduller/<module>/kaldir")
    def user_module_remove(user_id: int, module: str):
        json_body(set())
        return ok(modul=accounts_admin.remove_module(core, me(), user_id, module))

    @blueprint.post("/kullanicilar/<int:user_id>/sifre-kodu")
    def user_password_code(user_id: int):
        data = json_body({"saat"})
        return ok(kod=accounts_admin.issue_password_code(core, me(), user_id,
                                                         hours=data.get("saat", accounts_admin.CODE_HOURS)))

    @blueprint.post("/kullanicilar/<int:user_id>/sifre")
    def user_password(user_id: int):
        data = json_body({"sifre"}, {"sifre"})
        return ok(kullanici=service.set_password(core, me(), user_id, data["sifre"]))

    @blueprint.post("/kullanicilar/<int:user_id>/indirme")
    def user_download(user_id: int):
        data = json_body({"eposta"}, {"eposta"})
        return ok(indirme=downloads.send(core, me(), user_id, mail=data["eposta"]))

    @blueprint.post("/kullanicilar/<int:user_id>/oturumlari-kapat")
    def user_sessions_close(user_id: int):
        data = json_body({"oturum_id"})
        return ok(**accounts_admin.close_sessions(core, me(), user_id, session_id=data.get("oturum_id")))

    @blueprint.post("/kullanicilar/<int:user_id>/iki-adim-sifirla")
    def user_two_factor_reset(user_id: int):
        json_body(set())
        return ok(kullanici=service.reset_two_factor(core, me(), user_id))

    @blueprint.post("/ofisler/<office_id>/durum")
    def office_status(office_id: str):
        data = json_body({"durum"}, {"durum"})
        return ok(ofis=agents_admin.set_office_status(core, me(), office_id, data["durum"]))

    # ----- başvurular ------------------------------------------------------------------------------
    @blueprint.get("/basvurular")
    def applications_list():
        return ok(basvurular=accounts_admin.list_applications(core, me(), durum=_optional(request.args.get("durum"))))

    @blueprint.post("/basvurular/<application_id>/onayla")
    def application_approve(application_id: str):
        data = json_body({"baslangic", "bitis", "paket", "not"}, {"bitis"})
        account = accounts_admin.approve_application(
            core, me(), application_id, bitis=data["bitis"], baslangic=_optional(data.get("baslangic")),
            paket=_optional(data.get("paket")), note=data.get("not"))
        # The account is open whatever happens next; a failed link or mail is reported, never raised.
        try:
            link = downloads.send(core, me(), account["kullanici_id"], mail=True, welcome=True)
        except Exception:  # noqa: BLE001
            LOG.exception("Onaylanan hesaba indirme bağlantısı hazırlanamadı (kullanıcı %s).", account["kullanici_id"])
            link = {"eposta": "gonderilemedi", "baglanti": None}
        return ok(hesap=account, indirme=link)

    @blueprint.post("/basvurular/<application_id>/reddet")
    def application_reject(application_id: str):
        data = json_body({"not"})
        return ok(basvuru=accounts_admin.reject_application(core, me(), application_id, note=data.get("not")))

    # ----- destek ----------------------------------------------------------------------------------
    @blueprint.get("/destek")
    def tickets():
        with core.db.transaction() as cur:
            return ok(**list_tickets(core, cur, me(), durum=_optional(request.args.get("durum")),
                                     tur=_optional(request.args.get("tur"))))

    @blueprint.get("/destek/<int:number>")
    def ticket(number: int):
        with core.db.transaction() as cur:
            return ok(talep=open_ticket(core, cur, me(), number))

    @blueprint.post("/destek/<int:number>/yanit")
    def ticket_answer(number: int):
        data = json_body({"metin", "durum", "yayin_surumu"}, limit=REPLY_LIMIT)
        with core.db.transaction() as cur:
            return ok(talep=answer_ticket(core, cur, me(), number, metin=data.get("metin"), durum=data.get("durum"),
                                          yayin_surumu=data.get("yayin_surumu")))

    @blueprint.get("/destek/<int:number>/ek/<attachment_id>")
    def ticket_file(number: int, attachment_id: str):
        with core.db.transaction() as cur:
            found = ticket_attachment(core, cur, me(), number, attachment_id)
        # Images open in the panel; the flow and error log attachments are downloads.
        return file_response(found, inline=found[1].startswith("image/"))

    # ----- olaylar ---------------------------------------------------------------------------------
    # ----- eğitim videoları ------------------------------------------------------------------------
    TRAINING_FIELDS = {"baglanti", "baslik", "aciklama", "kategori", "sira", "yayinda"}

    @blueprint.get("/egitim")
    def training_list():
        return ok(videolar=training.listing(core, me()))

    @blueprint.post("/egitim")
    def training_create():
        data = json_body(TRAINING_FIELDS, {"baglanti", "baslik"})
        return ok(201, video=training.create(core, me(), data))

    @blueprint.post("/egitim/<int:video_id>")
    def training_update(video_id: int):
        data = json_body(TRAINING_FIELDS, {"baglanti", "baslik"})
        return ok(video=training.update(core, me(), video_id, data))

    @blueprint.post("/egitim/<int:video_id>/sil")
    def training_delete(video_id: int):
        json_body(set())
        return ok(video=training.delete(core, me(), video_id))

    @blueprint.get("/olaylar")
    def event_list():
        return ok(**service.event_page(core, me(), page=_page(request.args.get("sayfa")),
                                       kind=_optional(request.args.get("tur")),
                                       target=_optional(request.args.get("hedef"))))

    return blueprint
