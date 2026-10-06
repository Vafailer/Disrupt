'use strict';
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
let ambiguous = false, pauseList = false, finishList = null;
function reply(data,status=200) { return {ok:status >= 200 && status < 300,status,headers:new Headers(),json:async () => structuredClone(data)}; }
w.fetch = async (input,options={}) => {
  const url = new URL(input,w.location.href), method = options.method || 'GET';
  const body = options.body ? JSON.parse(options.body) : null;
  calls.push({url,method,body,options});
  if (url.pathname === '/health' || url.pathname === '/api/v1/provider/usage') return reply({simulation:true});
  if (url.pathname === '/api/v1/auth/me') return reply({username:'Тест',csrf_token:'synthetic-csrf'});
  if (url.pathname === '/api/v1/categories' || url.pathname === '/api/v1/jobs') return reply([]);
  if (url.pathname === '/api/v1/telegram/links') return reply({identities:[],pending:[]});
  if (url.pathname === '/api/v1/notes') return reply([note,other]);
  if (url.pathname === `/api/v1/notes/${note.id}` && method === 'GET') return reply(note);
  if (url.pathname === `/api/v1/notes/${other.id}` && method === 'GET') return reply(other);
  if (url.pathname === `/api/v1/notes/${other.id}/reminders`) return reply([]);
  if (/\/(opened|original-opened)$/.test(url.pathname)) return reply(null,204);
  if (method !== 'GET') assert.equal(options.headers['X-CSRF-Token'],'synthetic-csrf');
  if (url.pathname === '/api/v1/reminders/resolve-time') {
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
  for (let n=0;n<150;n++) {
    if (condition()) return;
    await new Promise(resolve => setImmediate(resolve));
  }
  throw new Error(`UI did not settle: ${$('reminders-message').textContent} ${$('message').textContent}`);
}
function submit(id) { $(id).dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); }
function input(id,value) { $(id).value = value; $(id).dispatchEvent(new w.Event('input',{bubbles:true})); }
async function time() {
  $('reminder-check-time').click(); await until(() => !$('reminder-controls').disabled && $('reminder-preview').textContent);
}
function row(id) { return [...$('reminders-list').querySelectorAll('article')].find(card => card.dataset.id === id); }

(async () => {
  try {
    w.eval(fs.readFileSync(path.join(root,'reminders.js'),'utf8'));
    w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8'));
    await until(() => $('notes').querySelectorAll('button').length === 2);
    $('notes').querySelector('button').click();
    await until(() => $('reminders-list').textContent.includes('пока нет') && !$('title').disabled);
    assert.ok($('reminder-telegram').textContent.includes('нужен доступный Telegram'));
    $('items').querySelector('button[type="button"]').click();
    assert.equal($('reminder-target').value,note.items[0].id);
    assert.equal($('reminder-target').options.length,2,'Only note and open tasks are targets');
    assert.equal($('reminder-text').value,'Позвонить');
    assert.equal($('reminder-confirm').disabled,true);
    input('reminder-zone','Europe/Moscow'); input('reminder-local','2090-10-08T12:00');
    await time(); assert.equal($('reminder-confirm').disabled,false);
    assert.ok($('reminder-preview').textContent.includes('UTC+03:00'));
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

    const first = reminders[0];
    row(first.id).querySelector('[data-action="edit"]').click();
    assert.equal($('reminder-target').disabled,true);
    input('reminder-text','Моя правка'); await time(); conflictEdit = true; submit('reminder-form');
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
    input('reminder-text','Сохранено, хотя ответ потерян'); await time(); lostEdit = true; submit('reminder-form');
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

    $('reminder-new').click(); input('reminder-zone','Europe/Berlin'); input('reminder-local','2090-10-29T02:30');
    ambiguous = true; await time();
    assert.equal($('reminder-confirm').disabled,true,'An ambiguous time cannot be silently selected');
    $('reminder-time-choice').value = '2090-10-29T01:30:00+00:00';
    $('reminder-time-choice').dispatchEvent(new w.Event('change'));
    assert.equal($('reminder-confirm').disabled,false);
    input('reminder-local','2090-10-30T02:30'); assert.equal($('reminder-confirm').disabled,true);
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
    await until(() => $('title').value === other.title && !$('title').disabled && $('reminders-list').textContent.includes('пока нет'));
    finishList(); await new Promise(resolve => setImmediate(resolve));
    assert.equal($('reminders-list').querySelector('article'),null,'A late response from another note must not replace the current list');
    console.log('Reminder DOM checks passed: zone confirmation, task targets, draft guards, lost-response replay, edit reconciliation, generation conflict, unknown warning, cancellation, DST choice, pagination and stale responses.');
  } finally { dom.window.close(); }
})().catch(error => {console.error(error);process.exitCode = 1;});
