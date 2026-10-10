'use strict';
// Focus mode. Autosave, the title that fits, icon panels with popovers, import into the capture field, no footer.
const assert = require('node:assert/strict'), fs = require('fs'), path = require('path'), {JSDOM} = require('jsdom');
const root = path.join(__dirname, '../app/static');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const reply = (data, status = 200) => ({ok: status < 400, status, headers: new Headers(), json: async () => structuredClone(data)});
// Let pending observers and timers finish before the document goes away.
async function closeWindow(t) {
  for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve));
  t.dom.window.close();
}

async function settled() { for (let i = 0; i < 30; i++) await new Promise(resolve => setImmediate(resolve)); }
const summaries = [
  {id: 'n1', title: 'Заметка', version: 3, updated_at: 1791220000, category_id: null, channel: 'web', input_kind: 'text'},
  {id: 'n2', title: 'Другая', version: 1, updated_at: 1791220000, category_id: null, channel: 'web', input_kind: 'text'},
];

function boot() {
  const dom = new JSDOM(read('index.html'), {url: 'https://beresta.invalid/', runScripts: 'outside-only'}), w = dom.window;
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event('close')); };
  try { Object.defineProperty(w.crypto, 'randomUUID', {value: require('node:crypto').randomUUID, configurable: true}); } catch { /* already provided */ }
  w.confirm = () => true;
  w.noteAutosaveMs = 10; w.noteAutosaveRetryMs = 600000;
  const state = {
    calls: [], patches: [], active: 0, maxActive: 0, patchHook: null,
    note: {id: 'n1', capture_id: 'k1', channel: 'web', input_kind: 'text', title: 'Заметка', markdown: 'Первый текст',
      original_text: 'Исходник', conclusions: [], version: 3, provider: 'manual', category_id: null,
      category_name: null, structure_confirmed_at: null, created_at: 1791220000, updated_at: 1791220000,
      items: [{id: 'i1', kind: 'task', text: 'Позвонить', status: 'open', version: 1},
        {id: 'i2', kind: 'task', text: 'Купить', status: 'completed', version: 1}]},
  };
  w.fetch = async (input, options = {}) => {
    const url = new URL(input, w.location.href), method = options.method || 'GET';
    const body = options.body && typeof options.body === 'string' ? JSON.parse(options.body) : null;
    state.calls.push({url, method, body});
    if (url.pathname === '/health' || url.pathname === '/api/v1/provider/usage') return reply({simulation: true});
    if (url.pathname === '/api/v1/auth/me') return reply({username: 'tester', csrf_token: 'csrf'});
    if (url.pathname === '/api/v1/categories') return reply([]);
    if (url.pathname === '/api/v1/jobs') return reply([]);
    if (url.pathname === '/api/v1/notes') return reply(summaries);
    if (url.pathname === '/api/v1/notes/n1/revisions') return reply([]);
    if (url.pathname === '/api/v1/notes/n1' && method === 'GET') return reply(state.note);
    if (url.pathname === '/api/v1/notes/n2' && method === 'GET') return reply({...state.note, id: 'n2', title: 'Другая', version: 1});
    if (url.pathname === '/api/v1/notes/n1' && method === 'PATCH') {
      state.patches.push(body); state.active++; state.maxActive = Math.max(state.maxActive, state.active);
      try {
        if (state.patchHook) { const early = await state.patchHook(body); if (early) return early; }
        if (body.version !== state.note.version) return reply({detail: 'Заметка уже изменена. Обновите её перед сохранением'}, 409);
        Object.assign(state.note, {title: body.title, markdown: body.markdown, version: state.note.version + 1});
        return reply(state.note);
      } finally { state.active--; }
    }
    if (url.pathname.endsWith('/opened') || url.pathname.endsWith('/original-opened') || url.pathname === '/api/v1/search/events') return reply(null, 204);
    throw new Error(`Unexpected ${method} ${url.pathname}`);
  };
  w.eval(read('app.js') + '\n' + read('workspace.js') + '\n' + read('focus.js'));
  const wait = ms => new Promise(resolve => w.setTimeout(resolve, ms));
  const until = async (check, what) => { for (let i = 0; i < 200; i++) { if (check()) return; await wait(5); } throw new Error(`Timed out: ${what}`); };
  const $ = id => w.document.getElementById(id);
  const type = (field, value) => { field.value = value; field.dispatchEvent(new w.Event('input', {bubbles: true})); };
  return {dom, w, $, state, wait, until, type};
}
async function openFirst(t) {
  await settled(); t.$('notes').querySelector('button').click(); await settled();
  assert.equal(t.$('markdown').value, 'Первый текст');
}
const keydown = (w, target, init) => target.dispatchEvent(new w.KeyboardEvent('keydown', {bubbles: true, cancelable: true, ...init}));

