'use strict';
// Second brain screens: navigation, overview, assistant, related notes and suggested reminders.
const assert = require('node:assert/strict'), fs = require('fs'), path = require('path'), {JSDOM} = require('jsdom');
const root = path.join(__dirname, '../app/static');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const reply = (data, status = 200) => ({ok: status < 400, status, headers: new Headers(), json: async () => structuredClone(data)});
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(condition, what = 'condition') {
  for (let i = 0; i < 600; i++) {
    if (condition()) return;
    await sleep(5);
  }
  throw new Error(`Timed out waiting for ${what}`);
}

function dashboard(days, {empty = false} = {}) {
  const rows = [];
  for (let i = 0; i < days; i++) {
    const date = new Date(Date.UTC(2026, 9, 10 - (days - 1 - i)));
    rows.push({date: date.toISOString().slice(0, 10), notes: empty ? 0 : i % 4});
  }
  const total = rows.reduce((sum, row) => sum + row.notes, 0);
  return {
    days, timezone: 'Europe/Moscow', period: {from: rows[0].date, to: rows.at(-1).date},
    totals: {notes: empty ? 0 : 84, notes_in_period: total},
    captures: {total: empty ? 0 : total, by_channel: {web: 5, telegram: 2}, by_input_kind: {text: 6, audio: 1}, ai: 4, manual: 3},
    notes_per_day: rows,
    tasks: {open: 4, completed: 6, completed_in_period: 5, completion_rate: 0.6}, ideas: 3,
    categories: [{category_id: 'c1', name: 'Работа', notes: 9, tasks_total: 8, tasks_done: 3},
      {category_id: 'c2', name: 'Идеи', notes: 4, tasks_total: 0, tasks_done: 0}],
    streak_days: 6,
    reminders: {upcoming_7d: 2, next_at: '2026-10-11T06:00:00+00:00', next_timezone: 'Europe/Moscow'},
    ai: {units_used: 3, units_limit: 30, units_remaining: 27, resets_at: '2026-10-11T00:00:00+03:00'},
  };
}
const askRow = {
  id: 'a1', kind: 'ask', status: 'succeeded', question: 'Что я обещал Марку?', days: 90, units: 1,
  error_code: null, error_message: null, created_at: 1791220000, input_note_ids: ['n1', 'n2'],
  result: {answer_markdown: 'Вы обещали <b>макет</b>.\n- Прислать макет\n- Позвонить',
    citations: [{note_id: 'n1', quote: 'прислать макет', note_title: 'План'}, {note_id: 'n2', quote: 'позвонить', note_title: 'Другая'}]},
};
const recRow = {
  id: 'r1', kind: 'recommend', status: 'succeeded', question: null, days: null, units: 1, error_code: null, error_message: null,
  created_at: 1791220100, input_note_ids: ['n1'],
  result: {suggestions: [
    {kind: 'task', title: 'Позвать людей', text: 'Позовите десять знакомых.', note_ids: ['n1'], quote: 'Позвать', notes: [{id: 'n1', title: 'План'}]},
    {kind: 'habit', title: 'Вечерний обзор', text: 'Разбирайте задачи вечером.', note_ids: [], quote: '', notes: []},
  ]},
};
const digestRow = {
  id: 'd1', kind: 'digest', status: 'succeeded', question: null, days: 7, units: 1, error_code: null, error_message: null,
  created_at: 1791220200, input_note_ids: ['n1'],
  result: {summary_markdown: 'Неделя прошла в планах.', highlights: [{note_id: 'n1', quote: 'план', note_title: 'План'}],
    open_tasks: [{id: 'i1', text: 'Позвонить маме', note_id: 'n1', note_title: 'План'}], themes: ['запуск', 'отзывы']},
};

