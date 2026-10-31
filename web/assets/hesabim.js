/* peyrev.com/hesabim/: Peyrev hesabıyla giriş; lisans durumu, kişisel indirme bağlantısı, lisans uzatma isteği, şifre
 * değiştirme, açık Peyrev oturumları ve Robotlarım (/api/uye/*; şifre Studio'yla ortak /api/rpa/hesap/sifre ucuyla
 * değişir). Robotlarım yalnız hesabın kendi bilgisayarlarını gösterir; Durdur ve Yeniden çalıştır komut yazar, komut
 * robotun bir sonraki durum bildirimiyle gider. Dijital İkiz hakkı sürerken "Dijital İkiz'i aç" /api/uye/ikiz'e gider;
 * sunucu oradan /ikiz/'e (hak yoksa ?ikiz=yok, oturum yoksa ?ikiz=giris ile buraya) yönlendirir.
 *
 * Derleme adımı ve harici betik yok. Sunucudan gelen her metin DOM'a yalnız textContent ya da metin düğümü olarak
 * yazılır. Oturum HttpOnly çerezdedir (Path=/api); durum değiştiren istekler X-Peyrev-Uye başlığını taşır.
 */
(function () {
  'use strict';

  const API = '/api/';
  const ROOT = document.getElementById('hesabim');
  if (!ROOT) return;
  const UNREACHABLE = 'Sunucuya ulaşılamadı. İnternet bağlantınızı kontrol edip tekrar deneyin.';
  const PACKAGES = [['rpa', 'Peyrev', '10 $ / ay + KDV'], ['rpa_ajan', 'Peyrev + Ajanlar', '20 $ / ay + KDV'],
    ['ikiz', 'Peyrev Dijital İkiz', '100 $ / ay + KDV']];
  const TWIN = '/api/uye/ikiz';
  // Set by the server's redirect from /api/uye/ikiz: "yok" = no Dijital İkiz right, "giris" = sign in first.
  const TWIN_STATE = new URLSearchParams(window.location.search).get('ikiz');
  const STATES = { aktif: 'Geçerli', sure_doldu: 'Süresi doldu', yetki_yok: 'Hakkı yok' };

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

  function day(value) {
    const [date, time] = String(value || '').split(/[ T]/);
    const parts = date.split('-');
    return parts.length === 3 ? parts[2] + '.' + parts[1] + '.' + parts[0] + (time ? ' ' + time.slice(0, 5) : '') : '';
  }

  class Refused extends Error {}

  async function api(path, body, options) {
    const settings = options || {};
    const init = { method: body === undefined ? 'GET' : 'POST', cache: 'no-store',
      credentials: settings.anonymous ? 'omit' : 'same-origin', headers: { Accept: 'application/json' } };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      if (!settings.anonymous) init.headers['X-Peyrev-Uye'] = '1';
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
    if (response.status === 401 && !settings.anonymous) {
      showLogin(data.kod === 'OTURUM_GECERSIZ' ? data.message : '');
      throw new Refused(data.message || '');
    }
    throw new Error(data.message || 'İşlem tamamlanamadı. Daha sonra tekrar deneyin.');
  }

  function notice(text, tone) {
    return el('p', { class: 'form-status' + (tone ? ' is-' + tone : ''), role: tone === 'error' ? 'alert' : 'status', text });
  }

  async function guarded(button, work) {
    const box = button.closest('form, .card') || ROOT;
    box.querySelectorAll('.form-status').forEach((node) => node.remove());
    button.disabled = true;
    try {
      await work(box);
    } catch (error) {
      if (!(error instanceof Refused)) box.append(notice(error.message, 'error'));
    } finally {
      button.disabled = false;
    }
  }

  function field(label, control, hint) {
    return el('label', { class: 'field', for: control.id }, [label, control, hint ? el('span', { class: 'hint', text: hint }) : null]);
  }

  // ----- giriş ----------------------------------------------------------------------------------
  function showLogin(message) {
    const login = el('input', { id: 'h-giris', autocomplete: 'username', autocapitalize: 'none', spellcheck: 'false',
      maxlength: 150, required: true });
    const password = el('input', { id: 'h-sifre', type: 'password', autocomplete: 'current-password', maxlength: 256,
      required: true });
    const submit = el('button', { class: 'button button-gold', type: 'submit', text: 'Giriş yap' });
    const form = el('form', { class: 'card community-login', novalidate: true, 'aria-labelledby': 'h-giris-baslik' }, [
      el('h2', { id: 'h-giris-baslik', text: 'Giriş' }),
      TWIN_STATE === 'giris' ? el('p', { class: 'form-status', role: 'status',
        text: 'Dijital İkiz\'i açmak için Peyrev hesabınızla giriş yapın.' }) : null,
      el('p', { text: 'Peyrev uygulamasına girdiğiniz kullanıcı adı ve şifreyle girin. Lisansınızın süresi dolmuş olsa da girip uzatma isteyebilirsiniz.' }),
      field('Kullanıcı adı veya e-posta', login), field('Şifre', password),
      message ? notice(message, 'error') : null, submit,
    ]);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      guarded(submit, async () => {
        if (!login.value.trim() || !password.value) throw new Error('Kullanıcı adınızı ve şifrenizi yazın.');
        const signed = await api('uye/giris', { kullanici: login.value.trim(), sifre: password.value });
        if (TWIN_STATE === 'giris' && signed.uye) {
          // Came from the Dijital İkiz sign-in: go on to it; the server checks the right and sends back if there is none.
          window.location.assign(TWIN);
          return;
        }
        await boot();
      });
    });
    ROOT.replaceChildren(el('div', { class: 'community-gate' }, [form, el('aside', { class: 'card account community-about' }, [
      el('h2', { text: 'Hesabınız yok mu?' }),
      el('p', { text: 'Başvuru formunu doldurun; ödemeniz onaylanınca hesabınız açılır ve indirme bağlantınız e-postanıza gelir.' }),
      el('a', { class: 'button button-outline', href: '/#basvuru', text: 'Hesap başvurusu' }),
      el('p', { class: 'aside', text: 'Şifrenizi unuttuysanız info@peyrev.com adresine yazın; size tek kullanımlık şifre kodu gönderelim. Kodu Peyrev\'in giriş ekranındaki "Şifre kodum var" ile kullanırsınız.' }),
    ])]));
    login.focus();
  }

  // ----- hesap ----------------------------------------------------------------------------------
  function licenseRow(label, right) {
    const until = right.suresiz ? 'Süresiz' : (right.bitis ? day(right.bitis) + ' tarihine kadar' : '—');
    return el('li', { class: 'account-right' + (right.durum === 'aktif' ? ' is-ok' : '') }, [
      el('strong', { text: label }), el('span', { text: (STATES[right.durum] || right.durum) + ' · ' + until })]);
  }

  function downloadCard(account) {
    const result = el('div', { class: 'account-download' });
    const create = el('button', { class: 'button button-gold', type: 'button', text: 'İndirme bağlantısı oluştur' });
    create.addEventListener('click', () => guarded(create, async () => {
      const data = await api('uye/indirme', {});
      const url = data.indirme.baglanti;
      result.replaceChildren(
        el('div', { class: 'download-choices' }, [
          el('a', { class: 'download-choice', href: url + '/windows' }, [el('strong', { text: 'Windows' }),
            el('span', { text: 'Windows 10 ve 11 (64 bit)' })]),
          el('a', { class: 'download-choice', href: url + '/mac' }, [el('strong', { text: 'macOS' }),
            el('span', { text: 'Apple işlemcili Mac (M1 ve sonrası)' })]),
        ]),
        el('p', { class: 'aside', text: 'Bağlantı ' + day(data.indirme.gecerlilik) + ' saatine kadar geçerli; önceki bağlantınız iptal oldu.' }));
    }));
    const last = account.indirme;
    return el('section', { class: 'card account-card', 'aria-labelledby': 'h-indir' }, [
      el('h2', { id: 'h-indir', text: 'Peyrev\'i indir' }),
      account.abone
        ? el('p', { text: 'Kurulum dosyasına kişisel bağlantıyla ulaşırsınız. Kurulu Peyrev yeni sürümleri kendiliğinden indirir.' })
        : el('p', { class: 'form-status is-error', text: 'İndirme bağlantısı aboneliği devam eden hesaplara verilir. Önce lisansınızı uzatın.' }),
      last ? el('p', { class: 'aside', text: 'Son bağlantı: ' + day(last.olusturma) + ' · ' + last.kullanim + ' kez kullanıldı'
        + (last.gecerli ? ' · ' + day(last.gecerlilik) + ' saatine kadar geçerli' : ' · süresi doldu') }) : null,
      account.abone ? create : null,
      result,
    ]);
  }

  function renewCard(account) {
    const current = account.lisans.ikiz.durum === 'aktif' ? 'ikiz' : account.lisans.ajanlar.durum === 'aktif' ? 'rpa_ajan' : 'rpa';
    const choices = PACKAGES.map(([value, label, price]) => {
      const radio = el('input', { type: 'radio', name: 'paket', value, checked: value === current });
      return { radio, node: el('label', { class: 'option' }, [radio, el('span', null, [el('strong', { text: label }), price])]) };
    });
    const submit = el('button', { class: 'button button-gold', type: 'submit', text: 'Uzatma iste' });
    const form = el('form', { class: 'account-renew', novalidate: true }, [
      el('fieldset', { class: 'choice' }, [el('legend', { text: 'Paket' })].concat(choices.map((item) => item.node))),
      submit,
    ]);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      guarded(submit, async (box) => {
        const chosen = choices.find((item) => item.radio.checked);
        const data = await api('uye/uzatma', { paket: chosen.radio.value });
        box.append(notice(data.message, 'ok'));
      });
    });
    return el('section', { class: 'card account-card', 'aria-labelledby': 'h-lisans' }, [
      el('h2', { id: 'h-lisans', text: 'Lisans' }),
      el('ul', { class: 'account-rights' }, [licenseRow('Peyrev', account.lisans.peyrev), licenseRow('Ajanlar', account.lisans.ajanlar),
        licenseRow('Dijital İkiz', account.lisans.ikiz)]),
      account.yonetici ? el('p', { class: 'aside', text: 'Yönetici hesabı: Peyrev ve Dijital İkiz lisans satırı olmadan da açılır.' }) : null,
      account.lisans.ikiz.durum === 'aktif' ? el('div', { class: 'account-actions' }, [
        el('a', { class: 'button button-gold', href: TWIN, text: 'Dijital İkiz\'i aç' }),
        el('span', { class: 'aside', text: 'Deponuzun canlı modeli peyrev.com/ikiz/ adresinde açılır.' })]) : null,
      el('h3', { text: 'Lisansımı uzat' }),
      el('p', { class: 'aside', text: 'İsteğiniz bize ulaşır; ödeme bilgisini e-posta adresinize iletiriz. Ödeme onaylanınca süreniz uzar.' }),
      form,
    ]);
  }

  function passwordCard(account) {
    const current = el('input', { id: 'h-mevcut', type: 'password', autocomplete: 'current-password', maxlength: 256, required: true });
    const fresh = el('input', { id: 'h-yeni', type: 'password', autocomplete: 'new-password', maxlength: 128, required: true });
    const repeat = el('input', { id: 'h-tekrar', type: 'password', autocomplete: 'new-password', maxlength: 128, required: true });
    const submit = el('button', { class: 'button button-outline', type: 'submit', text: 'Şifreyi değiştir' });
    const form = el('form', { class: 'card account-card', novalidate: true, 'aria-labelledby': 'h-sifre-baslik' }, [
      el('h2', { id: 'h-sifre-baslik', text: 'Şifre' }),
      el('p', { class: 'aside', text: 'Şifreniz Peyrev uygulamasıyla ortaktır. Değişince açık Peyrev oturumunuz yeni şifreyle yeniden giriş ister.' }),
      field('Mevcut şifre', current), field('Yeni şifre', fresh, '8-128 karakter; en az bir harf ve bir rakam.'),
      field('Yeni şifre (tekrar)', repeat), submit,
    ]);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      guarded(submit, async () => {
        if (!current.value) throw new Error('Mevcut şifrenizi yazın.');
        if (fresh.value.length < 8 || fresh.value.length > 128) throw new Error('Yeni şifre 8-128 karakter olmalıdır.');
        if (!/\p{L}/u.test(fresh.value) || !/\d/.test(fresh.value)) throw new Error('Yeni şifrede en az bir harf ve bir rakam bulunmalıdır.');
        if (fresh.value !== repeat.value) throw new Error('Yeni şifre ile tekrarı aynı değil.');
        await api('rpa/hesap/sifre', { kullanici: account.giris, sifre: current.value, yeni_sifre: fresh.value }, { anonymous: true });
        showLogin('');
        ROOT.querySelector('.community-login').append(notice('Şifreniz değiştirildi. Yeni şifrenizle giriş yapın.', 'ok'));
      });
    });
    return form;
  }

  function sessionsCard(account) {
    return el('section', { class: 'card account-card', 'aria-labelledby': 'h-oturum' }, [
      el('h2', { id: 'h-oturum', text: 'Peyrev oturumu' }),
      account.studio.length ? el('ul', { class: 'account-rights' }, account.studio.map((item) => el('li', null, [
        el('strong', { text: 'Sürüm ' + item.surum }), el('span', { text: 'Cihaz ' + item.cihaz + '… · son görülme ' + day(item.son_gorulme) })])))
        : el('p', { class: 'aside', text: 'Şu an açık Peyrev oturumunuz yok.' }),
      el('p', { class: 'aside', text: 'Hesabınız aynı anda bir bilgisayarda açık olur; başka bir bilgisayarda giriş yapınca önceki oturum kapanır.' }),
    ]);
  }

  // ----- Robotlarım ----------------------------------------------------------------------------
  const TONES = { yesil: 'chip-soru', sari: 'chip-duyuru', kirmizi: 'chip-hata' };

  function ago(minutes) {
    if (minutes < 1) return 'az önce';
    if (minutes < 120) return minutes + ' dk önce';
    if (minutes < 2880) return Math.floor(minutes / 60) + ' sa önce';
    return Math.floor(minutes / 1440) + ' gün önce';
  }

  function chip(text, tone) {
    return el('span', { class: 'chip' + (tone ? ' ' + tone : ''), text });
  }

  function commandButton(robot, kind, target, name, reload) {
    const label = kind === 'durdur' ? 'Durdur' : 'Yeniden çalıştır';
    const button = el('button', { class: 'button button-small ' + (kind === 'durdur' ? 'button-quiet-dark' : 'button-outline'),
      type: 'button', text: label, disabled: !robot.komut_verilebilir });
    button.addEventListener('click', () => {
      if (!window.confirm('«' + name + '» için ' + label.toLocaleLowerCase('tr') + ' komutu bilgisayarınıza gönderilsin mi?')) return;
      guarded(button, async (box) => {
        await api('uye/robotlar/' + robot.id + '/komut', { tur: kind, hedef: target });
        await reload();
        box.append(notice(label + ' komutu yazıldı; Peyrev bir sonraki bildiriminde (en geç 5 dakika) uygular.', 'ok'));
      });
    });
    return button;
  }

  function robotOffice(robot, office, reload) {
    const counts = office.son_24_saat;
    const offered = new Set();
    const rows = office.calisan.map((item) => el('li', null, [
      el('span', null, [el('strong', { text: item.akis }), ' · çalışıyor, başladı ' + day(item.baslangic)
        + (item.adim ? ' · adım no ' + item.adim : '')]),
      commandButton(robot, 'durdur', item.run_id, item.akis, reload),
    ])).concat(office.son_hatalar.map((item) => {
      const first = !offered.has(item.workflow_id);
      offered.add(item.workflow_id);
      return el('li', null, [
        el('span', null, [el('strong', { text: item.akis }), ' · ', chip(item.kategori_adi, 'chip-hata'), ' ' + day(item.zaman)]),
        first ? commandButton(robot, 'yeniden_calistir', item.workflow_id, item.akis, reload) : null,
      ]);
    }));
    return el('div', { class: 'robot-office' }, [
      el('h4', { text: (office.ad || 'Hesabınız') + (office.telegram ? ' · Telegram bağlı' : '') }),
      el('p', { class: 'aside', text: 'Son 24 saat: ' + counts.basarili + ' başarılı · ' + counts.hatali + ' hatalı · '
        + counts.iptal + ' iptal' + (office.ardisik_hata >= 3 ? ' · art arda ' + office.ardisik_hata + ' hata' : '') }),
      rows.length ? el('ul', { class: 'account-rights' }, rows) : null,
    ]);
  }

  function robotBlock(robot, reload) {
    const commands = robot.komutlar.map((item) => el('li', null, [
      el('span', { text: item.tur_adi + ' · ' + (item.akis || '') + ' · ' + day(item.olusturuldu) }),
      el('span', { text: item.sonuc_adi }),
    ]));
    return el('div', { class: 'robot' }, [
      el('h3', null, [robot.etiket + ' ', chip('Son bildirim ' + ago(robot.dakika), TONES[robot.renk]),
        robot.kapali ? chip('Peyrev kapalı') : null, robot.ekran_kilitli ? chip('Ekran kilitli', 'chip-duyuru') : null,
        robot.disk_uyari ? chip('Boş disk %' + robot.bos_disk, 'chip-hata') : null]),
      el('p', { class: 'aside', text: robot.isletim_sistemi + ' · Peyrev ' + robot.surum + ' · son bildirim ' + day(robot.son_gorulme) }),
    ].concat(robot.ofisler.map((office) => robotOffice(robot, office, reload)), commands.length ? [
      el('h4', { text: 'Son komutlar' }), el('ul', { class: 'account-rights' }, commands)] : []));
  }

  function robotsCard() {
    const body = el('div', { class: 'robot-list' }, [el('p', { class: 'aside', text: 'Robotlarınız yükleniyor…' })]);
    const reload = async () => {
      const data = await api('uye/robotlar');
      body.replaceChildren(...(data.robotlar.length ? data.robotlar.map((robot) => robotBlock(robot, reload))
        : [el('p', { class: 'aside', text: 'Henüz durum bildiren bilgisayarınız yok. Peyrev 1.5.0 ve sonrası, Ayarlar\'daki '
          + '"Robot Paneline durum bildir" açıkken kendiliğinden bildirir.' })]));
    };
    const refresh = el('button', { class: 'button button-outline button-small', type: 'button', text: 'Yenile' });
    refresh.addEventListener('click', () => guarded(refresh, reload));
    const card = el('section', { class: 'card account-card account-robots', 'aria-labelledby': 'h-robot' }, [
      el('h2', { id: 'h-robot', text: 'Robotlarım' }),
      el('p', { class: 'aside', text: 'Peyrev kurulu bilgisayarlarınızın durumu. Yalnız akış adları, durumlar, adım '
        + 'numaraları ve hata türleri gelir; hata metni, değişkenler, dosyalar ve ekran görüntüsü bilgisayarınızdan çıkmaz.' }),
      body, refresh,
    ]);
    reload().catch((error) => {
      if (!(error instanceof Refused)) body.replaceChildren(notice(error.message, 'error'));
    });
    return card;
  }

  function show(account) {
    const out = el('button', { class: 'button button-outline button-small', type: 'button', text: 'Çıkış' });
    out.addEventListener('click', () => guarded(out, async () => {
      await api('uye/cikis', {});
      showLogin('');
    }));
    ROOT.replaceChildren(
      el('div', { class: 'community-bar' }, [
        el('p', null, ['Merhaba, ', el('strong', { text: account.ad }), el('span', { class: 'chip' + (account.abone ? ' chip-soru' : ''),
          text: account.abone ? 'Abonelik devam ediyor' : 'Abonelik devam etmiyor' })]),
        el('div', { class: 'community-bar-actions' }, [account.abone ? el('a', { class: 'button button-outline button-small',
          href: '/topluluk/', text: 'Topluluk' }) : null, out]),
      ]),
      el('p', { class: 'aside', text: 'Kullanıcı adınız: ' + account.giris }),
      TWIN_STATE === 'yok' && account.lisans.ikiz.durum !== 'aktif' ? el('p', { class: 'form-status is-error', role: 'status',
        text: 'Bu hesapta Dijital İkiz hakkı yok ya da süresi dolmuş. Peyrev Dijital İkiz paketine geçmek için aşağıdan '
          + 'uzatma isteyin.' }) : null,
      el('div', { class: 'account-grid' }, [renewCard(account), downloadCard(account), passwordCard(account), sessionsCard(account),
        robotsCard()]),
    );
  }

  async function boot() {
    try {
      show((await api('uye/hesap')).hesap);
    } catch (error) {
      if (!(error instanceof Refused)) ROOT.replaceChildren(notice(error.message, 'error'));
    }
  }

  boot();
})();
