/* Peyrev yönetim paneli: tek sayfa, derleme adımı yok.
 *
 * Veriler yalnız /api/yonetim uçlarından gelir ve DOM'a metin düğümü ya da textContent olarak yazılır; HTML olarak
 * yorumlanan hiçbir yazım yoktur. Oturum HttpOnly çerezdedir, betik belirteci görmez. Durum değiştiren her istek
 * X-Peyrev-Yonetim başlığını taşır ve referrerPolicy 'same-origin' ile gider: sayfanın no-referrer kuralı isteklere
 * geçmez, Origin göndermeyen tarayıcıda da sunucu Referer'i denetleyebilir.
 *
 * Şifre kodu ve şifre değiştirme Studio'yla ortak hesap uçlarını (/api/rpa/hesap/*) kullanır; kurallar oradadır.
 *
 * Bölümler adres çubuğundaki çapadır: #ozet, #kullanicilar, #kullanici-<id>, #basvurular, #destek, #destek-<no>,
 * #olaylar, #hesabim. Bildirimlerdeki https://peyrev.com/yonetim/#destek-<no> bağlantısı girişten sonra talebi açar.
 */
(function () {
  'use strict';

  const API = '/api/yonetim/';
  const ACCOUNT_API = '/api/rpa/hesap/';
  const TITLE = 'Peyrev yönetimi';
  const UNREACHABLE = 'Sunucuya ulaşılamadı. İnternet bağlantınızı kontrol edip tekrar deneyin.';
  const FAILED = 'İşlem tamamlanamadı. Daha sonra tekrar deneyin.';
  const SESSION_CODES = ['OTURUM_GEREKLI', 'OTURUM_GECERSIZ'];
  const MODULES = { MOD_RPA: 'Peyrev', MOD_AJAN: 'Ajanlar' };
  const PACKAGES = { rpa: 'Peyrev', rpa_ajan: 'Peyrev + Ajanlar' };
  const ROLES = { kullanici: 'Kullanıcı', yonetici: 'Yönetici' };
  const ACCOUNT_STATES = { aktif: 'Aktif', pasif: 'Pasif' };
  const RIGHT_STATES = { aktif: 'Kullanabilir', sure_doldu: 'Süresi doldu', yetki_yok: 'Hakkı yok' };
  const SOURCES = { orkestrai: 'orkestrai.net\'ten taşındı', basvuru: 'Bireysel başvuru', yonetim: 'Yönetimden açıldı' };
  const CODE_DURATIONS = [['24', '24 saat'], ['72', '3 gün'], ['168', '7 gün']];
  const ATTACHMENT_TITLES = { akis: 'Akış yapısı (değerler maskeli)', hata_kaydi: 'Son çalışmanın hata kaydı' };
  const TICKET_TONES = { acik: 'warn', inceleniyor: 'accent', yanitlandi: 'ok', planlandi: 'accent', yayinlandi: 'ok' };
  const APPLICATION_TABS = [['bekliyor', 'Ödeme bekleyen'], ['reddedildi', 'Reddedilen'], ['onaylandi', 'Onaylanan']];
  const APPLICATION_EMPTY = {
    bekliyor: 'Ödeme bekleyen başvuru yok.', reddedildi: 'Reddedilen başvuru yok.', onaylandi: 'Onaylanan başvuru yok.',
  };
  // Olay ayrıntısındaki alanların görünen adları, gösterildikleri sırayla; listede olmayan alan (ör. taşıma aracının)
  // sonda, olduğu gibi gösterilir.
  const DETAIL_LABELS = {
    giris: 'Giriş adı', ad_soyad: 'Ad soyad', telefon: 'Telefon', alanlar: 'Değişen alanlar', onceki: 'Önceki',
    onceki_durum: 'Önceki durum', rol: 'Rol', durum: 'Durum', modul: 'Modül', moduller: 'Modüller', paket: 'Paket',
    baslangic: 'Başlangıç', bitis: 'Bitiş', gecerlilik: 'Geçerlilik', kod_gecerlilik: 'Kodun geçerliliği',
    yayin_surumu: 'Yayın sürümü', yanit_uzunlugu: 'Yanıt uzunluğu', not: 'Not', basvuru_id: 'Başvuru',
    kullanici_id: 'Hesap', oturum_id: 'Oturum', adet: 'Adet', kapatilan_oturum: 'Kapatılan Studio oturumu',
    kapatilan_panel_oturumu: 'Kapatılan panel oturumu', ip: 'IP adresi', iki_adim: 'İki adımlı doğrulama',
  };
  const DETAIL_ORDER = Object.keys(DETAIL_LABELS);
  // Destek talebi durumlarının adları (sunucunun listesi Destek bölümünde yüklenince onunkiler geçerlidir).
  const TICKET_STATES = {
    acik: 'Açık', inceleniyor: 'İnceleniyor', yanitlandi: 'Yanıtlandı', planlandi: 'Planlandı', yayinlandi: 'Yayınlandı',
    kapandi: 'Kapatıldı',
  };
  const MOMENT = new Intl.DateTimeFormat('tr-TR', { timeZone: 'Europe/Istanbul', year: 'numeric', month: '2-digit',
    day: '2-digit', hour: '2-digit', minute: '2-digit' });

  const $ = (id) => document.getElementById(id);
  const content = $('icerik');
  const views = { boot: $('acilis'), login: $('giris'), app: $('uygulama') };
  const loginForm = $('giris-formu');
  const loginStatus = $('giris-durumu');
  const codeField = $('kod-alani');
  const resetForm = $('sifre-kodu-formu');
  const resetStatus = $('sifre-kodu-durumu');
  const toast = { box: $('bildirim-alani'), status: $('bildirim'), alert: $('uyari'), close: $('bildirim-kapat'), timer: 0 };
  const state = {
    admin: null, ticketStates: null,
    users: { ara: '', durum: '', sayfa: 1 },
    applications: 'bekliyor',
    tickets: { durum: '', tur: '' },
    events: { tur: '', sayfa: 1 },
  };
  let uid = 0;

  class SessionLost extends Error {}

  // ----- DOM yardımcıları ---------------------------------------------------------------------
  /** Öğe kurar: class, text, on<olay> ve öznitelikler; true boole öznitelik, false/null/undefined atlanır.
   *  Çocuklar düğüm ya da metindir; metinler her zaman metin düğümü olur. */
  function h(tag, props, children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = String(value);
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : String(value));
    }
    for (const child of [].concat(children === undefined ? [] : children)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : String(child));
    }
    return node;
  }

  function nextId(prefix) {
    uid += 1;
    return prefix + '-' + uid;
  }

  function day(value) {
    if (!value) return '';
    const [date, time] = String(value).split(' ');
    const parts = date.split('-');
    const text = parts.length === 3 ? parts[2] + '.' + parts[1] + '.' + parts[0] : String(value);
    return time ? text + ' ' + time : text;
  }

  function isoDay(date) {
    return new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Istanbul', year: 'numeric', month: '2-digit',
      day: '2-digit' }).format(date);
  }

  function today() {
    return isoDay(new Date());
  }

  function yearLater() {
    const date = new Date();
    date.setFullYear(date.getFullYear() + 1);
    return isoDay(date);
  }

  function query(path, params) {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== '' && value !== null && value !== undefined && !(key === 'sayfa' && value === 1)) {
        search.set(key, value);
      }
    }
    const text = search.toString();
    return text ? path + '?' + text : path;
  }

  function pill(text, tone) {
    return h('span', { class: 'pill' + (tone ? ' pill-' + tone : ''), text });
  }

  function empty(text) {
    return h('p', { class: 'empty', text });
  }

  /** İçeriği değiştirir; null, undefined ve false atlanır (replaceChildren onları metne çevirirdi). */
  function put(target, ...children) {
    target.replaceChildren(...children.filter((child) => child !== null && child !== undefined && child !== false));
  }

  function facts(pairs) {
    const list = h('dl', { class: 'facts' });
    for (const [label, value] of pairs) {
      if (value === undefined || value === null || value === '') continue;
      list.append(h('div', null, [h('dt', { text: label }), h('dd', null, value)]));
    }
    return list;
  }

  /** Adres "@" işaretinden sonra satır atlayabilir; dar sütunda kelime ortasından bölünmez. */
  function breakable(text) {
    if (!text) return '';
    return String(text).split('@').flatMap((part, index) => (index ? ['@', h('wbr'), part] : [part]));
  }

  function mail(address) {
    return address ? h('a', { href: 'mailto:' + address }, breakable(address)) : null;
  }

  function phone(number) {
    return number ? h('a', { href: 'tel:' + number.replace(/[^+0-9]/g, ''), text: number }) : null;
  }

  function field(label, control, options) {
    const settings = options || {};
    if (!control.id) control.id = nextId('alan');
    const parts = [h('label', { for: control.id, text: label }), control];
    if (settings.hint) {
      const hintId = nextId('ipucu');
      control.setAttribute('aria-describedby', hintId);
      parts.push(h('span', { class: 'hint', id: hintId, text: settings.hint }));
    }
    return h('div', { class: 'field' + (settings.wide ? ' wide' : '') }, parts);
  }

  function input(name, props) {
    return h('input', Object.assign({ name }, props));
  }

  function select(name, options, value) {
    return h('select', { name }, options.map(([key, text]) => h('option', { value: key, selected: key === value, text })));
  }

  function checkbox(name, label, checked) {
    const control = h('input', { type: 'checkbox', name, checked: Boolean(checked) });
    return { control, node: h('label', { class: 'check' }, [control, label]) };
  }

  function section(id, title, children) {
    return h('section', { class: 'card', 'aria-labelledby': id }, [h('h2', { id, tabindex: '-1', text: title })]
      .concat(children));
  }

  // ----- bildirim -----------------------------------------------------------------------------
  function say(text, kind) {
    clearTimeout(toast.timer);
    const error = kind === 'error';
    toast.status.textContent = error ? '' : text;
    toast.alert.textContent = error ? text : '';
    toast.box.classList.toggle('is-error', error);
    toast.box.classList.add('is-open');
    toast.close.hidden = false;
    if (!error) toast.timer = setTimeout(hideToast, 6000);
  }

  function hideToast() {
    clearTimeout(toast.timer);
    toast.box.classList.remove('is-open');
    toast.close.hidden = true;
    toast.status.textContent = '';
    toast.alert.textContent = '';
  }

  // ----- sunucu -------------------------------------------------------------------------------
  /** JSON isteği: { response, data }; ağ hatası okunur bir metinle düşer. */
  async function send(url, init) {
    let response;
    try {
      response = await fetch(url, Object.assign({ referrerPolicy: 'same-origin', cache: 'no-store' }, init));
    } catch (error) {
      throw new Error(UNREACHABLE);
    }
    let data = {};
    try {
      data = (await response.json()) || {};
    } catch (error) {
      data = {};
    }
    return { response, data };
  }

  function failure(data) {
    const error = new Error(data.message || FAILED);
    error.kod = data.kod;
    return error;
  }

  async function api(path, body) {
    const post = body !== undefined;
    const init = { method: post ? 'POST' : 'GET', credentials: 'same-origin', headers: { Accept: 'application/json' } };
    if (post) {
      init.headers['Content-Type'] = 'application/json';
      init.headers['X-Peyrev-Yonetim'] = '1';
      init.body = JSON.stringify(body);
    }
    const { response, data } = await send(API + path, init);
    if (response.ok && data.success === true) return data;
    if (response.status === 401 && SESSION_CODES.includes(data.kod)) {
      showLogin(data.kod === 'OTURUM_GECERSIZ' ? data.message : '');
      throw new SessionLost(data.message || '');
    }
    throw failure(data);
  }

  /** Studio'yla ortak hesap uçları (şifre kodu, şifre değiştirme): oturumsuzdur, çerez gönderilmez. */
  async function accountApi(path, body) {
    const { response, data } = await send(ACCOUNT_API + path, {
      method: 'POST', credentials: 'omit',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (response.ok && data.success === true) return data;
    throw failure(data);
  }

  /** Sunucudaki şifre kuralının önden denetimi (8-128 karakter, harf ve rakam) ve tekrarın eşleşmesi. */
  function passwordProblem(password, repeat) {
    if (password.length < 8 || password.length > 128) return 'Yeni şifre 8-128 karakter olmalıdır.';
    if (!/\p{L}/u.test(password) || !/\d/.test(password)) return 'Yeni şifrede en az bir harf ve bir rakam bulunmalıdır.';
    if (password !== repeat) return 'Yeni şifre ile tekrarı aynı değil.';
    return '';
  }

  async function fetchText(url) {
    let response;
    try {
      response = await fetch(url, { credentials: 'same-origin', referrerPolicy: 'same-origin', cache: 'no-store' });
    } catch (error) {
      throw new Error(UNREACHABLE);
    }
    if (response.status === 401) {
      showLogin('');
      throw new SessionLost('');
    }
    if (!response.ok) throw new Error('Ek açılamadı.');
    return response.text();
  }

  /** Düğmeyi işi bitene kadar devre dışı bırakır; onay sorusu varsa önce sorar, hatayı bildirime yazar. */
  async function run(control, work, question) {
    const text = typeof question === 'function' ? question() : question;
    if (text && !window.confirm(text)) return;
    control.disabled = true;
    try {
      await work();
    } catch (error) {
      if (!(error instanceof SessionLost)) say(error.message, 'error');
    } finally {
      control.disabled = false;
    }
  }

  function button(label, work, options) {
    const settings = options || {};
    const node = h('button', {
      type: settings.submit ? 'submit' : 'button',
      class: 'btn' + (settings.kind ? ' btn-' + settings.kind : '') + (settings.small ? ' btn-small' : ''),
    }, label);
    if (work) node.addEventListener('click', () => run(node, work, settings.confirm));
    return node;
  }

  function form(fields, label, work, options) {
    const settings = options || {};
    const submit = button(label, null, { kind: settings.kind || 'primary', submit: true });
    const node = h('form', { class: 'form-grid', novalidate: true },
      fields.concat([h('div', { class: 'actions wide' }, [submit].concat(settings.extra || []))]));
    node.addEventListener('submit', (event) => {
      event.preventDefault();
      run(submit, work, settings.confirm);
    });
    return node;
  }

  /** Yükleniyor, hata ve yeniden deneme durumlarıyla bir alanı doldurur. */
  async function fill(target, work) {
    target.setAttribute('aria-busy', 'true');
    target.replaceChildren(h('p', { class: 'loading', text: 'Yükleniyor…' }));
    try {
      await work(target);
    } catch (error) {
      if (error instanceof SessionLost) return;
      target.replaceChildren(h('div', { class: 'notice notice-error', role: 'alert' }, [
        h('p', { text: error.message }),
        h('div', { class: 'actions' }, [button('Tekrar dene', () => fill(target, work))]),
      ]));
    } finally {
      target.removeAttribute('aria-busy');
    }
  }

  /** Alanı doldurur ve yenileme işlevi döndürür; yenilemeden sonra odak verilen bölüm başlığına gider. */
  function reloadable(target, work) {
    const refresh = async (focusId) => {
      await fill(target, (node) => work(node, refresh));
      const heading = focusId ? document.getElementById(focusId) : null;
      if (heading) heading.focus();
    };
    refresh();
    return refresh;
  }

  async function copy(text, what) {
    try {
      await navigator.clipboard.writeText(text);
      say(what + ' panoya kopyalandı.', 'ok');
    } catch (error) {
      say(what + ' kopyalanamadı; seçip elle kopyalayın.', 'error');
    }
  }

  function codeBox(code, title, note) {
    return h('div', { class: 'code-box' }, [
      h('p', { text: title }),
      h('p', { class: 'code', text: code.kod }),
      h('p', { class: 'muted', text: note }),
      h('div', { class: 'actions' }, [button('Kodu kopyala', () => copy(code.kod, 'Kod'))]),
    ]);
  }

  // ----- sayfa iskeleti ve yönlendirme --------------------------------------------------------
  function page(title, options) {
    const settings = options || {};
    const heading = h('h1', { tabindex: '-1', text: title });
    const head = h('div', { class: 'page-head' }, [heading].concat(settings.actions || []));
    if (settings.lead) head.append(h('p', { class: 'lead', text: settings.lead }));
    const body = h('div', { class: 'panel-section' });
    const back = settings.back
      ? h('a', { class: 'back', href: settings.back[0] }, [h('span', { 'aria-hidden': 'true', text: '← ' }), settings.back[1]])
      : null;
    content.replaceChildren(...[back, head, body].filter(Boolean));
    document.title = title + ' · ' + TITLE;
    return { heading, head, body, retitle(text) { heading.textContent = text; document.title = text + ' · ' + TITLE; } };
  }

  const ROUTES = [
    [/^ozet$/, 'ozet', () => renderSummary()],
    [/^kullanicilar$/, 'kullanicilar', () => renderUsers()],
    [/^kullanici-(\d{1,18})$/, 'kullanicilar', (match) => renderUser(match[1])],
    [/^basvurular$/, 'basvurular', () => renderApplications()],
    [/^destek$/, 'destek', () => renderTickets()],
    [/^destek-(\d{1,10})$/, 'destek', (match) => renderTicket(match[1])],
    [/^olaylar$/, 'olaylar', () => renderEvents()],
    [/^hesabim$/, 'hesabim', () => renderAccount()],
  ];

  function route() {
    if (!state.admin) return;
    const hash = location.hash.slice(1) || 'ozet';
    for (const [pattern, name, render] of ROUTES) {
      const match = pattern.exec(hash);
      if (!match) continue;
      for (const link of document.querySelectorAll('.sections a')) {
        if (link.dataset.section === name) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
      }
      window.scrollTo(0, 0);
      render(match);
      const heading = content.querySelector('h1');
      if (heading) heading.focus({ preventScroll: true });
      return;
    }
    location.replace('#ozet');
  }

  function badge(node, count, label) {
    node.hidden = !count;
    node.replaceChildren(String(count || ''), h('span', { class: 'visually-hidden', text: ' ' + label }));
  }

  function setBadges(summary) {
    badge($('rozet-basvuru'), summary.bekleyen_basvuru, 'ödeme bekleyen');
    badge($('rozet-destek'), summary.okunmamis_destek, 'okunmamış');
  }

  async function refreshBadges() {
    try {
      setBadges(await api('ozet'));
    } catch (error) {
      // The badges keep their last value; the section itself reports its own errors.
    }
  }

  function pager(data, go) {
    const pages = Math.max(1, Math.ceil(data.toplam / data.sayfa_boyu));
    if (pages <= 1) return null;
    const previous = button('← Önceki', () => go(data.sayfa - 1));
    const next = button('Sonraki →', () => go(data.sayfa + 1));
    previous.disabled = data.sayfa <= 1;
    next.disabled = data.sayfa >= pages;
    return h('nav', { class: 'pager', 'aria-label': 'Sayfalar' },
      [previous, h('span', { class: 'muted', text: 'Sayfa ' + data.sayfa + ' / ' + pages }), next]);
  }

  // ----- Özet ---------------------------------------------------------------------------------
  function stat(value, label, href) {
    return h(href ? 'a' : 'div', { class: 'stat', href },
      [h('span', { class: 'stat-value', text: value }), h('span', { class: 'stat-label', text: label })]);
  }

  function renderSummary() {
    const view = page('Özet', { lead: 'Hesapların, başvuruların ve destek taleplerinin şu anki durumu.' });
    reloadable(view.body, async (target, refresh) => {
      const [data, model] = await Promise.all([api('ozet'), api('ortak-model').catch(() => null)]);
      setBadges(data);
      target.replaceChildren(
        h('div', { class: 'stats' }, [
          stat(data.aktif_kullanici, 'Aktif hesap', '#kullanicilar'),
          stat(data.lisansli_kullanici, 'Peyrev\'i kullanabilen hesap'),
          stat(data.yakinda_bitecek, '30 gün içinde hakkı bitecek hesap'),
          stat(data.bekleyen_basvuru, 'Ödeme bekleyen başvuru', '#basvurular'),
          stat(data.okunmamis_destek, 'Okunmamış destek talebi', '#destek'),
        ]),
        endingSection(data),
        modelSection(model, refresh),
      );
    });
  }

  function endingSection(data) {
    const id = 'bolum-bitecek';
    if (!data.bitecekler.length) {
      return section(id, '30 gün içinde bitecek haklar', [empty('Önümüzdeki 30 günde biten modül hakkı yok.')]);
    }
    const list = h('ul', { class: 'rows' }, data.bitecekler.map((item) => h('li', { class: 'row' }, [
      h('div', null, [
        h('a', { href: '#kullanici-' + item.kullanici_id, text: item.ad_soyad || item.giris }),
        h('div', { class: 'sub' }, breakable(item.giris)),
      ]),
      h('div', { class: 'pills' }, [
        pill(MODULES[item.modul] || item.modul, 'accent'),
        pill(day(item.bitis) + ' · ' + (item.kalan_gun === 0 ? 'son gün bugün' : item.kalan_gun + ' gün kaldı'),
          item.kalan_gun <= 7 ? 'warn' : ''),
      ]),
    ])));
    const note = data.bitecekler.length >= 50 ? h('p', { class: 'muted', text: 'İlk 50 hak gösteriliyor.' }) : null;
    return section(id, '30 gün içinde bitecek haklar', [list, note]);
  }

  function modelReason(model) {
    if (model.enabled) {
      return 'Ayar geçerli ve çalıştırıcı aynı ayarla çalışıyor. Modeller: ' + (model.ayar.modeller || []).join(', ') + '.';
    }
    if (!model.ayar.dosya) return 'Ayar dosyası yok; Studio ajanları bu bağlantıyı kapalı görür.';
    if (!model.ayar.gecerli) return 'Ayar dosyası geçersiz ya da güvenilir değil; özellik kapalı.';
    if (!model.calistirici.calisiyor) return 'Ayar geçerli ama çalıştırıcı çalışmıyor; özellik kapalı.';
    return 'Çalıştırıcı eski ayarla çalışıyor; yeniden başlatılana kadar özellik kapalı.';
  }

  function modelSection(model, refresh) {
    const id = 'bolum-model';
    if (!model) return section(id, 'Ortak model', [empty('Ortak modelin durumu alınamadı.')]);
    const jobs = model.isler || {};
    const waiting = (jobs.queued || 0) + (jobs.running || 0);
    const clear = waiting ? button('Kuyruğu boşalt', async () => {
      const result = await api('ortak-model/kuyrugu-bosalt', {});
      say(result.iptal + ' iş iptal edildi.', 'ok');
      refresh(id);
    }, { kind: 'danger', confirm: 'Sıradaki ve çalışan bütün ortak model işleri iptal edilsin mi?' }) : null;
    return section(id, 'Ortak model', [
      h('div', { class: 'pills' }, [pill(model.enabled ? 'Açık' : 'Kapalı', model.enabled ? 'ok' : '')]),
      h('p', { class: 'muted', text: modelReason(model) }),
      facts([['Sırada bekleyen iş', jobs.queued || 0], ['Çalışan iş', jobs.running || 0]]),
      clear ? h('div', { class: 'actions' }, [clear]) : null,
    ]);
  }

  // ----- Kullanıcılar -------------------------------------------------------------------------
  function rightText(user, module) {
    const row = (user.moduller || {})[module];
    if (!row) return module === 'MOD_RPA' && user.rol === 'yonetici' ? 'Yönetici olarak açık' : 'Yok';
    if (!row.bitis) return 'Süresiz';
    return (row.bitis < today() ? 'Süresi doldu: ' : 'Bitiş: ') + day(row.bitis);
  }

  function userCard(user) {
    return h('article', { class: 'card' }, [
      h('div', { class: 'card-head' }, [
        h('h2', { class: 'card-title' }, h('a', { href: '#kullanici-' + user.kullanici_id, text: user.ad_soyad || user.giris })),
        h('div', { class: 'pills' }, [
          pill(ROLES[user.rol] || user.rol, user.rol === 'yonetici' ? 'accent' : ''),
          user.durum === 'pasif' ? pill('Pasif', 'danger') : null,
          user.oturum_acik ? pill('Studio açık', 'ok') : null,
        ]),
      ]),
      facts([
        ['Giriş adı', breakable(user.giris)], ['Peyrev', rightText(user, 'MOD_RPA')], ['Ajanlar', rightText(user, 'MOD_AJAN')],
        ['Telefon', user.telefon], ['Açılış', day(user.acilis)], ['Kaynak', SOURCES[user.kaynak] || user.kaynak],
      ]),
    ]);
  }

  function renderUsers() {
    const create = button('Yeni kullanıcı', null, { kind: 'primary' });
    const view = page('Kullanıcılar', {
      actions: [create],
      lead: 'Bütün Peyrev hesapları. Hesabın ayrıntısında modül hakları, şifre kodu, Studio oturumları ve ajan ofisleri yönetilir.',
    });
    const creator = h('div', { hidden: true, id: 'yeni-kullanici' });
    create.setAttribute('aria-expanded', 'false');
    create.setAttribute('aria-controls', 'yeni-kullanici');
    create.addEventListener('click', () => {
      const opening = creator.hidden;
      creator.hidden = !opening;
      create.setAttribute('aria-expanded', String(opening));
      if (opening) {
        creator.replaceChildren(newUserForm());
        creator.querySelector('input').focus();
      }
    });
    const search = input('ara', { type: 'search', value: state.users.ara, maxlength: 150,
      placeholder: 'Ad, kullanıcı adı veya e-posta', autocomplete: 'off' });
    const status = select('durum', [['', 'Tümü'], ['aktif', 'Aktif'], ['pasif', 'Pasif']], state.users.durum);
    const toolbar = h('form', { class: 'toolbar', role: 'search' },
      [field('Ara', search), field('Durum', status), button('Ara', null, { submit: true })]);
    const list = h('div', { class: 'panel-section' });
    const load = () => fill(list, async (target) => {
      const data = await api(query('kullanicilar', state.users));
      if (!data.kullanicilar.length) {
        target.replaceChildren(empty(state.users.ara || state.users.durum ? 'Bu aramayla eşleşen hesap yok.' : 'Henüz hesap yok.'));
        return;
      }
      put(target,
        h('p', { class: 'muted', role: 'status', text: data.toplam + ' hesap' }),
        h('div', { class: 'cards' }, data.kullanicilar.map(userCard)),
        pager(data, (number) => {
          state.users.sayfa = number;
          return load();
        }),
      );
    });
    toolbar.addEventListener('submit', (event) => {
      event.preventDefault();
      state.users = { ara: search.value.trim(), durum: status.value, sayfa: 1 };
      load();
    });
    status.addEventListener('change', () => toolbar.requestSubmit());
    view.body.replaceChildren(creator, toolbar, list);
    load();
  }

  function newUserForm() {
    const login = input('giris', { maxlength: 150, autocomplete: 'off', autocapitalize: 'none', spellcheck: 'false',
      required: true });
    const name = input('ad_soyad', { maxlength: 120, autocomplete: 'off', required: true });
    const tel = input('telefon', { type: 'tel', maxlength: 32, autocomplete: 'off' });
    const role = select('rol', Object.entries(ROLES), 'kullanici');
    const start = input('baslangic', { type: 'date', value: today(), required: true });
    const end = input('bitis', { type: 'date', value: yearLater(), required: true });
    const agents = checkbox('ajan', 'Ajanlar hakkı da verilsin');
    const result = h('div', { class: 'panel-section' });
    const node = form([
      field('Kullanıcı adı veya e-posta', login,
        { hint: 'Kişi Peyrev\'e bununla girer: 3-64 karakter (harf, rakam, . _ + -) ya da e-posta adresi.' }),
      field('Ad soyad', name),
      field('Telefon (isteğe bağlı)', tel),
      field('Rol', role, { hint: 'Yönetici bu paneli kullanır.' }),
      field('Lisans başlangıcı', start),
      field('Lisans bitişi', end, { hint: 'Bitiş günü dahildir. Süresiz hak hesap açıldıktan sonra ayrıntıdan verilir.' }),
      h('div', { class: 'field wide' }, agents.node),
    ], 'Hesabı aç', async () => {
      const created = await api('kullanicilar', {
        giris: login.value.trim(), ad_soyad: name.value.trim(), telefon: tel.value.trim(), rol: role.value,
        baslangic: start.value, bitis: end.value, ajan: agents.control.checked,
      });
      node.hidden = true;
      put(result,
        created.uyari ? h('p', { class: 'notice notice-warn', role: 'alert', text: created.uyari }) : null,
        codeBox(created.kod, created.kullanici.giris + ' hesabı açıldı. İlk şifre kodu:',
          'Kodu kişiye güvenli bir kanaldan iletin. ' + day(created.kod.gecerlilik) + ' saatine kadar geçerlidir ve '
          + 'bir kez kullanılır. Kişi Peyrev giriş ekranında "Şifre kodum var" ile kullanıcı adını, kodu ve kendi '
          + 'şifresini yazar. Kod bir daha gösterilmez.'),
        h('div', { class: 'actions' }, [h('a', { class: 'btn', href: '#kullanici-' + created.kullanici.kullanici_id,
          text: 'Hesabın ayrıntısını aç' })]),
      );
      result.querySelector('.code').setAttribute('tabindex', '-1');
      result.querySelector('.code').focus();
      say('Hesap açıldı.', 'ok');
    }, {
      confirm: () => (name.value.trim() || login.value.trim()) + ' için ' + ROLES[role.value].toLocaleLowerCase('tr')
        + ' hesabı ' + day(start.value) + ' – ' + day(end.value) + ' lisansıyla'
        + (agents.control.checked ? ' (Ajanlar dahil)' : '') + ' açılsın mı?',
    });
    return h('section', { class: 'card', 'aria-labelledby': 'yeni-baslik' },
      [h('h2', { id: 'yeni-baslik', text: 'Yeni kullanıcı' }), node, result]);
  }

  // ----- Kullanıcı ayrıntısı ------------------------------------------------------------------
  function renderUser(userId) {
    const view = page('Hesap', { back: ['#kullanicilar', 'Kullanıcılar'] });
    const path = 'kullanicilar/' + userId;
    reloadable(view.body, async (target, refresh) => {
      const data = await api(path);
      const user = data.kullanici;
      view.retitle(user.ad_soyad || user.giris);
      const events = h('div', { class: 'panel-section' });
      target.replaceChildren(
        accountSection(user, path, refresh),
        rightsSection(user, path, refresh),
        codeSection(user, path),
        studioSection(data.oturumlar, path, refresh),
        officeSection(data.ofisler, refresh),
        otherSection(data),
        section('bolum-olay', 'Son olaylar', [events]),
      );
      fill(events, async (node) => {
        const found = await api(query('olaylar', { hedef: 'kullanici:' + user.kullanici_id }));
        node.replaceChildren(found.olaylar.length
          ? h('div', { class: 'cards' }, found.olaylar.slice(0, 10).map(eventCard))
          : empty('Bu hesapla ilgili olay yok.'));
      });
    });
  }

  function accountSection(user, path, refresh) {
    const id = 'bolum-hesap';
    const self = user.kullanici_id === state.admin.kullanici_id;
    const name = input('ad_soyad', { value: user.ad_soyad, maxlength: 120, required: true });
    const tel = input('telefon', { type: 'tel', value: user.telefon || '', maxlength: 32 });
    const details = form([field('Ad soyad', name), field('Telefon', tel)], 'Bilgileri kaydet', async () => {
      const result = await api(path + '/bilgiler', { ad_soyad: name.value.trim(), telefon: tel.value.trim() });
      say(result.kullanici.degisti ? 'Bilgiler kaydedildi.' : 'Değişiklik yok.', 'ok');
      refresh(id);
    });
    const role = select('rol', Object.entries(ROLES), user.rol);
    const roleForm = form([field('Rol', role, {
      hint: self ? 'Kendi yönetici yetkinizi kaldıramazsınız.'
        : 'Yönetici bu paneli kullanır ve Peyrev\'i hak satırı olmadan da açar; Ajanlar için hak gerekir.',
    })], 'Rolü kaydet', async () => {
      await api(path + '/rol', { rol: role.value });
      say('Rol kaydedildi.', 'ok');
      refresh(id);
    }, { confirm: () => user.giris + ' hesabının rolü "' + ROLES[role.value] + '" olsun mu?' });
    if (self) {
      role.disabled = true;
      roleForm.querySelector('button[type="submit"]').disabled = true;
    }
    const status = user.durum === 'aktif'
      ? (self ? null : button('Hesabı pasif yap', async () => {
        await api(path + '/durum', { durum: 'pasif' });
        say(user.giris + ' pasif yapıldı.', 'ok');
        refresh(id);
      }, { kind: 'danger', confirm: user.giris + ' pasif yapılsın mı? Kişi Peyrev\'e giremez; açık Studio ve panel oturumları kapanır.' }))
      : button('Hesabı etkinleştir', async () => {
        await api(path + '/durum', { durum: 'aktif' });
        say(user.giris + ' etkinleştirildi.', 'ok');
        refresh(id);
      }, { confirm: user.giris + ' yeniden etkinleştirilsin mi?' });
    const reset = user.iki_adim && !self ? button('İki adımlı doğrulamayı sıfırla', async () => {
      await api(path + '/iki-adim-sifirla', {});
      say('İki adımlı doğrulama sıfırlandı.', 'ok');
      refresh(id);
    }, { confirm: user.giris + ' için iki adımlı doğrulama kapatılsın mı? Panel oturumları kapanır; kişi şifresiyle girip Hesabım bölümünden yeniden açar.' }) : null;
    return section(id, 'Hesap', [
      h('div', { class: 'pills' }, [
        pill(ROLES[user.rol] || user.rol, user.rol === 'yonetici' ? 'accent' : ''),
        pill(ACCOUNT_STATES[user.durum] || user.durum, user.durum === 'aktif' ? 'ok' : 'danger'),
      ]),
      facts([
        ['Giriş adı', breakable(user.giris)], ['Peyrev kullanımı', RIGHT_STATES[user.rpa] || user.rpa],
        ['İki adımlı doğrulama', user.iki_adim ? 'Açık' : 'Kapalı'], ['Kaynak', SOURCES[user.kaynak] || user.kaynak],
        ['Açılış', day(user.acilis)], ['Son değişiklik', day(user.guncelleme)],
      ]),
      details,
      roleForm,
      status || reset ? h('div', { class: 'actions' }, [status, reset]) : null,
    ]);
  }

  function rightsSection(user, path, refresh) {
    const id = 'bolum-haklar';
    const blocks = Object.entries(MODULES).map(([module, label]) => {
      const row = user.moduller[module];
      const start = input('baslangic', { type: 'date', value: row ? row.baslangic || '' : today() });
      const end = input('bitis', { type: 'date', value: row ? row.bitis || '' : yearLater() });
      let status = 'Hak yok';
      let tone = '';
      if (row && row.durum === 'aktif') {
        status = row.bitis ? 'Geçerli · bitiş ' + day(row.bitis) : 'Süresiz';
        tone = 'ok';
      } else if (row) {
        status = 'Süresi doldu · ' + day(row.bitis);
        tone = 'danger';
      } else if (module === 'MOD_RPA' && user.rol === 'yonetici') {
        status = 'Satır yok · yönetici olarak açık';
      }
      const remove = row ? button('Hakkı kaldır', async () => {
        await api(path + '/moduller/' + module + '/kaldir', {});
        say(label + ' hakkı kaldırıldı.', 'ok');
        refresh(id);
      }, { kind: 'danger', confirm: label + ' hakkı kaldırılsın mı? Açık Studio bir sonraki lisans yenilemesinde (en geç 10 dakika) bu hakkı kaybeder.' }) : null;
      const node = form([
        field('Başlangıç', start),
        field('Bitiş', end, { hint: 'Boş bırakılırsa süresiz. Bitiş günü dahildir.' }),
      ], row ? 'Hakkı kaydet' : 'Hakkı ver', async () => {
        await api(path + '/moduller/' + module, { baslangic: start.value, bitis: end.value });
        say(label + ' hakkı kaydedildi.', 'ok');
        refresh(id);
      }, {
        extra: [remove],
        confirm: () => label + ' hakkı ' + (end.value ? day(end.value) + ' tarihine kadar' : 'süresiz') + ' olsun mu?',
      });
      return h('div', { class: 'sub-block' }, [
        h('div', { class: 'card-head' }, [h('h3', { text: label }), pill(status, tone)]),
        node,
      ]);
    });
    return section(id, 'Modül hakları', [
      h('p', { class: 'muted', text: 'Peyrev için MOD_RPA, Ajanlar için MOD_AJAN hakkı gerekir; Ajanlar ikisini birlikte ister. Değişiklik kişinin açık Studio\'suna bir sonraki lisans yenilemesinde (en geç 10 dakika) yansır.' }),
    ].concat(blocks));
  }

  function codeSection(user, path) {
    const id = 'bolum-kod';
    const duration = select('saat', CODE_DURATIONS, user.kaynak === 'yonetim' ? '168' : '24');
    const result = h('div');
    const node = form([field('Geçerlilik', duration)], 'Şifre kodu üret', async () => {
      const data = await api(path + '/sifre-kodu', { saat: Number(duration.value) });
      result.replaceChildren(codeBox(data.kod, 'Yeni şifre kodu:',
        'Kodu kişiye güvenli bir kanaldan iletin. ' + day(data.kod.gecerlilik) + ' saatine kadar geçerlidir ve bir kez '
        + 'kullanılır; önceki kodlar geçersiz oldu. Kişi Peyrev giriş ekranında "Şifre kodum var" ile yeni şifresini '
        + 'belirler. Kod bir daha gösterilmez.'));
      const shown = result.querySelector('.code');
      shown.setAttribute('tabindex', '-1');
      shown.focus();
      say('Şifre kodu üretildi.', 'ok');
    }, { confirm: user.giris + ' için yeni şifre kodu üretilsin mi? Önceki kullanılmamış kod geçersiz olur.' });
    return section(id, 'Şifre kodu', [
      h('p', { class: 'muted', text: 'Şifresini unutan ya da henüz belirlemeyen kişi için tek kullanımlık kod.'
        + (user.durum === 'pasif' ? ' Hesap pasif olduğundan kod, hesap etkinleşene kadar kullanılamaz.' : '') }),
      node,
      result,
    ]);
  }

  function studioSection(sessions, path, refresh) {
    const id = 'bolum-studio';
    const intro = h('p', { class: 'muted', text: 'Hesap aynı anda tek bilgisayarda açık olabilir. Kapatılan oturumun Studio\'su en geç 10 dakika içinde giriş ekranına döner.' });
    if (!sessions.length) return section(id, 'Studio oturumları', [intro, empty('Açık Studio oturumu yok.')]);
    const rows = h('ul', { class: 'rows' }, sessions.map((session) => h('li', { class: 'row' }, [
      facts([['Cihaz', session.cihaz + '…'], ['Sürüm', session.surum], ['IP adresi', session.ip],
        ['Giriş', day(session.acilis)], ['Son görülme', day(session.son_gorulme)]]),
      h('div', { class: 'actions' }, [button('Oturumu kapat', async () => {
        await api(path + '/oturumlari-kapat', { oturum_id: session.id });
        say('Studio oturumu kapatıldı.', 'ok');
        refresh(id);
      }, { small: true, confirm: 'Bu Studio oturumu kapatılsın mı?' })]),
    ])));
    return section(id, 'Studio oturumları', [intro, rows]);
  }

  function officeSection(offices, refresh) {
    const id = 'bolum-ofis';
    const intro = h('p', { class: 'muted', text: 'Kişinin Peyrev\'de kurduğu ajan ofisleri (en çok ' + offices.sinir
      + '). Pasif ofise sahibi ve Studio erişemez; kayıtları korunur.' });
    if (!offices.liste.length) return section(id, 'Ajan ofisleri', [intro, empty('Ajan ofisi yok.')]);
    const rows = h('ul', { class: 'rows' }, offices.liste.map((office) => {
      const active = office.durum === 'aktif';
      return h('li', { class: 'row' }, [
        h('div', { class: 'panel-section' }, [
          h('div', { class: 'card-head' }, [h('h3', { text: office.ad }), pill(active ? 'Aktif' : 'Pasif', active ? 'ok' : 'danger')]),
          facts([['Görev', office.gorev + (office.acik_gorev ? ' (' + office.acik_gorev + ' açık)' : '')],
            ['Onay', office.onay + (office.bekleyen_onay ? ' (' + office.bekleyen_onay + ' bekliyor)' : '')],
            ['Kuruluş', day(office.olusturma)]]),
        ]),
        h('div', { class: 'actions' }, [button(active ? 'Ofisi pasif yap' : 'Ofisi etkinleştir', async () => {
          await api('ofisler/' + office.id + '/durum', { durum: active ? 'pasif' : 'aktif' });
          say(office.ad + (active ? ' pasif yapıldı.' : ' etkinleştirildi.'), 'ok');
          refresh(id);
        }, {
          small: true, kind: active ? 'danger' : '',
          confirm: active ? '"' + office.ad + '" ofisi pasif yapılsın mı? Sahibi ve Studio bu ofise erişemez; kayıtlar korunur.'
            : '"' + office.ad + '" ofisi yeniden etkinleştirilsin mi?',
        })]),
      ]);
    }));
    return section(id, 'Ajan ofisleri', [intro, rows]);
  }

  function otherSection(data) {
    const pairs = [['Destek talepleri', data.destek.toplam + (data.destek.acik ? ' (' + data.destek.acik + ' açık)' : '')]];
    if (data.basvuru) {
      pairs.push(['Başvuru paketi', PACKAGES[data.basvuru.paket] || data.basvuru.paket],
        ['Başvuru', day(data.basvuru.olusturma)], ['Onay', day(data.basvuru.karar)]);
    }
    return section('bolum-diger', 'Destek ve başvuru', [facts(pairs)]);
  }

  // ----- Başvurular ---------------------------------------------------------------------------
  function renderApplications() {
    const view = page('Başvurular', {
      lead: 'Bireysel kullanıcılar peyrev.com/#basvuru formundan başvurur ve şifresini kendisi belirler. Ödemeyi '
        + 'gördüğünüzde başvuruyu onaylayın: hesap ve lisans birlikte açılır, kişi Peyrev\'e başvurudaki şifresiyle '
        + 'girer. Onaylanmayan başvurular 90 gün sonra silinir.',
    });
    const tabs = h('div', { class: 'segmented', role: 'group', 'aria-label': 'Başvuru durumu' });
    const list = h('div', { class: 'panel-section' });
    const load = () => fill(list, async (target) => {
      const data = await api(query('basvurular', { durum: state.applications }));
      if (!data.basvurular.length) {
        target.replaceChildren(empty(APPLICATION_EMPTY[state.applications]));
        return;
      }
      target.replaceChildren(h('div', { class: 'cards' }, data.basvurular.map((item) => (
        state.applications === 'bekliyor' ? pendingCard(item, load) : decidedCard(item)))));
    });
    const paint = () => {
      for (const node of tabs.children) node.setAttribute('aria-pressed', String(node.dataset.durum === state.applications));
    };
    for (const [key, label] of APPLICATION_TABS) {
      const tab = button(label, () => {
        state.applications = key;
        paint();
        return load();
      });
      tab.dataset.durum = key;
      tabs.append(tab);
    }
    paint();
    view.body.replaceChildren(tabs, list);
    load();
  }

  function pendingCard(item, reload) {
    const start = input('baslangic', { type: 'date', value: today() });
    const end = input('bitis', { type: 'date', value: yearLater(), required: true });
    const plan = select('paket', Object.entries(PACKAGES), item.paket);
    const note = input('not', { maxlength: 300, placeholder: 'Ör. ödeme açıklaması' });
    const reject = button('Reddet', async () => {
      await api('basvurular/' + item.id + '/reddet', note.value.trim() ? { not: note.value.trim() } : {});
      say('Başvuru reddedildi.', 'ok');
      refreshBadges();
      return reload();
    }, { kind: 'danger', confirm: item.ad_soyad + ' başvurusu reddedilsin mi? Kayıtlı şifre silinir; Not alanı gerekçe olarak saklanır.' });
    const node = form([
      field('Başlangıç', start),
      field('Bitiş', end, { hint: 'Bitiş günü dahildir.' }),
      field('Paket', plan, { hint: 'Peyrev + Ajanlar seçilirse Ajanlar hakkı da açılır.' }),
      field('Not', note, { hint: 'İsteğe bağlı: onayda ödeme açıklaması, retde gerekçe olarak saklanır.' }),
    ], 'Ödeme alındı, hesabı aç', async () => {
      const body = { baslangic: start.value, bitis: end.value, paket: plan.value };
      if (note.value.trim()) body.not = note.value.trim();
      const result = await api('basvurular/' + item.id + '/onayla', body);
      say(result.hesap.eposta + ' hesabı açıldı. Kişi Peyrev\'e başvurudaki şifresiyle girebilir.', 'ok');
      refreshBadges();
      return reload();
    }, {
      extra: [reject],
      confirm: () => item.ad_soyad + ' için ' + PACKAGES[plan.value] + ' hesabı ' + day(start.value || today()) + ' – '
        + day(end.value) + ' dönemiyle açılsın mı?',
    });
    return h('article', { class: 'card' }, [
      h('div', { class: 'card-head' }, [h('h2', { class: 'card-title', text: item.ad_soyad }), pill(PACKAGES[item.paket] || item.paket, 'accent')]),
      facts([['E-posta', mail(item.eposta)], ['Telefon', phone(item.telefon)], ['Başvuru', day(item.olusturma)]]),
      node,
    ]);
  }

  function decidedCard(item) {
    const approved = item.durum === 'onaylandi';
    return h('article', { class: 'card' }, [
      h('div', { class: 'card-head' }, [
        h('h2', { class: 'card-title', text: item.ad_soyad }),
        pill(approved ? 'Onaylandı' : 'Reddedildi', approved ? 'ok' : 'danger'),
      ]),
      facts([['E-posta', mail(item.eposta)], ['Telefon', phone(item.telefon)], ['Paket', PACKAGES[item.paket] || item.paket],
        ['Başvuru', day(item.olusturma)], ['Karar', day(item.karar)], ['Not', item.not]]),
      item.kullanici_id ? h('a', { href: '#kullanici-' + item.kullanici_id, text: 'Hesabı görüntüle' }) : null,
    ]);
  }

  // ----- Destek -------------------------------------------------------------------------------
  function ticketCard(item) {
    return h('article', { class: 'card' }, [
      h('div', { class: 'card-head' }, [
        h('h2', { class: 'card-title' }, [
          item.okunmamis ? h('span', { class: 'dot' }, h('span', { class: 'visually-hidden', text: 'Okunmamış:' })) : null,
          h('a', { href: '#destek-' + item.no, text: '#' + item.no + ' · ' + item.baslik }),
        ]),
        pill(item.durum_adi, TICKET_TONES[item.durum] || ''),
      ]),
      facts([['Kullanıcı', item.kullanici || breakable(item.giris)], ['E-posta', mail(item.eposta)], ['Tür', item.tur_adi],
        ['İlgili adım', item.adim], ['Studio', item.surum + ' · ' + item.platform], ['Yayın', item.yayin_surumu],
        ['Güncelleme', day(item.guncelleme)]]),
    ]);
  }

  function renderTickets() {
    const view = page('Destek talepleri', {
      lead: 'Kullanıcılar öneri, adım isteği, hata bildirimi ve sorularını Peyrev\'deki "Öneri ve destek" düğmesiyle '
        + 'yazar. Yanıtınız kullanıcının uygulamasında görünür; "Yayınlandı" durumunda yazdığınız sürüm Yenilikler '
        + 'penceresinde "Sizin öneriniz" olarak çıkar.',
    });
    const filters = h('form', { class: 'toolbar filters', 'aria-label': 'Süzgeç' });
    filters.addEventListener('submit', (event) => event.preventDefault());
    const list = h('div', { class: 'panel-section' });
    const load = () => fill(list, async (target) => {
      const data = await api(query('destek', state.tickets));
      state.ticketStates = data.durumlar;
      badge($('rozet-destek'), data.okunmamis, 'okunmamış');
      if (!filters.childElementCount) {
        const status = select('durum', [['', 'Tümü']].concat(Object.entries(data.durumlar)), state.tickets.durum);
        const kind = select('tur', [['', 'Tümü']].concat(Object.entries(data.turler)), state.tickets.tur);
        const change = () => {
          state.tickets = { durum: status.value, tur: kind.value };
          load();
        };
        status.addEventListener('change', change);
        kind.addEventListener('change', change);
        filters.append(field('Durum', status), field('Tür', kind));
      }
      if (!data.talepler.length) {
        target.replaceChildren(empty(state.tickets.durum || state.tickets.tur ? 'Bu süzgeçte talep yok.' : 'Henüz destek talebi yok.'));
        return;
      }
      target.replaceChildren(
        h('p', { class: 'muted', role: 'status',
          text: data.talepler.length + ' talep' + (data.okunmamis ? ' · ' + data.okunmamis + ' okunmamış' : '') }),
        h('div', { class: 'cards' }, data.talepler.map(ticketCard)),
      );
    });
    view.body.replaceChildren(filters, list);
    load();
  }

  function message(item) {
    const admin = item.yazan === 'yonetici';
    return h('div', { class: 'message ' + (admin ? 'yonetici' : 'kullanici') },
      [h('span', { class: 'message-meta', text: (admin ? 'Yönetici' : 'Kullanıcı') + ' · ' + day(item.zaman) }), item.metin]);
  }

  function attachment(ticket, item) {
    const url = API + 'destek/' + ticket.no + '/ek/' + encodeURIComponent(item.id);
    const size = Math.max(1, Math.round(item.boyut / 1024)) + ' KB';
    if (item.bicim.indexOf('image/') === 0) {
      return h('figure', null, [
        h('a', { href: url, target: '_blank', rel: 'noopener' }, h('img', { src: url, alt: item.ad, loading: 'lazy' })),
        h('figcaption', { text: item.ad + ' · ' + size }),
      ]);
    }
    const holder = h('div');
    const box = h('details', null, [h('summary', { text: (ATTACHMENT_TITLES[item.tur] || item.ad) + ' · ' + size }), holder]);
    let loaded = false;
    box.addEventListener('toggle', async () => {
      if (!box.open || loaded) return;
      loaded = true;
      holder.replaceChildren(h('p', { class: 'loading', text: 'Yükleniyor…' }));
      try {
        const text = await fetchText(url);
        let pretty = text;
        try {
          pretty = JSON.stringify(JSON.parse(text), null, 2);
        } catch (error) {
          pretty = text;
        }
        holder.replaceChildren(h('pre', { tabindex: '0', text: pretty }),
          h('a', { href: url, download: item.ad, text: 'Dosyayı indir' }));
      } catch (error) {
        loaded = false;
        if (!(error instanceof SessionLost)) holder.replaceChildren(h('p', { class: 'notice notice-error', text: error.message }));
      }
    });
    return box;
  }

  function replyForm(ticket, states, refresh) {
    const text = h('textarea', { name: 'metin', maxlength: 5000, rows: 5 });
    const status = select('durum', Object.entries(states), ticket.durum === 'acik' ? 'yanitlandi' : ticket.durum);
    const release = input('yayin_surumu', { value: ticket.yayin_surumu || '', maxlength: 20, inputmode: 'decimal',
      placeholder: 'Ör. 1.0.1' });
    const releaseField = field('Yayınlandığı sürüm', release,
      { hint: 'Kullanıcının Yenilikler penceresinde bu sürümle birlikte görünür.' });
    const paint = () => {
      releaseField.hidden = status.value !== 'yayinlandi';
    };
    status.addEventListener('change', paint);
    paint();
    const send = async (withText) => {
      const body = { durum: status.value };
      if (withText) {
        if (!text.value.trim()) throw new Error('Yanıt yazın.');
        body.metin = text.value.trim();
      }
      if (status.value === 'yayinlandi') body.yayin_surumu = release.value.trim();
      await api('destek/' + ticket.no + '/yanit', body);
      say('#' + ticket.no + ' güncellendi.', 'ok');
      refresh('bolum-yanit');
    };
    const onlyStatus = button('Yalnız durumu kaydet', () => send(false));
    return form([
      field('Yanıtınız', text, { wide: true, hint: 'Kullanıcının Peyrev\'inde görünür; en çok 5000 karakter.' }),
      field('Durum', status),
      releaseField,
    ], 'Yanıtı gönder', () => send(true), { extra: [onlyStatus] });
  }

  function renderTicket(number) {
    const view = page('Talep #' + number, { back: ['#destek', 'Destek talepleri'] });
    reloadable(view.body, async (target, refresh) => {
      const ticket = (await api('destek/' + number)).talep;
      const states = state.ticketStates || (await api('destek')).durumlar;
      state.ticketStates = states;
      view.retitle('#' + ticket.no + ' · ' + ticket.baslik);
      refreshBadges();
      const files = ticket.ekler.length
        ? section('bolum-ekler', 'Ekler', [h('div', { class: 'attachments' }, ticket.ekler.map((item) => attachment(ticket, item)))])
        : null;
      put(target,
        section('bolum-talep', 'Talep', [
          h('div', { class: 'pills' }, [pill(ticket.durum_adi, TICKET_TONES[ticket.durum] || ''), pill(ticket.tur_adi)]),
          facts([['Kullanıcı', ticket.kullanici || breakable(ticket.giris)], ['E-posta', mail(ticket.eposta)],
            ['Hesap', h('a', { href: '#kullanici-' + ticket.kullanici_id },
              breakable(ticket.giris || '#' + ticket.kullanici_id))],
            ['İlgili adım', ticket.adim], ['Studio', ticket.surum + ' · ' + ticket.platform],
            ['Yayın', ticket.yayin_surumu], ['Açılış', day(ticket.olusturma)], ['Güncelleme', day(ticket.guncelleme)]]),
        ]),
        section('bolum-yazisma', 'Yazışma', [h('div', { class: 'thread' }, ticket.mesajlar.map(message))]),
        files,
        section('bolum-yanit', 'Yanıt ve durum', [replyForm(ticket, states, refresh)]),
      );
    });
  }

  // ----- Olaylar ------------------------------------------------------------------------------
  function targetLink(target) {
    if (!target) return null;
    let match = /^kullanici:(\d+)$/.exec(target);
    if (match) return h('a', { href: '#kullanici-' + match[1], text: 'Hesap #' + match[1] });
    match = /^talep:(\d+)$/.exec(target);
    if (match) return h('a', { href: '#destek-' + match[1], text: 'Talep #' + match[1] });
    match = /^(ajan_ofisi|basvuru):([0-9a-f]{8})[0-9a-f]{24}$/.exec(target);
    if (match) return (match[1] === 'basvuru' ? 'Başvuru ' : 'Ajan ofisi ') + match[2] + '…';
    return target === 'llm_isler' ? 'Ortak model kuyruğu' : target;
  }

  function instant(text) {
    const date = new Date(text);
    return Number.isNaN(date.getTime()) ? text : MOMENT.format(date);
  }

  /** Olay ayrıntısındaki bir değerin okunur hali: tarih Türkiye saatiyle, evet/hayır, bilinen adlar Türkçe. */
  function detailText(key, value) {
    if (value === null || value === undefined) return key === 'bitis' ? 'süresiz' : '—';
    if (typeof value === 'boolean') return value ? 'evet' : 'hayır';
    if (key === 'yanit_uzunlugu' && typeof value === 'number') return value + ' karakter';
    if (Array.isArray(value)) return value.map((item) => detailText(key, item)).join(', ');
    if (typeof value === 'object') {
      return Object.entries(value).map(([name, item]) => (DETAIL_LABELS[name] || name).toLocaleLowerCase('tr') + ' '
        + detailText(name, item)).join(', ');
    }
    const text = String(value);
    if (/^\d{4}-\d{2}-\d{2}T/.test(text)) return instant(text);
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return day(text);
    let names = Object.assign({}, ROLES, ACCOUNT_STATES, TICKET_STATES, state.ticketStates || {});
    if (key === 'alanlar') names = DETAIL_LABELS;
    else if (key === 'modul' || key === 'moduller') names = MODULES;
    else if (key === 'paket') names = PACKAGES;
    return Object.prototype.hasOwnProperty.call(names, text) ? names[text] : text;
  }

  function eventDetails(value) {
    const entries = value && typeof value === 'object' ? Object.entries(value) : [];
    if (!entries.length) return null;
    const rank = (key) => (DETAIL_ORDER.includes(key) ? DETAIL_ORDER.indexOf(key) : DETAIL_ORDER.length);
    entries.sort((first, second) => rank(first[0]) - rank(second[0]));
    return h('dl', { class: 'event-details' }, entries.map(([key, item]) => {
      const text = detailText(key, item);
      return h('div', null, [
        h('dt', { text: (DETAIL_LABELS[key] || key) + ':' }),
        h('dd', { class: /^[0-9a-f]{32}$/.test(text) ? 'mono' : null },
          key === 'kullanici_id' && Number.isInteger(item) ? targetLink('kullanici:' + item) : text),
      ]);
    }));
  }

  function eventCard(item) {
    const actor = item.yonetici ? (item.yonetici.ad_soyad || item.yonetici.giris || '#' + item.yonetici.kullanici_id) : 'Sistem';
    return h('article', { class: 'card event' }, [
      h('div', { class: 'event-head' }, [h('h3', { text: item.tur_adi }), h('span', { class: 'event-time', text: day(item.zaman) })]),
      h('p', { class: 'sub' }, [actor].concat(item.hedef ? [' · ', targetLink(item.hedef)] : [])),
      eventDetails(item.ayrinti),
    ]);
  }

  function renderEvents() {
    const view = page('Olaylar', {
      lead: 'Yönetim işlemlerinin kaydı: kim, ne zaman, neyi değiştirdi. Şifre, kod ve yanıt metni kaydedilmez.',
    });
    const filters = h('form', { class: 'toolbar filters', 'aria-label': 'Süzgeç' });
    filters.addEventListener('submit', (event) => event.preventDefault());
    const list = h('div', { class: 'panel-section' });
    const load = () => fill(list, async (target) => {
      const data = await api(query('olaylar', state.events));
      if (!filters.childElementCount) {
        const kind = select('tur', [['', 'Tümü']].concat(Object.entries(data.turler)), state.events.tur);
        kind.addEventListener('change', () => {
          state.events = { tur: kind.value, sayfa: 1 };
          load();
        });
        filters.append(field('Tür', kind));
      }
      if (!data.olaylar.length) {
        target.replaceChildren(empty(state.events.tur ? 'Bu türde olay yok.' : 'Henüz olay kaydı yok.'));
        return;
      }
      put(target,
        h('p', { class: 'muted', role: 'status', text: data.toplam + ' olay' }),
        h('div', { class: 'cards' }, data.olaylar.map(eventCard)),
        pager(data, (number) => {
          state.events.sayfa = number;
          return load();
        }),
      );
    });
    view.body.replaceChildren(filters, list);
    load();
  }

  // ----- Hesabım ------------------------------------------------------------------------------
  function renderAccount() {
    const view = page('Hesabım');
    reloadable(view.body, async (target, refresh) => {
      const data = await api('ben');
      state.admin = data.yonetici;
      $('yonetici-adi').textContent = data.yonetici.ad_soyad || data.yonetici.giris;
      target.replaceChildren(
        section('bolum-profil', 'Hesap', [
          facts([['Ad soyad', data.yonetici.ad_soyad], ['Giriş adı', breakable(data.yonetici.giris)],
            ['Bu oturumun açılışı', day(data.oturum.acilis)], ['En geç bitiş', day(data.oturum.bitis)]]),
        ]),
        passwordSection(data.yonetici),
        twoFactorSection(data.yonetici, refresh),
        sessionsSection(data, refresh),
      );
    });
  }

  function passwordSection(admin) {
    const current = input('sifre', { type: 'password', autocomplete: 'current-password', maxlength: 256, required: true });
    const fresh = input('yeni_sifre', { type: 'password', autocomplete: 'new-password', maxlength: 128, required: true });
    const repeat = input('tekrar', { type: 'password', autocomplete: 'new-password', maxlength: 128, required: true });
    return section('bolum-sifre', 'Şifre', [
      h('p', { class: 'muted', text: 'Şifreniz Peyrev uygulamasıyla ortaktır. Şifre değişince açık Peyrev oturumlarınız ve bütün panel oturumlarınız kapanır; panele yeni şifrenizle yeniden girersiniz.' }),
      form([
        field('Mevcut şifre', current),
        field('Yeni şifre', fresh, { hint: '8-128 karakter; en az bir harf ve bir rakam.' }),
        field('Yeni şifre (tekrar)', repeat),
      ], 'Şifreyi değiştir', async () => {
        if (!current.value) throw new Error('Mevcut şifrenizi yazın.');
        const problem = passwordProblem(fresh.value, repeat.value);
        if (problem) throw new Error(problem);
        await accountApi('sifre', { kullanici: admin.giris, sifre: current.value, yeni_sifre: fresh.value });
        showLogin('Şifreniz değiştirildi. Yeni şifrenizle giriş yapın.', 'ok');
      }, { confirm: 'Şifreniz değiştirilsin mi? Panelden ve Peyrev uygulamasından çıkış yapılır.' }),
    ]);
  }

  function twoFactorSection(admin, refresh) {
    const id = 'bolum-iki-adim';
    const password = input('sifre', { type: 'password', autocomplete: 'current-password', maxlength: 256, required: true });
    if (admin.iki_adim) {
      const code = input('kod', { inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: 8, required: true });
      return section(id, 'İki adımlı doğrulama', [
        h('div', { class: 'pills' }, [pill('Açık', 'ok')]),
        h('p', { class: 'muted', text: 'Girişte şifrenizle birlikte doğrulama uygulamanızdaki 6 haneli kod istenir. Kapatmak için şifrenizi ve güncel kodu yazın.' }),
        form([field('Şifre', password), field('Doğrulama kodu', code)], 'İki adımlı doğrulamayı kapat', async () => {
          await api('iki-adim/kapat', { sifre: password.value, kod: code.value.trim() });
          say('İki adımlı doğrulama kapatıldı.', 'ok');
          refresh(id);
        }, { kind: 'danger', confirm: 'İki adımlı doğrulama kapatılsın mı? Girişte yalnız şifre istenir.' }),
      ]);
    }
    const setup = h('div');
    const start = form([field('Şifre', password, { hint: 'Kurulumu başlatmak için şifrenizi yeniden yazın.' })],
      'Kurulumu başlat', async () => {
        const data = await api('iki-adim/baslat', { sifre: password.value });
        password.value = '';
        start.hidden = true;
        setup.replaceChildren(setupSteps(data, refresh, id, () => {
          setup.replaceChildren();
          start.hidden = false;
        }));
        setup.querySelector('h3').focus();
      });
    return section(id, 'İki adımlı doğrulama', [
      h('div', { class: 'pills' }, [pill('Kapalı', 'warn')]),
      h('p', { class: 'muted', text: 'Açtığınızda panele girişte şifrenizle birlikte telefonunuzdaki doğrulama uygulamasının (ör. Google Authenticator, Microsoft Authenticator) ürettiği 6 haneli kod istenir.' }),
      start,
      setup,
    ]);
  }

  function setupSteps(data, refresh, id, cancel) {
    const code = input('kod', { inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: 8, required: true });
    const link = /^otpauth:\/\//.test(data.baglanti) ? h('a', { href: data.baglanti, text: 'bu bağlantıyla açın' }) : null;
    return h('div', { class: 'code-box' }, [
      h('h3', { tabindex: '-1', text: 'Doğrulama uygulamanıza ekleyin' }),
      h('ol', { class: 'steps' }, [
        h('li', null, ['Doğrulama uygulamasında yeni hesap ekleyin ve aşağıdaki anahtarı elle yazın'].concat(
          link ? [' ya da uygulama bu cihazdaysa ', link] : []).concat(['.'])),
        h('li', { text: 'Uygulamanın gösterdiği 6 haneli kodu yazıp onaylayın.' }),
      ]),
      h('p', { class: 'code', text: data.gizli }),
      h('p', { class: 'muted', text: 'Anahtar yalnız şimdi gösterilir ve ' + day(data.gecerlilik) + ' saatine kadar onay bekler. Kimseyle paylaşmayın.' }),
      h('div', { class: 'actions' }, [button('Anahtarı kopyala', () => copy(data.gizli.replace(/\s/g, ''), 'Anahtar'))]),
      form([field('Uygulamadaki kod', code)], 'Kodu doğrula ve aç', async () => {
        const result = await api('iki-adim/onayla', { kod: code.value.trim() });
        say('İki adımlı doğrulama açıldı.' + (result.kapatilan ? ' Diğer ' + result.kapatilan + ' panel oturumunuz kapatıldı.' : ''), 'ok');
        refresh(id);
      }, { extra: [button('Vazgeç', cancel)] }),
    ]);
  }

  function sessionsSection(data, refresh) {
    const id = 'bolum-oturumlar';
    const others = data.oturumlar.filter((item) => !item.bu_oturum).length;
    const rows = h('ul', { class: 'rows' }, data.oturumlar.map((item) => h('li', { class: 'row' }, [facts([
      ['Oturum', item.bu_oturum ? 'Bu oturum' : 'Başka oturum'], ['Giriş', day(item.acilis)],
      ['Son işlem', day(item.son_islem)], ['En geç bitiş', day(item.bitis)], ['IP adresi', item.ip], ['Tarayıcı', item.tarayici],
    ])])));
    const close = others ? button('Diğer oturumları kapat', async () => {
      const result = await api('oturumlar/digerlerini-kapat', {});
      say(result.kapatilan + ' oturum kapatıldı.', 'ok');
      refresh(id);
    }, { kind: 'danger', confirm: 'Bu oturum dışındaki panel oturumlarınız kapatılsın mı?' }) : null;
    return section(id, 'Panel oturumlarınız', [
      h('p', { class: 'muted', text: 'Oturum girişten 12 saat sonra ya da ' + data.oturum.bosta_dakika + ' dakika işlem yapılmazsa kapanır.' }),
      rows,
      close ? h('div', { class: 'actions' }, [close]) : null,
    ]);
  }

  // ----- giriş, şifre kodu, çıkış ve açılış --------------------------------------------------
  function notice(node, text, tone) {
    node.hidden = !text;
    node.textContent = text || '';
    node.className = 'notice ' + (tone === 'ok' ? 'notice-ok' : 'notice-error');
  }

  function loginMessage(text, tone) {
    notice(loginStatus, text, tone);
  }

  /** Giriş formunu gösterir; şifre kodu formu kapanır. */
  function showLoginForm(text, tone) {
    resetForm.hidden = true;
    resetForm.reset();
    notice(resetStatus, '');
    loginForm.hidden = false;
    loginMessage(text, tone);
    loginForm.elements.giris.focus();
  }

  function showLogin(text, tone) {
    state.admin = null;
    content.replaceChildren();
    hideToast();
    views.boot.hidden = true;
    views.app.hidden = true;
    views.login.hidden = false;
    document.title = 'Giriş · ' + TITLE;
    showLoginForm(text, tone);
  }

  function enter(admin) {
    state.admin = admin;
    $('yonetici-adi').textContent = admin.ad_soyad || admin.giris;
    views.boot.hidden = true;
    views.login.hidden = true;
    views.app.hidden = false;
    refreshBadges();
    route();
  }

  loginForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const fields = loginForm.elements;
    const submit = loginForm.querySelector('button[type="submit"]');
    const body = { giris: fields.giris.value.trim(), sifre: fields.sifre.value };
    if (!codeField.hidden && fields.kod.value.trim()) body.kod = fields.kod.value.trim();
    if (!body.giris || !body.sifre) {
      loginMessage('Kullanıcı adınızı ve şifrenizi yazın.');
      return;
    }
    submit.disabled = true;
    loginMessage('');
    try {
      const result = await api('giris', body);
      loginForm.reset();
      codeField.hidden = true;
      enter(result.yonetici);
    } catch (error) {
      if (error.kod === 'KOD_GEREKLI' || error.kod === 'KOD_HATALI') {
        codeField.hidden = false;
        fields.kod.value = '';
        fields.kod.focus();
      }
      loginMessage(error.message);
    } finally {
      submit.disabled = false;
    }
  });

  $('kod-ac').addEventListener('click', () => {
    loginForm.hidden = true;
    loginMessage('');
    resetForm.hidden = false;
    resetForm.elements.kullanici.value = loginForm.elements.giris.value.trim();
    document.title = 'Şifre kodu · ' + TITLE;
    $('sifre-kodu-baslik').focus();
  });

  $('kod-kapat').addEventListener('click', () => {
    document.title = 'Giriş · ' + TITLE;
    showLoginForm('');
  });

  resetForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const fields = resetForm.elements;
    const submit = resetForm.querySelector('button[type="submit"]');
    const login = fields.kullanici.value.trim();
    const code = fields.kod.value.trim();
    let problem = '';
    if (!login) problem = 'Kullanıcı adınızı veya e-posta adresinizi yazın.';
    else if (code.length < 8) problem = 'Şifre kodunu yazın.';
    else problem = passwordProblem(fields.yeni_sifre.value, fields.tekrar.value);
    if (problem) {
      notice(resetStatus, problem);
      return;
    }
    submit.disabled = true;
    notice(resetStatus, '');
    try {
      const result = await accountApi('sifre-yenile', { kullanici: login, kod: code, yeni_sifre: fields.yeni_sifre.value });
      document.title = 'Giriş · ' + TITLE;
      showLoginForm(result.message, 'ok');
      loginForm.elements.giris.value = login;
      loginForm.elements.sifre.focus();
    } catch (error) {
      notice(resetStatus, error.message);
    } finally {
      submit.disabled = false;
    }
  });

  $('cikis').addEventListener('click', (event) => {
    const control = event.currentTarget;
    run(control, async () => {
      await api('cikis', {});
      showLogin('Oturumunuz kapatıldı.', 'ok');
    });
  });

  for (const link of document.querySelectorAll('.sections a')) {
    // Choosing the open section again reloads it.
    link.addEventListener('click', () => {
      if (link.getAttribute('href') === location.hash) route();
    });
  }

  toast.close.addEventListener('click', hideToast);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && toast.box.classList.contains('is-open')) hideToast();
  });
  window.addEventListener('hashchange', route);

  async function boot() {
    try {
      enter((await api('ben')).yonetici);
    } catch (error) {
      if (error instanceof SessionLost) return;
      views.boot.replaceChildren(error.message + ' ', button('Tekrar dene', boot));
    }
  }

  boot();
})();
