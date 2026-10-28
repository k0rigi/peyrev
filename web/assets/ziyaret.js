/* peyrev.com'un çerezsiz ziyaret sayımı: sayfa açılınca bir kez POST /api/ziyaret (peyrev.ziyaret).
 *
 * Gönderilen yalnız sayfanın yolu ve başka bir siteden gelindiyse o sitenin alan adıdır; çerez, tarayıcı kimliği ya da
 * kişiye ait bir bilgi yoktur. Yerel önizlemede ve otomasyonla açılan tarayıcıda sayım yapılmaz.
 */
(function () {
  'use strict';

  if (location.hostname !== 'peyrev.com' || navigator.webdriver || !navigator.sendBeacon) return;
  let source = '';
  try {
    const host = document.referrer ? new URL(document.referrer).hostname : '';
    if (host !== location.hostname) source = host;
  } catch (error) {
    source = '';
  }
  const body = JSON.stringify({ yol: location.pathname, kaynak: source });
  navigator.sendBeacon('/api/ziyaret', new Blob([body], { type: 'text/plain' }));
})();