function boot({url = 'https://beresta.invalid/', usage = {}, settings = true, days = dashboard, related = null} = {}) {
  const dom = new JSDOM(read('index.html'), {url, runScripts: 'outside-only'}), w = dom.window;
  const $ = id => w.document.getElementById(id);
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event('close')); };
  try { Object.defineProperty(w.crypto, 'randomUUID', {value: require('node:crypto').randomUUID, configurable: true}); } catch { /* provided */ }
  w.confirm = () => true;
  w.assistantPollMs = 5; w.reminderDebounceMs = 0;
  const task = {id: 'i1', kind: 'task', text: 'Позвонить маме', status: 'open', version: 1, due_text: 'завтра в 10', due_at: null,
    proposed_reminder: {local_time: '2026-10-11T10:00:00', timezone: 'Europe/Moscow', label: 'завтра в 10:00'}};
  const state = {
    calls: [], settings, polls: 0, queued: 2, askError: null, task, hold: null,
    usage: {simulation: false, daily_unit_limit: 30, daily_units_used: 3, daily_units_remaining: 27,
      limit_resets_at: new Date(Date.now() + 3 * 3600000).toISOString(), ...usage},
    related: related || [{note_id: 'n2', title: 'Другая', category_id: null, updated_at: 1791220000, score: 0.3, shared_terms: ['макет', 'запуск']}],
    dashboard: {fn: days, empty: false},
  };
  const note = id => ({id, capture_id: `k-${id}`, channel: 'web', input_kind: 'text', title: id === 'n1' ? 'План' : 'Другая',
    markdown: '## Текст', original_text: 'Исходник', items: id === 'n1' ? [state.task] : [], conclusions: [], version: 1,
    provider: 'manual', category_id: null, structure_confirmed_at: null, created_at: 1791220000, updated_at: 1791220000});
  w.fetch = async (input, options = {}) => {
    const u = new URL(input, w.location.href), method = options.method || 'GET', p = u.pathname;
    const body = options.body && typeof options.body === 'string' ? JSON.parse(options.body) : null;
    state.calls.push({url: u, method, body, headers: options.headers || {}});
    if (p === '/health') return reply({simulation: false});
    if (p === '/api/v1/auth/me') return reply({username: 'tester', csrf_token: 'csrf'});
    if (p === '/api/v1/provider/usage') return reply(state.usage);
    if (p === '/api/v1/categories' || p === '/api/v1/jobs') return reply([]);
    if (p === '/api/v1/notes') return reply([{id: 'n1', title: 'План', version: 1, updated_at: 1791220000}, {id: 'n2', title: 'Другая', version: 1, updated_at: 1791220000}]);
    if (/^\/api\/v1\/captures\/k-?[\w-]*$/.test(p) && method === 'GET') return reply({capture_id: 'k1', note_id: 'n1', input_kind: 'text'});
    if (/^\/api\/v1\/notes\/n[12]$/.test(p) && method === 'GET') return reply(note(p.slice(-2)));
    if (/^\/api\/v1\/notes\/n[12]\/related$/.test(p)) return reply({note_id: p.split('/')[4], related: state.related});
    if (/^\/api\/v1\/notes\/n[12]\/reminders$/.test(p)) return reply([]);
    if (p === '/api/v1/telegram/links') return reply({identities: [], pending: []});
    if (/\/(opened|original-opened)$/.test(p)) return reply(null, 204);
    if (p === '/api/v1/reminders/resolve-time') {
      return reply({local_time: body.local_time, timezone: body.timezone, ambiguous: false, choices: [
        {scheduled_at: '2090-10-11T07:00:00+00:00', local_at: '2090-10-11T10:00:00+03:00', utc_offset: '+03:00', is_future: true}]});
    }
    if (p === '/api/v1/dashboard') return reply(state.dashboard.fn(Number(u.searchParams.get('days')), {empty: state.dashboard.empty}));
    if (p === '/api/v1/assistant/settings') {
      if (method === 'PATCH') { state.settings = body.recommendations_enabled; }
      return reply({recommendations_enabled: state.settings});
    }
    if (p === '/api/v1/assistant/requests') return reply([{...digestRow, result: null}, {...recRow, result: null}, {...askRow, result: null}]);
    if (method === 'POST' && p === '/api/v1/assistant/ask') {
      if (state.askError) return reply(state.askError.data, state.askError.status);
      return reply({id: 'a1', status: 'queued'}, 202);
    }
    if (method === 'POST' && p === '/api/v1/assistant/recommendations') return reply({id: 'r1', status: 'queued'}, 202);
    if (method === 'POST' && p === '/api/v1/assistant/digest') return reply({id: 'd1', status: 'queued'}, 202);
    const request = p.match(/^\/api\/v1\/assistant\/requests\/(\w+)$/);
    if (request) {
      state.polls++;
      if (state.hold) return reply({...askRow, status: 'running', result: null});
      const rows = {a1: askRow, r1: recRow, d1: digestRow};
      if (state.queued > 0) { state.queued--; return reply({...rows[request[1]], status: 'queued', result: null}); }
      return reply(rows[request[1]]);
    }
    throw new Error(`Unexpected ${method} ${p}`);
  };
  w.eval(read('reminders.js') + '\n' + read('app.js') + '\n' + read('workspace.js') + '\n' + read('brain.js'));
  return {dom, w, $, state};
}
const posts = (state, suffix) => state.calls.filter(c => c.method === 'POST' && c.url.pathname.endsWith(suffix));

