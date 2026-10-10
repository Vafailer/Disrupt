'use strict';
let telegramLinked = false;
const assert = require('node:assert/strict');
const {randomUUID} = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const root = path.join(__dirname,'../app/static');
const dom = new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{
  url:'https://beresta.invalid/',runScripts:'outside-only',
});
const w = dom.window, $ = id => w.document.getElementById(id);
Object.defineProperty(w.crypto,'randomUUID',{value:randomUUID});
// Fixed clock inside the window only: Saturday 2026-10-10 12:00 in Moscow. Intl keeps working on Date subclasses.
const RealDate = w.Date;
let clock = RealDate.parse('2026-10-10T09:00:00Z');
w.Date = class extends RealDate {
  constructor(...args) { if (args.length) super(...args); else super(clock); }
  static now() { return clock; }
};
w.reminderDebounceMs = 0;
w.confirm = () => false;
const note = {id:randomUUID(),capture_id:randomUUID(),title:'План',original_text:'План',markdown:'План',
  provider:'manual',version:1,conclusions:[],category_id:null,structure_confirmed_at:null,items:[
    {id:randomUUID(),kind:'task',text:'Позвонить',status:'open',version:1},
    {id:randomUUID(),kind:'task',text:'Сделано',status:'completed',version:1},
    {id:randomUUID(),kind:'idea',text:'Идея',status:'open',version:1},
  ]};
