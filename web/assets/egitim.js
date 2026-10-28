/* peyrev.com/egitim/: ücretsiz eğitim videoları (GET /api/egitim). Derleme adımı ve harici betik yok.
 *
 * Sunucudan gelen her metin DOM'a textContent ile yazılır. Video kimliği 11 karakterlik YouTube biçimine uymazsa video
 * gösterilmez. Video, ziyaretçi oynat'a basana kadar yüklenmez: önce yalnız küçük resim (i.ytimg.com) gelir, sonra
 * youtube-nocookie.com oynatıcısı açılır. Sayfanın CSP kuralı yalnız bu iki adrese izin verir.
 */
(function () {
  'use strict';

  const ENDPOINT = '/api/egitim';
  const VIDEO = /^[A-Za-z0-9_-]{11}$/;
  const ALL = 'Tümü';
  const list = document.getElementById('videolar');
  const filters = document.getElementById('suzgec');
  const status = document.getElementById('durum');
  if (!list) return;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function play(frame, video) {
    const player = document.createElement('iframe');
    player.src = 'https://www.youtube-nocookie.com/embed/' + video.video + '?autoplay=1&rel=0';
    player.title = video.baslik;
    player.allow = 'autoplay; encrypted-media; picture-in-picture; fullscreen';
    player.allowFullscreen = true;
    player.referrerPolicy = 'strict-origin-when-cross-origin';
    frame.replaceChildren(player);
    player.focus();
  }

  function card(video) {
    const item = el('li', 'video');
    item.dataset.kategori = video.kategori;
    const frame = el('div', 'video-frame');
    const thumb = document.createElement('img');
    thumb.src = 'https://i.ytimg.com/vi/' + video.video + '/hqdefault.jpg';
    thumb.alt = '';
    thumb.loading = 'lazy';
    thumb.decoding = 'async';
    const start = el('button', 'video-play');
    start.type = 'button';
    start.setAttribute('aria-label', video.baslik + ' videosunu oynat');
    start.append(el('span', '', '▶'));
    start.addEventListener('click', () => play(frame, video));
    frame.append(thumb, start);
    const body = el('div', 'video-body');
    body.append(el('p', 'video-meta', video.kategori), el('h2', '', video.baslik));
    if (video.aciklama) body.append(el('p', '', video.aciklama));
    item.append(frame, body);
    return item;
  }

  function show(category) {
    for (const button of filters.children) button.setAttribute('aria-pressed', String(button.dataset.kategori === category));
    for (const item of list.children) item.hidden = category !== ALL && item.dataset.kategori !== category;
  }

  function empty(title, text) {
    status.replaceChildren(el('strong', '', title), el('span', '', text));
    status.hidden = false;
  }

  async function load() {
    let data;
    try {
      const response = await fetch(ENDPOINT, { credentials: 'omit', cache: 'no-store', headers: { Accept: 'application/json' } });
      data = await response.json();
      if (!response.ok || data.success !== true || !Array.isArray(data.videolar)) throw new Error('liste');
    } catch (error) {
      empty('Videolar şu an yüklenemedi.', 'Biraz sonra sayfayı yenileyin.');
      return;
    }
    const videos = data.videolar.filter((video) => video && VIDEO.test(String(video.video)));
    if (!videos.length) {
      empty('Eğitim videoları yakında burada.', 'İlk videolar hazırlanıyor. Bu arada Peyrev hakkında her şeyi ana sayfada bulabilirsiniz.');
      return;
    }
    list.replaceChildren(...videos.map(card));
    const categories = Array.from(new Set(videos.map((video) => video.kategori)));
    if (categories.length > 1) {
      for (const name of [ALL].concat(categories)) {
        const button = el('button', 'filter', name);
        button.type = 'button';
        button.dataset.kategori = name;
        button.addEventListener('click', () => show(name));
        filters.append(button);
      }
      filters.hidden = false;
      show(ALL);
    }
    status.hidden = true;
    list.hidden = false;
  }

  load();
})();