async function navigationAndHashes() {
  const {dom, w, $, state} = boot();
  try {
    await until(() => !$('workspace').hidden && state.calls.some(c => c.url.pathname === '/api/v1/notes'), 'workspace');
    assert.equal($('overview-card').hidden, true); assert.equal($('assistant-card').hidden, true);
    assert.equal($('capture-card').hidden, false);
    $('nav-overview').click();
    assert.equal($('overview-card').hidden, false); assert.equal($('capture-card').hidden, true);
    assert.equal(w.location.hash, '#overview');
    assert.equal($('nav-overview').getAttribute('aria-pressed'), 'true');
    assert.equal($('workspace').classList.contains('has-view'), true);
    $('nav-assistant').click();
    assert.equal($('assistant-card').hidden, false); assert.equal($('overview-card').hidden, true);
    assert.equal(w.location.hash, '#assistant');
    assert.equal($('nav-overview').getAttribute('aria-pressed'), 'false');
    $('new-note').click();
    await until(() => $('assistant-card').hidden, 'assistant closes');
    assert.equal($('capture-card').hidden, false); assert.equal(w.location.hash, '');
    // Deep link by hash, as the browser would send it.
    w.location.hash = '#overview';
    w.dispatchEvent(new w.HashChangeEvent('hashchange'));
    assert.equal($('overview-card').hidden, false);
    // A note takes the column back.
    $('notes').querySelector('button').click();
    await until(() => !$('note-card').hidden && !$('title').disabled, 'note');
    await until(() => $('overview-card').hidden, 'overview steps aside');
    assert.equal($('workspace').classList.contains('has-view'), false);
    assert.equal(posts(state, '/ask').length + posts(state, '/digest').length + posts(state, '/recommendations').length, 0,
      'Navigation never calls the assistant');
  } finally { dom.window.close(); }
  const deep = boot({url: 'https://beresta.invalid/#assistant'});
  try {
    await until(() => !deep.$('assistant-card').hidden, 'deep link opens the assistant');
    assert.equal(deep.$('capture-card').hidden, true);
  } finally { deep.dom.window.close(); }
}

async function overview() {
  const {dom, w, $, state} = boot();
  try {
    await until(() => !$('workspace').hidden, 'workspace');
    $('nav-overview').click();
    await until(() => $('overview-body').querySelector('.br-kpis'), 'dashboard');
    assert.equal(state.calls.find(c => c.url.pathname === '/api/v1/dashboard').url.searchParams.get('days'), '30');
    assert.deepEqual([...$('overview-body').querySelectorAll('.br-kpi-value')].map(n => n.textContent), ['43', '5', '6', '27']);
    assert.match($('overview-body').querySelector('.br-kpis').textContent, /60% всех задач/);
    assert.equal($('overview-body').querySelectorAll('.br-bar').length, 30);
    const chart = $('overview-body').querySelector('svg.br-chart');
    assert.ok(chart.getAttribute('aria-label').includes('Заметки по дням'));
    assert.ok(chart.querySelector('title'));
    assert.equal(chart.querySelectorAll('.br-col > title').length, 30);
    assert.equal($('overview-body').querySelectorAll('.br-project').length, 2);
    assert.match($('overview-body').textContent, /Ближайшее напоминание/);
    assert.match($('overview-body').textContent, /Telegram/);
    $('overview-period').querySelector('[data-days="7"]').click();
    await until(() => $('overview-body').querySelectorAll('.br-bar').length === 7, 'seven days');
    assert.equal(state.calls.filter(c => c.url.pathname === '/api/v1/dashboard').at(-1).url.searchParams.get('days'), '7');
    assert.equal($('overview-period').querySelector('[data-days="7"]').getAttribute('aria-pressed'), 'true');
    $('overview-period').querySelector('[data-days="90"]').click();
    await until(() => $('overview-body').querySelectorAll('.br-bar').length === 90, 'ninety days');
    state.dashboard.empty = true;
    $('overview-period').querySelector('[data-days="30"]').click();
    await until(() => $('overview-start'), 'empty state');
    assert.equal($('overview-body').querySelector('svg'), null);
    assert.match($('overview-body').textContent, /Здесь появится ваша статистика/);
    $('overview-start').click();
    await until(() => $('overview-card').hidden, 'capture opens');
    assert.equal($('capture-card').hidden, false);
  } finally { dom.window.close(); }
}

