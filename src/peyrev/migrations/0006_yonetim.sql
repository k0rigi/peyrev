-- Yönetim paneli (peyrev.admin): yönetici oturumları ve iki adımlı doğrulamanın kullanılmış son adımı.
-- Bu tablolara yalnız o modül yazar. Panelin işlem kayıtları çekirdekteki yonetim_olaylari tablosundadır (0001); iki
-- adımlı doğrulamanın gizli anahtarı kullanicilar.totp_gizli kolonunda mühürlü durur (peyrev/admin/totp.py).
--
-- Eski sistemin yönetim sayfasında sunucu tarafı oturum ve iki adımlı doğrulama yoktu; bu tablolara veri taşınmadı. Yöneticiler panele
-- ilk kez Peyrev şifreleriyle girer ve iki adımlı doğrulamayı Hesabım bölümünden açar.

-- Panel oturumu: tarayıcı rastgele belirteci yalnız HttpOnly çerezde tutar, burada SHA-256 özeti durur. Süresi dolan,
-- kapanan ya da geçersizleşen oturumun satırı silinir; giriş ve çıkış yonetim_olaylari'nda kalır.
CREATE TABLE yonetim_oturumlari (
    id TEXT PRIMARY KEY CHECK (id ~ '^[0-9a-f]{32}$'),
    kullanici_id BIGINT NOT NULL REFERENCES kullanicilar (id) ON DELETE CASCADE,
    belirtec_ozeti TEXT NOT NULL UNIQUE CHECK (belirtec_ozeti ~ '^[0-9a-f]{64}$'),
    sifre_izi TEXT NOT NULL,             -- girişteki şifre izi (users.fingerprint): şifre değişince oturum düşer
    ip TEXT NOT NULL DEFAULT '' CHECK (length(ip) <= 64),
    tarayici TEXT NOT NULL DEFAULT '' CHECK (length(tarayici) <= 200),
    olusturma TIMESTAMPTZ NOT NULL,
    son_gorulme TIMESTAMPTZ NOT NULL,    -- 2 saatlik boşta süresi buradan sayılır
    bitis TIMESTAMPTZ NOT NULL,          -- mutlak sınır: girişten 12 saat sonra
    -- İki adımlı doğrulama kurulurken gösterilen, henüz kodla onaylanmamış gizli anahtar (mühürlü) ve son geçerlilik anı.
    totp_bekleyen TEXT,
    totp_bekleyen_bitis TIMESTAMPTZ,
    CHECK ((totp_bekleyen IS NULL) = (totp_bekleyen_bitis IS NULL))
);
CREATE INDEX yonetim_oturumlari_kullanici_idx ON yonetim_oturumlari (kullanici_id, son_gorulme DESC);
CREATE INDEX yonetim_oturumlari_bitis_idx ON yonetim_oturumlari (bitis);

-- Kabul edilen son TOTP adımı (30 sn): bir kod girişte, kurulumda ya da kapatmada yalnız bir kez geçer.
CREATE TABLE yonetim_iki_adim (
    kullanici_id BIGINT PRIMARY KEY REFERENCES kullanicilar (id) ON DELETE CASCADE,
    son_adim BIGINT NOT NULL
);