const other = {...note,id:randomUUID(),capture_id:randomUUID(),title:'Другая',items:[]};
const calls = [], reminders = [], creations = new Map();
let lostCreate = true, conflictEdit = false, lostEdit = false, cancelledDuringEdit = false;
let ambiguous = false, past = false, pauseList = false, finishList = null;
function reply(data,status=200) { return {ok:status >= 200 && status < 300,status,headers:new Headers(),json:async () => structuredClone(data)}; }
w.fetch = async (input,options={}) => {
  const url = new URL(input,w.location.href), method = options.method || 'GET';
  const body = options.body ? JSON.parse(options.body) : null;
  calls.push({url,method,body,options});
  if (url.pathname === '/health' || url.pathname === '/api/v1/provider/usage') return reply({simulation:true});
  if (url.pathname === '/api/v1/auth/me') return reply({username:'Тест',csrf_token:'synthetic-csrf'});
  if (url.pathname === '/api/v1/categories' || url.pathname === '/api/v1/jobs') return reply([]);
  if (url.pathname === '/api/v1/telegram/links') return reply({identities:telegramLinked ? [{bot_id:1,telegram_user_id:2,notifications_enabled:true,delivery_status:'available'}] : [],pending:[]});
  if (url.pathname === '/api/v1/notes') return reply([note,other]);
  if (url.pathname === `/api/v1/notes/${note.id}` && method === 'GET') return reply(note);
  if (url.pathname === `/api/v1/notes/${other.id}` && method === 'GET') return reply(other);
  if (url.pathname === `/api/v1/notes/${other.id}/reminders`) return reply([]);
  if (/\/(opened|original-opened)$/.test(url.pathname)) return reply(null,204);
  if (method !== 'GET') assert.equal(options.headers['X-CSRF-Token'],'synthetic-csrf');
  if (url.pathname === '/api/v1/reminders/resolve-time') {
    if (past) return reply({local_time:body.local_time,timezone:body.timezone,ambiguous:false,choices:[
      {scheduled_at:'2020-01-01T09:00:00+00:00',local_at:'2020-01-01T12:00:00+03:00',utc_offset:'+03:00',is_future:false}]});
    return reply({local_time:body.local_time,timezone:body.timezone,ambiguous,choices:ambiguous ? [
      {scheduled_at:'2090-10-29T00:30:00+00:00',local_at:'2090-10-29T02:30:00+02:00',utc_offset:'+02:00',is_future:true},
      {scheduled_at:'2090-10-29T01:30:00+00:00',local_at:'2090-10-29T02:30:00+01:00',utc_offset:'+01:00',is_future:true},
    ] : [{scheduled_at:'2090-10-08T09:00:00+00:00',local_at:'2090-10-08T12:00:00+03:00',utc_offset:'+03:00',is_future:true}]});
  }
  if (url.pathname === `/api/v1/notes/${note.id}/reminders` && method === 'GET') {
    const list = reminders.slice(Number(url.searchParams.get('offset')),Number(url.searchParams.get('offset')) + Number(url.searchParams.get('limit')));
    if (pauseList) { pauseList = false; return new Promise(resolve => {finishList = () => resolve(reply(list));}); }
    return reply(list);
  }
  if (url.pathname === `/api/v1/notes/${note.id}/reminders` && method === 'POST') {
    const key = options.headers['Idempotency-Key'];
    if (!creations.has(key)) {
      const row = {...body,id:randomUUID(),note_id:note.id,status:'confirmed',generation:1,
        confirmed_at:'2026-10-06T15:00:00Z',delivery_status:null,previous_attempt_unknown:false};
      creations.set(key,row); reminders.push(row);
    }
    if (lostCreate) { lostCreate = false; throw new Error('Сеть оборвалась после commit'); }
    return reply(creations.get(key),201);
  }
  const match = url.pathname.match(/^\/api\/v1\/reminders\/([^/]+)(\/cancel)?$/);
  if (match) {
    const row = reminders.find(r => r.id === match[1]); assert.ok(row);
    if (method === 'GET') return reply(row);
    if (match[2]) {
      if (body.generation !== row.generation) return reply({detail:'Напоминание изменилось'},409);
      row.status = 'cancelled'; row.generation++; return reply(row);
    }
    if (conflictEdit) {
      conflictEdit = false; row.generation++; row.text = '<img src=x onerror=alert(1)> Другая вкладка';
      return reply({detail:'Напоминание уже изменено. Обновите его'},409);
    }
    assert.equal(body.generation,row.generation);
    if (cancelledDuringEdit) {
      cancelledDuringEdit = false; row.generation++; row.status = 'cancelled';
      throw new Error('Потеря ответа, другая вкладка отменила напоминание');
    }
    Object.assign(row,body); row.generation++; row.status = 'confirmed';
    if (lostEdit) { lostEdit = false; throw new Error('Потерян ответ правки'); }
    return reply(row);
  }
  throw new Error(`Unexpected request ${method} ${url.pathname}`);
};
async function until(condition) {
  // Debounced work runs on timers, so also give real time a chance, not only immediates.
  for (let n=0;n<400;n++) {
    if (condition()) return;
    await new Promise(resolve => n % 10 === 9 ? setTimeout(resolve,5) : setImmediate(resolve));
  }
  throw new Error(`UI did not settle: ${$('reminders-message').textContent} ${$('message').textContent}`);
}
function submit(id) { $(id).dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); }
function input(id,value) { $(id).value = value; $(id).dispatchEvent(new w.Event('input',{bubbles:true})); }
const checking = 'Проверяем время…';
// The time is resolved automatically after every change, so the test only waits for the preview.
async function time() {
  await until(() => !$('reminder-controls').disabled && $('reminder-preview').textContent && $('reminder-preview').textContent !== checking);
}
async function setWhen(date,clockTime) { input('reminder-date',date); input('reminder-time',clockTime); await time(); }
function resolves() { return calls.filter(c => c.url.pathname === '/api/v1/reminders/resolve-time'); }
function chip(kind) { return $('reminder-chips').querySelector(`[data-pick="${kind}"]`); }
async function pickChip(kind) { chip(kind).click(); await time(); }
const sleep = ms => new Promise(resolve => setTimeout(resolve,ms));
function row(id) { return [...$('reminders-list').querySelectorAll('article')].find(card => card.dataset.id === id); }

