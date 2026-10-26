---
name: rpa-release
description: Peyrev Studio'nun yeni sürümünü (ör. 1.0.0 → 1.1.0) derleyip peyrev.com/indir üzerinden yayınlamak ve kurulu uygulamalara otomatik güncelleme olarak ulaştırmak için kullan. Sürüm artırma, GitHub Actions derlemesi (indirme ve güncelleme imajı), imzalı bildirimler (guncelleme.manifest ve 1.0.x'in okuduğu stable.manifest geçiş kanalı), GitHub ön sürümü ve peyrev deposundaki Studio yayını iş akışını kapsar.
---

> Bu skill Studio deposundan (`/Users/k0rigi/Desktop/Web projeleri/peyrev-studio`) kopyalandı. İçindeki `docs/`, `credentials/`, `scripts/`, `src/peyrev_studio/` yolları o depoya göredir; sunucu yolları (`src/peyrev/…`) bu depoya.

# Sürüm yayınlama

Yayın dışa dönüktür: kurulu tüm uygulamalar yeni sürümü indirir. Kullanıcı açıkça "yayınla / devreye al" demediyse başlatmadan önce sor. Ayrıntılı mimari: `docs/guncelleme-mimarisi.md` (özellikle "Peyrev adına geçiş (1.1.0)").

Güncellemeler `https://peyrev.com/indir/` altında iki bildirimle yayımlanır; ikisi de Peyrev'in asıl anahtarıyla (`credentials/peyrev-update-signing-ed25519.pem`) imzalanır ve `k0rigi/peyrev` deposunun **Studio yayını** (`studio-yayin.yml`) iş akışıyla yayımlanır:

| Bildirim | Okuyan kurulumlar | Profil | İçerik |
| --- | --- | --- | --- |
| `guncelleme.manifest` | 1.1.0 ve sonrası, yeni indirmeler | `--profile peyrev` | İndirme sayfası (`index.html`), bildirim, EXE ve indirme imajı (`Peyrev.app`) |
| `stable.manifest` | 1.0.x | `--profile gecis` | Yalnız bildirim ve paketleri (sayfa yok): EXE ve güncelleme imajı (`-guncelleme.dmg`) |

Her sürüm `peyrev` profiliyle yayımlanır. `gecis` profili yalnız 1.0.x kurulumlarını yeni adlara geçiren 1.1.x sürümü içindir: `stable.manifest` o sürümde kalır, sonraki sürümler oraya konmaz; 1.0.x önce geçiş sürümünü, ardından `guncelleme.manifest`'ten güncel sürümü alır. Geçiş sürümü kusurlu çıkarsa düzeltme 1.1.x sürümünün de `gecis` ile yayımlanması yalnız kullanıcının açık onayıyla yapılır. 1.2.0 ve sonrası `gecis` ile yayımlanmaz (1.0.x güncelleyicileri onları kuramaz). Bundan sonra `stable.manifest` yalnız süresi dolmadan yeniden imzalanır (8. adım).

Ortak değişkenler:

