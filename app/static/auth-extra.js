'use strict';
// Sign-in through Telegram, password recovery, privacy consent and account deletion.
// Loaded after app.js. It uses api(), authMessage(), message() and enterApp() from there.
// The one-time tokens live in memory only and never in storage.
(() => {
  const POLL_MS = 2000, GRACE_MS = 2000;
  const el = id => document.getElementById(id);
  const botLink = url => typeof url === 'string' && url.startsWith('https://t.me/');
  const send = (path, body) => api(path, {method: 'POST', body: JSON.stringify(body || {})});
  const consent = () => el('accept-policy');
  const say = (id, text) => { el(id).textContent = text; };

  function info(text = '') {
    const line = el('auth-info');
    line.textContent = text; line.hidden = !text;
  }

  function clearHash() {
    try { history.replaceState(null, '', location.pathname + location.search); } catch { location.hash = ''; }
  }

  function openDialog(id) {
    const dialog = el(id);
    if (!dialog.open) dialog.showModal();
    return dialog;
  }

  // One poller per flow. check(job) returns true once the flow has finished by itself.
  function makePoller(check) {
    let job = null, timer = null;
    const stopTimer = () => { clearTimeout(timer); timer = null; };
    const schedule = () => {
      stopTimer();
      if (job && !document.hidden) timer = setTimeout(tick, POLL_MS);
    };
    async function tick() {
      timer = null;
      const mine = job;
      if (!mine) return;
      if (Date.now() >= mine.expires + GRACE_MS) { job = null; mine.onExpired(); return; }
      let finished = false;
      try { finished = await check(mine); }
      catch (e) {
        if (job !== mine) return;
        if ([401, 403, 404, 409].includes(e.status)) { job = null; mine.onFail(e); return; }
      }
      if (job !== mine) return;
      if (finished) { job = null; return; }
      schedule();
    }
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) stopTimer(); else if (job) tick();
    });
    return {
      start(next) { job = next; schedule(); },
      stop() { job = null; stopTimer(); },
    };
  }

  // Sign in through the bot

  const loginButton = el('tg-login'), loginBox = el('tg-login-box');
  const loginPoller = makePoller(async job => {
    const result = await api(`/api/v1/auth/telegram/status/${encodeURIComponent(job.id)}`);
    if (result.status === 'expired') { job.onExpired(); return true; }
    if (result.status !== 'ok') return false;
    resetLogin();
    try {
      await enterApp(result);
      message(result.new_account ? 'Аккаунт создан. Добро пожаловать!' : '');
    } catch (e) { message(e.message); }
    return true;
  });

  function resetLogin() {
    loginPoller.stop();
    loginBox.hidden = true; loginButton.disabled = false; say('tg-login-status', '');
  }

  loginButton.onclick = async () => {
    const box = consent();
    if (!box.checked) {
      authMessage('Чтобы войти через Telegram, примите политику конфиденциальности.');
      box.focus(); return;
    }
    authMessage(); info(); resetLogin();
    loginButton.disabled = true;
    try {
      const start = await send('/api/v1/auth/telegram/start', {accept_policy: true, policy_version: box.dataset.policyVersion});
      if (!botLink(start.deep_link)) throw new Error('Не удалось начать вход. Попробуйте ещё раз.');
      el('tg-login-link').href = start.deep_link;
      loginBox.hidden = false;
      say('tg-login-status', 'Ждём подтверждения в Telegram…');
      loginPoller.start({
        id: start.login_id,
        expires: Date.parse(start.expires_at),
        onExpired() { resetLogin(); authMessage('Время вышло. Нажмите «Войти через Telegram» ещё раз.'); },
        onFail(e) { resetLogin(); authMessage(e.message); },
      });
    } catch (e) {
      loginButton.disabled = false;
      authMessage(e.message);
    }
  };

  el('tg-login-cancel').onclick = () => { resetLogin(); authMessage(); };

  // Forgot password

  const forgot = el('forgot-password');
  forgot.onclick = async () => {
    const username = el('login').value.trim();
    if (!username) {
      authMessage('Введите имя пользователя выше. Ссылка придёт в Telegram.');
      el('login').focus(); return;
    }
    authMessage(); info();
    forgot.disabled = true;
    try {
      const answer = await send('/api/v1/auth/recovery/telegram', {username});
      info(answer.message);
    } catch (e) { authMessage(e.message); }
    finally { forgot.disabled = false; }
  };

  // New password from the link in the bot message. The token leaves the address bar at once.

  let resetToken = null;
  const resetForm = el('reset-form');

  function openReset() {
    const found = /^#reset=([A-Za-z0-9_-]{20,200})$/.exec(location.hash);
    if (!found) return;
    resetToken = found[1];
    clearHash();
    say('reset-status', '');
    openDialog('reset-dialog');
    el('reset-password').focus();
  }

  resetForm.onsubmit = async event => {
    event.preventDefault();
    const first = el('reset-password').value, second = el('reset-password2').value;
    if ([...first].length < 10 || [...first].length > 128) return say('reset-status', 'Пароль должен содержать от 10 до 128 символов.');
    if (first !== second) return say('reset-status', 'Пароли не совпадают.');
    if (!resetToken) return say('reset-status', 'Ссылка устарела. Запросите новую.');
    const button = el('reset-submit');
    button.disabled = true;
    try {
      await send('/api/v1/auth/password-reset', {token: resetToken, password: first});
      resetToken = null;
      el('reset-password').value = ''; el('reset-password2').value = '';
      el('reset-dialog').close();
      // The server ended every session of this account, so a signed-in page must start over.
      if (!el('workspace').hidden) { location.reload(); return; }
      authMessage(); info('Пароль изменён. Войдите с новым паролем.');
    } catch (e) { say('reset-status', e.message); }
    finally { button.disabled = false; }
  };

  window.addEventListener('hashchange', openReset);
  openReset();

  // Privacy policy: older accounts accept the current version once.

  el('policy-accept').onclick = async () => {
    const button = el('policy-accept');
    button.disabled = true;
    try {
      await send('/api/v1/account/accept-policy', {policy_version: consent().dataset.policyVersion});
      el('policy-banner').hidden = true;
    } catch (e) { message(e.message); }
    finally { button.disabled = false; }
  };

  // Account deletion. The operator removes the data; the page only files the request.

  const deletePoller = makePoller(async job => {
    const result = await send(`/api/v1/account/delete-request/telegram/${encodeURIComponent(job.id)}`);
    if (result.status === 'expired') { job.onExpired(); return true; }
    if (result.status !== 'requested') return false;
    finishDeletion();
    return true;
  });

  function finishDeletion() {
    deletePoller.stop();
    say('delete-status', 'Запрос принят. Мы вышли из аккаунта.');
    setTimeout(() => location.reload(), 1500);
  }

  function resetDelete() {
    deletePoller.stop();
    el('delete-telegram-box').hidden = true; el('delete-telegram-start').disabled = false;
    el('delete-password').value = ''; say('delete-status', '');
  }

  el('open-delete').onclick = () => { resetDelete(); openDialog('delete-dialog'); };
  el('delete-dialog').addEventListener('close', resetDelete);

  el('delete-form').onsubmit = async event => {
    event.preventDefault();
    const password = el('delete-password').value;
    if (!password) return say('delete-status', 'Введите пароль или подтвердите в Telegram.');
    const button = el('delete-submit');
    button.disabled = true;
    try {
      await send('/api/v1/account/delete-request', {password});
      finishDeletion();
    } catch (e) { say('delete-status', e.message); }
    finally { button.disabled = false; }
  };

  el('delete-telegram-start').onclick = async () => {
    const button = el('delete-telegram-start');
    button.disabled = true; say('delete-status', '');
    try {
      const start = await send('/api/v1/account/delete-request/telegram');
      if (!botLink(start.deep_link)) throw new Error('Не удалось начать подтверждение. Попробуйте ещё раз.');
      el('delete-telegram-link').href = start.deep_link;
      el('delete-telegram-box').hidden = false;
      say('delete-status', 'Откройте Telegram и нажмите «Запустить». Ждём подтверждения…');
      deletePoller.start({
        id: start.login_id,
        expires: Date.parse(start.expires_at),
        onExpired() { resetDelete(); say('delete-status', 'Время вышло. Начните ещё раз.'); },
        onFail(e) { resetDelete(); say('delete-status', e.message); },
      });
    } catch (e) {
      button.disabled = false;
      say('delete-status', e.message);
    }
  };

  el('deletion-cancel').onclick = async () => {
    const button = el('deletion-cancel');
    button.disabled = true;
    try {
      await send('/api/v1/account/delete-request/cancel');
      el('deletion-banner').hidden = true;
      message('Запрос на удаление отменён.');
    } catch (e) { message(e.message); }
    finally { button.disabled = false; }
  };
})();