async function autosaveDebounce() {
  const t = boot(); const {$, state, wait, type} = t;
  try {
    await openFirst(t);
    // Several keystrokes make one request with the stored version.
    type($('markdown'), 'П'); type($('markdown'), 'Пр'); type($('markdown'), 'Привет');
    assert.equal(state.patches.length, 0, 'nothing is sent while typing');
    assert.equal($('note-save-text').textContent, 'Сохраняется…');
    await wait(60); await settled();
    assert.equal(state.patches.length, 1);
    assert.deepEqual([state.patches[0].version, state.patches[0].markdown, state.patches[0].title], [3, 'Привет', 'Заметка']);
    assert.equal($('note-save-text').textContent, 'Сохранено');
    assert.equal($('markdown').value, 'Привет'); assert.equal($('markdown').disabled, false);
    // An unchanged text sends nothing, also after typing and deleting the same letter.
    type($('markdown'), 'Привет!'); type($('markdown'), 'Привет');
    await wait(60); await settled();
    assert.equal(state.patches.length, 1);
    // The title goes the same way and the next save carries the new version.
    type($('title'), 'Новое имя');
    await wait(60); await settled();
    assert.equal(state.patches.length, 2);
    assert.deepEqual([state.patches[1].version, state.patches[1].title, state.patches[1].markdown], [4, 'Новое имя', 'Привет']);
    // An empty text is not sent. The status says why.
    type($('markdown'), '  ');
    await wait(60); await settled();
    assert.equal(state.patches.length, 2); assert.match($('note-save-text').textContent, /пустая/);
    // A title with a line break stays on one line.
    type($('title'), 'Две\nстроки');
    assert.equal($('title').value, 'Две строки');
  } finally { await closeWindow(t); }
}

async function idleFlushDoesNotLockAutosave() {
  const t = boot(); const {w, $, state, wait, type} = t;
  try {
    await openFirst(t);
    assert.equal(await w.BerestaFocus.flush(), true);
    assert.equal(state.patches.length, 0, 'An unchanged note should not be patched');
    type($('markdown'), 'После пустой проверки');
    await wait(60); await settled();
    assert.equal(state.patches.length, 1, 'An idle flush must not block the next autosave');
    assert.equal(state.note.markdown, 'После пустой проверки');
    assert.equal(await w.BerestaFocus.flush(), true);
    type($('title'), 'После сохранения');
    await wait(60); await settled();
    assert.equal(state.patches.length, 2);
    assert.equal(state.patches[1].version, 4);
  } finally { await closeWindow(t); }
}

async function overlappingSaves() {
  const t = boot(); const {$, state, wait, until, type} = t;
  try {
    await openFirst(t);
    let release; const gate = new Promise(resolve => { release = resolve; });
    state.patchHook = async body => { if (body.markdown === 'A') await gate; return null; };
    type($('markdown'), 'A');
    await until(() => state.patches.length === 1, 'first request');
    type($('markdown'), 'AB');
    await wait(60);
    assert.equal(state.patches.length, 1, 'no second request while the first is open');
    release(); await until(() => state.patches.length === 2 && state.active === 0, 'second request'); await settled();
    assert.equal(state.maxActive, 1, 'one request at a time');
    assert.deepEqual(state.patches.map(p => [p.version, p.markdown]), [[3, 'A'], [4, 'AB']]);
    assert.equal(state.note.markdown, 'AB'); assert.equal($('markdown').value, 'AB');
    assert.equal($('note-save-text').textContent, 'Сохранено');
  } finally { await closeWindow(t); }
}