```bash
ROOT="/Users/k0rigi/Desktop/Web projeleri/peyrev-studio"
PY="$HOME/Library/Application Support/Peyrev/runtime/bin/python"
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

- `pyproject.toml` (`version`), `src/peyrev_studio/__init__.py` (`__version__`), `packaging/windows-installer.iss` (`AppVersion`) ve README'deki `Peyrev-Setup-<V>-Windows-x64.exe` / `Peyrev-<V>-macOS-arm64.dmg` adlarını güncelle; `src/peyrev_studio/release_notes.py` en üstüne sürümün kullanıcıya dönük maddelerini ekle. `tests/test_release_notes.py` bunların birlikte değiştiğini denetler.
- Kullanıcıya dönük değişiklikleri README ve `docs/` içine işle.
- 1.2.0'dan beri `scripts/build_desktop.py` içindeki `MAC_PROGRAM` ve `MAC_BUNDLE_ID` `Peyrev` ve `com.peyrev.studio`'dur; derleme yalnız indirme imajını üretir. 1.1.0'ın güncelleme imajı `stable.manifest` ile peyrev.com'da durur (yeniden imza için oradan indirilir). Bu sürüm yalnız 1.1.0 ve sonrası tarafından kurulur; sürüm notunda macOS'un Erişilebilirlik ve Ekran Kaydı iznini bir kez daha isteyebileceğini yaz.
- `"$PY" -m pytest -p no:cacheprovider`, `"$PY" -m ruff check .`, `node --check src/peyrev_studio/static/app.js`.
- Commit + push. Push'ta **Python checks** yalnız Python 3.12'yi çalıştırır; yayından önce bir kez **Run workflow** ile tam 3.11–3.14 matrisini başlat (`POST /repos/k0rigi/peyrev-studio/actions/workflows/tests.yml/dispatches`). macOS ve Windows'taki tüm işler başarılı olmalıdır.
- Bu depo ve `peyrev` deposu açık olduğu sürece testler, derleme ve peyrev.com yayını ücretsizdir. Bir depo gizli yapıldıysa önce kotayı kontrol et (`GET /users/k0rigi/settings/billing/usage`; 2.000 dk, macOS 10×, Windows 2×) ve kota yetmeyecekse kullanıcıya sor.

## 2. Paketleri derle

```bash
"$SCRATCH/ghapi.sh" POST /repos/k0rigi/peyrev-studio/actions/workflows/desktop-build.yml/dispatches -d '{"ref":"main"}'
```

**Build desktop apps** tamamlanınca artefaktları indir: `Peyrev-Windows-x64` (`Peyrev-Setup-$V-Windows-x64.exe`) ve `Peyrev-macOS` (indirme imajı `Peyrev-$V-macOS-arm64.dmg` ve güncelleme imajı `Peyrev-$V-macOS-arm64-guncelleme.dmg`). Dosyalar yayına hazır adlarıyla gelir; yeniden adlandırma gerekmez. İndirme isteği `archive_download_url` adresine yönlendirilir; `curl -L` ile ZIP olarak indirip `$SCRATCH/release-$V/` içine aç.

```bash
RUN=<run id>
"$SCRATCH/ghapi.sh" GET /repos/k0rigi/peyrev-studio/actions/runs/$RUN/artifacts   # id + archive_download_url
"$SCRATCH/ghapi.sh" GET <archive_download_url> -L -o "$SCRATCH/win.zip"
```

`build-check-*` artefaktlarındaki `package-check.json` dosyasında `ok: true` ve `frozen: true` olduğunu kontrol et. İki imajın da birim adı Peyrev'dir. İndirme imajının kökünde `Peyrev.app`, güncelleme imajının kökünde ise 1.0.x güncelleyicilerinin aradığı eski klasör adı (`legacy.BUNDLE`) bulunmalıdır; içerik aynı uygulamadır:

```bash
for f in "Peyrev-$V-macOS-arm64.dmg" "Peyrev-$V-macOS-arm64-guncelleme.dmg"; do
  mkdir -p "$SCRATCH/dmg" && hdiutil attach -readonly -nobrowse -mountpoint "$SCRATCH/dmg" "$SCRATCH/release-$V/$f" \
    && ls "$SCRATCH/dmg"; hdiutil detach "$SCRATCH/dmg"
done
```

Paketleri iCloud ile eşitlenen proje klasörüne değil, geçici klasöre indir: her yayın ~1,5 GB geçici alan kullanır ve Masaüstü iCloud'a yüklenir. İşe başlamadan `df -h /` ile en az 3 GB boş alan olduğunu kontrol et; disk dolarsa araçlar dahil hiçbir şey dosya yazamaz.

## 3. İmzalı yayını hazırla

```bash
cd "$ROOT" && "$PY" scripts/prepare_update_release.py --profile peyrev --version $V \
  --windows "$SCRATCH/release-$V/Peyrev-Setup-$V-Windows-x64.exe" \
  --macos-arm64 "$SCRATCH/release-$V/Peyrev-$V-macOS-arm64.dmg" \
  --signing-key credentials/peyrev-update-signing-ed25519.pem \
  --output "$SCRATCH/update-site-$V" --archive "$SCRATCH/publish-bundle-$V.tar.gz"
