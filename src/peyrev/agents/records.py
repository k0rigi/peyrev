"""Ajan merkezinin kayıtları: hesabın ofisleri, kendine verdiği insan görevleri ve oturuma bağlı, tek kullanımlık
onaylar.

Her kayıt bir hesaba (sahip_id) ve onun bir ofisine aittir; ofissiz hesap yalnız ofis dışı (ofis_id NULL) kayıtlarına
ulaşır. Veritabanına giden her işlem, salt okuma dahil, `_transaction` içinde çalışır: hesap satırı ilk kilittir ve
kimlik, oturum, lisans ve ofis kilit altında yeniden doğrulanır. Taşınan geçmişte görevi atanan veya onayı isteyen eski
bir ekip üyesi olabilir; bu satırlar sahibin kapsamında geçmiş olarak okunur, sahip başkasının işini yapmış gösteremez.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from datetime import UTC, timedelta

from psycopg.errors import UniqueViolation

from ..common import ApiError
from . import metrics
from .entitlement import MAX_OFFICES, relock, require

ID = re.compile(r"^[0-9a-f]{32}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
TASK_STATES = ("queued", "running", "waiting_user", "succeeded", "failed", "cancelled")
FINISHED_STATES = ("succeeded", "failed", "cancelled")
# A deleted task stays in the table for the audit trail but is hidden from every reader.
DELETED = "deleted"
TRANSITIONS = {"queued": ("running", "cancelled"),
               "running": ("waiting_user", "succeeded", "failed", "cancelled"),
               "waiting_user": ("running", "cancelled")}
LABELS = {"idle": "Görev bekliyor", "queued": "Görev sırasında", "running": "Görev üzerinde çalışıyor",
          "waiting_user": "Kullanıcı yanıtını bekliyor"}
OFFICE_NAME = re.compile(r"[\w&.,'()/+\- ]+")
SAME_NAME = "Bu adla bir ofis zaten var; başka bir ad seçin."


def _error(message: str, status: int = 400, kod: str = "GECERSIZ_GIRDI") -> ApiError:
    return ApiError(message, status, kod)


def _id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise _error("Kayıt kimliği geçersiz.")
    return value


def _digest(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise _error("İşlem özeti geçersiz.")
    return value


def _text(value, maximum, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise _error("Metin alanı boş veya izin verilen uzunluğu aşıyor.")
    return value


def _integer(value, minimum=1, maximum=2147483647):
    if type(value) is not int or not minimum <= value <= maximum:
        raise _error("Sayısal alan izin verilen aralıkta değil.")
    return value


def _json(value) -> str:
    """Studio'nun canonical() işleviyle aynı kanonik JSON: özetler iki tarafta aynı çıkar."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise _error("JSON içeriği geçersiz.") from None


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _office_name(value):
    if (not isinstance(value, str) or value != value.strip() or not 2 <= len(value) <= 120 or "  " in value
            or "_" in value or not OFFICE_NAME.fullmatch(value)):
        raise _error("Ofis adı 2-120 karakter olmalı; harf, rakam, boşluk ve & . , ' ( ) / + - kullanılabilir.")
    return value


def _timestamp(value):
    return None if value is None else value.astimezone(UTC).isoformat()


def _task(row, detail=True) -> dict:
    result = {"id": row["id"], "assignee_user_id": row["atanan_id"], "created_by": row["olusturan_id"],
              "title": row["baslik"], "priority": row["oncelik"], "status": row["durum"], "version": row["surum"],
              "created_at": _timestamp(row["olusturma"]), "started_at": _timestamp(row["baslama"]),
              "finished_at": _timestamp(row["bitis"]), "updated_at": _timestamp(row["guncelleme"])}
    if detail:
        result.update(instructions=row["talimat"], summary=row["sonuc_ozeti"])
    return result


