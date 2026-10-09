'use strict';
// Note screen, source tags and filters, quiet provider banner and AI limit hints.
const assert = require('node:assert/strict'), fs = require('fs'), path = require('path'), {JSDOM} = require('jsdom');
const root = path.join(__dirname, '../app/static');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const reply = (data, status = 200) => ({ok: status < 400, status, headers: new Headers(), json: async () => structuredClone(data)});
async function settled() { for (let i = 0; i < 30; i++) await new Promise(resolve => setImmediate(resolve)); }

const cloudUsage = {simulation: false, model: 'deepseek-v4-flash', global_used: 6, global_limit: 100, global_remaining: 94, user_remaining: 50};
const summaries = [
  {id: 'n1', title: 'Из Telegram', version: 3, updated_at: 1791220000, category_id: 'c1', channel: 'telegram', input_kind: 'text'},
  {id: 'n2', title: 'Голосом', version: 1, updated_at: 1791220000, category_id: null, channel: 'telegram', input_kind: 'audio'},
  {id: 'n3', title: 'Из браузера', version: 1, updated_at: 1791220000, category_id: null, channel: 'web', input_kind: 'text'},
];

function boot({health = {simulation: false}, usage = cloudUsage, list = () => summaries, capture = null} = {}) {
  const dom = new JSDOM(read('index.html'), {url: 'https://beresta.invalid/', runScripts: 'outside-only'}), w = dom.window;
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event('close')); };
  try { Object.defineProperty(w.crypto, 'randomUUID', {value: require('node:crypto').randomUUID, configurable: true}); } catch { /* already provided */ }
  w.confirm = () => true;
  const state = {
    calls: [],
    note: {id: 'n1', capture_id: 'k1', channel: 'telegram', input_kind: 'text', title: 'Из Telegram', markdown: '## План\nПозвонить',
      original_text: 'Исходник', items: [], conclusions: [], version: 3, provider: 'cloudru', category_id: 'c1',
      category_name: 'Работа', structure_confirmed_at: null, created_at: 1791220000, updated_at: 1791220000},
  };
  w.fetch = async (input, options = {}) => {
    const url = new URL(input, w.location.href), method = options.method || 'GET';
    const body = options.body && typeof options.body === 'string' ? JSON.parse(options.body) : null;
    state.calls.push({url, method, body});
    if (url.pathname === '/health') return reply(health);
    if (url.pathname === '/api/v1/provider/usage') return reply(usage);
    if (url.pathname === '/api/v1/auth/me') return reply({username: 'tester', csrf_token: 'csrf'});
    if (url.pathname === '/api/v1/categories') return reply([{id: 'c1', name: 'Работа', version: 1}]);
    if (url.pathname === '/api/v1/jobs') return reply([]);
    if (url.pathname === '/api/v1/notes') return reply(list(url.searchParams));
    if (url.pathname === '/api/v1/captures/text') return reply(capture, 202);
    if (url.pathname === '/api/v1/notes/n1' && method === 'GET') return reply(state.note);
    if (url.pathname === '/api/v1/notes/n2' && method === 'GET') return reply({...state.note, id: 'n2', title: 'Другая'});
    if (url.pathname === '/api/v1/notes/n1' && method === 'PATCH') {
      assert.equal(body.version, state.note.version);
      Object.assign(state.note, {title: body.title, markdown: body.markdown, version: state.note.version + 1});
      return reply(state.note);
    }
    if (url.pathname === '/api/v1/notes/n1/confirm-structure') {
      Object.assign(state.note, {structure_confirmed_at: 1791220100, version: state.note.version + 1});
      return reply(state.note);
    }
    if (url.pathname.endsWith('/opened') || url.pathname.endsWith('/original-opened') || url.pathname === '/api/v1/search/events') return reply(null, 204);
    throw new Error(`Unexpected ${method} ${url.pathname}`);
  };
  w.eval(read('app.js') + '\n' + read('workspace.js'));
  return {dom, w, $: id => w.document.getElementById(id), state};
}
const key = (w, target, init) => target.dispatchEvent(new w.KeyboardEvent('keydown', {bubbles: true, cancelable: true, ...init}));
const lastList = state => state.calls.filter(c => c.url.pathname === '/api/v1/notes' && c.method === 'GET').at(-1).url.searchParams;