async function askFlow() {
  const {dom, w, $, state} = boot();
  try {
    await until(() => !$('workspace').hidden, 'workspace');
    $('nav-assistant').click();
    await until(() => $('assistant-history').querySelector('.br-history-row') && /осталось/.test($('ask-cost').textContent), 'history and usage');
    assert.equal($('assistant-history').querySelectorAll('.br-history-row').length, 3);
    assert.equal($('ask-cost').textContent, 'Стоит 1 единицу ИИ · осталось 27 из 30');
    $('ask-question').value = 'Что я обещал Марку?'; $('ask-period').value = '30';
    $('ask-form').dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true}));
    await until(() => $('ask-result').querySelector('.br-answer'), 'answer');
    const post = posts(state, '/ask')[0];
    assert.deepEqual(post.body, {question: 'Что я обещал Марку?', days: 30});
    assert.ok(post.headers['Idempotency-Key'] && post.headers['X-CSRF-Token'] === 'csrf');
    assert.ok(state.polls >= 3, 'polled until the answer was ready');
    assert.equal($('ask-result').querySelector('.br-answer b'), null, 'Markdown never becomes markup');
    assert.ok($('ask-result').querySelector('.br-answer').textContent.includes('<b>макет</b>'));
    assert.equal($('ask-result').querySelectorAll('.br-answer li').length, 2);
    const cites = [...$('ask-result').querySelectorAll('.br-cite')];
    assert.equal(cites.length, 2);
    assert.equal(cites[0].textContent, 'Из заметки «План»«прислать макет»');
    cites[1].click();
    await until(() => !$('note-card').hidden && $('title').value === 'Другая', 'cited note');
    assert.equal($('assistant-card').hidden, true);
  } finally { dom.window.close(); }

  // A slow answer stops after the limit and stays in history. Leaving the screen stops polling.
  const slow = boot();
  try {
    slow.w.assistantMaxWaitMs = 40; slow.state.hold = true;
    await until(() => !slow.$('workspace').hidden, 'workspace');
    slow.$('nav-assistant').click();
    await until(() => slow.$('assistant-history').querySelector('.br-history-row'), 'history');
    slow.$('ask-question').value = 'Долгий вопрос';
    slow.$('ask-form').dispatchEvent(new slow.w.Event('submit', {bubbles: true, cancelable: true}));
    await until(() => /задерживается/.test(slow.$('ask-status').textContent), 'timeout text');
    assert.equal(slow.$('ask-submit').disabled, false);
  } finally { slow.dom.window.close(); }
  const leaving = boot();
  try {
    leaving.state.hold = true;
    await until(() => !leaving.$('workspace').hidden, 'workspace');
    leaving.$('nav-assistant').click();
    leaving.$('ask-question').value = 'Вопрос без ответа';
    leaving.$('ask-form').dispatchEvent(new leaving.w.Event('submit', {bubbles: true, cancelable: true}));
    await until(() => leaving.state.polls >= 2, 'polling');
    leaving.$('nav-overview').click();
    await sleep(30);
    const seen = leaving.state.polls;
    await sleep(60);
    assert.equal(leaving.state.polls, seen, 'No polling after navigation');
  } finally { leaving.dom.window.close(); }
}

