# Peyrev sunucusu

peyrev.com'un merkezi sunucusu. Peyrev Studio (masaüstü akış stüdyosu) şu işler için buraya bağlanır:

- lisans ve hesap işlemleri, şifre değiştirme ve şifre kodu,
- bireysel başvurular,
- destek talepleri ve bildirimleri,
- ajan ofisleri, görevler, onaylar ve ortak model erişimi,
- imzalı güncelleme dosyaları ve indirme sayfası (`/indir/`).

Yönetim paneli `/yonetim/` adresindedir.

- Python 3.12, Flask 3, Gunicorn, PostgreSQL 16 (psycopg 3).
- Firma kavramı yoktur: her hesap bir kullanıcıdır. Modül hakları (`MOD_RPA`, `MOD_AJAN`) kullanıcıya bitiş tarihiyle
  verilir; `bitis` boşsa süresizdir ve bitiş günü (Türkiye saati) dahildir.
- Sırlar (veritabanı şifresi, JWT ve lisans imza anahtarları, bildirim bilgileri) depoya girmez. Sunucuda
  `/etc/peyrev/` altında `root:peyrev 0640` dosyalarda durur ve GitHub Secrets ile yönetilir.

## Geliştirme

```bash
uv venv && uv pip install -e ".[test,mysql]" ruff
pytest                      # yerelde gömülü PostgreSQL (pgserver), CI'da PEYREV_TEST_DATABASE_URL
ruff check .
```

Veritabanı yapısı `src/peyrev/migrations/NNNN_ad.sql` geçişleriyle kurulur (`python -m peyrev.migrate`). Her geçiş bir kez,
kendi işleminde uygulanır; dosyaların başında orkestrai (MariaDB) tablolarından eşleme yazılıdır.

## Sunucu düzeni

orkestrai.net ile aynı AlmaLinux 8 sunucusunda, ondan bağımsız çalışır:

| Parça | Yer |
| --- | --- |
| API | `peyrev.service` (systemd, `peyrev` hesabı), Gunicorn `127.0.0.1:5101` |
| Kod | `/opt/peyrev/releases/<commit>` (her sürüm kendi sanal ortamıyla), etkin sürüm `/opt/peyrev/current` |
| Python | uv ile `/opt/peyrev/python` (CPython 3.12); sistem Python'una dokunulmaz |
| Veritabanı | PostgreSQL 16 (yalnız localhost): `peyrev` ve taşıma provası için `peyrev_prova` |
| Sırlar | `/etc/peyrev/` (`db.env`, `jwt_secret`, `lisans_ed25519`, isteğe bağlı `destek.env`, `llm.env`) |
| Yedek | Her gece 03:30, `pg_dump` ve sırlar, 14 gün: `/var/backups/peyrev` (`/etc/cron.d/peyrev-yedek`) |
| Site | CyberPanel sitesi `peyrev.com`; OpenLiteSpeed `/api/` ve `/yonetim/` isteklerini API'ye iletir, gerisi statiktir |
| SSL | Let's Encrypt, peyrev.com + www.peyrev.com (acme.sh kendiliğinden yeniler); www ve http, https://peyrev.com'a yönlenir |

## İş akışları

Depo herkese açık olduğundan Actions günlükleri de herkese açıktır. Sunucuya dokunan iş akışları
`.github/scripts/uzak.py` ile çalışır: betiğin bütün çıktısı sahibin açık anahtarıyla şifrelenir
(`.github/rapor-anahtari.pub`) ve yapıt olarak saklanır. Günlükte yalnız `GENEL:` satırları (durum kodları gibi) görünür.
Sunucu SSH anahtarı betiklerde sabittir.

| İş akışı | Ne yapar |
| --- | --- |
| Testler | Çekme isteklerinde ve main dışındaki dallarda testler (PostgreSQL 16) |
| Dağıtım | main'e her push'ta testler, ardından dağıtım (`deploy/dagitim.sh`): sürüm klasörü, özet denetimli bağımlılıklar, geçişler, yeniden başlatma, sağlık denetimi ve gerekirse önceki sürüme dönüş. `DAGITIM_ACIK=1` depo değişkeniyle açılır |
| Sunucu işleri | `inceleme` ve `kaynak-inceleme` (salt okunur), `kurulum-deneme` / `kurulum` (`deploy/kurulum.sh`), `bakim-ac` / `bakim-kapat` (orkestrai'de taşıma bakımı), `tasima-prova` / `tasima-gercek` |
| Studio yayını | Studio paketlerini `/indir/` altına doğrulayarak yayımlar (`inspect`, `publish`, `prune`) |
| Destek bildirim ayarları | Telegram ve e-posta bilgilerini Secrets'tan `/etc/peyrev/destek.env` dosyasına yazar ve deneme bildirimi gönderir |

Her sunucu işi önce ve sonra orkestrai.net'in açılış sayfasını ve `/api/rpa/lisans/anahtar` ucunu denetler. Kurulum, aynı
sunucudaki diğer sitelerin durum kodlarını da karşılaştırır ve fark görürse o adımı geri alır.

Şifreli bir raporu açmak için (yalnız özel anahtarın sahibi):

```bash
python tools/rapor-coz.py <run_id> --anahtar "<Studio deposu>/credentials/peyrev-rapor-x25519.pem"
```
