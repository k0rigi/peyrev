-- Robot Paneli: her Studio kurulumu (bilgisayar) hesabının bir robotudur. Studio 5 dakikada bir, açılışta ve bir akış
-- hatayla bitince durumunu bildirir; yalnız akış adları, durumlar, adım numaraları ve hata türleri gelir (hata metni,
-- değişken, dosya adı ya da ekran görüntüsü gelmez). Robot hesabın Studio oturumundaki cihaz kimliğiyle tanınır.
-- Komutlar (durdur, yeniden çalıştır) panelden ya da Hesabım'dan yazılır, bir sonraki bildirimde robota gider ve
-- sonucu ondan sonraki bildirimle döner. 30 günden eski komutlar yeni bildirim gelince silinir.

CREATE TABLE robotlar (
    id BIGSERIAL PRIMARY KEY,
    kullanici_id BIGINT NOT NULL REFERENCES kullanicilar (id) ON DELETE CASCADE,
    kurulum_kimligi TEXT NOT NULL CHECK (kurulum_kimligi ~ '^[0-9a-f]{32,64}$'),
    etiket TEXT NOT NULL DEFAULT '' CHECK (char_length(etiket) <= 60),
    surum TEXT NOT NULL DEFAULT '' CHECK (char_length(surum) <= 20),
    isletim_sistemi TEXT NOT NULL DEFAULT '' CHECK (char_length(isletim_sistemi) <= 40),
    son_gorulme TIMESTAMPTZ NOT NULL,
    kapali BOOLEAN NOT NULL DEFAULT FALSE,     -- Studio kapandı ya da durum bildirimi kapatıldı: sessizlik uyarısı yok
    durum JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (octet_length(durum::text) <= 32768),
    hata_uyarisi TIMESTAMPTZ,                  -- art arda hata uyarısının gönderildiği an (saatte en çok bir)
    hata_uyari_imi TIMESTAMPTZ,                -- o uyarıdaki en yeni hatanın anı: yalnız daha yeni hatalar uyarır
    sessizlik_uyarisi TIMESTAMPTZ,             -- sessizlik uyarısının anı: robot yeniden bildirene kadar bir kez
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kullanici_id, kurulum_kimligi)
);
CREATE INDEX robotlar_son_gorulme_idx ON robotlar (son_gorulme DESC);

CREATE TABLE robot_komutlari (
    id BIGSERIAL PRIMARY KEY,
    robot_id BIGINT NOT NULL REFERENCES robotlar (id) ON DELETE CASCADE,
    tur TEXT NOT NULL CHECK (tur IN ('durdur', 'yeniden_calistir')),
    hedef TEXT NOT NULL CHECK (hedef ~ '^[0-9a-f]{32}$'),   -- durdur: çalışma kimliği; yeniden çalıştır: akış kimliği
    ofis_id TEXT CHECK (ofis_id IS NULL OR ofis_id ~ '^[0-9a-f]{32}$'),
    akis TEXT NOT NULL DEFAULT '' CHECK (char_length(akis) <= 200),
    olusturan TEXT NOT NULL CHECK (olusturan IN ('yonetici', 'uye')),
    olusturan_id BIGINT REFERENCES kullanicilar (id) ON DELETE SET NULL,
    olusturuldu TIMESTAMPTZ NOT NULL,
    gonderildi TIMESTAMPTZ,
    sonuc TEXT CHECK (sonuc IS NULL OR sonuc IN ('tamam', 'bulunamadi', 'reddedildi', 'mesgul', 'suresi_doldu')),
    sonuclandi TIMESTAMPTZ
);
CREATE INDEX robot_komutlari_robot_idx ON robot_komutlari (robot_id, olusturuldu DESC);
CREATE INDEX robot_komutlari_zaman_idx ON robot_komutlari (olusturuldu);
