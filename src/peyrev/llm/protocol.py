"""Ortak model (LLM) için sınırlı JSON sözleşmesi: sabitler, istek ve sonuç doğrulaması, hata kodları.

Kaynak orkestrai `api/ajanlar/llm_protocol.py`; sınırlar ve metinler Studio sözleşmesi gereği aynıdır. Yalnız standart
kütüphane kullanır: çalıştırıcı (python -m peyrev.llm.worker) da bu modülü yükler.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

REQUEST_LIMIT = 64 * 1024
RESULT_LIMIT = 128 * 1024
QUEUE_LIMIT = 4  # bekleyen iş; çalışan iş bu sayıya girmez (en çok 1 çalışan + 4 bekleyen)
JOB_LIMIT = 128  # süresi dolmamış bütün işler (bitmişler dahil)
USER_LIMIT = 2  # kullanıcı başına bekleyen + çalışan (orkestrai'de firma başınaydı)
JOB_TTL = 300  # gönderimden saniye; sonra iş, içerik ve tekrar anahtarı her durumda silinir
RESERVE_TOKENS = 512
DEFAULT_CONTEXT, DEFAULT_OUTPUT = 8192, 1024
NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CALL_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
UUID = re.compile(r"^[0-9a-f]{32}$")
NOT_FOUND = "Model işi bulunamadı veya saklama süresi doldu."
QUEUE_FULL = "Ortak model kuyruğu dolu; mevcut işin bitmesini bekleyin."
# Başarısız işin metni yalnız koddan üretilir; sağlayıcı ayrıntısı hiçbir zaman saklanmaz ve aktarılmaz.
FAILURES = {"LLM_BAGLAM_SINIRI": "İstek modelin bağlam sınırını aşıyor; konuşmayı kısaltın.",
            "LLM_ZAMAN_ASIMI": "Model yanıt süresi doldu.",
            "LLM_KULLANIM_EKSIK": "Model kullanım bilgisi eksik.",
            "LLM_YANIT_GECERSIZ": "Model yanıtı doğrulanamadı.",
            "LLM_YEREL_GEREKLI": "Ortak modelin yerel kurulumu ve kapalı bulut erişimi doğrulanamadı.",
            "LLM_ARAC_DESTEGI": "Seçilen yerel model araç çağrılarını desteklemiyor.",
            "LLM_BOYUT_SINIRI": "Model içeriği izin verilen boyutu aşıyor.",
            "LLM_ERISIM": "Yerel model hizmeti yanıt vermedi."}


class LLMError(Exception):
    """İstemciye gösterilebilen, gizli veri içermeyen hata; uçlar ApiError biçimine çevirir."""

    def __init__(self, message: str = "Model isteği geçersiz.", status: int = 400, kod: str = "LLM_GECERSIZ_GIRDI"):
        super().__init__(message)
        self.status = status
        self.kod = kod


def unavailable() -> LLMError:
    return LLMError("Ortak model hizmeti henüz etkin değil veya şu anda kullanılamıyor.", 503, "LLM_KAPALI")


def too_large() -> LLMError:
    return LLMError("Model içeriği izin verilen boyutu aşıyor.", 413, "LLM_BOYUT_SINIRI")


def _pairs(items: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _constant(_value: str) -> Any:
    raise ValueError("constant")


def dumps(value: Any, limit: int) -> bytes:
    """Kanonik JSON (sıralı anahtar, boşluksuz, ensure_ascii=False); özet ve boyut bu baytlardan hesaplanır."""
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise LLMError() from None
    if len(raw) > limit:
        raise too_large()
    return raw


def loads(raw: bytes, limit: int) -> Any:
    """Sıkı okuma: yinelenen anahtar, NaN/Infinity, 10000'den çok değer ve 20'den derin yapı yok."""
    if not isinstance(raw, bytes) or len(raw) > limit:
        raise too_large()
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        stack, count = [(value, 0)], 0
        while stack:
            item, depth = stack.pop()
            count += 1
            if count > 10000 or depth > 20 or (isinstance(item, float) and not math.isfinite(item)):
                raise ValueError("structure")
            if isinstance(item, dict):
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                stack.extend((child, depth + 1) for child in item)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise LLMError() from None


def fields(value: Any, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> None:
    if not isinstance(value, dict) or set(required) - set(value) or set(value) - set(required) - set(optional):
        raise LLMError()


def text(value: Any, limit: int, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise LLMError()
    return value


def number(value: Any, minimum: int = 1, maximum: int = 4096) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise LLMError()
    return value


def name(value: Any) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise LLMError()
    return value


def call_id(value: Any) -> str:
    if not isinstance(value, str) or not CALL_ID.fullmatch(value):
        raise LLMError()
    return value


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not UUID.fullmatch(value):
        raise LLMError()
    return value


def model_names(settings: dict[str, str]) -> tuple[str, ...]:
    """Operatörün izin listesi; liste bozuksa boş döner ve özellik kapanır."""
    try:
        names = loads(settings.get("LLM_MODELS", "[]").encode("utf-8"), 8192)
        if not isinstance(names, list) or len(names) > 20:
            return ()
        result: list[str] = []
        for value in names:
            text(value, 200)
            if any(char.isspace() or ord(char) < 32 for char in value) or value.endswith("-cloud"):
                return ()
            if value not in result:
                result.append(value)
        return tuple(result)
    except LLMError:
        return ()


def model_limits(settings: dict[str, str]) -> tuple[int, int]:
    """(bağlam, çıktı) token sınırları; geçersizse LLMError 503 LLM_YAPILANDIRMA."""
    try:
        context = number(int(settings.get("LLM_CONTEXT_TOKENS", str(DEFAULT_CONTEXT))), minimum=2048, maximum=32768)
        output = number(int(settings.get("LLM_MAX_OUTPUT_TOKENS", str(DEFAULT_OUTPUT))), minimum=1, maximum=4096)
    except (TypeError, ValueError, LLMError):
        raise LLMError("Ortak model sınırları geçersiz.", 503, "LLM_YAPILANDIRMA") from None
    if output + RESERVE_TOKENS >= context:
        raise LLMError("Ortak model çıktı sınırı bağlam için çok büyük.", 503, "LLM_YAPILANDIRMA")
    return context, output


def validate_request(value: Any, allowed_models: tuple[str, ...], max_output_tokens: int = DEFAULT_OUTPUT) -> dict:
    """Studio isteğini kanonik biçime getirir ve doğrular; hem API hem çalıştırıcı uygular."""
    value = loads(dumps(value, REQUEST_LIMIT), REQUEST_LIMIT)
    fields(value, ("request_id", "model", "messages", "tools", "max_tokens"))
    identifier(value["request_id"])
    text(value["model"], 200)
    if value["model"] not in allowed_models:
        raise LLMError("Seçilen model ortak sunucuda izinli değil.", 400, "LLM_MODEL_IZNI")
    number(value["max_tokens"], maximum=max_output_tokens)
    if not isinstance(value["tools"], list) or len(value["tools"]) > 32:
        raise LLMError()
    tool_names = set()
    for tool in value["tools"]:
        fields(tool, ("type", "name", "description", "parameters"))
        if tool["type"] != "function" or not isinstance(tool["parameters"], dict):
            raise LLMError()
        name(tool["name"])
        text(tool["description"], 2000, empty=True)
        if tool["name"] in tool_names:
            raise LLMError()
        tool_names.add(tool["name"])
    messages = value["messages"]
    if not isinstance(messages, list) or not 1 <= len(messages) <= 100:
        raise LLMError()
    pending: dict[str, str] = {}
    previous_ids: set[str] = set()
    for message in messages:
        fields(message, ("role", "content"), ("tool_calls", "tool_call_id", "name"))
        role = message["role"]
        if role not in ("system", "user", "assistant", "tool"):
            raise LLMError()
        text(message["content"], 40000, empty=True)
        if pending and role != "tool":
            raise LLMError("Araç sonuçları önceki araç çağrılarını karşılamalı.")
        if role == "tool":
            fields(message, ("role", "content", "tool_call_id", "name"))
            tool_id, tool_name = call_id(message["tool_call_id"]), name(message["name"])
            if pending.pop(tool_id, None) != tool_name:
                raise LLMError("Araç sonucu çağrı kimliğiyle eşleşmiyor.")
        elif role == "assistant":
            fields(message, ("role", "content"), ("tool_calls",))
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list) or len(calls) > 16:
                raise LLMError()
            for call in calls:
                fields(call, ("id", "name", "arguments"))
                tool_id, tool_name = call_id(call["id"]), name(call["name"])
                if tool_id in previous_ids or not isinstance(call["arguments"], dict):
                    raise LLMError()
                pending[tool_id] = tool_name
                previous_ids.add(tool_id)
        else:
            fields(message, ("role", "content"))
    if pending:
        raise LLMError("Araç çağrılarının sonuçları eksik.")
    return value


def validate_result(value: Any) -> dict:
    """Model sonucunu doğrular: {text, tool_calls, usage}; araç çağrıları yalnız veridir."""
    fields(value, ("text", "tool_calls", "usage"))
    text(value["text"], RESULT_LIMIT, empty=True)
    if not isinstance(value["tool_calls"], list) or len(value["tool_calls"]) > 16:
        raise LLMError()
    seen = set()
    for call in value["tool_calls"]:
        fields(call, ("id", "name", "arguments"))
        call_id(call["id"])
        name(call["name"])
        if call["id"] in seen or not isinstance(call["arguments"], dict):
            raise LLMError()
        seen.add(call["id"])
    usage = value["usage"]
    fields(usage, ("input_tokens", "output_tokens", "total_tokens"))
    for count in usage.values():
        number(count, minimum=0, maximum=2000000)
    if not value["text"] and not value["tool_calls"]:
        raise LLMError("Model boş yanıt verdi.", 502, "LLM_YANIT_GECERSIZ")
    if usage["output_tokens"] <= 0:
        raise LLMError("Model kullanım bilgisi eksik.", 502, "LLM_KULLANIM_EKSIK")
    if usage["input_tokens"] + usage["output_tokens"] != usage["total_tokens"]:
        raise LLMError()
    return loads(dumps(value, RESULT_LIMIT - 1024), RESULT_LIMIT - 1024)