shasum -a 256 "$SCRATCH/publish-bundle-$V.tar.gz"
```

1.0.x'in alacağı geçiş sürümünde ikinci arşivi aynı EXE ve **güncelleme imajıyla** hazırla:

```bash
cd "$ROOT" && "$PY" scripts/prepare_update_release.py --profile gecis --version $V \
  --windows "$SCRATCH/release-$V/Peyrev-Setup-$V-Windows-x64.exe" \
  --macos-arm64 "$SCRATCH/release-$V/Peyrev-$V-macOS-arm64-guncelleme.dmg" \
  --signing-key credentials/peyrev-update-signing-ed25519.pem --expires-in-days 365 \
  --output "$SCRATCH/update-site-gecis-$V" --archive "$SCRATCH/publish-bundle-gecis-$V.tar.gz"
shasum -a 256 "$SCRATCH/publish-bundle-gecis-$V.tar.gz"
```

Çıktı klasörleri ve arşivler yeni olmalıdır. Betik anahtarın uygulamaya gömülü açık anahtarlardan (`update_config.PUBLIC_KEYS`) biriyle eşleştiğini denetler ve bildirimi o bildirimi okuyan kurulumların gerçek güncelleme istemcisiyle doğrular. `peyrev` profilinin ürettiği indirme sayfası Peyrev markalıdır ve form içermez; hesap başvurusu `https://peyrev.com/#basvuru` bağlantısıyla yapılır. `gecis` profili sayfa üretmez. `guncelleme.manifest` varsayılan olarak 90 gün geçerlidir; sonraki sürüm daha geç çıkacaksa süre dolmadan aynı paketlerle yeniden imzalanır.

Yedek anahtar (`credentials/peyrev-update-backup-ed25519.pem`) yalnız asıl anahtar kaybolduğunda veya sızdığında, kullanıcının açık kararıyla kullanılır; ardından yeni bir yedek açık anahtarla `PUBLIC_KEYS` güncellenip yeni sürüm yayımlanır (`docs/guncelleme-mimarisi.md` → Yayın güveni).

## 4. GitHub ön sürümü ve aktarım bağlantısı

- `v$V-test.1` etiketiyle **prerelease** oluştur (`POST /repos/k0rigi/peyrev-studio/releases`). `target_commitish` olarak paketlerin derlendiği commit'in tam SHA'sını ver. Başlık ve açıklama önceki sürümlerdeki gibi Türkçedir: değişiklikler, kurulum adresi (`https://peyrev.com/indir/`), dosya adları, test sürümü uyarısı ve doğrulama kapsamı.
- EXE, iki DMG, `publish-bundle-$V.tar.gz` ve (geçiş sürümünde) `publish-bundle-gecis-$V.tar.gz` dosyalarını `uploads.github.com/repos/k0rigi/peyrev-studio/releases/<id>/assets?name=<ad>` adresine `Content-Type: application/octet-stream` ile yükle.
- Her arşiv varlığının kısa ömürlü indirme adresini al: `GET /repos/k0rigi/peyrev-studio/releases/assets/<asset id>` isteğini **yalnız** `Accept: application/octet-stream` başlığıyla yap ve yönlendirmeyi izleme. `ghapi.sh` kendi `Accept` başlığını da eklediği için burada doğrudan `curl -w "%{redirect_url}"` kullan; belirteci yine `-K <(printf …)` ile ver, `-H` ile komut satırına yazma. Adres `https://release-assets.githubusercontent.com/...` ile başlar ve kısa süre geçerlidir. Hemen kullan; ekrana, belgeye veya commit'e yazma.

## 5. peyrev.com'a yayınla

`k0rigi/peyrev` deposunda önce inceleme, sonra yayın çalıştır. Geçiş sürümünde iki arşiv ayrı ayrı yayımlanır: **önce `gecis` arşivi, sonra `peyrev` arşivi**. Sunucu iş akışları tek eşzamanlılık grubundadır ve bekleyen işin yerine yenisi geçer; bir işi başlatmadan önce öncekinin bitmesini bekle.