(async () => {
  try {
    w.eval(fs.readFileSync(path.join(root,'reminders.js'),'utf8'));
    w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8'));
    await until(() => $('notes').querySelectorAll('button').length === 2);
    $('notes').querySelector('button').click();
    await until(() => $('reminders-list').textContent.includes('Напоминаний нет') && !$('title').disabled);
    assert.equal($('reminder-telegram').hidden,false,'The warning shows while Telegram is not linked');
    assert.ok($('reminder-telegram').textContent.includes('Telegram не подключён'));
    assert.equal($('reminder-link').hidden,false,'Telegram link is offered only while Telegram is not linked');
    assert.equal($('reminders-heading').textContent,'Напоминания');
    assert.equal($('reminder-new').closest('.reminders-head') !== null,true,'The new button sits in the header');
    assert.equal($('reminders-refresh').closest('.reminders-head') !== null,true);
    assert.equal($('reminders-panel').querySelectorAll(':scope > p.muted, :scope > fieldset > p.muted:not(.reminders-empty)').length,0,'No explanatory paragraphs');
    assert.equal($('reminder-form').hidden,true);
    assert.equal($('reminder-target-wrap').hidden,false,'A note with open tasks offers a target');
    assert.equal($('reminder-target-wrap').querySelector('label').textContent,'Напомнить о');
    telegramLinked = true; $('reminders-refresh').click();
    await until(() => $('reminder-telegram').hidden);
    assert.equal($('reminder-link').hidden,true,'A linked Telegram shows no warning');
    telegramLinked = false; $('reminders-refresh').click();
    await until(() => !$('reminder-telegram').hidden);
    $('items').querySelector('.item-remind').click();
    assert.equal($('reminder-target').value,note.items[0].id);
    assert.equal($('reminder-target').options.length,2,'Only note and open tasks are targets');
    assert.equal($('reminder-text').value,'Позвонить');
    assert.equal($('reminder-confirm').disabled,true);
    assert.equal($('reminder-confirm').textContent,'Напомнить');
    assert.equal($('reminder-zone').value,w.Intl.DateTimeFormat().resolvedOptions().timeZone,'Zone defaults to the browser zone');
    assert.equal($('reminder-check-time').hidden,true,'There is no manual check step');
    assert.equal($('reminder-custom').hidden,true);
    assert.equal($('reminder-zone-edit').hidden,true);
    $('reminder-zone-change').click(); assert.equal($('reminder-zone-edit').hidden,false);
    input('reminder-zone','Europe/Moscow');
    assert.equal($('reminder-zone-name').textContent,'Москва (Europe/Moscow)');
    await pickChip('tomorrow');
    assert.equal($('reminder-local').value,'2026-10-11T09:00:00');
    assert.equal(resolves().at(-1).body.timezone,'Europe/Moscow');
    assert.equal(resolves().at(-1).body.local_time,'2026-10-11T09:00:00');
    $('reminder-chips').querySelector('[data-pick="custom"]').click();
    assert.equal($('reminder-date').value,'2026-10-11'); assert.equal($('reminder-time').value,'09:00');
    await setWhen('2090-10-08','12:00');
    assert.equal($('reminder-local').value,'2090-10-08T12:00:00');
    assert.equal($('reminder-confirm').disabled,false);
    assert.equal($('reminder-preview').textContent,'Напомню в воскресенье, 8 октября 2090 года, в 12:00 (Москва).\nПозвонить');
    assert.equal(calls.filter(c => c.method === 'POST' && c.url.pathname.endsWith('/reminders')).length,0);

    $('title').value = 'Не терять правку заметки'; submit('reminder-form');
    assert.equal($('message').textContent,'Сначала сохраните остальные правки.');
    $('title').value = note.title;
    submit('reminder-form');
    await until(() => $('reminders-message').textContent.includes('Ответ потерян') && !$('reminder-controls').disabled);
    assert.equal($('reminder-fields').disabled,true);
    $('new-note').click(); assert.equal($('note-card').hidden,false,'Unknown creation remains a guarded draft');
    submit('reminder-form');
    await until(() => $('reminder-form').hidden && $('reminders-list').querySelector('article') && !$('title').disabled);
    const creates = calls.filter(c => c.method === 'POST' && c.url.pathname.endsWith('/reminders'));
    assert.equal(creates.length,2); assert.deepEqual(creates[0].body,creates[1].body);
    assert.equal(creates[0].options.headers['Idempotency-Key'],creates[1].options.headers['Idempotency-Key']);
    assert.equal(creates[0].body.scheduled_at,'2090-10-08T09:00:00+00:00');
    assert.equal(reminders.length,1);
    assert.ok(row(reminders[0].id).querySelector('.reminder-when').textContent.startsWith('Вс, 8 окт 2090, 12:00'));
    assert.ok(row(reminders[0].id).querySelector('.reminder-status').textContent.includes('Подтверждено'));
    assert.equal(row(reminders[0].id).querySelectorAll('button').length,2,'Row has only edit and cancel');

    const first = reminders[0];
    row(first.id).querySelector('[data-action="edit"]').click();
    assert.equal($('reminder-target').disabled,true);
    await time();
    assert.equal($('reminder-date').value,'2090-10-08'); assert.equal($('reminder-time').value,'12:00');
    assert.equal($('reminder-custom').hidden,false); assert.equal($('reminder-confirm').textContent,'Сохранить изменения');
    input('reminder-text','Моя правка'); assert.ok($('reminder-preview').textContent.includes('Моя правка'));
    conflictEdit = true; submit('reminder-form');
    await until(() => !$('reminder-conflict').hidden && !$('title').disabled);
    assert.equal($('reminder-text').value,'Моя правка'); assert.equal($('reminder-confirm').disabled,true);
    $('reminder-compare').click(); await until(() => !$('reminder-remote').hidden && !$('title').disabled);
    assert.ok($('reminder-remote').textContent.includes('Другая вкладка'));
    assert.equal($('reminder-remote').querySelector('img'),null);
    $('reminder-use-version').click(); assert.equal($('reminder-text').value,'Моя правка');
    await time(); submit('reminder-form'); await until(() => $('reminder-form').hidden && !$('title').disabled);
    const edits = calls.filter(c => c.method === 'PATCH');
    assert.equal(edits[0].body.generation,1); assert.equal(edits[1].body.generation,2);
    assert.equal(first.generation,3);

    row(first.id).querySelector('[data-action="edit"]').click();
    await time(); input('reminder-text','Сохранено, хотя ответ потерян'); lostEdit = true; submit('reminder-form');
    await until(() => $('reminders-message').textContent.includes('Ответ потерян') && !$('title').disabled);
    const patchCount = calls.filter(c => c.method === 'PATCH').length;
    submit('reminder-form'); await until(() => $('reminder-form').hidden && !$('title').disabled);
    assert.equal(calls.filter(c => c.method === 'PATCH').length,patchCount,'Lost edit response is checked, never blindly patched again');

    row(first.id).querySelector('[data-action="edit"]').click(); await time();
    cancelledDuringEdit = true; submit('reminder-form');
    await until(() => $('reminders-message').textContent.includes('Ответ потерян') && !$('title').disabled);
    submit('reminder-form'); await until(() => !$('reminder-remote').hidden && !$('title').disabled);
    assert.equal($('reminder-form').hidden,false,'A concurrent cancellation with unchanged fields must not be mistaken for a saved edit');
    assert.ok($('reminder-remote').textContent.includes('Отменено'));
    $('reminder-use-version').click(); await time(); submit('reminder-form');
    await until(() => $('reminder-form').hidden && !$('title').disabled);
    assert.equal(first.status,'confirmed','Reactivation requires explicit confirmation on the new generation');

    first.previous_attempt_unknown = true; first.delivery_status = 'unknown';
    $('reminders-refresh').click(); await until(() => row(first.id).querySelector('.reminder-warning'));
    assert.ok(row(first.id).textContent.includes('могла состояться'));
    w.confirm = () => true; row(first.id).querySelector('[data-action="cancel"]').click();
    await until(() => first.status === 'cancelled' && !$('title').disabled);
    assert.ok($('reminders-message').textContent.includes('могла состояться'));
    assert.equal(row(first.id).querySelector('[data-action="cancel"]').disabled,true);

    $('reminder-new').click();
    assert.equal($('reminder-zone').value,'Europe/Moscow','A confirmed zone is remembered for the next reminder');
    input('reminder-zone','Europe/Berlin'); ambiguous = true;
    input('reminder-local','2090-10-29T02:30'); await time();
    assert.equal($('reminder-date').value,'2090-10-29'); assert.equal($('reminder-time').value,'02:30');
    assert.equal($('reminder-time-choices').hidden,false);
    assert.equal($('reminder-confirm').disabled,true,'An ambiguous time cannot be silently selected');
    assert.ok($('reminder-preview').textContent.includes('наступит дважды'));
    assert.ok([...$('reminder-time-choice').options].some(option => option.textContent.startsWith('Первый раз, 2:30, UTC+02:00')));
    $('reminder-time-choice').value = '2090-10-29T01:30:00+00:00';
    $('reminder-time-choice').dispatchEvent(new w.Event('change'));
    assert.equal($('reminder-confirm').disabled,false);
    assert.ok($('reminder-preview').textContent.startsWith('Напомню') && $('reminder-preview').textContent.includes('(Берлин)'));
    input('reminder-time','03:30'); assert.equal($('reminder-confirm').disabled,true);
    ambiguous = false; await time(); assert.equal($('reminder-time-choices').hidden,true);
    $('reminder-discard').click();

    // Quick picks use the fixed clock: Saturday 2026-10-10, 12:00 in Moscow.
    $('reminder-new').click(); input('reminder-zone','Europe/Moscow');
    assert.deepEqual([...$('reminder-chips').querySelectorAll('button')].map(b => [b.textContent,b.hidden]),[
      ['Через час',false],['Сегодня в 19:00',false],['Завтра в 9:00',false],['В понедельник в 9:00',false],
      ['Через неделю',false],['Другое время',false]]);
    assert.ok([...$('reminder-chips').querySelectorAll('button')].every(b => b.getAttribute('aria-pressed') === 'false'));
    for (const [kind,local] of [['hour','2026-10-10T13:00:00'],['today','2026-10-10T19:00:00'],['tomorrow','2026-10-11T09:00:00'],
      ['monday','2026-10-12T09:00:00'],['week','2026-10-17T12:00:00'],['custom','2026-10-11T09:00:00']]) {
      await pickChip(kind);
      assert.equal(resolves().at(-1).body.local_time,local,kind); assert.equal($('reminder-local').value,local,kind);
      assert.deepEqual([...$('reminder-chips').querySelectorAll('[aria-pressed="true"]')].map(b => b.dataset.pick),[kind]);
      assert.equal($('reminder-custom').hidden,kind !== 'custom');
      assert.equal($('reminder-confirm').disabled,false,'A picked chip resolves at once');
    }
    clock = RealDate.parse('2026-10-10T09:07:30Z'); await pickChip('hour');
    assert.equal(resolves().at(-1).body.local_time,'2026-10-10T13:10:00','Rounded up to the next five minutes');
    await pickChip('week'); assert.equal(resolves().at(-1).body.local_time,'2026-10-17T12:10:00');
    $('reminder-discard').click();
    clock = RealDate.parse('2026-10-10T16:00:00Z');
    $('reminder-new').click(); input('reminder-zone','Europe/Moscow');
    assert.equal(chip('today').hidden,true,'Today at 19:00 is hidden after 18:30');
    assert.equal(chip('tomorrow').hidden,false);
    await pickChip('tomorrow'); assert.equal(resolves().at(-1).body.local_time,'2026-10-11T09:00:00');
    input('reminder-zone','Asia/Vladivostok');
    await until(() => resolves().at(-1).body.timezone === 'Asia/Vladivostok'); await time();
    assert.equal(resolves().at(-1).body.local_time,'2026-10-12T09:00:00','An active chip is recomputed in the new zone');
    assert.equal(chip('tomorrow').getAttribute('aria-pressed'),'true');
    $('reminder-discard').click();
    clock = RealDate.parse('2026-10-11T09:00:00Z');
    $('reminder-new').click(); input('reminder-zone','Europe/Moscow');
    assert.equal(chip('monday').hidden,true,'On Sunday tomorrow is already Monday'); $('reminder-discard').click();
    clock = RealDate.parse('2026-10-12T09:00:00Z');
    $('reminder-new').click(); input('reminder-zone','Europe/Moscow'); await pickChip('monday');
    assert.equal(resolves().at(-1).body.local_time,'2026-10-19T09:00:00','On Monday the next Monday is a week away');
    $('reminder-discard').click(); clock = RealDate.parse('2026-10-10T09:00:00Z');

    // A past time is explained and cannot be confirmed.
    $('reminder-new').click(); input('reminder-zone','Europe/Moscow'); past = true;
    await pickChip('hour');
    assert.equal($('reminder-preview').textContent,'Это время уже прошло. Выберите другое.');
    assert.equal($('reminder-confirm').disabled,true); past = false;
    await pickChip('hour'); assert.equal($('reminder-confirm').disabled,false);

    // Automatic resolve is debounced: a burst of edits sends one request with the last value.
    w.reminderDebounceMs = 60; const before = resolves().length;
    input('reminder-date','2090-01-01'); input('reminder-date','2090-01-02'); input('reminder-time','10:00'); input('reminder-time','10:05');
    assert.equal(resolves().length,before,'Nothing is sent while the user is still editing');
    assert.equal($('reminder-confirm').disabled,true);
    await sleep(250); await time();
    assert.equal(resolves().length,before + 1); assert.equal(resolves().at(-1).body.local_time,'2090-01-02T10:05:00');
    w.reminderDebounceMs = 0;

    // A response that arrives after a newer change is ignored.
    let release = null; const realFetch = w.fetch;
    w.fetch = (input_,options = {}) => String(input_).includes('resolve-time') && !release
      ? new Promise(resolve => { release = () => resolve(realFetch(input_,options)); }) : realFetch(input_,options);
    input('reminder-time','11:00'); await until(() => release);
    w.fetch = realFetch; input('reminder-time','11:05'); await time();
    const fresh = $('reminder-preview').textContent;
    ambiguous = true; release(); await sleep(20); ambiguous = false;
    assert.equal($('reminder-preview').textContent,fresh,'A stale response must not replace the newer preview');
    assert.equal($('reminder-time-choices').hidden,true); assert.equal($('reminder-confirm').disabled,false);
    assert.equal($('reminder-local').value,'2090-01-02T11:05:00');
    $('reminder-discard').click();

    for (let n=0;n<20;n++) reminders.push({...first,id:randomUUID(),previous_attempt_unknown:false,text:`Строка ${n}`});
    $('reminders-refresh').click(); await until(() => !$('reminders-more').hidden);
    assert.equal($('reminders-list').querySelectorAll('article').length,20);
    $('reminders-more').click(); await until(() => $('reminders-more').hidden);
    assert.equal($('reminders-list').querySelectorAll('article').length,21);
    const moreCall = calls.filter(c => c.url.pathname.endsWith('/reminders') && c.method === 'GET').at(-1);
    assert.equal(moreCall.url.searchParams.get('offset'),'20');
    pauseList = true; $('reminders-refresh').click(); await until(() => finishList);
    $('notes').querySelectorAll('button')[1].click();
    await until(() => $('title').value === other.title && !$('title').disabled && $('reminders-list').textContent.includes('Напоминаний нет'));
    finishList(); await new Promise(resolve => setImmediate(resolve));
    assert.equal($('reminders-list').querySelector('article'),null,'A late response from another note must not replace the current list');
    assert.equal($('reminder-target-wrap').hidden,true,'A note without open tasks has no target choice');
    $('reminder-new').click(); assert.equal($('reminder-target-wrap').hidden,true); assert.equal($('reminder-target').value,'');
    $('reminder-discard').click();
    console.log('Reminder DOM checks passed: quick picks, automatic debounced resolve, past time, hidden target, zone confirmation, task targets, draft guards, lost-response replay, edit reconciliation, generation conflict, unknown warning, cancellation, DST choice, pagination and stale responses.');
  } finally {
    // Let in-flight list requests settle before the document goes away.
    for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve));
    dom.window.close();
  }
})().catch(error => {console.error(error);process.exitCode = 1;});
