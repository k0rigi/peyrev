# Peyrev sunucusu

peyrev.com'un merkezi sunucusu. Peyrev Studio (masaüstü akış stüdyosu) şu işler için buraya bağlanır:

- lisans ve hesap işlemleri, şifre değiştirme ve şifre kodu,
- bireysel başvurular,
- destek talepleri ve bildirimleri,
- ajan ofisleri, görevler, onaylar ve ortak model erişimi,
- imzalı güncelleme dosyaları ve indirme sayfası (`/indir/`).

İmzalı lisanstaki ürün adı `peyrev`dir. Lisans alabilen en eski Studio sürümü 1.1.0'dır (`MINIMUM_VERSION`); daha eski
sürümler giriş yapamaz, güncelleyicileri 1.1.0'ı kendiliğinden kurar.

Yönetim paneli `/yonetim/` adresindedir (aşağıda).

- Python 3.12, Flask 3, Gunicorn, PostgreSQL 16 (psycopg 3).
- Firma kavramı yoktur: her hesap bir kullanıcıdır. Modül hakları (`MOD_RPA`, `MOD_AJAN`) kullanıcıya bitiş tarihiyle
  verilir; `bitis` boşsa süresizdir ve bitiş günü (Türkiye saati) dahildir.
- Sırlar depoya girmez; sunucuda `/etc/peyrev/` altında `root:peyrev 0640` dosyalarda durur. Veritabanı şifresi
  (`db.env`), JWT sırrı ve lisans imza anahtarı sunucuda üretilir (kurulum ve Dağıtım; var olan asla ezilmez); lisans
  anahtarının kopyası yalnız aynı sunucudaki gece yedeğindedir. GitHub Secrets'ta yalnız SSH girişi (`HOST`, `USERNAME`,
  `PASSWORD`) ve bildirim bilgileri (`DESTEK_*`) bulunur.

## Yönetim paneli

`https://peyrev.com/yonetim/`; uçları `/api/yonetim/*` (`src/peyrev/admin/`). Bölümler: Özet, Kullanıcılar (hesap açma,
modül hakları, şifre kodu, Studio oturumları, ajan ofisleri), Başvurular, Destek, Olaylar ve Hesabım. Sayfa da API'den,
satır içi betik ve stil olmadan, sıkı güvenlik başlıklarıyla gelir.

- **Giriş:** yalnız durumu `aktif`, rolü `yonetici` olan hesap girer. Giriş adı ve şifre Studio'yla aynıdır, hatalı
  deneme sınırı da ortaktır (15 dakikada hesap başına 10, adres başına 30). Hesap sayacı, hesabın asıl giriş adıyla ya
  da @ öncesiyle yapılan hatalı denemeleri toplar: kısa ad, e-postanın tamamı ve büyük/küçük harf yazımları arasında
  geçmek yeni deneme hakkı vermez (Studio'nun şifre değiştirme ve şifre kodu uçlarında da). Hatalı girişte bilinmeyen
  ad ile tek hesaba uyan ad aynı sayıda şifre özeti denetler, yanıt süresi hesabın varlığını belli etmez; birden çok
  hesaba uyan kısa ad her fazla hesap için bir özet daha denetler. İlk yönetici eski sistemden taşınan yönetici
  hesabıdır; başka yönetici panelde rol verilerek açılır. Yönetici kendi
  hesabını pasif yapamaz, kendi yönetici rolünü kaldıramaz.
- **İki adımlı doğrulama:** isteğe bağlıdır ve Hesabım'dan açılır (TOTP; Google Authenticator, Microsoft Authenticator
  gibi uygulamalar). Açıkken girişte 6 haneli kod istenir ve her kod bir kez geçer. Kodunu kaybeden yöneticinin
  doğrulamasını başka bir yönetici, kullanıcı ayrıntısından sıfırlar.
- **Oturum:** girişten 12 saat sonra ya da 2 saat işlem yapılmazsa biter; yönetici başına en çok 10 oturum açık kalır.
  Belirteç yalnız `peyrev_yonetim` çerezindedir (HttpOnly, Secure, SameSite=Strict), sunucu özetini saklar. Şifre
  değişince, hesap pasifleşince ya da yönetici rolü kalkınca oturum kapanır.
- Durum değiştiren her istek `X-Peyrev-Yonetim: 1` başlığını ve `https://peyrev.com` kökenini taşır (CSRF). Her
  değişiklik `yonetim_olaylari`'na yazılır ve Olaylar bölümünde görünür; şifre, kod ve yanıt metni yazılmaz.

## Geliştirme

```bash
uv venv && uv pip install -e ".[test]" ruff
pytest                      # yerelde gömülü PostgreSQL (pgserver), CI'da PEYREV_TEST_DATABASE_URL
ruff check .
```

Veritabanı yapısı `src/peyrev/migrations/NNNN_ad.sql` geçişleriyle kurulur (`python -m peyrev.migrate`). Her geçiş bir kez,
kendi işleminde uygulanır; uygulanmış dosya değiştirilmez, değişiklik yeni dosyayla yapılır.

## Sunucu düzeni

