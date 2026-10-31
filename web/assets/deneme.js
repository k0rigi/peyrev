// Deneme sayfası (/egitim/deneme/web/): web adımlarının denendiği form, arama ve gecikmeli rapor. Hiçbir bilgi sunucuya
// gönderilmez; kullanıcının yazdığı her şey sayfaya yalnız metin olarak yazılır.
(function () {
  "use strict";
  var form = document.getElementById("kayit-formu");
  if (!form) return;

  var belge = document.getElementById("belge");
  belge.addEventListener("change", function () {
    var names = Array.prototype.map.call(belge.files, function (file) { return file.name; });
    document.getElementById("dosya-adi").textContent = names.length ? "Seçilen dosya: " + names.join(", ") : "Dosya seçilmedi.";
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var ad = document.getElementById("ad").value.trim();
    var sehir = document.getElementById("sehir").value;
    var sonuc = document.getElementById("sonuc");
    if (!ad || !sehir) {
      sonuc.textContent = "Ad Soyad ve Şehir gerekli.";
      return;
    }
    sonuc.textContent = "Kaydedildi: " + ad + " / " + sehir;
    var item = document.createElement("li");
    item.textContent = ad + " · " + (document.getElementById("eposta").value.trim() || "e-posta yok") + " · " + sehir;
    document.getElementById("kayitlar").appendChild(item);
    form.reset();
    document.getElementById("dosya-adi").textContent = "Dosya seçilmedi.";
  });

  var ara = document.getElementById("ara");
  function filtrele() {
    var aranan = ara.value.trim().toLocaleLowerCase("tr");
    var shown = 0;
    document.querySelectorAll("#siparisler tbody tr").forEach(function (row) {
      var match = !aranan || row.textContent.toLocaleLowerCase("tr").indexOf(aranan) !== -1;
      row.hidden = !match;
      if (match) shown += 1;
    });
    document.getElementById("arama-sonuc").textContent = shown + " sipariş gösteriliyor.";
  }
  ara.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      filtrele();
    }
  });
  ara.addEventListener("search", filtrele);

  document.getElementById("rapor").addEventListener("click", function () {
    var durum = document.getElementById("rapor-durum");
    durum.textContent = "Hazırlanıyor…";
    window.setTimeout(function () { durum.textContent = "Rapor hazır"; }, 2000);
  });
})();