async function conflictKeepsText() {
  const t = boot(); const {w, $, state, wait, type} = t;
  try {
    await openFirst(t);
    state.patchHook = async () => reply({detail: 'Заметка уже изменена. Обновите её перед сохранением'}, 409);
    type($('markdown'), 'Мой текст');
    await wait(60); await settled();
    assert.equal(state.patches.length, 1);
    assert.equal($('markdown').value, 'Мой текст', 'typed text stays');
    assert.equal($('note-save-status').dataset.state, 'conflict');
    assert.match($('note-save-text').textContent, /изменили в другом месте/);
    assert.match($('message').textContent, /Заметка уже изменена/);
    assert.equal($('note-reload').hidden, false);
    assert.equal(w.BerestaFocus.pending(), false, 'no more attempts after a conflict');
    type($('markdown'), 'Мой текст ещё'); await wait(60); await settled();
    assert.equal(state.patches.length, 1); assert.equal($('markdown').value, 'Мой текст ещё');
    // The person can load the server version on purpose.
    w.confirm = () => false; $('note-reload').click(); await settled();
    assert.equal($('markdown').value, 'Мой текст ещё', 'declined, nothing is replaced');
    w.confirm = () => true; $('note-reload').click(); await settled();
    assert.equal($('markdown').value, 'Первый текст'); assert.equal($('note-reload').hidden, true);
    assert.equal($('note-save-text').textContent, '');
  } finally { await closeWindow(t); }
}

async function failedSaveAndLeaving() {
  const t = boot(); const {w, $, state, wait, type} = t;
  try {
    await openFirst(t);
    // A server error says it will retry and keeps the text. Leaving then asks, because the text is not saved.
    state.patchHook = async () => reply({detail: 'Сбой'}, 500);
    type($('markdown'), 'Черновик');
    await wait(60); await settled();
    assert.equal($('note-save-text').textContent, 'Не сохранено, повторим');
    let asked = 0; w.confirm = () => { asked++; return false; };
    const reads = () => state.calls.filter(c => c.url.pathname === '/api/v1/notes/n2').length;
    [...$('notes').querySelectorAll('button')][1].click(); await settled();
    assert.equal(asked, 1); assert.equal(reads(), 0); assert.equal($('markdown').value, 'Черновик');
    // Once the server is back, leaving saves first and asks nothing.
    state.patchHook = null; w.confirm = () => { throw new Error('no prompt expected'); };
    [...$('notes').querySelectorAll('button')][1].click(); await settled();
    assert.equal(state.note.markdown, 'Черновик'); assert.equal(reads(), 1); assert.equal($('title').value, 'Другая');
  } finally { await closeWindow(t); }

  // The same for the "new record" button and the section links.
  const u = boot(); const {w: w2, $: $2, state: s2, type: type2} = u;
  try {
    await openFirst(u);
    w2.confirm = () => { throw new Error('no prompt expected'); };
    type2($2('markdown'), 'Перед новой записью');
    $2('new-note').click(); await settled();
    assert.equal(s2.patches.length, 1); assert.equal(s2.patches[0].markdown, 'Перед новой записью');
    assert.equal($2('capture-card').hidden, false); assert.equal($2('note-card').hidden, true);
  } finally { await closeWindow(u); }
}

