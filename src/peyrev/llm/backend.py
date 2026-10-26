"""Ollama yerel sohbet protokolü (yalnız çalıştırıcı kullanır); model araç çağrılarını hiçbir zaman yürütmez.

Her işte görev içeriği gönderilmeden önce `/api/status` ile bulut
erişiminin kapalı olduğu, `/api/show` ile modelin yerel kurulu olduğu doğrulanır; araç isteyen iş modelin `tools`
yeteneğini ister. Üç çağrının toplam süresi 90 sn'dir; zaman aşımı ya da iptalden sonra çıkarım yeniden denenmez.
Ortam vekili ve yönlendirme kullanılmaz; adres yalnız yerel döngü arayüzü olabilir.
"""

from __future__ import annotations

import errno
import threading
import time
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .protocol import (
    DEFAULT_CONTEXT,
    REQUEST_LIMIT,
    RESERVE_TOKENS,
    RESULT_LIMIT,
    LLMError,
    dumps,
    loads,
    name,
    number,
)

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
TIMEOUT = 90


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "Yönlendirme engellendi.", headers, None)


def local_url(value: str) -> str:
    """Ollama kök adresi yalnız http://127.0.0.1:<port> ya da http://[::1]:<port> olabilir (ad çözümlenmez)."""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1") or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                or not 1 <= (80 if parsed.port is None else parsed.port) <= 65535):
            raise ValueError("url")
    except (TypeError, ValueError, AttributeError):
        raise LLMError("Ollama adresi yalnız yerel döngü arayüzü olabilir (http://127.0.0.1:11434).", 503,
                       "LLM_YAPILANDIRMA") from None
    return "http://" + parsed.netloc


def runner_options(context_tokens: int) -> dict[str, int]:
    # Match the Ollama service's four-CPU quota for loading and inference.
    return {"num_ctx": context_tokens, "num_thread": 4}


def ollama_payload(request: dict, context_tokens: int = DEFAULT_CONTEXT) -> dict:
    messages = []
    for message in request["messages"]:
        converted = {"role": message["role"], "content": message["content"]}
        if message["role"] == "tool":
            converted["tool_name"] = message["name"]
        if message.get("tool_calls"):
            converted["tool_calls"] = [
                {"type": "function", "function": {"name": call["name"], "arguments": call["arguments"]}}
                for call in message["tool_calls"]]
        messages.append(converted)
    tools = [{"type": "function", "function": {key: tool[key] for key in ("name", "description", "parameters")}}
             for tool in request["tools"]]
    # Conservative byte budget instead of implicit context truncation; Studio computes the same estimate.
    prompt_size = len(dumps({"messages": messages, "tools": tools}, REQUEST_LIMIT))
    if prompt_size + request["max_tokens"] + RESERVE_TOKENS > context_tokens:
        raise LLMError("İstek modelin bağlam sınırını aşıyor; konuşmayı kısaltın.", 400, "LLM_BAGLAM_SINIRI")
    return {"model": request["model"], "messages": messages, "tools": tools, "stream": False, "truncate": False,
            "think": False, "options": dict(runner_options(context_tokens), num_predict=request["max_tokens"]),
            "keep_alive": -1}