```bash
"$SCRATCH/ghapi.sh" POST /repos/k0rigi/peyrev/actions/workflows/studio-yayin.yml/dispatches \
  -d '{"ref":"main","inputs":{"action":"inspect"}}'
# Geçiş sürümünde önce gecis arşivi (özet ve adres publish-bundle-gecis-$V.tar.gz'nin), bitince peyrev arşivi:
"$SCRATCH/ghapi.sh" POST /repos/k0rigi/peyrev/actions/workflows/studio-yayin.yml/dispatches \
  -d "{\"ref\":\"main\",\"inputs\":{\"action\":\"publish\",\"version\":\"$V\",\"bundle_sha256\":\"<sha>\",\"bundle_url\":\"<location>\"}}"
```

İş akışı yalnız sitenin `indir/` klasörünü değiştirir. Arşivi kısa ömürlü adresten indirip özetini, bildirimin imzasını (Peyrev'in asıl veya yedek açık anahtarı) ve paket özetlerini doğrular; sunucu aynı denetimleri yineler, paketleri `releases/$V/` altına koyar (aynı sürümün iki arşivi bu klasörü paylaşır) ve arşivdeki bildirimi (`guncelleme.manifest` ya da `stable.manifest`) en son, atomik olarak yayımlar. Eski bir sürümü ve aynı sürümü farklı baytlarla yayımlamayı reddeder. Girdiler veya davranış değiştiyse peyrev deposundaki `studio-yayin.yml` ve `.github/scripts/studio-yayin.py` esas alınır. Sonucu `GET /repos/k0rigi/peyrev/actions/runs?event=workflow_dispatch&per_page=5` ile bul ve işin günlüğüne bak; `inspect` çıktısı günlükte yazar. Depo herkese açık olduğundan günlükler de açıktır; betik yalnız sürüm ve sabit hata metinleri yazar. Başarısız olursa günlüğü oku; aktarım adresinin süresi dolduysa 4. adımdaki gibi yeni adres al (arşiv değişmediyse özet aynı kalır). Depo açık olduğundan bu iş Actions kotasına sayılmaz.

Geçiş sürümünde yayından sonra gerçek bir Mac'te ve Windows'ta 1.0.x kuruluyken denetle: arka planda indirme → kapat/aç → sessiz kurulum → Peyrev açılır; Mac'te uygulama kendini `Peyrev.app` olarak adlandırıp yeniden açılır, Windows'ta program `%LOCALAPPDATA%\Programs\Peyrev\Peyrev.exe` altına taşınır ve eski klasör silinir. Ardından giriş, akışlar ve ayarlar (taşınan çalışma alanı), kayıtlı şifreler, **Bilgisayar açılınca başlat**, Mac izinleri ve güncelleme durumu (güncel görünmeli) denetlenir.

## 6. Doğrula

```bash
manifest() { curl -s "$1" | python3 -c "import sys,json,base64;d=json.loads(base64.b64decode(json.load(sys.stdin)['payload']));print(d['version'], d['expires_at'])"; }
manifest https://peyrev.com/indir/guncelleme.manifest
manifest https://peyrev.com/indir/stable.manifest      # geçiş sürümü
curl -sI https://peyrev.com/indir/releases/$V/Peyrev-Setup-$V-Windows-x64.exe | head -1
curl -sI https://peyrev.com/indir/releases/$V/Peyrev-$V-macOS-arm64.dmg | head -1
curl -sI https://peyrev.com/indir/releases/$V/Peyrev-$V-macOS-arm64-guncelleme.dmg | head -1   # geçiş sürümünde
```

İndirme sayfasının (`https://peyrev.com/indir/`) yeni sürümü gösterdiğini kontrol et. Kurulu uygulamalar yeni sürümü arka planda indirir ve bir sonraki açılışta kurar.

## 7. Eski paketleri kaldır

Yalnız bildirimlerin gösterdiği sürümlerin kurulum paketleri erişilebilir kalır; eski paketler (özellikle lisans denetimi daha zayıf olanlar) hiçbir yerde durmaz. Yeni sürüm doğrulandıktan sonra:

- **peyrev.com:** `studio-yayin.yml` iş akışını `action: prune`, `version: $V` ile çalıştır. Betik yalnız güncel bildirimlerin (`guncelleme.manifest`, `stable.manifest`) andığı sürümleri tutar, `releases/` altındaki diğer sürüm klasörlerini siler ve beklenmeyen bir dosya görürse hiçbir şey silmeden durur. Sonra eski bir paketin adresinin `404` verdiğini kontrol et. Geçiş sürümünün paketleri `stable.manifest` kaldıkça yerinde durur.
- **GitHub:** Önceki ön sürümleri ve etiketlerini sil (`GET /repos/k0rigi/peyrev-studio/releases`, her eski kayıt için `DELETE /repos/k0rigi/peyrev-studio/releases/<id>` ve `DELETE /repos/k0rigi/peyrev-studio/git/refs/tags/<etiket>`). Derleme artefaktları bir gün sonra kendiliğinden silinir; beklemeden silmek için `DELETE /repos/k0rigi/peyrev-studio/actions/artifacts/<id>`. Geçiş sürümünün paketleri peyrev.com'da durduğu için yeniden imza için GitHub'da tutulmaları gerekmez.
- **En düşük sürüm:** Yeni sürüm lisans veya güvenlik açığı kapatıyorsa, kullanıcının kendi kurulumu güncellendikten sonra peyrev deposunda `src/peyrev/accounts/licensing.py` içindeki `MINIMUM_VERSION` değerini bu sürüme yükseltmeyi kullanıcıya öner. Onay verirse commit + push ile gönder (**Dağıtım** iş akışı). Bundan sonra eski sürümler lisans alamaz ("Bu Peyrev sürümü artık desteklenmiyor"); güncelleme düzeneği lisanstan bağımsız çalıştığı için kendilerini güncelleyebilirler.

## 8. Geçiş bildirimini yeniden imzala

1.0.x kurulumu kaldıkça `stable.manifest` süresi dolmamalıdır (yayında 365 gün; `manifest https://peyrev.com/indir/stable.manifest` ikinci değeri yazar). Süre bitmeden:

1. İki paketi (EXE ve güncelleme imajı) peyrev.com'dan indir ve özetlerini bildirimdekiyle karşılaştır; bayt bayt aynı olmalıdır:

   ```bash
   curl -s https://peyrev.com/indir/stable.manifest | python3 -c "import sys,json,base64;d=json.loads(base64.b64decode(json.load(sys.stdin)['payload']));[print(k, v['sha256'], v['url']) for k, v in d['assets'].items()]"
   curl -s -o "$SCRATCH/release-<geçiş sürümü>/<dosya>" <url> && shasum -a 256 "$SCRATCH/release-<geçiş sürümü>/<dosya>"
   ```

2. 3. adımdaki `--profile gecis` komutuyla, geçiş sürümünün numarası ve bu paketlerle yeni bir arşiv üret (yeni çıktı klasörü ve arşiv adıyla), geçici bir ön sürüme yükle ve kısa ömürlü adresini al.
3. `studio-yayin.yml` `action: publish`, `version: <geçiş sürümü>`: sunucu aynı sürümü yalnız paketler bayt bayt aynıysa kabul eder ve yalnız bildirimi yeniler.
4. Geçici ön sürümü sil.

1.0.x kurulumu kalmadığında geçiş kanalı kullanıcının kararıyla kapatılır; ondan sonra kalan 1.0.x kurulumları ancak elle güncellenir. Aynı zamanda `src/peyrev_studio/legacy.py` ve Windows kurulum betiğindeki taşıma bölümü 1.1.0'dan eski kurulum kalmadığında kaldırılabilir (kullanıcıya öner).

Son olarak geçici klasördeki paketleri ve arşivleri sil; güncel dosyalar GitHub ön sürümünde ve `https://peyrev.com/indir/` adresinde durur. Kullanıcıya yayınlanan sürümü, paket adlarını ve gerçek bilgisayarda yapılması gereken açılış kontrolünü bildir.
