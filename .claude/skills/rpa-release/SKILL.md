---
name: rpa-release
description: Peyrev Studio'nun (1.0.0'dan önceki adı RpaOrkestrAI) yeni sürümünü (ör. 1.0.0 → 1.1.0) derleyip peyrev.com/indir üzerinden yayınlamak ve kurulu uygulamalara otomatik güncelleme olarak ulaştırmak için kullan. Sürüm artırma, GitHub Actions derlemesi, imzalı bildirim, GitHub ön sürümü, peyrev deposundaki Studio yayını iş akışını ve köprü sürüm 1.0.0'ın orkestrai.net/rpa eski kanalını kapsar.
---

> Bu skill Studio deposundan (`/Users/k0rigi/Desktop/Web projeleri/RpaOrkestrAI`) kopyalandı. İçindeki `docs/`, `credentials/`, `scripts/`, `src/rpa_orkestrai/` yolları o depoya göredir; sunucu yolları (`src/peyrev/…`) bu depoya.

# Sürüm yayınlama

Yayın dışa dönüktür: kurulu tüm uygulamalar yeni sürümü indirir. Kullanıcı açıkça "yayınla / devreye al" demediyse başlatmadan önce sor. Ayrıntılı mimari: `docs/guncelleme-mimarisi.md`.

İki güncelleme kanalı vardır:

| Kanal | Okuyan kurulumlar | İmza anahtarı | Yayın iş akışı |
| --- | --- | --- | --- |
| `https://peyrev.com/indir/` (asıl) | 1.0.0 ve sonrası, yeni indirmeler | `credentials/peyrev-update-signing-ed25519.pem` (`--profile peyrev`) | `k0rigi/peyrev` → **Studio yayını** (`studio-yayin.yml`) |
| `https://orkestrai.net/rpa/` (eski) | 0.9.53 ve öncesi (yalnız bu kanala ve eski anahtara güvenir) | `credentials/update-signing-ed25519.pem` (`--profile legacy`) | `k0rigi/orkestrai` → **Publish RpaOrkestrAI desktop downloads** (`publish-rpa.yml`) |

Eski kanala yalnız köprü sürümü 1.0.0 bir kez yayımlanır (6. adım); sonraki her sürüm yalnız peyrev.com'dan yayımlanır. Eski kanalda bundan sonra yalnız 1.0.0 bildiriminin süresi dolmadan yeniden imzalanması (9. adım) yapılır; köprü kusurlu çıkarsa düzeltme sürümünün eski kanala konması yalnız kullanıcının açık onayıyla yapılır.

Ortak değişkenler:

```bash
ROOT="/Users/k0rigi/Desktop/Web projeleri/RpaOrkestrAI"
PY="$HOME/Library/Application Support/RpaOrkestrAI/runtime/bin/python"
UV="$ROOT/.bootstrap/bin/uv"
SCRATCH=<oturumun geçici klasörü>   # iCloud dışında
V=1.1.0   # yeni sürüm
```

`gh` kurulu değildir. GitHub API'sini git kimlik bilgisiyle çağıran küçük bir yardımcıyı geçici klasörde oluştur ve belirteci ekrana yazdırma:

```bash
cat > "$SCRATCH/ghapi.sh" <<'EOF'
#!/bin/bash
TOKEN=$(printf "protocol=https\nhost=github.com\n\n" | git credential fill 2>/dev/null | sed -n 's/^password=//p')
M=$1; P=$2; shift 2
case "$P" in http*) URL="$P";; *) URL="https://api.github.com$P";; esac
# Belirteç komut satırına (ps) düşmez: curl onu bir dosya tanıtıcısındaki yapılandırmadan okur.
curl -sS -K <(printf 'header = "Authorization: Bearer %s"\n' "$TOKEN") -X "$M" -H "Accept: application/vnd.github+json" "$URL" "$@"
EOF
chmod +x "$SCRATCH/ghapi.sh"
```

## 1. Sürümü hazırla