def _approval(row, detail=True) -> dict:
    # The Studio session of the request never leaves the server.
    result = {"id": row["id"], "requester_user_id": row["isteyen_id"], "approver_user_id": row["onayci_id"],
              "run_id": row["calisma_id"], "workflow_id": row["akis_id"], "workflow_digest": row["akis_ozeti"],
              "inputs_digest": row["girdi_ozeti"], "preview_digest": row["onizleme_ozeti"],
              "binding_digest": row["baglam_ozeti"], "attempt": row["deneme"], "status": row["durum"],
              "version": row["surum"], "decision_reason": row["karar_nedeni"],
              "created_at": _timestamp(row["olusturma"]), "expires_at": _timestamp(row["gecerlilik"]),
              "decided_at": _timestamp(row["karar_zamani"]), "consumed_at": _timestamp(row["tuketim_zamani"])}
    if detail:
        result.update(preview=json.loads(row["onizleme"]), reason=row["gerekce"])
    return result


def _scope(identity, column="ofis_id"):
    """Records of the office the request acts for; an account without an office reaches only records outside offices."""
    if identity.office_id:
        return " AND " + column + " = %s", [identity.office_id]
    return " AND " + column + " IS NULL", []


def _can(identity, capability) -> bool:
    return capability in identity.entitlement["yetkiler"]


@contextmanager
def _transaction(core, authenticated, capability="agents.use"):
    """`authenticated` is the request's identity from the HTTP layer; the block gets the one checked under lock."""
    with core.db.transaction() as cur:
        identity = relock(core, cur, authenticated)
        require(identity, capability)
        yield cur, identity


def _event(core, cur, identity, kind, record_id, event, details, *, system=False):
    cur.execute("INSERT INTO ajan_olaylari (id, sahip_id, kayit_turu, kayit_id, kullanici_id, aktor, olay, ayrinti, "
                "olusturma) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
                (uuid.uuid4().hex, identity.user["id"], kind, record_id, None if system else identity.user["id"],
                 "system" if system else "human", event, _json(details), core.clock.now()))


# ----- human tasks: the owner's own work items ------------------------------------------------------------------
def _find_task(cur, identity, task_id):
    scope, params = _scope(identity)
    cur.execute("SELECT * FROM ajan_gorevleri WHERE id = %s AND sahip_id = %s" + scope + " FOR UPDATE",
                [_id(task_id), identity.user["id"], *params])
    row = cur.fetchone()
    if (row is None or row["durum"] == DELETED
            or (not _can(identity, "agents.manage") and row["atanan_id"] != identity.user["id"])):
        raise _error("Görev bulunamadı.", 404, "BULUNAMADI")
    return row


def create_task(core, authenticated, data):
    payload = {"request_id": _id(data["request_id"]), "title": _text(data["title"], 160),
               "instructions": _text(data["instructions"], 20000, empty=True),
               "assignee_user_id": _integer(data["assignee_user_id"]), "priority": data.get("priority", "normal")}
    if payload["priority"] not in ("low", "normal", "high"):
        raise _error("Görev önceliği geçersiz.")
    request_digest = _hash(payload)
    with _transaction(core, authenticated, "agents.manage") as (cur, identity):
        me = identity.user["id"]
        cur.execute("SELECT * FROM ajan_gorevleri WHERE olusturan_id = %s AND istek_id = %s FOR UPDATE",
                    (me, payload["request_id"]))
        existing = cur.fetchone()
        if existing:
            if existing["istek_ozeti"] != request_digest:
                raise _error("İstek kimliği farklı bir görev için kullanılmış.", 409, "ISTEK_CAKISMASI")
            return _task(existing)
        # There are no colleagues: a task is always the owner's own.
        if payload["assignee_user_id"] != me:
            raise _error("Görevi yalnız kendinize atayabilirsiniz.", 403, "ATAMA_YETKISI_YOK")
        task_id, now = uuid.uuid4().hex, core.clock.now()
        cur.execute("INSERT INTO ajan_gorevleri (id, sahip_id, ofis_id, olusturan_id, atanan_id, istek_id, "
                    "istek_ozeti, baslik, talimat, oncelik, olusturma, guncelleme) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (task_id, me, identity.office_id, me, me, payload["request_id"], request_digest, payload["title"],
                     payload["instructions"], payload["priority"], now, now))
        _event(core, cur, identity, "task", task_id, "created", {"assignee_user_id": me, "version": 1})
        return _task(_find_task(cur, identity, task_id))


