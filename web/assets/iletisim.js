/* peyrev.com/iletisim/: iletişim formu (#iletisim → POST /api/iletisim, peyrev.iletisim). Derleme adımı ve harici kaynak yok.
 *
 * Gövde SENT'teki alanlardır; boş bal küpü (web_sitesi) gönderilmez. Önden denetimler sunucunun kurallarıyla aynı
 * metinleri verir; son söz sunucunundur ve yanıtındaki metin olduğu gibi (metin olarak) gösterilir.
 */
(function () {
  'use strict';

  const ENDPOINT = '/api/iletisim';
  const SENT = ['ad', 'eposta', 'konu', 'mesaj', 'web_sitesi'];
  const FAILED = 'Mesaj gönderilemedi. Daha sonra tekrar deneyin ya da info@peyrev.com adresine yazın.';
  const OFFLINE = 'Mesaj gönderilemedi. İnternet bağlantınızı kontrol edin.';

  const form = document.getElementById('iletisim');
  if (!form) return;
  const status = document.getElementById('iletisim-durumu');
  const submit = form.querySelector('button[type="submit"]');
  const submitText = submit.textContent;

  function show(text, tone) {
    status.textContent = text;
    status.className = 'form-status' + (tone ? ' is-' + tone : '');
    status.hidden = !text;
  }

  /** Önden denetim: sorun varsa [alan adı, metin]. */
  function problem(value) {
    const name = value('ad');
    const email = value('eposta');
    const message = value('mesaj');
    if (name.length < 2 || name.length > 120) return ['ad', 'Ad 2-120 karakter olmalıdır.'];
    if (!/^[^@\s<>"]{1,64}@[^@\s<>"]+\.[^@\s<>"]{2,}$/.test(email) || email.length > 150) {
      return ['eposta', 'Geçerli bir e-posta adresi yazın.'];
    }
    if (message.length < 10 || message.length > 3000) return ['mesaj', 'Mesaj 10-3000 karakter olmalıdır.'];
    return null;
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    for (const node of form.querySelectorAll('[aria-invalid="true"]')) node.removeAttribute('aria-invalid');
    const data = new FormData(form);
    const value = (name) => String(data.get(name) || '').trim();
    const found = problem(value);
    if (found) {
      const control = form.elements[found[0]];
      control.setAttribute('aria-invalid', 'true');
      show(found[1], 'error');
      control.focus();
      return;
    }
    const body = {};
    for (const name of SENT) {
      if (value(name) || name !== 'web_sitesi') body[name] = value(name);
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
      form.reset();
      show(answer.message || 'Mesajınız bize ulaştı.', 'ok');
      status.focus();
    } finally {
      submit.disabled = false;
      submit.textContent = submitText;
      form.removeAttribute('aria-busy');
    }
  });
})();