async function titleFit() {
  const t = boot(); const {w, $} = t;
  try {
    await openFirst(t);
    const heading = $('title').closest('.note-heading');
    // jsdom has no layout, so the measure is a stub: lines at a given font size.
    let result = w.BerestaFocus.fitTitle(() => 3);
    assert.equal(heading.classList.contains('is-clamped'), true, 'too long even at the minimum');
    assert.deepEqual({...result}, {size: 20, clamped: true}); assert.equal($('title').style.fontSize, '20px');
    result = w.BerestaFocus.fitTitle(size => (size > 28 ? 3 : 2));
    assert.deepEqual({...result}, {size: 28, clamped: false}); assert.equal($('title').style.fontSize, '28px');
    assert.equal(heading.classList.contains('is-clamped'), false);
    result = w.BerestaFocus.fitTitle(() => 1);
    assert.deepEqual({...result}, {size: 36, clamped: false}, 'a short title keeps the full size');
    // Phones use a smaller range.
    Object.defineProperty(w, 'innerWidth', {value: 400, configurable: true});
    assert.deepEqual({...w.BerestaFocus.fitTitle(() => 3)}, {size: 18, clamped: true});
    assert.deepEqual({...w.BerestaFocus.fitTitle(() => 1)}, {size: 26, clamped: false});
    // Typing fits again and mirrors the text for the clamped view.
    $('title').value = 'Очень длинный заголовок'; $('title').dispatchEvent(new w.Event('input', {bubbles: true}));
    assert.equal($('title-display').textContent, 'Очень длинный заголовок');
    // Resize fits again too (jsdom has no ResizeObserver, so the window event is used).
    $('title').style.fontSize = '10px'; w.dispatchEvent(new w.Event('resize'));
    assert.notEqual($('title').style.fontSize, '10px');
  } finally { await closeWindow(t); }
}

async function popovers() {
  const t = boot(); const {w, $} = t;
  try {
    await openFirst(t);
    const noteTools = ['Напоминания', 'Задачи и идеи', 'Связанные заметки', 'Свойства', 'Исходник', 'Дополнения ИИ', 'Версии'];
    const buttons = [...$('note-tools').querySelectorAll('.tool-btn')];
    assert.deepEqual(buttons.map(b => b.dataset.tip), noteTools, 'tooltip text');
    for (const b of [...buttons, ...$('capture-tools').querySelectorAll('.tool-btn')]) {
      assert.ok(b.getAttribute('aria-label'), 'aria-label');
      assert.ok(b.getAttribute('aria-label').startsWith(b.dataset.tip), 'name matches the tooltip');
      assert.equal(b.getAttribute('aria-expanded'), 'false'); assert.equal(b.getAttribute('aria-haspopup'), 'dialog');
      assert.equal(b.getAttribute('title'), null, 'the tooltip is custom, not only title');
      assert.equal(w.document.getElementById(b.getAttribute('aria-controls')).hidden, true);
    }
    assert.deepEqual([...$('capture-tools').querySelectorAll('.tool-btn')].map(b => b.dataset.tip), ['Голос', 'Импорт', 'История']);
    // The existing sections moved into the popovers, ids unchanged.
    for (const [id, pop] of [['view-reminders', 'reminders'], ['reminder-form', 'reminders'], ['view-tasks', 'tasks'], ['item-form', 'tasks'], ['view-related', 'related'],
      ['note-category', 'props'], ['confirm-structure', 'props'], ['view-original', 'original'], ['view-insights', 'insights'], ['view-history', 'history']]) {
      assert.equal($(id).closest(`#pop-${pop}`) !== null, true, `${id} in ${pop}`);
    }
    assert.equal($('capture-audio').closest('#pop-voice') !== null, true); assert.equal($('capture-jobs').closest('#pop-jobs') !== null, true);

    // Open, focus goes in, Esc closes and focus comes back.
    $('tool-props').click();
    assert.equal($('pop-props').hidden, false); assert.equal($('tool-props').getAttribute('aria-expanded'), 'true');
    assert.equal($('pop-props').contains(w.document.activeElement), true, 'focus moved into the popover');
    keydown(w, $('pop-props'), {key: 'Escape'});
    assert.equal($('pop-props').hidden, true); assert.equal($('tool-props').getAttribute('aria-expanded'), 'false');
    assert.equal(w.document.activeElement, $('tool-props'), 'focus returned');
    // The same button closes it.
    $('tool-props').click(); $('tool-props').click();
    assert.equal($('pop-props').hidden, true);
    // A click outside closes it. A click inside does not.
    $('tool-tasks').click(); assert.equal($('pop-tasks').hidden, false);
    $('items').click(); assert.equal($('pop-tasks').hidden, false, 'inside');
    $('markdown').click(); assert.equal($('pop-tasks').hidden, true, 'outside');
    // One popover at a time.
    $('tool-tasks').click(); $('tool-history').click();
    assert.equal($('pop-tasks').hidden, true); assert.equal($('pop-history').hidden, false);
    assert.equal(w.document.querySelectorAll('.tool-pop:not([hidden])').length, 1);
    // The close button works. An Escape that something else handled does not close it.
    $('pop-history').querySelector('.pop-close').click(); assert.equal($('pop-history').hidden, true);
    $('tool-tasks').click();
    const handled = new w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true, cancelable: true});
    $('item-text').addEventListener('keydown', event => event.preventDefault(), {once: true}); $('item-text').dispatchEvent(handled);
    assert.equal($('pop-tasks').hidden, false);
    // Switching notes closes the popover.
    [...$('notes').querySelectorAll('button')][1].click(); await settled();
    assert.equal($('pop-tasks').hidden, true);
    // Popovers belong to a screen: the capture ones close with it.
    $('new-note').click(); await settled(); $('tool-import').click();
    assert.equal($('pop-import').hidden, false);
    [...$('notes').querySelectorAll('button')][0].click(); await settled();
    assert.equal($('capture-card').hidden, true); assert.equal($('pop-import').hidden, true);
  } finally { await closeWindow(t); }
}