def get_task(core, authenticated, task_id):
    with _transaction(core, authenticated) as (cur, identity):
        return _task(_find_task(cur, identity, task_id))


def list_tasks(core, authenticated, status=None, limit=100):
    _integer(limit, maximum=100)
    if status is not None and status not in TASK_STATES:
        raise _error("Görev durumu geçersiz.")
    with _transaction(core, authenticated) as (cur, identity):
        scope, params = _scope(identity)
        sql = "SELECT * FROM ajan_gorevleri WHERE sahip_id = %s AND durum <> %s" + scope
        params = [identity.user["id"], DELETED, *params]
        if not _can(identity, "agents.manage"):
            sql += " AND atanan_id = %s"
            params.append(identity.user["id"])
        if status:
            sql += " AND durum = %s"
            params.append(status)
        cur.execute(sql + " ORDER BY olusturma DESC, id DESC LIMIT %s", [*params, limit])
        return [_task(row, detail=False) for row in cur.fetchall()]


def transition_task(core, authenticated, task_id, status, version, summary=""):
    _integer(version)
    _text(summary, 4000, empty=True)
    with _transaction(core, authenticated) as (cur, identity):
        row = _find_task(cur, identity, task_id)
        if row["surum"] != version:
            raise _error("Görev başka bir işlemde güncellendi; yeniden yükleyin.", 409, "SURUM_CAKISMASI")
        if status not in TRANSITIONS.get(row["durum"], ()):
            raise _error("Görevin bu durum geçişine izin verilmiyor.", 409, "DURUM_CAKISMASI")
        # Only the assignee reports work; on a migrated colleague's task the owner can cancel but not claim it.
        if row["atanan_id"] != identity.user["id"] and status != "cancelled":
            raise _error("Çalışma durumunu yalnız atanmış çalışan bildirebilir.", 403, "GOREV_YETKISI_YOK")
        now = core.clock.now()
        started = row["baslama"] or (now if status == "running" else None)
        finished = now if status in FINISHED_STATES else None
        cur.execute("UPDATE ajan_gorevleri SET durum = %s, surum = surum + 1, sonuc_ozeti = %s, baslama = %s, "
                    "bitis = %s, guncelleme = %s WHERE id = %s AND sahip_id = %s AND surum = %s",
                    (status, summary, started, finished, now, row["id"], identity.user["id"], version))
        if cur.rowcount != 1:
            raise _error("Görev başka bir işlemde güncellendi.", 409, "SURUM_CAKISMASI")
        _event(core, cur, identity, "task", row["id"], "status_changed",
               {"before": row["durum"], "after": status, "version": version + 1})
        return _task(_find_task(cur, identity, row["id"]))


def delete_task(core, authenticated, task_id, version):
    """Only a finished task is hidden; the row and its events remain for the audit trail."""
    _integer(version)
    with _transaction(core, authenticated, "agents.manage") as (cur, identity):
        row = _find_task(cur, identity, task_id)
        if row["surum"] != version:
            raise _error("Görev başka bir işlemde güncellendi; yeniden yükleyin.", 409, "SURUM_CAKISMASI")
        if row["durum"] not in FINISHED_STATES:
            raise _error("Yalnız tamamlanan, hata veren veya iptal edilen görev silinebilir; önce iptal edin.",
                         409, "DURUM_CAKISMASI")
        cur.execute("UPDATE ajan_gorevleri SET durum = %s, surum = surum + 1, guncelleme = %s "
                    "WHERE id = %s AND sahip_id = %s AND surum = %s",
                    (DELETED, core.clock.now(), row["id"], identity.user["id"], version))
        if cur.rowcount != 1:
            raise _error("Görev başka bir işlemde güncellendi.", 409, "SURUM_CAKISMASI")
        _event(core, cur, identity, "task", row["id"], "deleted", {"before": row["durum"], "version": version + 1})
        return {"id": row["id"], "deleted": True}