async function usageHints() {
  let t = boot(); await settled();
  try {
    assert.equal(t.$('mode').textContent, '', 'real provider leaves the banner empty');
    assert.ok(t.$('mode'));
    assert.equal(t.$('processing-mode').querySelector('option[value=ai]').disabled, false);
    assert.equal(t.$('ai-limit-note').hidden, true);
  } finally { t.dom.window.close(); }

  t = boot({usage: {...cloudUsage, user_remaining: 0}}); await settled();
  try {
    assert.equal(t.$('processing-mode').querySelector('option[value=ai]').disabled, true);
    assert.equal(t.$('processing-mode').value, 'manual');
    assert.match(t.$('mode').textContent, /Лимит ИИ исчерпан/);
    assert.ok(!t.$('mode').textContent.includes('Cloud.ru'));
  } finally { t.dom.window.close(); }

  t = boot({usage: {...cloudUsage, daily_unit_limit: 10, daily_units_used: 10, daily_units_remaining: 0}}); await settled();
  try {
    assert.equal(t.$('processing-mode').querySelector('option[value=ai]').disabled, true);
    assert.equal(t.$('ai-limit-note').hidden, false);
    assert.equal(t.$('ai-limit-note').textContent, 'Лимит ИИ на сегодня исчерпан. Запись сохранится без ИИ.');
    assert.equal(t.$('mode').textContent, '');
  } finally { t.dom.window.close(); }

  t = boot({usage: {...cloudUsage, daily_units_remaining: 2}}); await settled();
  try {
    assert.equal(t.$('processing-mode').querySelector('option[value=ai]').disabled, false);
    assert.equal(t.$('ai-limit-note').textContent, 'Осталось на сегодня: 2');
  } finally { t.dom.window.close(); }

  for (const remaining of [4, null, undefined, '2']) {
    t = boot({usage: {...cloudUsage, daily_units_remaining: remaining}}); await settled();
    try { assert.equal(t.$('ai-limit-note').hidden, true, String(remaining)); } finally { t.dom.window.close(); }
  }

  t = boot({health: {simulation: true}, usage: {simulation: true}}); await settled();
  try { assert.match(t.$('mode').textContent, /Демо без ИИ/); } finally { t.dom.window.close(); }

  // A capture answered without AI shows the same message, whichever flag the server uses.
  for (const flag of [{ai_limit_reached: true}, {processing_mode: 'manual'}]) {
    t = boot({capture: {id: 'j1', capture_id: 'k1', status: 'succeeded', note_id: 'n1', processing_mode: 'ai', ...flag}}); await settled();
    try {
      t.$('thought').value = 'Позвонить завтра';
      await t.$('capture-form').onsubmit({preventDefault() {}});
      await settled();
      assert.equal(t.$('ai-limit-note').textContent, 'Лимит ИИ на сегодня исчерпан. Запись сохранится без ИИ.');
      assert.equal(t.$('processing-mode').value, 'manual');
    } finally { t.dom.window.close(); }
  }
}

