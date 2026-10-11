'use strict';
// Password and code remain in the form/memory only. The server owns the binding cookie.
(() => {
  const el = id => document.getElementById(id), dialog = el('email-auth-dialog');
  let mode = 'register', registration = null, busy = false;
  const say = text => { el('email-auth-message').textContent = text; };
  const post = (path, body) => api('/api/v1/auth/email/' + path, {method:'POST',body:JSON.stringify(body)});
  const lock = value => {
    busy = value;
    for (const id of ['email-auth-send','email-auth-confirm','email-auth-retry','email-auth-close','email-auth-recover']) el(id).disabled = value;
  };
  function open(next) {
    mode = next; registration = null;
    el('email-auth-title').textContent = mode === 'register' ? 'Регистрация по почте' : 'Войти по почте';
    el('email-auth-consent-label').hidden = mode !== 'register';
    el('email-auth-recover').hidden = mode !== 'login';
    el('email-auth-send').textContent = mode === 'register' ? 'Получить код' : 'Войти';
    el('email-auth-password').autocomplete = mode === 'register' ? 'new-password' : 'current-password';
    el('email-auth-form').hidden = false; el('email-auth-code-form').hidden = true;
    say(''); if (!dialog.open) dialog.showModal(); el('email-auth-address').focus();
  }
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  dialog.addEventListener('close', () => {
    registration = null; el('email-auth-password').value = ''; el('email-auth-code').value = '';
  });
  el('email-auth-close').onclick = () => { if (!busy) dialog.close(); };
  el('email-auth-retry').onclick = () => {
    if (busy) return;
    registration = null; el('email-auth-code').value = '';
    el('email-auth-form').hidden = false; el('email-auth-code-form').hidden = true;
    say('Повторное письмо можно запросить через минуту после предыдущего.');
  };
  el('email-auth-recover').onclick = async () => {
    if (busy || mode !== 'login') return;
    if (!el('email-auth-address').checkValidity()) return say('Введите почту аккаунта.');
    lock(true); say('');
    try {
      const result = await api('/api/v1/auth/recovery/email', {method:'POST',body:JSON.stringify({email:el('email-auth-address').value})});
      say(result.message);
    } catch (error) { say(error.message); }
    finally { lock(false); }
  };
  el('email-auth-form').onsubmit = async event => {
    event.preventDefault(); if (busy) return;
    if (mode === 'register' && !el('email-auth-consent').checked) return say('Дайте согласие на обработку персональных данных.');
    const body = {email:el('email-auth-address').value,password:el('email-auth-password').value};
    if (mode === 'register') Object.assign(body, {accept_policy:true,policy_version:el('accept-policy').dataset.policyVersion});
    lock(true); say('');
    try {
      const result = await post(mode === 'register' ? 'registration/start' : 'login', body);
      if (mode === 'login') { dialog.close(); await enterApp(result); return; }
      registration = result.registration_id;
      el('email-auth-password').value = '';
      el('email-auth-form').hidden = true; el('email-auth-code-form').hidden = false;
      el('email-auth-code').focus();
    } catch (error) { say(error.message); }
    finally { lock(false); }
  };
  el('email-auth-code-form').onsubmit = async event => {
    event.preventDefault(); if (busy || !registration) return;
    lock(true); say('');
    try {
      const result = await post('registration/confirm', {registration_id:registration,code:el('email-auth-code').value});
      dialog.close(); await enterApp(result);
    } catch (error) { say(error.message); }
    finally { lock(false); }
  };
  api('/api/v1/auth/email/options').then(options => {
    el('email-login').hidden = !options.login_enabled;
    el('email-login').onclick = () => open('login');
    if (options.registration_enabled) {
      document.querySelector('.landing-note').textContent = 'Можно войти через Telegram или зарегистрироваться по почте с подтверждением.';
      for (const button of document.querySelectorAll('#landing-register,#landing-register-bottom,#auth-form button[value="register"]')) {
        button.addEventListener('click', event => {
          event.preventDefault(); event.stopImmediatePropagation(); open('register');
        }, true);
      }
    }
  }).catch(() => {}); // Existing username/Telegram access remains available if mail setup is unavailable.
})();