def task_metrics(core, authenticated, start, end):
    start, end = metrics.period(start, end)
    with _transaction(core, authenticated) as (cur, identity):
        # Only task identities, states, timestamps and state-transition events are read; joining the transitions
        # keeps the metrics independent of the last-100 task list.
        sql = ("SELECT t.id, t.atanan_id, t.durum, t.baslama, t.bitis, e.ayrinti AS olay_ayrinti, "
               "e.olusturma AS olay_zamani FROM ajan_gorevleri t "
               "LEFT JOIN ajan_olaylari e ON e.sahip_id = t.sahip_id AND e.kayit_id = t.id "
               "AND e.kayit_turu = 'task' AND e.olay = 'status_changed' "
               "WHERE t.sahip_id = %s AND t.durum <> %s AND t.olusturma >= %s AND t.olusturma < %s")
        scope, params = _scope(identity, "t.ofis_id")
        sql += scope
        params = [identity.user["id"], DELETED, start, end, *params]
        if not _can(identity, "agents.manage"):
            sql += " AND t.atanan_id = %s"
            params.append(identity.user["id"])
        cur.execute(sql + " ORDER BY t.id, e.olusturma, e.id", params)
        return metrics.calculate(iter(cur.fetchone, None), start, end, core.clock.now())


# ----- compatibility views of the former team screens -----------------------------------------------------------
def members(identity):
    """The team of an account is the account itself; it is its own approver."""
    return [{"kullanici_id": identity.user["id"], "ad_soyad": identity.user["ad_soyad"], "tur": "human",
             "onayci": identity.user["id"] == identity.entitlement["onayci_id"]}]


def office_people(core, authenticated):
    """Studio's 3D office shows the owner with grouped task states only; never task contents."""
    with _transaction(core, authenticated) as (cur, identity):
        me = identity.user["id"]
        scope, params = _scope(identity)
        cur.execute("SELECT durum, count(*) AS adet, min(id) AS gorev_id FROM ajan_gorevleri WHERE sahip_id = %s "
                    "AND atanan_id = %s AND durum IN ('queued', 'running', 'waiting_user')" + scope + " GROUP BY durum",
                    [me, me, *params])
        rows = {row["durum"]: row for row in cur.fetchall()}
        status = next((state for state in ("waiting_user", "running", "queued") if state in rows), "idle")
        return [{"id": "human-" + str(me), "user_id": me, "name": identity.user["ad_soyad"], "kind": "human",
                 "role": "human", "department": "", "status": status,
                 "task_id": rows[status]["gorev_id"] if status in rows else None, "task_label": LABELS[status],
                 "queue_count": rows["queued"]["adet"] if "queued" in rows else 0, "can_view_tasks": True}]


# ----- approvals ------------------------------------------------------------------------------------------------
def _find_approval(core, cur, identity, approval_id):
    scope, params = _scope(identity)
    cur.execute("SELECT * FROM ajan_onaylari WHERE id = %s AND sahip_id = %s" + scope + " FOR UPDATE",
                [_id(approval_id), identity.user["id"], *params])
    row = cur.fetchone()
    if row is None or (not _can(identity, "agents.approve") and row["isteyen_id"] != identity.user["id"]):
        raise _error("Onay kaydı bulunamadı.", 404, "BULUNAMADI")
    return _expire(core, cur, identity, row)