async function tagsAndFilters() {
  const t = boot({list: params => params.get('input_kind') === 'audio' && params.get('q') === 'нет' ? [] : summaries.filter(n =>
    (!params.get('channel') || n.channel === params.get('channel')) && (!params.get('input_kind') || n.input_kind === params.get('input_kind')))});
  await settled();
  try {
    const rows = [...t.$('notes').querySelectorAll('button')];
    assert.equal(rows.length, 3);
    assert.equal(rows[0].querySelector('.tag-telegram').textContent, 'Telegram');
    assert.ok(rows[0].querySelector('.tag-telegram svg'), 'paper plane icon');
    assert.equal(rows[0].querySelector('.tag-voice'), null);
    assert.equal(rows[1].querySelector('.tag-telegram') !== null && rows[1].querySelector('.tag-voice').textContent, 'Голос');
    assert.ok(rows[1].querySelector('.tag-voice svg'), 'microphone icon');
    assert.equal(rows[2].querySelector('.tag'), null, 'web text notes carry no tag');

    const chips = [...t.$('source-filter').querySelectorAll('button')];
    assert.deepEqual(chips.map(c => c.textContent), ['Все', 'Telegram', 'Голос', 'Текст']);
    const chip = name => chips.find(c => c.textContent === name);
    chip('Голос').click(); await settled();
    assert.equal(lastList(t.state).get('input_kind'), 'audio'); assert.equal(lastList(t.state).get('channel'), null);
    assert.equal(chip('Голос').getAttribute('aria-pressed'), 'true'); assert.equal(chip('Все').getAttribute('aria-pressed'), 'false');
    assert.equal(t.$('notes').querySelectorAll('button').length, 1);
    chip('Telegram').click(); await settled();
    assert.equal(lastList(t.state).get('channel'), 'telegram'); assert.equal(lastList(t.state).get('input_kind'), null);
    assert.equal(t.$('notes').querySelectorAll('button').length, 2);
    chip('Текст').click(); await settled();
    assert.equal(lastList(t.state).get('input_kind'), 'text'); assert.equal(lastList(t.state).get('channel'), null);

    // The source filter combines with search and category.
    t.$('search-query').value = 'нет'; t.$('category-filter').value = 'c1';
    chip('Голос').click(); await settled();
    assert.equal(lastList(t.state).get('q'), null, 'a chip does not submit unsent search text');
    t.$('search-form').dispatchEvent(new t.w.Event('submit', {bubbles: true, cancelable: true})); await settled();
    const params = lastList(t.state);
    assert.deepEqual([params.get('q'), params.get('category_id'), params.get('input_kind')], ['нет', 'c1', 'audio']);
    assert.ok(t.$('notes').querySelector('[data-empty=search]'), 'an empty filtered list uses the search hint');
    assert.equal(t.$('notes').querySelector('[data-empty=library]'), null);

    t.$('clear-search').click(); await settled();
    assert.equal(lastList(t.state).get('input_kind'), null);
    assert.equal(chip('Все').getAttribute('aria-pressed'), 'true');
    assert.equal(t.$('notes').querySelectorAll('button').length, 3);
  } finally { t.dom.window.close(); }

  const empty = boot({list: () => []}); await settled();
  try {
    assert.ok(empty.$('notes').querySelector('[data-empty=library]'), 'an empty library keeps the library hint');
    [...empty.$('source-filter').querySelectorAll('button')].find(c => c.textContent === 'Telegram').click(); await settled();
    assert.ok(empty.$('notes').querySelector('[data-empty=search]'));
  } finally { empty.dom.window.close(); }
}

