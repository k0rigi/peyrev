# Ortak model (LLM)

Studio ajanlarının "Peyrev sunucusu" bağlantısı bu modülü kullanır. Sunucu yalnız modelin yanıtını üretir; modelin
istediği araç çağrıları Studio'ya **veri olarak** döner ve sunucuda hiçbir zaman çalıştırılmaz. Özellik varsayılan
olarak kapalıdır: ayar dosyası yoksa `GET /llm/models` `enabled:false` döner, iş uçları 503 `LLM_KAPALI` verir
(orkestrai.net'te de kapalıydı; CPU sınaması 90 sn sınırını geçemedi).

## Uçlar (`/api/studio/ekip`, Studio sözleşmesi orkestrai ile aynı)

| Uç | Yanıt |
| --- | --- |
| `GET /llm/models` | 200 `{success, models, enabled, queue_limit:4, parallel:1, context_tokens, max_output_tokens, reserve_tokens:512, estimation:"utf8_bytes"}`; kapalıyken `models:[]` ve varsayılan sınırlar |
| `POST /llm/jobs` | 202 `{success, job}`; gövde `{request_id, model, messages, tools, max_tokens}` |
| `GET /llm/jobs/<32 hex>` | 200 `{success, job}` |
| `DELETE /llm/jobs/<32 hex>` | 200 `{success, job}`; durum `cancelled`, istek ve sonuç silinir |

İş nesnesi `{id, status, expires_at}`; `succeeded` iken `result:{text, tool_calls, usage}`, `failed` iken
`error, error_code` (metin koddan üretilir, sağlayıcı ayrıntısı aktarılmaz). Denetim sırası: gövde > 128 KiB → 413
`GOVDE_SINIRI`; sorguda `firma_id/company_id/kullanici_id/user_id` → 400; ajan kimliği (401/403); gönderimde gövde
denetimleri (JSON türü 400, > 64 KiB 413 `LLM_BOYUT_SINIRI`, bozuk JSON 400) kapalılıktan önce, iş kimliği
denetimi kapalılıktan sonra gelir. Kurallar ve hata kodları `protocol.py` ve `routes.py` başlıklarındadır.

## Ayar dosyası: `/etc/peyrev/llm.env`

```
LLM_MODELS='["qwen3:1.7b"]'
LLM_CONTEXT_TOKENS=8192
LLM_MAX_OUTPUT_TOKENS=1024
OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_NO_CLOUD=1
```

- `LLM_MODELS`: izinli modeller (JSON listesi, en çok 20; `-cloud` ile biten ad kabul edilmez). Boşsa özellik kapalı.
- `LLM_CONTEXT_TOKENS` 2048-32768 (8192), `LLM_MAX_OUTPUT_TOKENS` 1-4096 (1024); çıktı + 512 < bağlam.
- `OLLAMA_URL` yalnız `http://127.0.0.1:<port>` ya da `http://[::1]:<port>` (ad, yol, sorgu, kullanıcı bilgisi yok).
- `OLLAMA_NO_CLOUD=1` zorunludur; Ollama hizmeti de `OLLAMA_NO_CLOUD=1` ile çalışmalıdır. Her işte, görev içeriği
  gönderilmeden önce `/api/status` (`cloud.disabled`) ve `/api/show` (yerel model, araç yeteneği) yeniden denetlenir.
- Güven kuralı: `root:peyrev 0640` (ya da hizmet hesabının kendi dosyası); sembolik bağ, ikinci bağlantı, 8 KiB'den
  büyük dosya, grup yazma ya da diğerlerine okuma/yazma izni, grup/diğerlerinin yazabildiği üst dizin özelliği kapatır.
  Bilinmeyen ya da yinelenen anahtar, değerde `$`, `\`, ters tırnak ya da denetim karakteri de özelliği kapatır.
  Dosyada sır yoktur.
- API dosyayı her istekte okur. Dosya değişince çalıştırıcı yeniden başlatılmalıdır; o zamana kadar ayar izleri
  tutmadığından özellik kapalı görünür.

## Kuyruk (`llm_isler`, geçiş `0005_ortak_model.sql`)

Gunicorn birden çok süreçle çalıştığından işler PostgreSQL'dedir. Tek çıkarım, en çok 4 bekleyen iş, 128 iş kaydı,
kullanıcı başına 2 ve oturum başına 1 bekleyen/çalışan iş (aşılırsa 429 `LLM_KUYRUK_DOLU`). Aynı oturum ve
`request_id` aynı içerikle aynı işi döndürür, farklı içerik 409 alır. İş, durumu ne olursa olsun gönderimden
300 sn sonra silinir (her API isteği ve çalıştırıcı siler). İstek içeriği çalıştırıcı işi alınca silinir. Tablolar
`UNLOGGED`'dır; gece yedeği `pg_dump --no-unlogged-table-data` ile alınmalıdır.

## Çalıştırıcı: `python -m peyrev.llm.worker`

Yalnız standart kütüphane ve psycopg kullanır; tek örnek çalışır (PostgreSQL oturum kilidi), işleri sırayla
Ollama'ya gönderir, sonucu doğrular, iptal ve süre aşımını (90 sn) uygular, süresi dolan işleri siler, 2 sn'de bir
yaşam işareti yazar (`llm_calistirici`; 15 sn'den eski işaret "çalışmıyor" sayılır). Çıktıya yalnız sabit satırlar
yazar. Çıkış kodu: düzgün durunca 0; ayar kapalı ya da güvenilmez, ikinci örnek, veritabanı hatası 1.

Ortam: `PEYREV_DATABASE_URL` ya da `/etc/peyrev/db.env` (`DATABASE_URL`); `PEYREV_SECRETS_DIR` (varsayılan
`/etc/peyrev`, ayar `llm.env`). Önerilen birim (ortak model açılana kadar etkinleştirilmez):

```ini
[Unit]
Description=Peyrev ortak model çalıştırıcısı
After=network.target postgresql.service

[Service]
Type=simple
User=peyrev
Group=peyrev
WorkingDirectory=/opt/peyrev/app
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/opt/peyrev/venv/bin/python -B -m peyrev.llm.worker
Restart=on-failure
RestartSec=5
TimeoutStopSec=10
KillMode=control-group
UMask=0077
LimitCORE=0
MemoryAccounting=true
MemoryMax=256M
TasksMax=64
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
IPAddressDeny=any
IPAddressAllow=localhost

[Install]
WantedBy=multi-user.target
```

`peyrev` paketi API ile aynı biçimde içe aktarılabilmelidir (venv'e kurulu ya da
`Environment=PYTHONPATH=/opt/peyrev/app/src`). Çalıştırıcı diske yazmaz; ağda yalnız PostgreSQL'e ve
`127.0.0.1:11434`'e bağlanır. Ollama'nın kurulumu ve yönetimi bu modülün işi değildir.

## Yönetim paneli

`admin.status(core)` durum, çalıştırıcı ve iş sayılarını verir (içerik yok). `admin.clear_queue(core, yonetici_id)`
bekleyen ve çalışan işleri iptal eder ve `yonetim_olaylari`'na `llm_kuyruk_bosaltma` olayı yazar.
