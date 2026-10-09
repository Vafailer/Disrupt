'use strict';
// First-run checklist for a signed-in user with no notes. It reads state that app.js already
// renders (#notes, #jobs, #telegram-links) and drives existing buttons, so it adds no new API.
(() => {
  const get = id => document.getElementById(id);
  const column = document.querySelector('.editor-column');
  if (!column || !get('capture-card') || !get('notes')) return;
  const KEY = 'beresta.onboarding.dismissed.v1';
  let dismissedInMemory = false, emptyLibrary = false, telegramLinked = false, panel = null, telegramChecked = false;
  // Storage can be blocked or throw (private mode, site data off). Never let that break the page.
  function readDismissed() {
    if (dismissedInMemory) return true;
    try { return window.localStorage.getItem(KEY) === '1'; } catch { return false; }
  }
  function writeDismissed() {
    dismissedInMemory = true;
    try { window.localStorage.setItem(KEY, '1'); } catch { /* remembered for this page only */ }
  }
  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function button(label, className, onclick) {
    const el = node('button', className, label); el.type = 'button'; el.addEventListener('click', onclick); return el;
  }
  function focusThought() {
    get('capture-tabs').querySelector('[data-capture-view=capture-form]')?.click();
    const field = get('thought'); field.focus();
    field.scrollIntoView?.({block: 'center', behavior: 'smooth'});
  }
  function step(done, title, text, controls, note) {
    const item = node('li', done ? 'onboarding-step is-done' : 'onboarding-step');
    const mark = node('span', 'onboarding-mark'); mark.setAttribute('aria-hidden', 'true'); mark.textContent = done ? '✓' : '';
    const body = node('div', 'onboarding-body');
    const heading = node('h3', '', title);
    heading.append(node('span', 'sr-only', done ? ' (готово)' : ' (ещё не сделано)'));
    body.append(heading, node('p', '', text));
    if (note) body.append(node('p', 'onboarding-note', note));
    if (controls.length) { const row = node('div', 'actions'); row.append(...controls); body.append(row); }
    item.append(mark, body); return item;
  }
  function build() {
    const section = node('section', 'onboarding'); section.id = 'onboarding';
    section.setAttribute('aria-labelledby', 'onboarding-title');
    const head = node('div', 'onboarding-head'), titles = node('div');
    const title = node('h2', '', 'Добро пожаловать в beresta'); title.id = 'onboarding-title';
    titles.append(node('p', 'eyebrow', 'Первые шаги'), title, node('p', 'onboarding-lead', 'Пока записей нет. Вот с чего начать.'));
    const hide = button('Скрыть', 'quiet', () => { writeDismissed(); render(); });
    hide.id = 'onboarding-dismiss'; hide.setAttribute('aria-label', 'Скрыть первые шаги');
    head.append(titles, hide);
    const list = node('ol', 'onboarding-steps'); list.id = 'onboarding-steps';
    section.append(head, list); return section;
  }
  function renderSteps() {
    const hasJob = [...get('jobs').children].some(row => !row.textContent.startsWith('Не получилось'));
    const first = button('Написать запись', '', focusThought); first.id = 'onboarding-write';
    const example = button('Попробовать пример', 'secondary', () => { get('example').click(); focusThought(); }); example.id = 'onboarding-example';
    const telegram = button(telegramLinked ? 'Настройки Telegram' : 'Подключить Telegram', telegramLinked ? 'quiet' : 'secondary', () => get('open-telegram').click()); telegram.id = 'onboarding-telegram';
    panel.querySelector('#onboarding-steps').replaceChildren(
      step(hasJob, 'Сделайте первую запись', hasJob ? 'Запись сохранена. Заметка появится в библиотеке, когда обработка закончится.' : 'Напишите как есть, без оформления. Или возьмите готовый пример.', hasJob ? [] : [first, example]),
      step(telegramLinked, 'Подключите Telegram', telegramLinked ? 'Telegram подключён. Записывать и получать напоминания можно через @beresta_ru_bot.' : 'Бот @beresta_ru_bot принимает записи и присылает напоминания.', [telegram]),
      step(false, 'Посмотрите исходник и задачи', 'Откройте готовую заметку. Вкладка «Исходник» хранит вашу запись без изменений, «Задачи и идеи» собирает дела, «Дополнения ИИ» показывает догадки отдельно.', [], 'Этот шаг станет доступен после первой заметки.'),
    );
  }
  function render() {
    const visible = !get('workspace').hidden && emptyLibrary && !get('capture-card').hidden && !readDismissed();
    if (!visible) { panel?.remove(); panel = null; return; }
    if (!panel) { panel = build(); column.prepend(panel); }
    renderSteps();
    if (!telegramChecked) checkTelegram();
  }
  function refreshLibrary() {
    const notes = get('notes');
    if (notes.querySelector('button')) emptyLibrary = false;
    else if (notes.querySelector('[data-empty=library]')) emptyLibrary = true;
    render();
  }
  async function checkTelegram() {
    telegramChecked = true;
    try {
      const response = await fetch('/api/v1/telegram/links', {credentials: 'same-origin'});
      if (!response.ok) return;
      const links = await response.json();
      telegramLinked = Boolean(links.identities?.length);
      if (panel) renderSteps();
    } catch { /* optional hint, the button still works */ }
  }
  function refreshTelegramFromDialog() {
    const text = get('telegram-links').textContent;
    if (/Telegram подключён|Связан Telegram ID/.test(text) && !telegramLinked) { telegramLinked = true; if (panel) renderSteps(); }
  }
  new MutationObserver(refreshLibrary).observe(get('notes'), {childList: true});
  new MutationObserver(() => { if (panel) renderSteps(); }).observe(get('jobs'), {childList: true});
  new MutationObserver(refreshTelegramFromDialog).observe(get('telegram-links'), {childList: true, subtree: true, characterData: true});
  new MutationObserver(() => { if (get('workspace').hidden) { telegramChecked = false; telegramLinked = false; emptyLibrary = false; } render(); })
    .observe(get('workspace'), {attributes: true, attributeFilter: ['hidden']});
  new MutationObserver(render).observe(get('capture-card'), {attributes: true, attributeFilter: ['hidden']});
  get('telegram-dialog').addEventListener('close', () => { telegramChecked = false; if (panel) checkTelegram(); });
  refreshLibrary();
})();