def _expire(core, cur, identity, row):
    """Lazy expiry: a reader rejects a pending or approved record that ran out of time or lost its approver."""
    now = core.clock.now()
    invalid_owner = row["onayci_id"] != identity.entitlement["onayci_id"]
    if row["durum"] in ("pending", "approved") and (row["gecerlilik"] <= now or invalid_owner):
        reason = "owner_changed" if invalid_owner else "timeout"
        cur.execute("UPDATE ajan_onaylari SET durum = 'rejected', karar_nedeni = %s, surum = surum + 1, "
                    "karar_zamani = %s WHERE id = %s AND sahip_id = %s AND durum IN ('pending', 'approved')",
                    (reason, now, row["id"], identity.user["id"]))
        _event(core, cur, identity, "approval", row["id"], reason, {}, system=True)
        row = dict(row, durum="rejected", karar_nedeni=reason, surum=row["surum"] + 1, karar_zamani=now)
    return row


def request_approval(core, authenticated, data):
    preview = data["preview"]
    preview_json = _json(preview)
    if not isinstance(preview, dict) or len(preview_json.encode("utf-8")) > 32768:
        raise _error("Maskelenmiş onay önizlemesi geçersiz veya çok büyük.")
    payload = {"request_id": _id(data["request_id"]), "run_id": _id(data["run_id"]),
               "workflow_id": _id(data["workflow_id"]), "workflow_digest": _digest(data["workflow_digest"]),
               "inputs_digest": _digest(data["inputs_digest"]), "preview": preview,
               "reason": _text(data["reason"], 2000), "attempt": _integer(data["attempt"], maximum=100),
               "expires_in": _integer(data.get("expires_in", 600), maximum=900)}
    preview_digest = _hash(preview)
    with _transaction(core, authenticated) as (cur, identity):
        me, owner = identity.user["id"], identity.entitlement["onayci_id"]
        payload["session_id"] = identity.entitlement["oturum_id"]
        request_digest = _hash(payload)
        cur.execute("SELECT * FROM ajan_onaylari WHERE isteyen_id = %s AND istek_id = %s FOR UPDATE",
                    (me, payload["request_id"]))
        existing = cur.fetchone()
        if existing:
            if existing["istek_ozeti"] != request_digest:
                raise _error("İstek kimliği farklı bir onay için kullanılmış.", 409, "ISTEK_CAKISMASI")
            return _approval(_expire(core, cur, identity, existing))
        approval_id, now = uuid.uuid4().hex, core.clock.now()
        expires = now + timedelta(seconds=payload["expires_in"])
        binding = {"company_id": identity.entitlement["firma_id"], "requester_id": me, "owner_id": owner,
                   "session_id": payload["session_id"], "run_id": payload["run_id"],
                   "workflow_id": payload["workflow_id"], "workflow_digest": payload["workflow_digest"],
                   "inputs_digest": payload["inputs_digest"], "preview_digest": preview_digest,
                   "attempt": payload["attempt"], "expires_at": _timestamp(expires), "approval_id": approval_id}
        if identity.office_id:
            binding["office_id"] = identity.office_id
        cur.execute("INSERT INTO ajan_onaylari (id, sahip_id, ofis_id, isteyen_id, onayci_id, oturum_id, istek_id, "
                    "istek_ozeti, calisma_id, akis_id, akis_ozeti, girdi_ozeti, onizleme_ozeti, baglam_ozeti, "
                    "onizleme, gerekce, deneme, olusturma, gecerlilik) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (approval_id, me, identity.office_id, me, owner, payload["session_id"], payload["request_id"],
                     request_digest, payload["run_id"], payload["workflow_id"], payload["workflow_digest"],
                     payload["inputs_digest"], preview_digest, _hash(binding), preview_json, payload["reason"],
                     payload["attempt"], now, expires))
        _event(core, cur, identity, "approval", approval_id, "requested", {"binding_digest": _hash(binding)})
        return _approval(_find_approval(core, cur, identity, approval_id))


def get_approval(core, authenticated, approval_id):
    with _transaction(core, authenticated) as (cur, identity):
        return _approval(_find_approval(core, cur, identity, approval_id))