async function limitMessage() {
  const resets = new Date(Date.now() + 2 * 3600000 + 60000).toISOString();
  const {dom, w, $, state} = boot();
  try {
    state.askError = {status: 429, data: {detail: 'На сегодня лимит ИИ закончился. Он обновится завтра.', limit_resets_at: resets}};
    await until(() => !$('workspace').hidden, 'workspace');
    $('nav-assistant').click();
    $('ask-question').value = 'Вопрос сверх лимита';
    $('ask-form').dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true}));
    await until(() => /Обновится через/.test($('ask-status').textContent), '429 text');
    assert.match($('ask-status').textContent, /На сегодня лимит ИИ закончился/);
    assert.match($('ask-status').textContent, /2 ч/);
    assert.equal(state.polls, 0);
  } finally { dom.window.close(); }
  const spent = boot({usage: {daily_units_remaining: 0, daily_units_used: 30}});
  try {
    await until(() => !spent.$('workspace').hidden, 'workspace');
    spent.$('nav-assistant').click();
    await until(() => /исчерпан/.test(spent.$('ask-cost').textContent), 'spent text');
    assert.equal(spent.$('ask-submit').disabled, true); assert.equal(spent.$('digest-run').disabled, true);
  } finally { spent.dom.window.close(); }
}

async function recommendations() {
  const off = boot({settings: false});
  try {
    await until(() => !off.$('workspace').hidden, 'workspace');
    off.$('nav-assistant').click();
    await until(() => off.$('rec-disabled').hidden === false, 'disabled state');
    assert.equal(off.$('rec-switch').checked, false); assert.equal(off.$('rec-on').hidden, true);
    assert.equal(off.$('ask-submit').disabled, false, 'Ask still works');
    assert.equal(off.$('digest-run').disabled, false, 'Digest still works');
    off.$('rec-switch').checked = true;
    off.$('rec-switch').dispatchEvent(new off.w.Event('change', {bubbles: true}));
    await until(() => off.$('rec-disabled').hidden === true, 'enabled');
    const patch = off.state.calls.find(c => c.method === 'PATCH');
    assert.deepEqual(patch.body, {recommendations_enabled: true});
    assert.equal(patch.url.pathname, '/api/v1/assistant/settings');
    assert.equal(off.$('rec-on').hidden, false);
    off.$('rec-run').click();
    await until(() => off.$('rec-result').querySelectorAll('.br-suggestion').length === 2, 'suggestions');
    assert.deepEqual([...off.$('rec-result').querySelectorAll('.br-kind')].map(n => n.textContent), ['Задача', 'Привычка']);
    assert.equal(off.$('rec-result').querySelectorAll('.br-chip').length, 1);
    assert.ok(posts(off.state, '/recommendations')[0].headers['Idempotency-Key']);
    // Create a note: the capture form is prefilled and the person saves it.
    off.$('rec-result').querySelector('.br-suggestion button.secondary').click();
    assert.equal(off.$('capture-card').hidden, false);
    assert.ok(off.$('thought').value.includes('Позовите десять знакомых.'));
    assert.equal(posts(off.state, '/text').length, 0, 'Nothing is saved without the person');
    // Hide is local only.
    await until(() => off.$('assistant-card').hidden, 'assistant steps aside');
    off.$('nav-assistant').click();
    await until(() => !off.$('assistant-card').hidden, 'assistant again');
    off.$('rec-result').replaceChildren();
    off.$('assistant-history').querySelector('.br-history-row:nth-child(2)').click();
    await until(() => off.$('rec-result').querySelectorAll('.br-suggestion').length === 2, 'reopened');
    const before = off.state.calls.length;
    [...off.$('rec-result').querySelectorAll('.br-suggestion')][0].querySelector('button.quiet').click();
    assert.equal(off.$('rec-result').querySelectorAll('.br-suggestion').length, 1);
    assert.equal(off.state.calls.length, before, 'Hiding makes no request');
    off.$('rec-switch').checked = false;
    off.$('rec-switch').dispatchEvent(new off.w.Event('change', {bubbles: true}));
    await until(() => off.$('rec-on').hidden === true, 'disabled again');
    assert.deepEqual(off.state.calls.filter(c => c.method === 'PATCH').at(-1).body, {recommendations_enabled: false});
  } finally { off.dom.window.close(); }
}

async function digest() {
  const {dom, w, $, state} = boot();
  try {
    await until(() => !$('workspace').hidden, 'workspace');
    $('nav-assistant').click();
    $('digest-run').click();
    await until(() => $('digest-result').querySelector('.br-themes'), 'digest');
    assert.deepEqual(posts(state, '/digest')[0].body, {days: 7});
    assert.equal($('digest-result').querySelectorAll('.br-chip').length, 2);
    assert.equal($('digest-result').querySelector('.br-task').textContent, 'Позвонить маме');
    $('digest-result').querySelector('.br-task').click();
    await until(() => !$('note-card').hidden && $('title').value === 'План', 'task note');
  } finally { dom.window.close(); }
}

