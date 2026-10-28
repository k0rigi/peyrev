/* peyrev.com ana sayfası: hesap başvurusu formu (#basvuru → POST /api/rpa/basvuru). Derleme adımı ve harici kaynak yok.
 *
 * Gövde, sunucunun kabul ettiği alanlardır (peyrev.accounts.applications): SENT'teki adlar ve her zaman onay: true.
 * "tekrar" yalnız tarayıcıda denetlenir; boş telefon ve boş bal küpü (web_sitesi) gönderilmez. Önden denetimler sunucunun
 * kurallarıyla aynı metinleri verir; son söz her zaman sunucunundur ve yanıtındaki metin olduğu gibi gösterilir. Başvuru
 * alınınca form yerini onay ekranına (#basvuru-tamam) bırakır.
 */
(function () {
  'use strict';

  const ENDPOINT = '/api/rpa/basvuru';
  const SENT = ['ad_soyad', 'eposta', 'telefon', 'sifre', 'paket', 'web_sitesi'];
  const OPTIONAL = ['telefon', 'web_sitesi'];
  const FAILED = 'Başvuru gönderilemedi. Daha sonra tekrar deneyin.';
  const OFFLINE = 'Başvuru gönderilemedi. İnternet bağlantınızı kontrol edin.';

  const form = document.getElementById('basvuru');
  if (!form) return;
  const status = document.getElementById('basvuru-durumu');
  const submit = form.querySelector('button[type="submit"]');
  const submitText = submit.textContent;

  function show(text, tone) {
    status.textContent = text;
    status.className = 'form-status' + (tone ? ' is-' + tone : '');
    status.hidden = !text;
  }

  /** Gönderilen başvurunun onay ekranı: form gizlenir, sunucunun metni ve başvuru e-postası metin olarak yazılır. */
  function done(text, email) {
    form.reset();
    show('');
    document.getElementById('basvuru-tamam-metni').textContent = text;
    document.getElementById('basvuru-tamam-eposta').textContent = email;
    const panel = document.getElementById('basvuru-tamam');
    panel.hidden = false;
    form.classList.add('is-done');
    form.scrollIntoView({ block: 'start' });
    panel.focus({ preventScroll: true });
  }

  function clearMarks() {
    for (const node of form.querySelectorAll('[aria-invalid="true"]')) node.removeAttribute('aria-invalid');
  }

  /** Önden denetim: sorun varsa [alan adı, metin]. */
  function problem(value, checked) {
    const name = value('ad_soyad').trim();
    const email = value('eposta').trim();
    const password = value('sifre');
    if (name.length < 3 || name.length > 120) return ['ad_soyad', 'Ad soyad 3-120 karakter olmalıdır.'];
    if (!/^[^@\s<>"]{1,64}@[^@\s<>"]+\.[^@\s<>"]{2,}$/.test(email) || email.length > 150) {
      return ['eposta', 'Geçerli bir e-posta adresi yazın.'];
    }
    if (password.length < 8 || password.length > 128) return ['sifre', 'Şifre 8-128 karakter olmalıdır.'];
    if (!/\p{L}/u.test(password) || !/\d/.test(password)) {
      return ['sifre', 'Şifrede en az bir harf ve bir rakam bulunmalıdır.'];
    }
    if (password !== value('tekrar')) return ['tekrar', 'Şifre ile tekrarı aynı değil.'];
    if (!checked) return ['onay', 'Başvuru için bilgilendirme metnini onaylamanız gerekir.'];
    return null;
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    clearMarks();
    const data = new FormData(form);
    const value = (name) => String(data.get(name) || '');
    const found = problem(value, data.get('onay') !== null);
    if (found) {
      const control = form.elements[found[0]];
      control.setAttribute('aria-invalid', 'true');
      show(found[1], 'error');
      control.focus();
      return;
    }
    const body = { onay: true };
    for (const name of SENT) {
      const text = name === 'sifre' ? value(name) : value(name).trim();
      if (text || !OPTIONAL.includes(name)) body[name] = text;
    }
    submit.disabled = true;
    submit.textContent = 'Gönderiliyor…';
    form.setAttribute('aria-busy', 'true');
    show('');
    try {
      let response;
      try {
        response = await fetch(ENDPOINT, {
          method: 'POST', credentials: 'omit', cache: 'no-store',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify(body),
        });
      } catch (error) {
        show(OFFLINE, 'error');
        return;
      }
      let answer = {};
      try {
        answer = (await response.json()) || {};
      } catch (error) {
        answer = {};
      }
      if (!response.ok || answer.success !== true) {
        show(answer.message || FAILED, 'error');
        return;
      }
      done(answer.message || 'Başvurunuz alındı.', body.eposta);
    } finally {
      submit.disabled = false;
      submit.textContent = submitText;
      form.removeAttribute('aria-busy');
    }
  });
})();