async function badges() {
  const t = boot(); const {w, $} = t;
  try {
    await openFirst(t);
    assert.equal($('badge-tasks').textContent, '1/2'); assert.equal($('badge-tasks').hidden, false);
    assert.equal($('tool-tasks').getAttribute('aria-label'), 'Задачи и идеи: 1/2');
    assert.equal($('badge-reminders').hidden, true); assert.equal($('dot-reminders').hidden, true);
    assert.equal($('tool-related').closest('.tool').hidden, true, 'no related notes, no button');
    // Related notes show the button with a count.
    const item = w.document.createElement('li'); $('related-list').append(item); $('view-related').hidden = false; await settled();
    assert.equal($('tool-related').closest('.tool').hidden, false); assert.equal($('badge-related').textContent, '1');
    // A proposed reminder lights the dot on the bell.
    const proposal = w.document.createElement('div'); proposal.className = 'br-propose'; $('items').append(proposal); await settled();
    assert.equal($('dot-reminders').hidden, false);
    // Active reminders are counted, cancelled ones are not.
    for (const cls of ['reminder-row', 'reminder-row is-cancelled', 'reminder-row']) {
      const row = w.document.createElement('article'); row.className = cls; $('reminders-list').append(row);
    }
    await settled(); assert.equal($('badge-reminders').textContent, '2');
    // The bell on a form that opens brings its popover.
    $('reminder-form').hidden = false; await settled();
    assert.equal($('pop-reminders').hidden, false);
  } finally { await closeWindow(t); }
}

