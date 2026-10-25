-- Ajan merkezi: kullanıcının ofisleri, kendine verdiği insan görevleri, tek kullanımlık onaylar ve denetim olayları.
-- Firma yoktur: her kayıt bir hesaba (sahip_id) ve onun bir ofisine (ofis_id; ofissiz hesapta NULL) aittir.
-- Kimlikler (ofis, görev, onay, olay, istek, çalışma, akış) 32 küçük harf onaltılık karakterdir ve taşımada AYNEN
-- korunur: Studio yerel ajanları, çalışmaları ve ofis-<id>.glb modeli ofis kimliğine bağlıdır. Tamsayı kimlik dizisi
-- yoktur; taşıma sonrası setval gerekmez.
--
-- MySQL (orkestrai) kaynağı → Peyrev eşlemesi (taşıma aracı bu bloğu uygular):
--   Genel: DATETIME / DATETIME(6) değerleri Türkiye saati (+03:00) sayılıp timestamptz'ye çevrilir; 'Aktif'/'Pasif' →
--          'aktif'/'pasif'; FirmaID taşınmaz (kapsam sahip_id'dir); kullanıcı kimlikleri (KullaniciID) aynen kalır.
--          sahip_id'si Peyrev'e taşınmamış bir hesaba düşen satır taşınmaz, arşivde kalır.
--   SYS_AjanOrganizasyonlari → tablo yok. OnayciID ve PolitikaSurumu taşınmaz; Durum <> 'Aktif' olan firmanın
--          hesaplarına MOD_AJAN verilmemesi hesap taşımasının işidir.
--   SYS_AjanOfisleri → ajan_ofisleri: OfisID → id, YetkiliID → sahip_id, Ad → ad, Durum → durum,
--          guncelleme = olusturma. olusturma: sahibin o ofisteki SYS_AjanOfisUyeleri.Olusturma değeri, yoksa ofisin
--          Olusturma'sı (ofis sırası, dolayısıyla Studio'nun varsayılan ilk ofisi korunur).
--   SYS_AjanOfisUyeleri → tablo yok. Sahiplik sahip_id ile örtüktür; yetkili olmayan üyelerin satırları taşınmaz
--          (bu hesaplar ofissiz başlar ve kendi ofislerini kurabilir).
--   SYS_AjanEkipGorevleri → ajan_gorevleri, bütün satırlar ('deleted' de gizli geçmiş olarak):
--          ID → id, OfisID → ofis_id, OlusturanID → olusturan_id, AtananID → atanan_id, IstekID → istek_id,
--          IstekOzeti → istek_ozeti, Baslik → baslik, Talimat → talimat, Oncelik → oncelik, Durum → durum,
--          Surum → surum, SonucOzeti → sonuc_ozeti, Olusturma → olusturma, Baslama → baslama, Bitis → bitis,
--          Guncelleme → guncelleme; sahip_id = OfisID'nin YetkiliID'si; ofis yoksa (OfisID NULL ya da silinmiş
--          ofis) OlusturanID.
--          Yetkili olmayan eski üyelere atanmış satırlar sahibin altında geçmiş olarak kalır (atanan_id ≠ sahip_id).
--   SYS_AjanEkipOnaylari → ajan_onaylari, yalnız Durum IN ('rejected', 'consumed'); bekleyen ve onaylanıp tüketilmemiş
--          satırlar ('pending', 'approved') eski Studio oturumuna bağlıdır ve taşınmaz:
--          ID → id, OfisID → ofis_id, IsteyenID → isteyen_id, OnayciID → onayci_id, OturumID → oturum_id,
--          IstekID → istek_id, IstekOzeti → istek_ozeti, CalismaID → calisma_id, AkisID → akis_id,
--          AkisOzeti → akis_ozeti, GirdiOzeti → girdi_ozeti, OnizlemeOzeti → onizleme_ozeti,
--          BaglamOzeti → baglam_ozeti, Onizleme → onizleme (kanonik JSON metni aynen), Gerekce → gerekce,
--          Deneme → deneme, Durum → durum,
--          Surum → surum, KararNedeni → karar_nedeni, Olusturma → olusturma, Gecerlilik → gecerlilik,
--          KararZamani → karar_zamani, TuketimZamani → tuketim_zamani;
--          sahip_id = OfisID'nin YetkiliID'si; ofis yoksa OnayciID.
--   SYS_AjanEkipOlaylari → ajan_olaylari, yalnız KayitTuru IN ('task', 'approval', 'office'):
--          ID → id, KayitTuru → kayit_turu, KayitID → kayit_id, KullaniciID → kullanici_id (NULL: sistem olayı),
--          Aktor → aktor, Olay → olay, Ayrinti → ayrinti (JSON metni → jsonb), Olusturma → olusturma;
--          sahip_id = kaydın sahibi: görev olayında görevin sahip_id'si, onay olayında (onay taşınmasa da) onayın
--          sahip_id kuralı, ofis olayında ofisin YetkiliID'si, silinmiş ofiste aynı KayitID'li office_created olayının
--          KullaniciID'si. 'license' ve 'account' olayları (operatör araçlarının kayıtları; account_created giriş adı
--          içerir) taşınmaz, arşivde kalır.
--   Taşıma sonrası: sahip başına en çok 50 ofis ve sahip başına tekil lower(ad) (ikisi de firma içi kuraldan gelir).