def list_approvals(core, authenticated, limit=100):
    _integer(limit, maximum=100)
    with _transaction(core, authenticated) as (cur, identity):
        scope, params = _scope(identity)
        sql = "SELECT * FROM ajan_onaylari WHERE sahip_id = %s" + scope
        params = [identity.user["id"], *params]
        if not _can(identity, "agents.approve"):
            sql += " AND isteyen_id = %s"
            params.append(identity.user["id"])
        cur.execute(sql + " ORDER BY olusturma DESC, id DESC LIMIT %s FOR UPDATE", [*params, limit])
        return [_approval(_expire(core, cur, identity, row), detail=False) for row in cur.fetchall()]


def decide_approval(core, authenticated, approval_id, decision, version):
    _integer(version)
    if decision not in ("approve", "reject"):
        raise _error("Onay kararı geçersiz.")
    target = "approved" if decision == "approve" else "rejected"
    denied = result = None
    with _transaction(core, authenticated, "agents.approve") as (cur, identity):
        row = _find_approval(core, cur, identity, approval_id)
        if row["durum"] == target and row["karar_nedeni"] == "user" and row["surum"] == version + 1:
            return _approval(row)
        if row["durum"] != "pending" or row["surum"] != version:
            # Raised after the commit, so that an expiry written above is kept.
            denied = _error("Onay başka bir işlemde güncellenmiş veya süresi dolmuş.", 409, "SURUM_CAKISMASI")
        else:
            now = core.clock.now()
            cur.execute("UPDATE ajan_onaylari SET durum = %s, karar_nedeni = 'user', surum = surum + 1, "
                        "karar_zamani = %s WHERE id = %s AND sahip_id = %s AND durum = 'pending' AND surum = %s",
                        (target, now, row["id"], identity.user["id"], version))
            if cur.rowcount != 1:
                raise _error("Onay başka bir işlemde güncellendi.", 409, "SURUM_CAKISMASI")
            _event(core, cur, identity, "approval", row["id"], target, {"version": version + 1})
            result = _approval(dict(row, durum=target, karar_nedeni="user", karar_zamani=now, surum=version + 1))
    if denied:
        raise denied
    return result


def consume_approval(core, authenticated, approval_id, data):
    expected = {"calisma_id": _id(data["run_id"]), "akis_id": _id(data["workflow_id"]),
                "akis_ozeti": _digest(data["workflow_digest"]), "girdi_ozeti": _digest(data["inputs_digest"]),
                "onizleme_ozeti": _digest(data["preview_digest"]), "deneme": _integer(data["attempt"], maximum=100)}
    denied = result = None
    with _transaction(core, authenticated) as (cur, identity):
        row = _find_approval(core, cur, identity, approval_id)
        if row["isteyen_id"] != identity.user["id"] or row["oturum_id"] != identity.entitlement["oturum_id"]:
            raise _error("Onay yalnız isteği açan Studio oturumunda kullanılabilir.", 403, "ONAY_OTURUMU")
        if any(row[field] != value for field, value in expected.items()):
            raise _error("Akış veya girdiler değişmiş; yeni onay gerekli.", 409, "ONAY_UYUSMUYOR")
        if row["durum"] != "approved":
            denied = _error("Onay verilmemiş, tüketilmiş veya süresi dolmuş.", 409, "ONAY_KULLANILAMAZ")
        else:
            now = core.clock.now()
            cur.execute("UPDATE ajan_onaylari SET durum = 'consumed', surum = surum + 1, tuketim_zamani = %s "
                        "WHERE id = %s AND sahip_id = %s AND durum = 'approved' AND surum = %s",
                        (now, row["id"], identity.user["id"], row["surum"]))
            if cur.rowcount != 1:
                raise _error("Onay başka bir işlemde tüketilmiş.", 409, "ONAY_KULLANILAMAZ")
            _event(core, cur, identity, "approval", row["id"], "consumed", {"binding_digest": row["baglam_ozeti"]})
            result = _approval(dict(row, durum="consumed", tuketim_zamani=now, surum=row["surum"] + 1))
    if denied:
        raise denied
    return result