async function importIntoCapture() {
  const t = boot(); const {w, $} = t;
  const pick = file => {
    Object.defineProperty($('import-file'), 'files', {value: file ? [file] : [], configurable: true});
    $('import-file').dispatchEvent(new w.Event('change', {bubbles: true}));
  };
  const textFile = (text, name = 'мысли.txt', type = 'text/plain') => new w.File([text], name, {type});
  try {
    await settled(); $('new-note').click(); await settled();
    assert.equal($('import-file').accept, '.txt,.md,text/plain,text/markdown');
    $('tool-import').click();
    // A text file fills the field and focuses it.
    pick(textFile('Привет\r\nмир\r\n')); await t.until(() => $('thought').value !== '', 'file read');
    assert.equal($('thought').value, 'Привет\nмир'); assert.equal(w.document.activeElement, $('thought'));
    assert.equal($('pop-import').hidden, true); assert.match($('message').textContent, /Текст из файла добавлен/);
    // A second file is added after a blank line.
    $('tool-import').click(); pick(textFile('# Заметка', 'a.md', 'text/markdown'));
    await t.until(() => $('thought').value.includes('# Заметка'), 'second file');
    assert.equal($('thought').value, 'Привет\nмир\n\n# Заметка');
    // Too long, in total or alone.
    $('thought').value = '';
    $('tool-import').click(); pick(textFile('я'.repeat(12001)));
    await t.until(() => $('import-status').textContent !== '', 'long file');
    assert.match($('import-status').textContent, /12000/); assert.equal($('import-status').classList.contains('is-error'), true);
    assert.equal($('thought').value, ''); assert.equal($('pop-import').hidden, false, 'stays open to show the error');
    pick(textFile('я'.repeat(60000)));
    assert.match($('import-status').textContent, /слишком большой/);
    $('thought').value = 'ю'.repeat(11000);
    pick(textFile('я'.repeat(2000)));
    await t.until(() => /вместе/.test($('import-status').textContent), 'sum too long');
    assert.equal($('thought').value.length, 11000);
    $('thought').value = '';
    // Not text.
    pick(textFile('x', 'photo.png', 'image/png')); assert.match($('import-status').textContent, /текстовый файл/);
    pick(textFile('a\u0000b')); await t.until(() => /UTF-8/.test($('import-status').textContent), 'binary');
    pick(textFile('  \n ')); await t.until(() => /пустой/.test($('import-status').textContent), 'empty');
    assert.equal($('thought').value, '');
    // Clipboard: without the API the person is told to paste by hand.
    $('import-paste').click(); await settled();
    assert.match($('import-status').textContent, /Ctrl\+V/); assert.match($('message').textContent, /Ctrl\+V/);
    assert.equal($('pop-import').hidden, true); assert.equal(w.document.activeElement, $('thought'));
    // With the API the text goes in. A refusal falls back to the same hint.
    Object.defineProperty(w.navigator, 'clipboard', {value: {readText: async () => 'Из буфера'}, configurable: true});
    $('tool-import').click(); $('import-paste').click(); await settled();
    assert.equal($('thought').value, 'Из буфера');
    Object.defineProperty(w.navigator, 'clipboard', {value: {readText: async () => { throw new Error('denied'); }}, configurable: true});
    $('message').textContent = ''; $('tool-import').click(); $('import-paste').click(); await settled();
    assert.match($('message').textContent, /Ctrl\+V/); assert.equal($('thought').value, 'Из буфера');
  } finally { await closeWindow(t); }
}

async function noFooter() {
  const t = boot(); const {w, $} = t;
  try {
    await settled();
    assert.equal($('app-footer'), null); assert.equal(w.document.querySelector('footer'), null);
    assert.ok(!w.document.body.textContent.includes('Текст и аудио. Оригиналы остаются у вас.'));
    assert.equal($('capture-tabs'), null);
    for (const id of ['capture-form', 'capture-audio', 'capture-jobs', 'thought', 'processing-mode', 'ai-switch', 'capture-submit', 'example']) assert.ok($(id), id);
  } finally { await closeWindow(t); }
}

module.exports = async () => {
  await idleFlushDoesNotLockAutosave();
  await autosaveDebounce();
  await overlappingSaves();
  await conflictKeepsText();
  await failedSaveAndLeaving();
  await titleFit();
  await popovers();
  await badges();
  await importIntoCapture();
  await noFooter();
  console.log('Focus checks passed: autosave, one request at a time, conflict, flush on leaving, title fit, popovers, badges, import, no footer.');
};
if (require.main === module) module.exports().catch(error => { console.error(error); process.exitCode = 1; });
