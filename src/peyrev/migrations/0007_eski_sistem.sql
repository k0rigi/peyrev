-- Eski sistemden 2026-10-26'da taşınan hesapların kaynak değeri 'tasima' olur (panelde "Eski sistemden taşındı").
-- Eski sistem kapandığından adı veride ve panelde geçmez; hesabın kendisi, kimliği ve alan_id'si değişmez.

ALTER TABLE kullanicilar DROP CONSTRAINT kullanicilar_kaynak_check;
UPDATE kullanicilar SET kaynak = 'tasima' WHERE kaynak = 'orkestrai';
ALTER TABLE kullanicilar ADD CONSTRAINT kullanicilar_kaynak_check CHECK (kaynak IN ('tasima', 'basvuru', 'yonetim'));