def cancel_approval(core, authenticated, approval_id):
    with _transaction(core, authenticated) as (cur, identity):
        row = _find_approval(core, cur, identity, approval_id)
        if row["isteyen_id"] != identity.user["id"] or row["oturum_id"] != identity.entitlement["oturum_id"]:
            raise _error("Onayı yalnız isteği açan Studio oturumu geri çekebilir.", 403, "ONAY_OTURUMU")
        if row["durum"] in ("rejected", "consumed"):
            return _approval(row)
        now = core.clock.now()
        cur.execute("UPDATE ajan_onaylari SET durum = 'rejected', karar_nedeni = 'withdrawn', surum = surum + 1, "
                    "karar_zamani = %s WHERE id = %s AND sahip_id = %s AND durum IN ('pending', 'approved') "
                    "AND surum = %s", (now, row["id"], identity.user["id"], row["surum"]))
        if cur.rowcount != 1:
            raise _error("Onay başka bir işlemde güncellendi.", 409, "SURUM_CAKISMASI")
        _event(core, cur, identity, "approval", row["id"], "withdrawn", {"version": row["surum"] + 1})
        return _approval(dict(row, durum="rejected", karar_nedeni="withdrawn", karar_zamani=now,
                              surum=row["surum"] + 1))


# ----- the account's offices --------------------------------------------------------------------------------------
def _office_row(cur, identity):
    if not identity.office_id:
        raise _error("Önce ofisinizi kurun.", 409, "OFIS_YOK")
    cur.execute("SELECT id, ad FROM ajan_ofisleri WHERE id = %s AND sahip_id = %s FOR UPDATE",
                (identity.office_id, identity.user["id"]))
    row = cur.fetchone()
    if row is None:
        raise _error("Ofis bulunamadı.", 404, "BULUNAMADI")
    return row


def my_office(core, authenticated):
    """The office on screen; its only person is the owner (no colleagues, no candidates)."""
    with _transaction(core, authenticated) as (cur, identity):
        me, can_create = identity.user["id"], identity.entitlement.get("ofis_kurabilir") is True
        if not identity.office_id:
            return {"office": None, "can_create": can_create, "members": [], "candidates": []}
        row = _office_row(cur, identity)
        return {"office": {"id": row["id"], "name": row["ad"], "authority_user_id": me, "is_authority": True},
                "can_create": can_create,
                "members": [{"user_id": me, "name": identity.user["ad_soyad"], "authority": True}], "candidates": []}


def my_offices(core, authenticated):
    """Every office of the account with its open human tasks and waiting approvals; no contents."""
    with _transaction(core, authenticated) as (cur, identity):
        me = identity.user["id"]
        cur.execute("SELECT ofis_id, count(*) AS adet FROM ajan_gorevleri WHERE sahip_id = %s "
                    "AND durum IN ('queued', 'running', 'waiting_user') GROUP BY ofis_id", (me,))
        tasks = {row["ofis_id"]: row["adet"] for row in cur.fetchall()}
        cur.execute("SELECT ofis_id, count(*) AS adet FROM ajan_onaylari WHERE sahip_id = %s AND durum = 'pending' "
                    "AND gecerlilik > %s GROUP BY ofis_id", (me, core.clock.now()))
        approvals = {row["ofis_id"]: row["adet"] for row in cur.fetchall()}
        # "people" stays for earlier Studio versions, which print it next to every office.
        offices = [{"id": office["id"], "name": office["ad"], "is_authority": True, "people": 1,
                    "open_tasks": tasks.get(office["id"], 0), "pending_approvals": approvals.get(office["id"], 0)}
                   for office in identity.entitlement.get("ofisler") or []]
        return {"offices": offices, "can_create": identity.entitlement.get("ofis_kurabilir") is True}


