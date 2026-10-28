-- peyrev.com/iletisim/ formundan gelen mesajlar ve çerezsiz ziyaret sayımı.
-- Mesajlar panelde okunur ve silinir; 365 günden eski mesajlar yeni mesaj gelince silinir.
-- Ziyaretler gün, sayfa ve yönlendiren sitenin alan adına göre yalnız sayı olarak tutulur: IP adresi, tarayıcı kimliği
-- ya da çerez yoktur. 400 günden eski günler silinir.

CREATE TABLE iletisim_mesajlari (
    id BIGSERIAL PRIMARY KEY,
    ad TEXT NOT NULL CHECK (char_length(ad) BETWEEN 2 AND 120),
    eposta TEXT NOT NULL CHECK (char_length(eposta) BETWEEN 3 AND 150),
    konu TEXT NOT NULL CHECK (konu IN ('satis', 'teknik', 'diger')),
    mesaj TEXT NOT NULL CHECK (char_length(mesaj) BETWEEN 10 AND 3000),
    ip TEXT NOT NULL DEFAULT '',
    okundu BOOLEAN NOT NULL DEFAULT FALSE,
    olusturma TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX iletisim_mesajlari_zaman_idx ON iletisim_mesajlari (olusturma DESC);

CREATE TABLE ziyaret_sayilari (
    gun DATE NOT NULL,
    yol TEXT NOT NULL CHECK (char_length(yol) BETWEEN 1 AND 100),
    kaynak TEXT NOT NULL DEFAULT '' CHECK (char_length(kaynak) <= 100),
    sayi INTEGER NOT NULL DEFAULT 1 CHECK (sayi >= 0),
    PRIMARY KEY (gun, yol, kaynak)
);