Paylaşılan AlmaLinux 8 sunucusunda, oradaki diğer sitelerden bağımsız çalışır:

| Parça | Yer |
| --- | --- |
| API | `peyrev.service` (systemd, `peyrev` hesabı), Gunicorn `127.0.0.1:5101` |
| Kod | `/opt/peyrev/releases/<commit>` (her sürüm kendi sanal ortamıyla), etkin sürüm `/opt/peyrev/current` |
| Python | uv ile `/opt/peyrev/python` (CPython 3.12); sistem Python'una dokunulmaz |
| Veritabanı | PostgreSQL 16 (yalnız localhost): `peyrev` |
| Sırlar | `/etc/peyrev/` (`db.env`, `jwt_secret`, `lisans_ed25519`, isteğe bağlı `destek.env`, `llm.env`) |
| Yedek | Her gece 03:30, `pg_dump` ve sırlar, 14 gün: `/var/backups/peyrev` (`/etc/cron.d/peyrev-yedek`) |
| Site | CyberPanel sitesi `peyrev.com`; OpenLiteSpeed `/api/` ve `/yonetim/` isteklerini API'ye iletir, gerisi statiktir |
| SSL | Let's Encrypt, peyrev.com + www.peyrev.com (acme.sh kendiliğinden yeniler). www ve http, https://peyrev.com'a yönlenir: statik sayfalarda `.htaccess`, `/api/` ve `/yonetim/`'de www için uygulama (308); vekil düz http'yi uygulamaya bildirmediğinden panel http ile açılırsa betiği giriş formunu göstermeden https'e geçer. Uygulama yanıtları `Strict-Transport-Security` (7 gün) taşır |

## İş akışları

Depo herkese açık olduğundan Actions günlükleri de herkese açıktır. Sunucuya dokunan iş akışları
`.github/scripts/uzak.py` ile çalışır: betiğin bütün çıktısı sahibin açık anahtarıyla şifrelenir
(`.github/rapor-anahtari.pub`) ve yapıt olarak saklanır. Günlükte yalnız `GENEL:` satırları (durum kodları gibi) görünür.
Studio yayını bunun dışındadır: kendi betiğiyle (`.github/scripts/studio-yayin.py`) çalışır, şifreli rapor üretmez ve
günlüğünde sürümle durum özeti (JSON) görünür. Sunucu SSH anahtarı betiklerde sabittir.

| İş akışı | Ne yapar |
| --- | --- |
| Testler | Çekme isteklerinde ve main dışındaki dallarda testler (PostgreSQL 16) |
| Dağıtım | main'e her push'ta testler, ardından dağıtım (`deploy/dagitim.sh`): sürüm klasörü, özet denetimli bağımlılıklar, geçişler, yeniden başlatma, sağlık denetimi ve gerekirse önceki sürüme dönüş. `DAGITIM_ACIK=1` depo değişkeniyle açılır |
| Sunucu işleri | `kurulum-deneme` / `kurulum` (`deploy/kurulum.sh`), `deneme-hesabi-kapat` (`deploy/deneme-hesabi.sh`: uçtan uca denemenin `peyrev.deneme` hesabını pasif yapar, oturumlarını kapatır) |
| Studio yayını | Studio paketlerini `/indir/` altına doğrulayarak yayımlar (`inspect`, `publish`, `prune`). İki bildirim vardır: `guncelleme.manifest` (Studio 1.1.0 ve sonrası) indirme sayfasıyla, `stable.manifest` (1.0.x) sayfasız yayımlanır; aynı sürümün iki arşivi `releases/<sürüm>` klasörünü paylaşır, yayımlanmış dosya değişmez. `prune` yalnız iki bildirimin andığı sürümleri bırakır |
| Destek bildirim ayarları | Telegram ve e-posta bilgilerini Secrets'tan `/etc/peyrev/destek.env` dosyasına yazar ve deneme bildirimi gönderir |

Kurulum, aynı sunucudaki diğer sitelerin durum kodlarını karşılaştırır ve fark görürse o adımı geri alır.

## Eski sistem

Hesaplar, haklar, başvurular, destek talepleri ve ajan kayıtları 2026-10-26'da eski sistemden (orkestrai.net) bir kez
taşındı; taşıma aracı işini bitirdiği için depodan kaldırıldı. Taşınan hesapların kaynağı `tasima`'dır (panelde "Eski
sistemden taşındı"), kimlikleri ve şifreleri aynen geldi: eski biçimdeki şifre özetleri (`pbkdf2:sha256:260000`)
doğrulanmaya devam eder.

Eski sistem aynı gün sunucudan tamamen kaldırıldı (site, veritabanı, e-posta, DNS bölgesi, sertifikalar, servis);
Peyrev ona hiçbir şekilde bağlı değildir. 1.0.0'dan eski Studio sürümleri kendini güncelleyemez; Peyrev
peyrev.com/indir'den yeniden kurulur, akışlar ve ayarlar korunur.

Şifreli bir raporu açmak için (yalnız özel anahtarın sahibi):

```bash
python tools/rapor-coz.py <run_id> --anahtar "<Studio deposu>/credentials/peyrev-rapor-x25519.pem"
```
