'use strict';
// Cookie and storage banner. It also gives other scripts one place to ask whether optional
// browser storage is allowed. Strictly necessary cookies (sign-in) never wait for this choice.
(() => {
  const KEY = 'beresta.cookie-consent.v1', VERSION = 1;
  // Conveniences that are written to localStorage only after "Принять все".
  const OPTIONAL_KEYS = ['beresta.onboarding.dismissed.v1', 'beresta.reminderZone'];
  let memoryChoice = null;
  const memoryValues = {};

  function readChoice() {
    if (memoryChoice) return memoryChoice;
    try {
      const raw = window.localStorage.getItem(KEY);
      if (!raw) return null;
      const saved = JSON.parse(raw);
      if (saved && (saved.choice === 'all' || saved.choice === 'necessary') && saved.version === VERSION) return saved;
    } catch { /* blocked or damaged storage counts as no choice */ }
    return null;
  }
  function choice() { return readChoice()?.choice || null; }
  function allowsOptional() { return choice() === 'all'; }

  function purgeOptional() {
    for (const key of OPTIONAL_KEYS) { try { window.localStorage.removeItem(key); } catch { /* nothing to remove */ } }
  }
  // Optional values go to localStorage only with "all". Otherwise they live for this tab only.
  function read(key) {
    if (allowsOptional()) {
      try { const value = window.localStorage.getItem(key); if (value !== null) return value; } catch { /* use fallbacks */ }
    }
    try { const value = window.sessionStorage.getItem(key); if (value !== null) return value; } catch { /* use memory */ }
    return Object.prototype.hasOwnProperty.call(memoryValues, key) ? memoryValues[key] : null;
  }
  function write(key, value) {
    memoryValues[key] = String(value);
    if (allowsOptional()) {
      try { window.localStorage.setItem(key, String(value)); return; } catch { /* fall through */ }
    }
    try { window.sessionStorage.setItem(key, String(value)); } catch { /* memory only */ }
  }

  let bar = null, returnFocus = null;
  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function save(value) {
    const record = {choice: value, version: VERSION, at: new Date().toISOString()};
    memoryChoice = record;
    try { window.localStorage.setItem(KEY, JSON.stringify(record)); } catch { /* kept in memory for this page */ }
    if (value !== 'all') purgeOptional();
    hide();
    document.dispatchEvent(new CustomEvent('beresta:cookie-consent', {detail: {choice: value}}));
  }
  function hide() {
    if (!bar) return;
    bar.remove(); bar = null;
    document.body.classList.remove('cookie-bar-open');
    if (returnFocus && document.contains(returnFocus)) returnFocus.focus();
    returnFocus = null;
  }
  function show(focus) {
    if (bar) { if (focus) bar.querySelector('button')?.focus(); return; }
    bar = node('div', 'cookie-bar');
    bar.id = 'cookie-consent';
    bar.setAttribute('role', 'dialog');
    bar.setAttribute('aria-modal', 'false');
    bar.setAttribute('aria-labelledby', 'cookie-consent-title');
    bar.setAttribute('aria-describedby', 'cookie-consent-text');
    const copy = node('div', 'cookie-copy');
    const title = node('p', 'cookie-title', 'Файлы cookie и память браузера'); title.id = 'cookie-consent-title';
    const text = node('p', 'cookie-text');
    text.id = 'cookie-consent-text';
    text.append('Для входа нужны служебные cookie, они работают при любом выборе. Остальное необязательно, это запомненный часовой пояс напоминаний и скрытые подсказки. Рекламы и аналитики нет. ');
    const link = node('a', '', 'Подробнее о cookie'); link.href = '/cookies';
    text.append(link, '.');
    copy.append(title, text);
    const buttons = node('div', 'cookie-buttons');
    const all = node('button', 'cookie-choice quiet', 'Принять все'); all.type = 'button'; all.id = 'cookie-accept-all';
    const necessary = node('button', 'cookie-choice quiet', 'Только необходимые'); necessary.type = 'button'; necessary.id = 'cookie-necessary';
    all.addEventListener('click', () => save('all'));
    necessary.addEventListener('click', () => save('necessary'));
    buttons.append(all, necessary);
    bar.append(copy, buttons);
    document.body.prepend(bar);
    document.body.classList.add('cookie-bar-open');
    if (focus) all.focus();
  }
  function reopen() {
    returnFocus = document.activeElement && document.activeElement !== document.body ? document.activeElement : null;
    show(true);
  }

  window.berestaConsent = {choice, allowsOptional, read, write, reopen};
  document.addEventListener('click', event => {
    const target = event.target.closest ? event.target.closest('[data-cookie-settings]') : null;
    if (!target) return;
    event.preventDefault();
    reopen();
  });
  if (!choice()) show(false);
})();