def normalize_response(value: Any, allowed_tools: set[str]) -> dict:
    if not isinstance(value, dict) or value.get("done") is not True or not isinstance(value.get("message"), dict):
        raise LLMError("Model yanıtı tamamlanmamış veya geçersiz.", 502, "LLM_YANIT_GECERSIZ")
    if value.get("remote_host") or value.get("remote_model"):
        raise LLMError("Ortak model yalnız yerel çalışabilir.", 503, "LLM_YEREL_GEREKLI")
    if value.get("done_reason") == "length":
        raise LLMError("Model yanıtı çıktı sınırında kesildi; araç çağrısı kullanılmadı.", 502, "LLM_YANIT_GECERSIZ")
    message = value["message"]
    if message.get("role") != "assistant" or not isinstance(message.get("content", ""), str):
        raise LLMError("Model yanıtı geçersiz.", 502, "LLM_YANIT_GECERSIZ")
    calls = message.get("tool_calls", [])
    if not isinstance(calls, list) or len(calls) > 16:
        raise LLMError("Model araç yanıtı geçersiz.", 502, "LLM_YANIT_GECERSIZ")
    tools = []
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        if (not isinstance(function, dict) or not isinstance(function.get("name"), str)
                or function["name"] not in allowed_tools or not isinstance(function.get("arguments"), dict)):
            raise LLMError("Model izinli araç biçiminde yanıt vermedi.", 502, "LLM_YANIT_GECERSIZ")
        # The call is returned to Studio as data only; ids are issued here, never taken from the model.
        tools.append({"id": uuid.uuid4().hex, "name": name(function["name"]), "arguments": function["arguments"]})
    try:
        input_tokens = number(value.get("prompt_eval_count"), minimum=0, maximum=1000000)
        output_tokens = number(value.get("eval_count"), minimum=1, maximum=1000000)
    except LLMError:
        raise LLMError("Model kullanım bilgisi eksik.", 502, "LLM_KULLANIM_EKSIK") from None
    result = {"text": message.get("content", ""), "tool_calls": tools,
              "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                        "total_tokens": input_tokens + output_tokens}}
    if not result["text"] and not tools:
        raise LLMError("Model boş yanıt verdi.", 502, "LLM_YANIT_GECERSIZ")
    return loads(dumps(result, RESULT_LIMIT), RESULT_LIMIT)


class OllamaBackend:
    def __init__(self, url: str = DEFAULT_OLLAMA_URL, timeout: int = TIMEOUT, context_tokens: int = DEFAULT_CONTEXT,
                 opener=None):
        self.url = local_url(url)
        self.timeout = number(timeout, maximum=120)
        self.context_tokens = number(context_tokens, minimum=2048, maximum=32768)
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())

    def generate(self, request: dict, cancelled: threading.Event) -> dict:
        deadline = time.monotonic() + self.timeout
        self._check_local_model(request["model"], bool(request["tools"]), cancelled, deadline)
        payload = ollama_payload(request, self.context_tokens)
        value = self._read_json("/api/chat", payload, cancelled, deadline)
        result = normalize_response(value, {tool["name"] for tool in request["tools"]})
        if result["usage"]["output_tokens"] > request["max_tokens"]:
            raise LLMError("Model yanıtı istenen çıktı bütçesini aşıyor; araç çağrısı kullanılmadı.", 502,
                           "LLM_YANIT_GECERSIZ")
        return result

    def check_cloud(self, cancelled: threading.Event, deadline: float) -> None:
        state = self._read_json("/api/status", None, cancelled, deadline)
        cloud = state.get("cloud") if isinstance(state, dict) else None
        if not isinstance(cloud, dict) or cloud.get("disabled") is not True:
            raise LLMError("Ortak modelin bulut erişimi kapalı olmalı.", 503, "LLM_YEREL_GEREKLI")

    def _check_local_model(self, model: str, with_tools: bool, cancelled: threading.Event, deadline: float) -> None:
        self.check_cloud(cancelled, deadline)
        info = self._read_json("/api/show", {"model": model}, cancelled, deadline)
        if (not isinstance(info, dict) or info.get("remote_host") or info.get("remote_model")
                or not isinstance(info.get("model_info"), dict) or not info["model_info"]):
            raise LLMError("Ortak modelin yerel kurulumu doğrulanamadı.", 503, "LLM_YEREL_GEREKLI")
        if with_tools and (not isinstance(info.get("capabilities"), list) or "tools" not in info["capabilities"]):
            raise LLMError("Seçilen yerel model araç çağrılarını desteklemiyor.", 400, "LLM_ARAC_DESTEGI")

    def _read_json(self, path: str, payload: dict | None, cancelled: threading.Event, deadline: float) -> Any:
        url = self.url + path
        if cancelled.is_set():
            raise LLMError("Model isteği iptal edildi.", 409, "LLM_IPTAL")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LLMError("Model yanıt süresi doldu.", 504, "LLM_ZAMAN_ASIMI")
        http_request = Request(url, data=None if payload is None else dumps(payload, 68 * 1024),
                               headers={"Content-Type": "application/json", "Accept": "application/json"},
                               method="GET" if payload is None else "POST")
        response = None
        finished = threading.Event()

        def close_on_cancel():
            while not finished.wait(0.1):
                if cancelled.is_set() or time.monotonic() > deadline:
                    if response is not None:
                        try:
                            response.close()
                        except (OSError, ValueError):
                            pass
                    return

        watcher = threading.Thread(target=close_on_cancel, daemon=True)
        watcher.start()
        try:
            response = self.opener.open(http_request, timeout=remaining)
            with response:
                if response.status != 200 or response.geturl() != url:
                    raise LLMError("Yerel model hizmeti yanıt vermedi.", 502, "LLM_ERISIM")
                raw = response.read(RESULT_LIMIT + 1)
            if cancelled.is_set():
                raise LLMError("Model isteği iptal edildi.", 409, "LLM_IPTAL")
            if time.monotonic() > deadline:
                raise LLMError("Model yanıt süresi doldu.", 504, "LLM_ZAMAN_ASIMI")
            return loads(raw, RESULT_LIMIT)
        except LLMError:
            raise
        except (HTTPError, URLError, OSError, ValueError) as error:
            # Cancellation wins; the provider's reason text is never kept or shown.
            if cancelled.is_set():
                raise LLMError("Model isteği iptal edildi.", 409, "LLM_IPTAL") from None
            reason = error.reason if isinstance(error, URLError) else error
            timed_out = isinstance(reason, TimeoutError) or (isinstance(reason, OSError)
                                                             and reason.errno == errno.ETIMEDOUT)
            if timed_out or time.monotonic() >= deadline:
                raise LLMError("Model yanıt süresi doldu.", 504, "LLM_ZAMAN_ASIMI") from None
            raise LLMError("Yerel model hizmeti yanıt vermedi; işlem yürütülmedi.", 502, "LLM_ERISIM") from None
        finally:
            finished.set()