async function relatedAndReminders() {
  const {dom, w, $, state} = boot();
  try {
    await until(() => !$('workspace').hidden && $('notes').querySelector('button'), 'workspace');
    $('notes').querySelector('button').click();
    await until(() => !$('view-related').hidden, 'related section');
    const rel = state.calls.find(c => c.url.pathname === '/api/v1/notes/n1/related');
    assert.equal(rel.url.searchParams.get('limit'), '5');
    assert.equal($('related-list').querySelectorAll('.br-rel').length, 1);
    assert.equal($('related-list').querySelector('.br-rel-title').textContent, 'Другая');
    assert.deepEqual([...$('related-list').querySelectorAll('.br-chip')].map(n => n.textContent), ['макет', 'запуск']);
    // The suggested reminder sits under the task.
    await until(() => !$('title').disabled, 'note idle');
    const box = $('items').querySelector('.br-propose');
    assert.ok(box && box.textContent.includes('Напомнить завтра в 10:00?'));
    box.querySelector('[data-propose="set"]').click();
    assert.equal($('reminder-form').hidden, false);
    assert.equal($('reminder-text').value, 'Позвонить маме');
    assert.equal($('reminder-target').value, 'i1');
    assert.equal($('reminder-date').value, '2026-10-11'); assert.equal($('reminder-time').value, '10:00');
    assert.equal($('reminder-zone').value, 'Europe/Moscow');
    await until(() => !$('reminder-confirm').disabled, 'resolved preview');
    assert.match($('reminder-preview').textContent, /Напомню/);
    assert.equal(posts(state, '/reminders').length, 0, 'The person still confirms');
    const resolve = state.calls.filter(c => c.url.pathname === '/api/v1/reminders/resolve-time').at(-1);
    assert.deepEqual(resolve.body, {local_time: '2026-10-11T10:00:00', timezone: 'Europe/Moscow'});
    $('reminder-discard').click();
    await sleep(30);
    $('items').querySelector('[data-propose="edit"]').click();
    await until(() => w.document.activeElement === $('reminder-time'), 'time field focus');
    assert.equal($('reminder-custom').hidden, false);
    // Related note opens on click.
    $('reminder-discard').click();
    $('related-list').querySelector('.br-rel').click();
    await until(() => $('title').value === 'Другая' && !$('title').disabled, 'related note');
    assert.equal($('view-related').hidden, false);
    assert.equal(state.calls.filter(c => c.url.pathname === '/api/v1/notes/n2/related').length, 1);
  } finally { dom.window.close(); }

  const empty = boot({related: []});
  try {
    await until(() => !empty.$('workspace').hidden && empty.$('notes').querySelector('button'), 'workspace');
    empty.$('notes').querySelector('button').click();
    await until(() => empty.state.calls.some(c => c.url.pathname === '/api/v1/notes/n1/related'), 'related request');
    await sleep(20);
    assert.equal(empty.$('view-related').hidden, true, 'An empty section stays hidden');
  } finally { empty.dom.window.close(); }

  // A Telegram link ends with #remind: the first suggestion opens its form on its own.
  const link = boot({url: 'https://beresta.invalid/?capture=k1#remind'});
  try {
    await until(() => !link.$('reminder-form').hidden, 'form from #remind');
    assert.equal(link.$('reminder-text').value, 'Позвонить маме');
    assert.equal(link.$('reminder-time').value, '10:00');
    assert.equal(link.w.location.hash, '');
    assert.equal(posts(link.state, '/reminders').length, 0);
  } finally { link.dom.window.close(); }
}

module.exports = async () => {
  await navigationAndHashes();
  await overview();
  await askFlow();
  await limitMessage();
  await recommendations();
  await digest();
  await relatedAndReminders();
  console.log('Brain checks passed: navigation and hashes, overview, ask with polling, 429, recommendations, digest, related notes, suggested reminders.');
};
if (require.main === module) module.exports().catch(error => { console.error(error); process.exitCode = 1; });