- `pyproject.toml` (`version`), `src/rpa_orkestrai/__init__.py` (`__version__`), `packaging/windows-installer.iss` (`AppVersion`) ve README'deki `Peyrev-Setup-<V>-Windows-x64.exe` / `Peyrev-<V>-macOS-arm64.dmg` adlarını güncelle; `src/rpa_orkestrai/release_notes.py` en üstüne sürümün kullanıcıya dönük maddelerini ekle. `tests/test_release_notes.py` bunların birlikte değiştiğini denetler.
- Kullanıcıya dönük değişiklikleri README ve `docs/` içine işle.
- `"$PY" -m pytest -p no:cacheprovider`, `"$PY" -m ruff check .`, `node --check src/rpa_orkestrai/static/app.js`.
- Commit + push. Push'ta **Python checks** yalnız Python 3.12'yi çalıştırır; yayından önce bir kez **Run workflow** ile tam 3.11–3.14 matrisini başlat (`POST /repos/k0rigi/RpaOrkestrAI/actions/workflows/tests.yml/dispatches`). macOS ve Windows'taki tüm işler başarılı olmalıdır.
- Bu depo ve `peyrev` deposu açık olduğu sürece testler, derleme ve peyrev.com yayını ücretsizdir. Yalnız eski kanal işi gizli `orkestrai` deposunda birkaç Linux dakikası harcar. Bir depo gizli yapıldıysa önce kotayı kontrol et (`GET /users/k0rigi/settings/billing/usage`; 2.000 dk, macOS 10×, Windows 2×) ve kota yetmeyecekse kullanıcıya sor.

## 2. Paketleri derle

```bash
"$SCRATCH/ghapi.sh" POST /repos/k0rigi/RpaOrkestrAI/actions/workflows/desktop-build.yml/dispatches -d '{"ref":"main"}'
```

**Build desktop apps** tamamlanınca artefaktları indir: `Peyrev-Windows-x64` (`Peyrev-Setup-$V-Windows-x64.exe`) ve `Peyrev-macOS` (`Peyrev-$V-macOS-arm64.dmg`). Dosyalar yayına hazır adlarıyla gelir; yeniden adlandırma gerekmez. İndirme isteği `archive_download_url` adresine yönlendirilir; `curl -L` ile ZIP olarak indirip `$SCRATCH/release-$V/` içine aç.

```bash
RUN=<run id>
"$SCRATCH/ghapi.sh" GET /repos/k0rigi/RpaOrkestrAI/actions/runs/$RUN/artifacts   # id + archive_download_url
"$SCRATCH/ghapi.sh" GET <archive_download_url> -L -o "$SCRATCH/win.zip"
```

`build-check-*` artefaktlarındaki `package-check.json` dosyasında `ok: true` ve `frozen: true` olduğunu kontrol et. DMG'nin kökünde `RpaOrkestrAI.app` bulunmalıdır (birim adı Peyrev); kurulu güncelleyiciler DMG'de tam bu adı arar, Finder uygulamayı yine Peyrev adıyla gösterir:

```bash
mkdir -p "$SCRATCH/dmg" && hdiutil attach -readonly -nobrowse -mountpoint "$SCRATCH/dmg" "$SCRATCH/release-$V/Peyrev-$V-macOS-arm64.dmg" \
  && ls "$SCRATCH/dmg"; hdiutil detach "$SCRATCH/dmg"
```

Paketleri iCloud ile eşitlenen proje klasörüne değil, geçici klasöre indir: her yayın ~1 GB geçici alan kullanır ve Masaüstü iCloud'a yüklenir. İşe başlamadan `df -h /` ile en az 2 GB boş alan olduğunu kontrol et; disk dolarsa araçlar dahil hiçbir şey dosya yazamaz.

## 3. İmzalı yayını hazırla

```bash
cd "$ROOT" && "$PY" scripts/prepare_update_release.py --profile peyrev --version $V \
  --windows "$SCRATCH/release-$V/Peyrev-Setup-$V-Windows-x64.exe" \
  --macos-arm64 "$SCRATCH/release-$V/Peyrev-$V-macOS-arm64.dmg" \
  --signing-key credentials/peyrev-update-signing-ed25519.pem \
  --output "$SCRATCH/update-site-$V" --archive "$SCRATCH/publish-bundle-$V.tar.gz"
shasum -a 256 "$SCRATCH/publish-bundle-$V.tar.gz"
```

