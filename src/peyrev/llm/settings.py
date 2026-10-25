"""Ortak model ayarları: yalnız operatörün ayar dosyasından (/etc/peyrev/llm.env) okunur; dosya yoksa özellik kapalıdır.

Biçim: KEY=VALUE satırları; boş satır ve # yorumu serbest; değer tek ya da çift tırnaklı olabilir; dosya komut olarak
çalıştırılmaz ve sır içermez.
    LLM_MODELS='["qwen3:1.7b"]'          izinli modeller, JSON listesi (en çok 20; boş ya da bozuksa kapalı)
    LLM_CONTEXT_TOKENS=8192              bağlam, 2048-32768 (varsayılan 8192)
    LLM_MAX_OUTPUT_TOKENS=1024           çıktı sınırı, 1-4096 (varsayılan 1024); çıktı + 512 bağlamdan küçük olmalı
    OLLAMA_URL=http://127.0.0.1:11434    yalnız yerel döngü arayüzü (varsayılan bu)
    OLLAMA_NO_CLOUD=1                    zorunlu: Ollama hizmeti bulut erişimi kapalı çalışır (her işte denetlenir)

Güven kuralı (kaynak orkestrai `api/ajanlar/llm_settings.py`): dosya sembolik bağ olmayan, tek bağlantılı, en çok 8 KiB
normal dosyadır; sahibi root ya da hizmetin kendi hesabıdır; izin bitleri 0640'ı aşmaz (grup yazamaz, diğerleri okuyamaz
ve yazamaz). Üst dizin root'a ya da hizmet hesabına aittir ve grup/diğerleri yazamaz. Önerilen: /etc/peyrev
root:peyrev 0750, llm.env root:peyrev 0640. Kural dışı dosya, bilinmeyen ya da yinelenen anahtar, yasak karakter
($, `, \\ ve denetim karakterleri) veya geçersiz değer özelliği kapatır (`enabled:false`).

API dosyayı her istekte yeniden okur. Çalıştırıcı başlarken okur ve ayarın izini yaşam işaretine yazar; izler tutmazsa
(dosya değişti, çalıştırıcı yeniden başlatılmadı) API özelliği kapalı gösterir (orkestrai'deki ready.json kapısı).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .backend import DEFAULT_OLLAMA_URL, local_url
from .protocol import DEFAULT_CONTEXT, DEFAULT_OUTPUT, LLMError, model_limits, model_names, unavailable

SETTINGS_LIMIT = 8192
ALLOWED_KEYS = frozenset(("LLM_MODELS", "LLM_CONTEXT_TOKENS", "LLM_MAX_OUTPUT_TOKENS", "OLLAMA_URL", "OLLAMA_NO_CLOUD"))


@dataclass(frozen=True)
class LLMConfig:
    """Ayrıştırılmış ayar. `models` boşsa özellik kapalıdır ve sınırlar varsayılandır."""

    models: tuple[str, ...] = ()
    context_tokens: int = DEFAULT_CONTEXT
    max_output_tokens: int = DEFAULT_OUTPUT
    ollama_url: str = DEFAULT_OLLAMA_URL
    fingerprint: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.models)


def fingerprint(settings: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def trusted_file(info: os.stat_result) -> bool:
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= SETTINGS_LIMIT
            and info.st_uid in (0, os.geteuid()) and not stat.S_IMODE(info.st_mode) & ~0o640)


def parse_settings(raw: bytes) -> dict[str, str]:
    if not isinstance(raw, bytes) or len(raw) > SETTINGS_LIMIT:
        raise unavailable()
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError:
        raise unavailable() from None
    result: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in ALLOWED_KEYS or key in result:
            raise unavailable()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise unavailable()
            value = value[1:-1]
        if any(ord(char) < 32 for char in value) or "\\" in value or "`" in value or "$" in value:
            raise unavailable()
        result[key] = value
    return result


def read_settings(path: Path) -> dict[str, str] | None:
    """Dosya yoksa None; güven kuralına uymuyorsa ya da bozuksa LLMError."""
    parent = descriptor = None
    try:
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(parent)
        if info.st_uid not in (0, os.geteuid()) or stat.S_IMODE(info.st_mode) & 0o022:
            raise unavailable()
        # O_NONBLOCK: a FIFO in place of the file must not block the request; it is rejected as not regular.
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        if not trusted_file(os.fstat(descriptor)):
            raise unavailable()
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            return parse_settings(stream.read(SETTINGS_LIMIT + 1))
    except FileNotFoundError:
        return None
    except OSError:
        raise unavailable() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)


def load(path: Path) -> LLMConfig:
    """Dosya yoksa, güvenilir değilse ya da herhangi bir değer geçersizse kapalı ayar döner (hata fırlatmaz)."""
    try:
        values = read_settings(Path(path))
        if values is None or values.get("OLLAMA_NO_CLOUD") != "1":
            return LLMConfig()
        models = model_names(values)
        if not models:
            return LLMConfig()
        context, output = model_limits(values)
        url = local_url(values.get("OLLAMA_URL", DEFAULT_OLLAMA_URL))
    except LLMError:
        return LLMConfig()
    return LLMConfig(models, context, output, url, fingerprint(values))
