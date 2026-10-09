'use strict';
// Landing for signed-out visitors. It only changes how the existing #auth-form is presented;
// app.js still owns validation, requests and messages.
(() => {
  const get = id => document.getElementById(id);
  const auth = get('auth'), form = get('auth-form');
  if (!auth || !form) return;
  const copy = {
    login: ['Войти в beresta', 'Продолжите с того места, где остановились.', 'current-password'],
    register: ['Создать аккаунт', 'Придумайте имя пользователя и пароль. Почта не нужна.', 'new-password'],
  };
  const actions = form.querySelector('.actions');
  const buttons = {login: form.querySelector('button[value=login]'), register: form.querySelector('button[value=register]')};
  function setIntent(intent) {
    if (!copy[intent]) return;
    auth.dataset.intent = intent;
    const [title, lead, autocomplete] = copy[intent];
    get('auth-title').textContent = title; get('auth-lead').textContent = lead;
    get('password').setAttribute('autocomplete', autocomplete);
    const other = intent === 'login' ? 'register' : 'login';
    // The first submit button is what Enter triggers, so the chosen intent goes first.
    buttons[intent].classList.remove('secondary'); buttons[other].classList.add('secondary');
    actions.prepend(buttons[intent]);
  }
  function goToForm(intent) {
    setIntent(intent);
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    get('auth-card').scrollIntoView?.({behavior: reduce ? 'auto' : 'smooth', block: 'center'});
    get('login').focus({preventScroll: true});
  }
  for (const [id, intent] of [['landing-register', 'register'], ['landing-register-bottom', 'register'], ['landing-login', 'login']]) {
    get(id).addEventListener('click', () => goToForm(intent));
  }
  // Ads and channel posts can link to /#register to open the form in sign-up mode.
  if (location.hash === '#register') setIntent('register');
})();