async function noteScreen() {
  const t = boot(), {w, $} = t; await settled();
  try {
    $('notes').querySelector('button').click(); await settled();
    assert.equal(w.document.querySelector('[data-note-view]'), null, 'tabs are gone');
    assert.equal($('view-edit'), null);
    assert.equal($('title').value, 'Из Telegram'); assert.equal($('note-heading-title').textContent, 'Из Telegram');
    assert.equal($('note-tags').querySelector('.tag-telegram').textContent, 'Telegram');
    assert.match($('note-mode').textContent, /Обработано ИИ/); assert.match($('note-mode').textContent, /v3/);
    assert.ok(!$('note-card').textContent.includes('Cloud.ru'));
    assert.notEqual($('note-date').textContent, '');
    assert.equal($('note-category').value, 'c1');
    assert.equal($('view-tasks').hidden, false); assert.equal($('view-reminders').hidden, false);
    assert.equal($('item-form').closest('#view-tasks') !== null, true);
    assert.equal($('reminders-panel').closest('#view-reminders') !== null, true);
    assert.equal($('confirm-structure').disabled, false);
    assert.equal($('preview').hidden, false); assert.equal($('markdown').hidden, true); assert.equal($('note-edit-actions').hidden, true);

    // Click on the text opens the editor in place.
    $('preview').click();
    assert.equal($('markdown').hidden, false); assert.equal($('preview').hidden, true); assert.equal($('note-edit-actions').hidden, false);
    assert.equal(w.document.activeElement, $('markdown'));
    $('markdown').value = 'Черновик'; $('markdown').dispatchEvent(new w.Event('input', {bubbles: true}));
    // Esc asks before it drops changes.
    w.confirm = () => false; key(w, $('markdown'), {key: 'Escape'});
    assert.equal($('markdown').hidden, false); assert.equal($('markdown').value, 'Черновик');
    w.confirm = () => true; key(w, $('markdown'), {key: 'Escape'});
    assert.equal($('markdown').hidden, true); assert.equal($('preview').hidden, false);
    assert.equal($('markdown').value, '## План\nПозвонить'); assert.equal($('note-edit-actions').hidden, true);

    // The button opens it too. Cancel works without a prompt when nothing changed.
    w.confirm = () => { throw new Error('no prompt expected'); };
    $('note-edit-start').click(); assert.equal($('markdown').hidden, false);
    $('note-edit-cancel').click(); assert.equal($('markdown').hidden, true);
    w.confirm = () => true;

    // Ctrl+Enter saves with the version check.
    $('note-edit-start').click(); $('markdown').value = 'Новый текст';
    key(w, $('markdown'), {key: 'Enter', ctrlKey: true}); await settled();
    const save = t.state.calls.filter(c => c.method === 'PATCH').at(-1);
    assert.deepEqual([save.body.version, save.body.markdown, save.body.title], [3, 'Новый текст', 'Из Telegram']);
    assert.equal($('markdown').hidden, true); assert.equal($('preview').textContent, 'Новый текст');
    assert.match($('note-mode').textContent, /v4/);
    assert.equal($('title').disabled, false);

    // A new title shows the save buttons without opening the text editor, and Save sends it.
    $('title').value = 'Новое имя'; $('title').dispatchEvent(new w.Event('input', {bubbles: true}));
    assert.equal($('note-edit-actions').hidden, false); assert.equal($('markdown').hidden, true);
    $('note-save').click(); await settled();
    assert.deepEqual(t.state.calls.filter(c => c.method === 'PATCH').at(-1).body.title, 'Новое имя');
    assert.equal($('note-edit-actions').hidden, true);
    assert.equal($('note-heading-title').textContent, 'Новое имя');

    // Unsaved text blocks switching to another note until the person agrees.
    $('note-edit-start').click(); $('markdown').value = 'Не терять';
    w.confirm = () => false;
    const readsBefore = t.state.calls.filter(c => c.url.pathname === '/api/v1/notes/n2').length;
    [...$('notes').querySelectorAll('button')][1].click(); await settled();
    assert.equal(t.state.calls.filter(c => c.url.pathname === '/api/v1/notes/n2').length, readsBefore);
    assert.equal($('markdown').value, 'Не терять'); assert.equal($('markdown').hidden, false);
    w.confirm = () => true;
    [...$('notes').querySelectorAll('button')][1].click(); await settled();
    assert.equal($('title').value, 'Другая'); assert.equal($('markdown').hidden, true);

    // Secondary panels: one open at a time, toggled by buttons.
    const panel = name => w.document.querySelector(`[data-note-panel=${name}]`);
    assert.equal(w.document.querySelectorAll('#note-toolbar button').length, 3);
    for (const name of ['original', 'insights', 'history']) {
      assert.equal(panel(name).getAttribute('aria-expanded'), 'false'); assert.equal($(`view-${name}`).hidden, true);
      assert.equal(panel(name).getAttribute('aria-controls'), `view-${name}`);
    }
    panel('history').click();
    assert.equal($('view-history').hidden, false); assert.equal(panel('history').getAttribute('aria-expanded'), 'true');
    panel('original').click(); await settled();
    assert.equal($('view-original').hidden, false); assert.equal($('view-history').hidden, true);
    assert.equal(panel('history').getAttribute('aria-expanded'), 'false');
    assert.equal($('original-details').open, true);
    assert.equal($('workspace').dataset.noteView, 'original');
    panel('original').click();
    assert.equal($('view-original').hidden, true); assert.equal($('workspace').dataset.noteView, 'read');
    panel('insights').click(); assert.equal($('view-insights').hidden, false);
    // Opening another note closes the panel.
    [...$('notes').querySelectorAll('button')][0].click(); await settled();
    assert.equal($('view-insights').hidden, true);

    $('confirm-structure').click(); await settled();
    assert.equal($('confirm-structure').disabled, true);
  } finally { await new Promise(resolve => w.setTimeout(resolve, 10)); t.dom.window.close(); }
}

module.exports = async () => {
  await usageHints();
  await tagsAndFilters();
  await noteScreen();
  console.log('Note UX checks passed: quiet banner, AI limit hints, source tags and filters, inline editing, panels.');
};
if (require.main === module) module.exports().catch(error => { console.error(error); process.exitCode = 1; });
