'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const ready = [], gone = [];
  const session = {
    csrf: '',
    ready: false,
    // Called now if signed in already, and again after every later sign-in.
    onReady(fn) { ready.push(fn); if (this.ready) fn(); },
    // Called on logout and on an expired session: screens must drop everything they show.
    onLogout(fn) { gone.push(fn); },
    expired(message) { showLogin(message || 'Сессия завершилась. Войдите снова.'); },
  };
  window.adminSession = session;
  const options = extra => ({credentials: 'same-origin', cache: 'no-store', redirect: 'error', ...extra});
  function message(text) {
    $('login-message').textContent = text || '';
    $('login-message').hidden = !text;
  }
  function showLogin(text) {
    const was = session.ready;
    session.ready = false; session.csrf = '';
    $('admin-app').hidden = true; $('login-view').hidden = false; $('logout').hidden = true;
    $('admin-user').textContent = '';
    $('admin-password').value = ''; $('admin-code').value = '';
    if (was) for (const fn of gone) fn();
    message(text);
  }
  function showApp(data) {
    if (typeof data.csrf_token !== 'string' || !data.csrf_token || typeof data.username !== 'string') throw new Error('schema');
    session.csrf = data.csrf_token; session.ready = true;
    message('');
    $('admin-user').textContent = data.username;
    $('login-view').hidden = true; $('admin-app').hidden = false; $('logout').hidden = false;
    $('admin-password').value = ''; $('admin-code').value = '';
    for (const fn of ready) fn();
  }
  async function check() {
    try {
      const response = await fetch('/admin-api/v1/me', options({headers: {Accept: 'application/json'}}));
      if (response.status === 401) return showLogin('');
      if (!response.ok) throw new Error('status');
      showApp(await response.json());
    } catch (error) {
      showLogin('Не удалось связаться с сервером. Проверьте, что вы в приватной сети.');
    }
  }
  $('login-form').addEventListener('submit', async event => {
    event.preventDefault();
    const body = {username: $('admin-username').value.trim(), password: $('admin-password').value, totp: $('admin-code').value.trim()};
    $('login-submit').disabled = true; message('');
    try {
      const response = await fetch('/admin-api/v1/login', options({
        method: 'POST', headers: {'Content-Type': 'application/json', Accept: 'application/json'}, body: JSON.stringify(body),
      }));
      if (response.status === 429) { message('Слишком много попыток. Подождите минуту.'); return; }
      // One message for every other failure: the page never says which part was wrong.
      if (!response.ok) { message('Не удалось войти. Проверьте логин, пароль и код.'); return; }
      showApp(await response.json());
    } catch (error) {
      message('Не удалось войти. Проверьте логин, пароль и код.');
    } finally {
      $('login-submit').disabled = false;
      $('admin-password').value = ''; $('admin-code').value = '';
    }
  });
  $('logout').addEventListener('click', async () => {
    const csrf = session.csrf;
    $('logout').disabled = true;
    try {
      await fetch('/admin-api/v1/logout', options({method: 'POST', headers: {'X-CSRF-Token': csrf}}));
    } catch (error) { /* The server session also ends by itself. */ }
    $('logout').disabled = false;
    showLogin('Вы вышли.');
  });
  check();
})();
