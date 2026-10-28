/* peyrev.com/topluluk/: aboneliği devam eden üyelerin konuları, yorumları ve paylaştığı akışlar (/api/topluluk/*).
 *
 * Derleme adımı ve harici betik yok. Sunucudan gelen her metin DOM'a yalnız textContent ya da metin düğümü olarak
 * yazılır; HTML olarak yorumlanan hiçbir yazım yoktur. Oturum HttpOnly çerezdedir (Path=/api/topluluk), betik
 * belirteci görmez. Durum değiştiren istekler X-Peyrev-Topluluk başlığını taşır (sunucu Origin'i de denetler).
 * Bölümler adres çubuğundaki çapadır: (boş) liste, #konu-<id>, #yeni.
 */
(function () {
  'use strict';

  const API = '/api/topluluk/';
  const ROOT = document.getElementById('topluluk');
  if (!ROOT) return;
  const UNREACHABLE = 'Sunucuya ulaşılamadı. İnternet bağlantınızı kontrol edip tekrar deneyin.';
  const FLOW_LIMIT = 1024 * 1024;
  const state = { member: null, categories: {}, filter: '', search: '', page: 1 };

  function el(tag, props, children) {
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

  function day(stamp) {
    const [date, time] = String(stamp || '').split(' ');
    const parts = date.split('-');
    return parts.length === 3 ? parts[2] + '.' + parts[1] + '.' + parts[0] + (time ? ' ' + time : '') : String(stamp || '');
  }

  class Refused extends Error {}

  async function api(path, body) {
    const init = { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { Accept: 'application/json' } };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.headers['X-Peyrev-Topluluk'] = '1';
      init.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(API + path, init);
    } catch (error) {
      throw new Error(UNREACHABLE);
    }
    let data = {};
    try {
      data = (await response.json()) || {};
    } catch (error) {
      data = {};
    }
    if (response.ok && data.success === true) return data;
    if (response.status === 401 || data.kod === 'ABONELIK_YOK') {
      state.member = null;
      showLogin(data.kod === 'ABONELIK_YOK' || data.kod === 'OTURUM_GECERSIZ' ? data.message : '');
      throw new Refused(data.message || '');
    }
    throw new Error(data.message || 'İşlem tamamlanamadı. Daha sonra tekrar deneyin.');
  }

  function notice(text, tone) {
    return el('p', { class: 'form-status' + (tone ? ' is-' + tone : ''), role: tone === 'error' ? 'alert' : 'status',
      text });
  }

  async function guarded(button, work) {
    button.disabled = true;
    try {
      await work();
    } catch (error) {
      if (!(error instanceof Refused)) {
        const box = button.closest('form, .community-card') || ROOT;
        box.querySelectorAll('.form-status.is-error').forEach((node) => node.remove());
        box.append(notice(error.message, 'error'));
      }
    } finally {
      button.disabled = false;
    }
  }

  // ----- giriş ----------------------------------------------------------------------------------
  function showLogin(message) {
    const login = el('input', { id: 't-giris', name: 'kullanici', autocomplete: 'username', autocapitalize: 'none',
      spellcheck: 'false', maxlength: 150, required: true });
    const password = el('input', { id: 't-sifre', name: 'sifre', type: 'password', autocomplete: 'current-password',
      maxlength: 256, required: true });
    const submit = el('button', { class: 'button button-gold', type: 'submit', text: 'Giriş yap' });
    const form = el('form', { class: 'card community-login', novalidate: true, 'aria-labelledby': 't-giris-baslik' }, [
      el('h2', { id: 't-giris-baslik', text: 'Üye girişi' }),
      el('p', { text: 'Peyrev uygulamasına girdiğiniz kullanıcı adı ve şifreyle girin.' }),
      el('label', { class: 'field', for: 't-giris' }, ['Kullanıcı adı veya e-posta', login]),
      el('label', { class: 'field', for: 't-sifre' }, ['Şifre', password]),
      message ? notice(message, 'error') : null,
      submit,
    ]);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      guarded(submit, async () => {
        if (!login.value.trim() || !password.value) throw new Error('Kullanıcı adınızı ve şifrenizi yazın.');
        const data = await api('giris', { kullanici: login.value.trim(), sifre: password.value });
        state.member = data.uye;
        await boot();
      });
    });
    const side = el('aside', { class: 'card account community-about' }, [
      el('h2', { text: 'Toplulukta neler var?' }),
      el('ul', { class: 'checks' }, [
        el('li', { text: 'Peyrev ekibinin duyuruları ve belgeleri' }),
        el('li', { text: 'Soru ve yardım: diğer üyeler ve ekip yanıtlar' }),
        el('li', { text: 'Akış paylaşımı: Studio\'da dışa aktardığınız akışı paylaşın, başkalarınınkini içe aktarın' }),
        el('li', { text: 'Öneri ve fikirler' }),
      ]),
      el('p', { text: 'Topluluk, aboneliği devam eden Peyrev hesaplarına açıktır.' }),
      el('a', { class: 'button button-outline', href: '/#basvuru', text: 'Hesap başvurusu' }),
    ]);
    ROOT.replaceChildren(el('div', { class: 'community-gate' }, [form, side]));
    login.focus();
  }

  // ----- liste ----------------------------------------------------------------------------------
  function topbar() {
    const out = el('button', { class: 'button button-outline button-small', type: 'button', text: 'Çıkış' });
    out.addEventListener('click', () => guarded(out, async () => {
      await api('cikis', {});
      state.member = null;
      showLogin('');
    }));
    return el('div', { class: 'community-bar' }, [
      el('p', null, ['Merhaba, ', el('strong', { text: state.member.ad }),
        state.member.yonetici ? el('span', { class: 'tag tag-team', text: 'Peyrev ekibi' }) : null]),
      el('div', { class: 'community-bar-actions' }, [
        el('a', { class: 'button button-gold button-small', href: '#yeni', text: 'Yeni konu' }), out]),
    ]);
  }

  function badge(text, kind) {
    return el('span', { class: 'chip' + (kind ? ' chip-' + kind : ''), text });
  }

  function topicCard(item) {
    return el('li', { class: 'community-card' + (item.sabit ? ' is-pinned' : '') }, [
      el('div', { class: 'community-card-head' }, [
        badge(state.categories[item.kategori] || item.kategori, item.kategori),
        item.sabit ? badge('Sabit', 'pin') : null,
        item.kilitli ? badge('Yoruma kapalı') : null,
        item.akis ? badge('Akış ekli', 'flow') : null,
      ]),
      el('h3', null, el('a', { href: '#konu-' + item.id, text: item.baslik })),
      el('p', { class: 'community-excerpt', text: item.ozet }),
      el('p', { class: 'community-meta' }, [author(item.yazar), ' · ' + day(item.olusturma) + ' · ' + item.yanit_sayisi
        + ' yanıt · ' + item.goruntulenme + ' görüntülenme']),
    ]);
  }

  function author(person) {
    return el('span', { class: 'community-author' }, [person.ad,
      person.yonetici ? el('span', { class: 'tag tag-team', text: 'Peyrev ekibi' }) : null]);
  }

  async function showList() {
    const search = el('input', { type: 'search', name: 'ara', value: state.search, maxlength: 100,
      placeholder: 'Konularda ara', 'aria-label': 'Konularda ara' });
    const searchForm = el('form', { class: 'community-search', role: 'search' }, [search,
      el('button', { class: 'button button-outline button-small', type: 'submit', text: 'Ara' })]);
    searchForm.addEventListener('submit', (event) => {
      event.preventDefault();
      state.search = search.value.trim();
      state.page = 1;
      showList();
    });
    const filters = el('div', { class: 'filters', role: 'group', 'aria-label': 'Kategori' },
      [['', 'Tümü']].concat(Object.entries(state.categories)).map(([key, label]) => {
        const chip = el('button', { class: 'filter', type: 'button', text: label, 'aria-pressed': String(state.filter === key) });
        chip.addEventListener('click', () => {
          state.filter = key;
          state.page = 1;
          showList();
        });
        return chip;
      }));
    const list = el('div', { class: 'community-list' }, notice('Konular yükleniyor…'));
    ROOT.replaceChildren(topbar(), el('div', { class: 'community-tools' }, [filters, searchForm]), list);
    document.title = 'Topluluk · Peyrev';
    let data;
    try {
      const params = new URLSearchParams();
      if (state.filter) params.set('kategori', state.filter);
      if (state.search) params.set('ara', state.search);
      if (state.page > 1) params.set('sayfa', String(state.page));
      data = await api('konular' + (params.toString() ? '?' + params : ''));
    } catch (error) {
      if (!(error instanceof Refused)) list.replaceChildren(notice(error.message, 'error'));
      return;
    }
    if (!data.konular.length) {
      list.replaceChildren(el('div', { class: 'notice' }, [el('strong', { text: state.search || state.filter
        ? 'Bu aramayla eşleşen konu yok.' : 'Henüz konu yok.' }),
        el('span', { text: 'İlk konuyu siz açın: "Yeni konu" ile soru sorun ya da bir akış paylaşın.' })]));
      return;
    }
    const pages = Math.max(1, Math.ceil(data.toplam / data.sayfa_boyu));
    const pager = pages > 1 ? el('nav', { class: 'community-pager', 'aria-label': 'Sayfalar' }, [
      pageButton('← Önceki', state.page - 1, state.page <= 1), el('span', { text: 'Sayfa ' + state.page + ' / ' + pages }),
      pageButton('Sonraki →', state.page + 1, state.page >= pages)]) : null;
    list.replaceChildren(el('ul', { class: 'community-topics' }, data.konular.map(topicCard)), pager || '');
  }

  function pageButton(label, target, disabled) {
    const button = el('button', { class: 'button button-outline button-small', type: 'button', text: label, disabled });
    button.addEventListener('click', () => {
      state.page = target;
      showList();
      window.scrollTo(0, ROOT.offsetTop - 80);
    });
    return button;
  }

  // ----- konu ----------------------------------------------------------------------------------
  function flowCard(topicId, flow) {
    const risky = flow.adimlar.filter((step) => step.dikkat);
    const url = API + 'konular/' + topicId + '/akis';
    return el('section', { class: 'community-flow', 'aria-label': 'Ekli akış' }, [
      el('div', { class: 'community-flow-head' }, [
        el('div', null, [el('p', { class: 'kicker', text: 'Ekli akış' }), el('h2', { text: flow.ad }),
          el('p', { class: 'aside', text: flow.adim_sayisi + ' adım · ' + Math.max(1, Math.round(flow.boyut / 1024))
            + ' KB · ' + flow.indirme + ' kez indirildi' })]),
        el('a', { class: 'button button-gold', href: url, download: '', text: 'Akışı indir' }),
      ]),
      el('p', { class: 'aside', text: 'Peyrev\'de Akışlarım → İçe aktar ile açın. İçe aktarmadan önce adımları inceleyin; '
        + 'bağlantı ve şifreleri kendi Peyrev\'inizde siz seçersiniz.' }),
      risky.length ? el('p', { class: 'form-status is-error', text: 'Dikkat: bu akışta komut çalıştırma, dosya, '
        + 'veritabanı ya da dışarıya gönderim yapan ' + risky.length + ' adım var; içe aktarmadan önce ne yaptıklarına bakın.' }) : null,
      el('details', null, [el('summary', { text: 'Adımları göster' }),
        el('ol', { class: 'community-steps' }, flow.adimlar.map((step) => el('li', {
          class: step.dikkat ? 'is-risky' : null, 'data-derinlik': String(Math.min(step.derinlik, 6)),
        }, [step.baslik || step.tur, ' ', el('code', { text: step.tur })])))]),
    ]);
  }

  function deleteButton(label, question, work) {
    const button = el('button', { class: 'button button-quiet-dark button-small', type: 'button', text: label });
    button.addEventListener('click', () => {
      if (!window.confirm(question)) return;
      guarded(button, work);
    });
    return button;
  }

  async function showTopic(topicId) {
    ROOT.replaceChildren(topbar(), notice('Konu yükleniyor…'));
    let data;
    try {
      data = await api('konular/' + topicId);
    } catch (error) {
      if (!(error instanceof Refused)) {
        ROOT.replaceChildren(topbar(), el('a', { class: 'community-back', href: '#', text: '← Konular' }),
          notice(error.message, 'error'));
      }
      return;
    }
    const topic = data.konu;
    document.title = topic.baslik + ' · Topluluk · Peyrev';
    const replies = el('div', { class: 'community-replies' });
    const paint = (items) => replies.replaceChildren(...(items.length ? items.map((item) => el('article', {
      class: 'community-reply' }, [
      el('p', { class: 'community-meta' }, [author(item.yazar), ' · ' + day(item.olusturma)]),
      el('p', { class: 'community-text', text: item.metin }),
      item.benim ? deleteButton('Sil', 'Yorumunuz silinsin mi?', async () => {
        await api('yorumlar/' + item.id + '/sil', {});
        showTopic(topicId);
      }) : null,
    ])) : [el('p', { class: 'aside', text: 'Henüz yanıt yok. İlk yanıtı siz yazın.' })]));
    paint(data.yorumlar);
    let reply;
    if (topic.kilitli) {
      reply = el('p', { class: 'notice', text: 'Bu konu yorumlara kapalı.' });
    } else {
      const text = el('textarea', { id: 't-yorum', name: 'metin', maxlength: 5000, rows: 4, required: true });
      const submit = el('button', { class: 'button button-gold', type: 'submit', text: 'Yanıtla' });
      reply = el('form', { class: 'card community-form', novalidate: true }, [
        el('label', { class: 'field', for: 't-yorum' }, ['Yanıtınız', text]), submit]);
      reply.addEventListener('submit', (event) => {
        event.preventDefault();
        guarded(submit, async () => {
          if (!text.value.trim()) throw new Error('Yanıtınızı yazın.');
          const result = await api('konular/' + topicId + '/yorumlar', { metin: text.value });
          text.value = '';
          paint(result.yorumlar);
        });
      });
    }
    ROOT.replaceChildren(topbar(), el('a', { class: 'community-back', href: '#', text: '← Konular' }),
      el('article', { class: 'card community-topic' }, [
        el('div', { class: 'community-card-head' }, [
          badge(state.categories[topic.kategori] || topic.kategori, topic.kategori),
          topic.sabit ? badge('Sabit', 'pin') : null, topic.kilitli ? badge('Yoruma kapalı') : null]),
        el('h2', { text: topic.baslik }),
        el('p', { class: 'community-meta' }, [author(topic.yazar), ' · ' + day(topic.olusturma) + ' · '
          + topic.goruntulenme + ' görüntülenme']),
        el('p', { class: 'community-text', text: topic.metin }),
        topic.akis ? flowCard(topic.id, topic.akis) : null,
        topic.benim ? deleteButton('Konuyu sil', 'Konunuz yanıtları ve akışıyla birlikte silinsin mi?', async () => {
          await api('konular/' + topicId + '/sil', {});
          location.hash = '';
        }) : null,
      ]),
      el('h2', { class: 'community-subtitle', text: 'Yanıtlar' }), replies, reply);
  }

  // ----- yeni konu ------------------------------------------------------------------------------
  function showNew() {
    document.title = 'Yeni konu · Topluluk · Peyrev';
    const kinds = Object.entries(state.categories).filter(([key]) => key !== 'duyuru' || state.member.yonetici);
    const kind = el('select', { id: 't-kategori', name: 'kategori' }, kinds.map(([key, label]) =>
      el('option', { value: key, text: label })));
    const title = el('input', { id: 't-baslik', name: 'baslik', maxlength: 150, required: true });
    const body = el('textarea', { id: 't-metin', name: 'metin', maxlength: 10000, rows: 8, required: true });
    const file = el('input', { id: 't-akis', name: 'akis', type: 'file', accept: '.json,application/json' });
    const fileState = el('p', { class: 'hint', text: 'İsteğe bağlı. Peyrev\'de akışı açıp "Dışa aktar" ile aldığınız '
      + '.json dosyası (en çok 1 MB). Paylaşmadan önce akışta kişisel ya da müşteriye ait bilgi olmadığından emin olun.' });
    let flow = null;
    file.addEventListener('change', async () => {
      flow = null;
      const chosen = file.files && file.files[0];
      if (!chosen) return;
      try {
        if (chosen.size > FLOW_LIMIT) throw new Error('Akış dosyası en çok 1 MB olabilir.');
        const parsed = JSON.parse(await chosen.text());
        if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.steps) || typeof parsed.name !== 'string') {
          throw new Error('Bu dosya bir Peyrev akışı değil. Akışlarım\'da akışı açıp "Dışa aktar" ile alın.');
        }
        flow = parsed;
        fileState.textContent = '"' + parsed.name + '" eklenecek.';
        if (kind.querySelector('option[value="akis"]')) kind.value = 'akis';
      } catch (error) {
        file.value = '';
        fileState.textContent = error instanceof SyntaxError ? 'Dosya okunamadı: geçerli bir JSON dosyası değil.' : error.message;
      }
    });
    const submit = el('button', { class: 'button button-gold', type: 'submit', text: 'Konuyu yayınla' });
    const form = el('form', { class: 'card community-form', novalidate: true, 'aria-labelledby': 't-yeni-baslik' }, [
      el('h2', { id: 't-yeni-baslik', text: 'Yeni konu' }),
      el('label', { class: 'field', for: 't-kategori' }, ['Kategori', kind]),
      el('label', { class: 'field', for: 't-baslik' }, ['Başlık', title]),
      el('label', { class: 'field', for: 't-metin' }, ['Metin', body]),
      el('label', { class: 'field', for: 't-akis' }, ['Akış dosyası', file, fileState]),
      el('p', { class: 'aside', text: 'Konunuz hemen yayınlanır ve abonelerin tamamı görür. Adınız ad soyadınızla görünür; '
        + 'e-posta adresiniz gösterilmez.' }),
      submit,
    ]);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      guarded(submit, async () => {
        if (title.value.trim().length < 5) throw new Error('Başlık en az 5 karakter olmalıdır.');
        if (!body.value.trim()) throw new Error('Metni yazın.');
        const payload = { kategori: kind.value, baslik: title.value.trim(), metin: body.value };
        if (flow) payload.akis = flow;
        const result = await api('konular', payload);
        location.hash = '#konu-' + result.konu.id;
      });
    });
    ROOT.replaceChildren(topbar(), el('a', { class: 'community-back', href: '#', text: '← Konular' }), form);
    title.focus();
  }

  // ----- yönlendirme ----------------------------------------------------------------------------
  function route() {
    if (!state.member) return;
    const hash = location.hash.slice(1);
    const match = /^konu-(\d{1,18})$/.exec(hash);
    if (match) showTopic(match[1]);
    else if (hash === 'yeni') showNew();
    else showList();
  }

  async function boot() {
    try {
      const data = await api('ben');
      state.member = data.uye;
      state.categories = data.kategoriler;
      route();
    } catch (error) {
      if (!(error instanceof Refused)) ROOT.replaceChildren(notice(error.message, 'error'));
    }
  }

  window.addEventListener('hashchange', route);
  boot();
})();