CREATE TABLE ajan_ofisleri (
    id TEXT COLLATE "C" PRIMARY KEY CHECK (id ~ '^[0-9a-f]{32}$'),
    sahip_id BIGINT NOT NULL REFERENCES kullanicilar (id) ON DELETE CASCADE,
    ad TEXT NOT NULL CHECK (length(ad) BETWEEN 2 AND 120),
    -- 'pasif': yalnız yönetim paneli koyar; sahibi ve Studio o ofise erişemez, kayıtları kalır.
    durum TEXT NOT NULL DEFAULT 'aktif' CHECK (durum IN ('aktif', 'pasif')),
    olusturma TIMESTAMPTZ NOT NULL DEFAULT now(),
    guncelleme TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ajan_ofisleri_sahip_idx ON ajan_ofisleri (sahip_id, olusturma, id);
-- Yedek: asıl denetim uygulamada casefold ile yapılır (aynı sahipte aynı ad iki kez olmaz).
CREATE UNIQUE INDEX ajan_ofisleri_ad_uq ON ajan_ofisleri (sahip_id, lower(ad));

-- Kendine görev: yeni satırda olusturan_id = atanan_id = sahip_id; taşınan geçmişte eski ekip üyeleri kalabilir.
CREATE TABLE ajan_gorevleri (
    id TEXT COLLATE "C" PRIMARY KEY CHECK (id ~ '^[0-9a-f]{32}$'),
    sahip_id BIGINT NOT NULL REFERENCES kullanicilar (id) ON DELETE CASCADE,
    ofis_id TEXT COLLATE "C" CHECK (ofis_id ~ '^[0-9a-f]{32}$'),   -- NULL: hesabın ofisi yokken açılan kayıt
    olusturan_id BIGINT NOT NULL,
    atanan_id BIGINT NOT NULL,
    istek_id TEXT COLLATE "C" NOT NULL CHECK (istek_id ~ '^[0-9a-f]{32}$'),
    istek_ozeti TEXT NOT NULL,
    baslik TEXT NOT NULL CHECK (length(baslik) BETWEEN 1 AND 160),
    talimat TEXT NOT NULL CHECK (length(talimat) <= 20000),
    oncelik TEXT NOT NULL CHECK (oncelik IN ('low', 'normal', 'high')),
    durum TEXT NOT NULL DEFAULT 'queued'
        CHECK (durum IN ('queued', 'running', 'waiting_user', 'succeeded', 'failed', 'cancelled', 'deleted')),
    surum INTEGER NOT NULL DEFAULT 1 CHECK (surum >= 1),
    sonuc_ozeti TEXT,
    olusturma TIMESTAMPTZ NOT NULL,
    baslama TIMESTAMPTZ,
    bitis TIMESTAMPTZ,
    guncelleme TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX ajan_gorevleri_istek_uq ON ajan_gorevleri (olusturan_id, istek_id);
CREATE INDEX ajan_gorevleri_kapsam_idx ON ajan_gorevleri (sahip_id, ofis_id, durum);
CREATE INDEX ajan_gorevleri_tarih_idx ON ajan_gorevleri (sahip_id, olusturma);

-- Onay: yeni satırda isteyen_id = onayci_id = sahip_id; tüketme ve geri çekme yalnız isteği açan Studio oturumundadır.
CREATE TABLE ajan_onaylari (
    id TEXT COLLATE "C" PRIMARY KEY CHECK (id ~ '^[0-9a-f]{32}$'),
    sahip_id BIGINT NOT NULL REFERENCES kullanicilar (id) ON DELETE CASCADE,
    ofis_id TEXT COLLATE "C" CHECK (ofis_id ~ '^[0-9a-f]{32}$'),
    isteyen_id BIGINT NOT NULL,
    onayci_id BIGINT NOT NULL,
    oturum_id TEXT NOT NULL,
    istek_id TEXT COLLATE "C" NOT NULL CHECK (istek_id ~ '^[0-9a-f]{32}$'),
    istek_ozeti TEXT NOT NULL,
    calisma_id TEXT NOT NULL,
    akis_id TEXT NOT NULL,
    akis_ozeti TEXT NOT NULL,
    girdi_ozeti TEXT NOT NULL,
    onizleme_ozeti TEXT NOT NULL,
    baglam_ozeti TEXT NOT NULL,
    onizleme TEXT NOT NULL,              -- kanonik JSON metni; sha256(onizleme) = onizleme_ozeti
    gerekce TEXT NOT NULL,
    deneme INTEGER NOT NULL CHECK (deneme BETWEEN 1 AND 100),
    durum TEXT NOT NULL DEFAULT 'pending' CHECK (durum IN ('pending', 'approved', 'rejected', 'consumed')),
    surum INTEGER NOT NULL DEFAULT 1 CHECK (surum >= 1),
    karar_nedeni TEXT CHECK (karar_nedeni IN ('user', 'timeout', 'owner_changed', 'withdrawn')),
    olusturma TIMESTAMPTZ NOT NULL,
    gecerlilik TIMESTAMPTZ NOT NULL,
    karar_zamani TIMESTAMPTZ,
    tuketim_zamani TIMESTAMPTZ
);
CREATE UNIQUE INDEX ajan_onaylari_istek_uq ON ajan_onaylari (isteyen_id, istek_id);
CREATE INDEX ajan_onaylari_kapsam_idx ON ajan_onaylari (sahip_id, ofis_id, olusturma);
CREATE INDEX ajan_onaylari_durum_idx ON ajan_onaylari (sahip_id, ofis_id, durum, gecerlilik);

-- Denetim: görev talimatı ve onay gerekçesi kopyalanmaz; satırlar silinmez (silinen ofis veya hesap olayları kalır).
CREATE TABLE ajan_olaylari (
    id TEXT COLLATE "C" PRIMARY KEY CHECK (id ~ '^[0-9a-f]{32}$'),
    sahip_id BIGINT NOT NULL,
    kayit_turu TEXT NOT NULL CHECK (kayit_turu IN ('task', 'approval', 'office')),
    kayit_id TEXT COLLATE "C" NOT NULL,
    kullanici_id BIGINT,                 -- eylemi yapan; sistem olayında NULL
    aktor TEXT NOT NULL CHECK (aktor IN ('human', 'system')),
    olay TEXT NOT NULL CHECK (length(olay) <= 32),
    ayrinti JSONB NOT NULL DEFAULT '{}'::jsonb,
    olusturma TIMESTAMPTZ NOT NULL
);
CREATE INDEX ajan_olaylari_kayit_idx ON ajan_olaylari (sahip_id, kayit_turu, kayit_id, olusturma);