Çıktı klasörü ve arşiv yeni olmalıdır. Betik anahtarın uygulamaya gömülü açık anahtarlardan (`update_config.PUBLIC_KEYS`) biriyle eşleştiğini denetler ve bildirimi kurulu uygulamaların gerçek güncelleme istemcisiyle doğrular. Ürettiği indirme sayfası Peyrev markalıdır ve form içermez; hesap başvurusu `https://peyrev.com/#basvuru` bağlantısıyla yapılır. Bildirim varsayılan olarak 90 gün geçerlidir; sonraki sürüm daha geç çıkacaksa süre dolmadan aynı paketlerle yeniden imzalanır.

Yedek anahtar (`credentials/peyrev-update-backup-ed25519.pem`) yalnız asıl anahtar kaybolduğunda veya sızdığında, kullanıcının açık kararıyla kullanılır; ardından yeni bir yedek açık anahtarla `PUBLIC_KEYS` güncellenip yeni sürüm yayımlanır (`docs/guncelleme-mimarisi.md` → Yayın güveni).

## 4. GitHub ön sürümü ve aktarım bağlantısı

- `v$V-test.1` etiketiyle **prerelease** oluştur (`POST /repos/k0rigi/RpaOrkestrAI/releases`). `target_commitish` olarak paketlerin derlendiği commit'in tam SHA'sını ver. Başlık ve açıklama önceki sürümlerdeki gibi Türkçedir: değişiklikler, kurulum adresi (`https://peyrev.com/indir/`), dosya adları, test sürümü uyarısı ve doğrulama kapsamı.
- EXE, DMG ve `publish-bundle-$V.tar.gz` dosyalarını (köprü sürümünde ayrıca `publish-bundle-legacy-$V.tar.gz`) `uploads.github.com/repos/k0rigi/RpaOrkestrAI/releases/<id>/assets?name=<ad>` adresine `Content-Type: application/octet-stream` ile yükle.
- Arşiv varlığının kısa ömürlü indirme adresini al: `GET /repos/k0rigi/RpaOrkestrAI/releases/assets/<asset id>` isteğini **yalnız** `Accept: application/octet-stream` başlığıyla yap ve yönlendirmeyi izleme. `ghapi.sh` kendi `Accept` başlığını da eklediği için burada doğrudan `curl -w "%{redirect_url}"` kullan; belirteci yine `-K <(printf …)` ile ver, `-H` ile komut satırına yazma. Adres `https://release-assets.githubusercontent.com/...` ile başlar ve kısa süre geçerlidir. Hemen kullan; ekrana, belgeye veya commit'e yazma.

## 5. peyrev.com'a yayınla

`k0rigi/peyrev` deposunda önce inceleme, sonra yayın çalıştır:

```bash
"$SCRATCH/ghapi.sh" POST /repos/k0rigi/peyrev/actions/workflows/studio-yayin.yml/dispatches \
  -d '{"ref":"main","inputs":{"action":"inspect"}}'
"$SCRATCH/ghapi.sh" POST /repos/k0rigi/peyrev/actions/workflows/studio-yayin.yml/dispatches \
  -d "{\"ref\":\"main\",\"inputs\":{\"action\":\"publish\",\"version\":\"$V\",\"bundle_sha256\":\"<sha>\",\"bundle_url\":\"<location>\"}}"
```

