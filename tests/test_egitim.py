"""Eğitim videoları: YouTube bağlantısının okunması, panelden ekleme/değiştirme/silme ve herkese açık liste."""

from __future__ import annotations

import pytest
from conftest import make_user
from test_admin_auth import events, post, signed_in

from peyrev import egitim
from peyrev.common import ApiError

ID = "dQw4w9WgXcQ"


@pytest.mark.parametrize("link", [
    ID, f"https://www.youtube.com/watch?v={ID}", f"https://youtube.com/watch?feature=share&v={ID}&t=42",
    f"https://m.youtube.com/watch?v={ID}", f"https://youtu.be/{ID}", f"https://youtu.be/{ID}?si=abc",
    f"youtu.be/{ID}", f"https://www.youtube.com/shorts/{ID}", f"https://www.youtube.com/embed/{ID}",
    f"https://www.youtube.com/live/{ID}?feature=share", f"https://www.youtube-nocookie.com/embed/{ID}",
    f"  https://www.youtube.com/watch?v={ID}  ",
])
def test_a_youtube_link_gives_the_video_id(link):
    assert egitim.video_id(link) == ID


@pytest.mark.parametrize("link", [
    "", None, 42, "kisa", f"https://evil.test/watch?v={ID}", f"https://youtube.com.evil.test/watch?v={ID}",
    f"javascript:alert(1)//youtu.be/{ID}", "https://www.youtube.com/watch?v=kisa", "https://www.youtube.com/",
    f"https://www.youtube.com/watch?v={ID}<script>", f"ftp://youtu.be/{ID}", "https://youtu.be/" + "a" * 600,
])
def test_anything_else_is_refused(link):
    with pytest.raises(ApiError, match="YouTube video bağlantısı okunamadı"):
        egitim.video_id(link)


@pytest.fixture
def admin(app, core):
    make_user(core, "yonetici@peyrev.test", rol="yonetici")
    return signed_in(app)


def test_videos_are_added_changed_and_deleted_from_the_panel_and_only_published_ones_are_public(admin, client, core):
    assert client.get("/api/egitim").get_json() == {"success": True, "videolar": [], "kategoriler": []}
    added = post(admin, "egitim", {"baglanti": f"https://youtu.be/{ID}", "baslik": "  İlk akışınız  ",
                                   "aciklama": "Excel'den\n\nERP'ye   giriş", "kategori": "Başlangıç", "sira": 1})
    assert added.status_code == 201
    video = added.get_json()["video"]
    assert (video["baslik"], video["aciklama"], video["kategori"], video["video"], video["sira"], video["yayinda"]) == (
        "İlk akışınız", "Excel'den\n\nERP'ye giriş", "Başlangıç", ID, 1, True)
    second = post(admin, "egitim", {"baglanti": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "baslik": "Ajanlar",
                                    "yayinda": False}).get_json()["video"]
    assert second["kategori"] == "Genel" and second["sira"] == 0
    public = client.get("/api/egitim").get_json()
    assert [item["video"] for item in public["videolar"]] == [ID] and public["kategoriler"] == ["Başlangıç"]
    assert set(public["videolar"][0]) == {"id", "baslik", "aciklama", "kategori", "video", "tarih"}
    assert client.get("/api/egitim").headers["Cache-Control"] == "no-store"
    # Publishing the second one and moving it first.
    changed = post(admin, f"egitim/{second['id']}", {"baglanti": "https://youtu.be/aaaaaaaaaaa", "baslik": "Ajanlar",
                                                       "yayinda": True, "sira": 0})
    assert changed.status_code == 200
    assert [item["baslik"] for item in client.get("/api/egitim").get_json()["videolar"]] == ["Ajanlar", "İlk akışınız"]
    assert post(admin, f"egitim/{video['id']}/sil", {}).get_json()["video"] == {"id": video["id"], "silindi": True}
    assert post(admin, f"egitim/{video['id']}/sil", {}).status_code == 404
    assert post(admin, f"egitim/{video['id']}", {"baglanti": ID, "baslik": "Yok"}).status_code == 404
    assert [row["tur"] for row in events(core) if row["tur"].startswith("egitim_")] == [
        "egitim_eklendi", "egitim_eklendi", "egitim_degisti", "egitim_silindi"]
    assert [row["ayrinti"] for row in events(core, "egitim_degisti")] == [{"alanlar": ["yayinda"]}]


@pytest.mark.parametrize(("body", "message"), [
    ({"baglanti": "https://evil.test/x", "baslik": "Video"}, egitim.INVALID_LINK),
    ({"baglanti": ID, "baslik": "ab"}, "Başlık 3-150 karakter olmalıdır."),
    ({"baglanti": ID, "baslik": "<b>Video</b>"}, "Başlık geçersiz karakter içeriyor."),
    ({"baglanti": ID, "baslik": "Video", "aciklama": "x" * 1001}, "Açıklama en çok 1000 karakter olabilir."),
    ({"baglanti": ID, "baslik": "Video", "aciklama": "<img>"}, "Açıklama geçersiz karakter içeriyor."),
    ({"baglanti": ID, "baslik": "Video", "sira": "1"}, "Sıra 0-9999 arasında bir sayı olmalıdır."),
    ({"baglanti": ID, "baslik": "Video", "sira": 10000}, "Sıra 0-9999 arasında bir sayı olmalıdır."),
    ({"baglanti": ID, "baslik": "Video", "yayinda": "evet"}, "Yayın seçimi geçersiz."),
])
def test_invalid_videos_are_refused_and_nothing_is_written(admin, core, body, message):
    answer = post(admin, "egitim", body)
    assert (answer.status_code, answer.get_json()["message"]) == (400, message)
    with core.db.transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM egitim_videolari")
        assert cur.fetchone()["n"] == 0
