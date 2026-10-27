-- Ücretsiz eğitim videoları (peyrev.com/egitim/). Video YouTube'da durur; burada yalnız 11 karakterlik video kimliği,
-- başlık, açıklama, kategori ve sıra tutulur. Panelden eklenir, değişir ve silinir (yonetim_olaylari'na yazılır).

CREATE TABLE egitim_videolari (
    id BIGSERIAL PRIMARY KEY,
    baslik TEXT NOT NULL CHECK (char_length(baslik) BETWEEN 3 AND 150),
    aciklama TEXT NOT NULL DEFAULT '' CHECK (char_length(aciklama) <= 1000),
    kategori TEXT NOT NULL DEFAULT 'Genel' CHECK (char_length(kategori) BETWEEN 1 AND 60),
    video_kimligi TEXT NOT NULL CHECK (video_kimligi ~ '^[A-Za-z0-9_-]{11}$'),
    sira INTEGER NOT NULL DEFAULT 0 CHECK (sira BETWEEN 0 AND 9999),
    yayinda BOOLEAN NOT NULL DEFAULT TRUE,
    olusturma TIMESTAMPTZ NOT NULL DEFAULT now(),
    guncelleme TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX egitim_videolari_yayin_idx ON egitim_videolari (yayinda, sira, id);