İş akışı yalnız sitenin `indir/` klasörünü değiştirir. Arşivi kısa ömürlü adresten indirip özetini, bildirimin imzasını (Peyrev'in asıl veya yedek açık anahtarı) ve paket özetlerini doğrular; sunucu aynı denetimleri yineler, paketleri `releases/$V/` altına koyar ve `stable.manifest` dosyasını en son, atomik olarak yayımlar. Eski bir sürümü ve aynı sürümü farklı baytlarla yayımlamayı reddeder. Sonucu `GET /repos/k0rigi/peyrev/actions/runs?event=workflow_dispatch&per_page=5` ile bul ve işin günlüğüne bak; `inspect` çıktısı (`current_version`, `versions`) günlükte yazar. Depo herkese açık olduğundan günlükler de açıktır; betik yalnız sürüm ve sabit hata metinleri yazar. Başarısız olursa günlüğü oku; aktarım adresinin süresi dolduysa 4. adımdaki gibi yeni adres al (arşiv değişmediyse özet aynı kalır). Depo açık olduğundan bu iş Actions kotasına sayılmaz.

## 6. Köprü sürüm: eski kanala yayın (yalnız 1.0.0)

0.9.53 ve önceki kurulumlar yalnız `https://orkestrai.net/rpa/stable.manifest` bildirimini okur, yalnız eski anahtara güvenir ve paketleri yalnız `https://orkestrai.net/rpa/` altından indirir. Bu yüzden 1.0.0, peyrev.com'dakiyle aynı paket dosyalarıyla bir kez de eski kanala yayımlanır:

1. **Önkoşul:** hesaplar peyrev.com'a taşınmış ve lisans sunucusu çalışıyor olmalı; aksi halde güncellenen kullanıcılar giriş yapamaz. `curl -s https://peyrev.com/api/rpa/lisans/anahtar` yanıtında `anahtar` = `licensing.LICENSE_PUBLIC_KEY`, `protokol` 2, `asgari_surum` 1.0.0 olmalı. peyrev.com/indir yayını (5. ve 7. adım) doğrulanmış olmalı.
2. Eski kanal arşivini aynı paketlerden hazırla:

   ```bash
   cd "$ROOT" && "$PY" scripts/prepare_update_release.py --profile legacy --version 1.0.0 \
     --windows "$SCRATCH/release-1.0.0/Peyrev-Setup-1.0.0-Windows-x64.exe" \
     --macos-arm64 "$SCRATCH/release-1.0.0/Peyrev-1.0.0-macOS-arm64.dmg" \
     --signing-key credentials/update-signing-ed25519.pem --expires-in-days 365 \
     --output "$SCRATCH/update-site-legacy-1.0.0" --archive "$SCRATCH/publish-bundle-legacy-1.0.0.tar.gz"
   shasum -a 256 "$SCRATCH/publish-bundle-legacy-1.0.0.tar.gz"
   ```

   Sayfa "RpaOrkestrAI artık Peyrev" der, aynı paketleri ve peyrev.com bağlantılarını gösterir; eski sürümlerin **Lisansı uzat** bağlantısı (`#bireysel`) sayfanın başvuru bölümüne düşer.
3. Arşivi aynı GitHub ön sürümüne yükle ve kısa ömürlü adresini al (4. adım).
4. `k0rigi/orkestrai` deposunda önce inceleme, sonra yayın:

   ```bash
   "$SCRATCH/ghapi.sh" POST /repos/k0rigi/orkestrai/actions/workflows/publish-rpa.yml/dispatches \
     -d '{"ref":"main","inputs":{"action":"inspect"}}'
   "$SCRATCH/ghapi.sh" POST /repos/k0rigi/orkestrai/actions/workflows/publish-rpa.yml/dispatches \
     -d "{\"ref\":\"main\",\"inputs\":{\"action\":\"publish\",\"version\":\"1.0.0\",\"bundle_sha256\":\"<sha>\",\"bundle_url\":\"<location>\"}}"
   ```

   İş akışı bildirimi eski anahtarla doğrular ve yalnız `/home/orkestrai.net/public_html/rpa` dizinini değiştirir. Kaynakları bu depodaki `packaging/hosting/publish-rpa.yml` ve `scripts/deploy_update_bundle.py` dosyalarıdır; değişirlerse orkestrai deposuna commit mesajında `[skip ci]` ile eşitlenir.
5. Gerçek bir Mac'te ve Windows'ta 0.9.53 kuruluyken: arka planda indirme → kapat/aç → sessiz kurulum → Peyrev açılır ve giriş ekranı Peyrev'e geçildiğini yazar. Aynı kullanıcı adı ve şifreyle giriş, akışlar, kayıtlı şifreler, otomatik başlatma ve Mac izinleri denetlenir; güncelleme durumu güncel görünmeli, eski anahtarla imzalı bekleyen kayıt silinmiş olmalıdır.
6. orkestrai'deki `RPA_ASGARI_SURUM` bu geçişte yükseltilmez; gerekirse yalnız öneri olarak kullanıcıya sunulur.
7. Eski kanalı buda: `publish-rpa.yml` `action: prune`, `version: 1.0.0`.

Eski kanala 1.0.0'dan yüksek sürüm konmaz. Tek istisna, yalnız kullanıcının açık onayıyla: köprü kusurlu çıkarsa düzeltme sürümü (ör. 1.0.1) iki kanala da yayımlanır (eski kanala `--profile legacy` ile); 1.0.0'ı deneyip kuramayan eski kurulumlar aynı sürümü yeniden denemez, farklı sürümü dener.

Eski kanal işi *"recent account payments have failed or your spending limit needs to be increased"* notuyla hiç başlamıyorsa gizli `orkestrai` deposunun Actions kotası dolmuştur. Kullanıcıya sor: harcama limiti mi, ayın 1'ini beklemek mi, yoksa yerelden yayın mı? orkestrai kuralları sunucuya elle yüklemeyi yasaklar. **Yerelden yayın yalnız kullanıcının açık onayıyla** yapılır ve aynı betik aynı doğrulamalarla çalıştırılır:

```bash
"$UV" venv -p "$PY" "$SCRATCH/pubvenv" && "$UV" pip install -p "$SCRATCH/pubvenv/bin/python" 'paramiko>=3.5,<5'
RULES="../orkestrai/.agents/rules/orkestrai.md"   # SSH adresi ve şifresi bu private dosyada
export HOST=$(sed -n 's/.*ssh ip: "\([^"]*\)".*/\1/p' "$RULES" | head -1)
export USERNAME=<SSH kullanıcı adı: kullanıcıya sor, depoya yazma>
export PASSWORD=$(sed -n 's/.*şifre: "\([^"]*\)".*/\1/p' "$RULES" | head -1)
RPA_ACTION=inspect "$SCRATCH/pubvenv/bin/python" scripts/deploy_update_bundle.py   # current_version + versions
RPA_ACTION=publish RPA_VERSION=1.0.0 RPA_BUNDLE_SHA256=<sha> RPA_BUNDLE_URL="$URL" \
  "$SCRATCH/pubvenv/bin/python" scripts/deploy_update_bundle.py
```

Sunucu adresini, kullanıcı adını ve şifreyi ekrana, belgeye veya commit'e yazma. Sunucu anahtarı betikte sabittir; bilinmeyen anahtar reddedilir.

## 7. Doğrula

```bash
manifest() { curl -s "$1" | python3 -c "import sys,json,base64;d=json.loads(base64.b64decode(json.load(sys.stdin)['payload']));print(d['version'], d['expires_at'])"; }
manifest https://peyrev.com/indir/stable.manifest
curl -sI https://peyrev.com/indir/releases/$V/Peyrev-Setup-$V-Windows-x64.exe | head -1
curl -sI https://peyrev.com/indir/releases/$V/Peyrev-$V-macOS-arm64.dmg | head -1
# Köprü sürümünde ve eski kanalın yeniden imzasından sonra:
manifest https://orkestrai.net/rpa/stable.manifest
curl -sI https://orkestrai.net/rpa/releases/1.0.0/Peyrev-Setup-1.0.0-Windows-x64.exe | head -1
curl -sI https://orkestrai.net/rpa/releases/1.0.0/Peyrev-1.0.0-macOS-arm64.dmg | head -1
```

İndirme sayfasının (`https://peyrev.com/indir/`) yeni sürümü gösterdiğini kontrol et. Kurulu uygulamalar yeni sürümü arka planda indirir ve bir sonraki açılışta kurar.

## 8. Eski paketleri kaldır

Yalnız güncel sürümün kurulum paketleri erişilebilir kalır; eski paketler (özellikle lisans denetimi daha zayıf olanlar) hiçbir yerde durmaz. Yeni sürüm doğrulandıktan sonra:

- **peyrev.com:** `studio-yayin.yml` iş akışını `action: prune`, `version: $V` ile çalıştır. Betik yalnız `stable.manifest` içindeki güncel sürümü tutar, `releases/` altındaki diğer sürüm klasörlerini siler ve beklenmeyen bir dosya görürse hiçbir şey silmeden durur. Sonra eski bir paketin adresinin `404` verdiğini kontrol et.
- **GitHub:** Önceki ön sürümleri ve etiketlerini sil (`GET /repos/k0rigi/RpaOrkestrAI/releases`, her eski kayıt için `DELETE /repos/k0rigi/RpaOrkestrAI/releases/<id>` ve `DELETE /repos/k0rigi/RpaOrkestrAI/git/refs/tags/<etiket>`). Derleme artefaktları bir gün sonra kendiliğinden silinir; beklemeden silmek için `DELETE /repos/k0rigi/RpaOrkestrAI/actions/artifacts/<id>`. 1.0.0 paketleri eski kanalda (`https://orkestrai.net/rpa/releases/1.0.0/`) durduğu için yeniden imza için GitHub'da tutulmaları gerekmez.
- **En düşük sürüm:** Yeni sürüm lisans veya güvenlik açığı kapatıyorsa, kullanıcının kendi kurulumu güncellendikten sonra peyrev deposunda `src/peyrev/accounts/licensing.py` içindeki `MINIMUM_VERSION` değerini bu sürüme yükseltmeyi kullanıcıya öner. Onay verirse commit + push ile gönder (**Dağıtım** iş akışı). Bundan sonra eski sürümler lisans alamaz ("Bu Peyrev sürümü artık desteklenmiyor"); güncelleme düzeneği lisanstan bağımsız çalıştığı için kendilerini güncelleyebilirler.

## 9. Eski kanalın bildirimini yeniden imzala

Eski kanal kapatılana kadar 1.0.0 bildiriminin süresi dolmamalıdır (yayında 365 gün; `manifest https://orkestrai.net/rpa/stable.manifest` ikinci değeri yazar). Süre bitmeden:

1. İki paketi eski kanaldan indir ve özetlerini bildirimdekiyle karşılaştır; bayt bayt aynı olmalıdır:

   ```bash
   curl -s https://orkestrai.net/rpa/stable.manifest | python3 -c "import sys,json,base64;d=json.loads(base64.b64decode(json.load(sys.stdin)['payload']));[print(k, v['sha256'], v['url']) for k, v in d['assets'].items()]"
   curl -s -o "$SCRATCH/release-1.0.0/<dosya>" <url> && shasum -a 256 "$SCRATCH/release-1.0.0/<dosya>"
   ```

2. 6. adımın 2. maddesindeki komutla yeni bir legacy arşivi üret (yeni çıktı klasörü ve arşiv adıyla), geçici bir ön sürüme yükle ve kısa ömürlü adresini al.
3. `publish-rpa.yml` `action: publish`, `version: 1.0.0`: sunucu aynı sürümü yalnız `releases/1.0.0` bayt bayt aynıysa kabul eder ve yalnız bildirimi ve sayfayı yeniler.
4. Geçici ön sürümü sil.

Eski kurulumlar azaldığında kanal kullanıcının kararıyla kapatılır; ondan sonra kalan eski kurulumlar ancak elle güncellenir ve eski özel anahtar imha edilir veya çevrimdışı arşivlenir.

Son olarak geçici klasördeki paketleri, arşivleri ve sanal ortamları sil; güncel dosyalar GitHub ön sürümünde ve `https://peyrev.com/indir/` adresinde durur. Kullanıcıya yayınlanan sürümü, paket adlarını ve gerçek bilgisayarda yapılması gereken açılış kontrolünü bildir.