def create_office(core, authenticated, name):
    """Any licensed account opens offices up to the limit and is the owner, authority and approver of each."""
    name = _office_name(name)
    with _transaction(core, authenticated) as (cur, identity):
        me, now = identity.user["id"], core.clock.now()
        # Closed offices count as well; the account row lock keeps the count and the name check race-free.
        cur.execute("SELECT id, ad FROM ajan_ofisleri WHERE sahip_id = %s FOR UPDATE", (me,))
        offices = cur.fetchall()
        if len(offices) >= MAX_OFFICES:
            raise _error("Hesabınızın ofis sınırına ulaşıldı.", 409, "OFIS_SINIRI")
        if any(row["ad"].casefold() == name.casefold() for row in offices):
            raise _error(SAME_NAME, 409, "OFIS_ADI")
        office_id = uuid.uuid4().hex
        try:
            cur.execute("INSERT INTO ajan_ofisleri (id, sahip_id, ad, olusturma, guncelleme) "
                        "VALUES (%s, %s, %s, %s, %s)", (office_id, me, name, now, now))
        except UniqueViolation:
            # The database's lower() differs from casefold() for a few letters (e.g. "İ"); both mean the same name.
            raise _error(SAME_NAME, 409, "OFIS_ADI") from None
        _event(core, cur, identity, "office", office_id, "office_created", {"name": name})
        return {"id": office_id, "name": name, "authority_user_id": me}


def rename_office(core, authenticated, name):
    name = _office_name(name)
    with _transaction(core, authenticated, "agents.manage") as (cur, identity):
        row = _office_row(cur, identity)
        cur.execute("SELECT ad FROM ajan_ofisleri WHERE sahip_id = %s AND id <> %s FOR UPDATE",
                    (identity.user["id"], row["id"]))
        if any(item["ad"].casefold() == name.casefold() for item in cur.fetchall()):
            raise _error(SAME_NAME, 409, "OFIS_ADI")
        try:
            cur.execute("UPDATE ajan_ofisleri SET ad = %s, guncelleme = %s WHERE id = %s AND sahip_id = %s",
                        (name, core.clock.now(), row["id"], identity.user["id"]))
        except UniqueViolation:
            raise _error(SAME_NAME, 409, "OFIS_ADI") from None
        _event(core, cur, identity, "office", row["id"], "office_renamed", {"name": name})
        return {"id": row["id"], "name": name}


def delete_office(core, authenticated, name):
    """The owner deletes an office with no open human task and no waiting approval; the typed name confirms it.

    Finished tasks, decided approvals and the audit keep their rows; the name can be used again."""
    with _transaction(core, authenticated, "agents.manage") as (cur, identity):
        row = _office_row(cur, identity)
        if " ".join(str(name or "").split()) != row["ad"]:
            raise _error("Silmek için ofisin adını aynen yazın.", 400, "OFIS_ADI_ONAYI")
        me = identity.user["id"]
        cur.execute("SELECT count(*) AS adet FROM ajan_gorevleri WHERE sahip_id = %s AND ofis_id = %s "
                    "AND durum IN ('queued', 'running', 'waiting_user')", (me, row["id"]))
        if cur.fetchone()["adet"]:
            raise _error("Ofiste açık insan görevleri var; önce tamamlayın veya silin.", 409, "ACIK_GOREV")
        cur.execute("SELECT count(*) AS adet FROM ajan_onaylari WHERE sahip_id = %s AND ofis_id = %s "
                    "AND durum IN ('pending', 'approved') AND gecerlilik > %s", (me, row["id"], core.clock.now()))
        if cur.fetchone()["adet"]:
            raise _error("Ofiste bekleyen onaylar var; önce karar verin veya sürelerinin dolmasını bekleyin.", 409,
                         "BEKLEYEN_ONAY")
        cur.execute("DELETE FROM ajan_ofisleri WHERE id = %s AND sahip_id = %s", (row["id"], me))
        _event(core, cur, identity, "office", row["id"], "office_deleted", {"name": row["ad"]})
        return {"id": row["id"], "name": row["ad"], "deleted": True}
