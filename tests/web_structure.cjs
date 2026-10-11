'use strict';

const assert = require('node:assert/strict');
const {randomUUID} = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');

const staticRoot = path.join(__dirname, '../app/static');
const dom = new JSDOM(fs.readFileSync(path.join(staticRoot, 'index.html'), 'utf8'), {
  url: 'https://beresta.invalid/', runScripts: 'outside-only',
});
const w = dom.window;
const $ = id => w.document.getElementById(id);
Object.defineProperty(w.crypto, 'randomUUID', {value: randomUUID});
w.confirm = () => false;

const note = {
  id: randomUUID(), capture_id: randomUUID(), title: 'План', original_text: 'Позвонить Оле завтра',
  markdown: '## <script>alert(1)</script>', conclusions: [], provider: 'mock', version: 1,
  category_id: randomUUID(), category_name: 'Работа', structure_confirmed_at: null,
  created_at: 1791220000, updated_at: 1791220000,
  items: [{id: randomUUID(), kind: 'task', text: 'Позвонить Оле', status: 'open', version: 1,
    source_quote: 'Позвонить Оле завтра', due_text: 'завтра', due_at: null}],
};
note.items[0].note_id = note.id;
const categories = [{id: note.category_id, name: 'Работа', version: 1}];
const calls = [];
let pauseItem = false, completeItem = null, conflictNextItem = false;

function reply(data, status = 200, headers = {}) {
  return {ok: status >= 200 && status < 300, status, headers: new Headers(headers),
    json: async () => structuredClone(data)};
}
w.fetch = async (input, options = {}) => {
  const url = new URL(input, w.location.href), method = options.method || 'GET';
  const body = options.body ? JSON.parse(options.body) : null;
  calls.push({url, method, body, options});
  if (url.pathname === '/health') return reply({simulation:true});
  if (url.pathname === '/api/v1/auth/me') return reply({username:'Тест',csrf_token:'test-csrf'});
  if (url.pathname === '/api/v1/provider/usage') return reply({simulation:true});
  if (url.pathname === '/api/v1/jobs') return reply([]);
  if (url.pathname === '/api/v1/categories') return reply(categories);
  if (url.pathname === '/api/v1/notes') {
    const offset = Number(url.searchParams.get('offset'));
    return reply(offset ? [] : [{id:note.id,title:note.title}], 200,
      url.searchParams.has('q') && !offset ? {'X-Next-Notes-Offset':'1'} : {});
  }
  if (url.pathname === `/api/v1/notes/${note.id}` && method === 'GET') return reply(note);
  if (url.pathname === `/api/v1/notes/${note.id}/items/${note.items[0].id}`) {
    assert.equal(options.headers['X-CSRF-Token'],'test-csrf');
    assert.equal(body.version,note.version);
    if (conflictNextItem) {
      conflictNextItem = false;
      return reply({detail:'Заметка уже изменена. Обновите её перед сохранением'},409);
    }
    const save = () => {
      Object.assign(note.items[0],{text:body.text,kind:body.kind,status:body.status,version:note.items[0].version + 1});
      note.version++; note.structure_confirmed_at = 1791220010;
      return reply(note);
    };
    if (pauseItem) return new Promise(resolve => {completeItem = () => resolve(save());});
    return save();
  }
  if (url.pathname === `/api/v1/categories/${categories[0].id}` && method === 'PATCH') {
    assert.equal(body.version,categories[0].version);
    categories[0].name = body.name; categories[0].version++;
    note.category_name = body.name;
    return reply(categories[0]);
  }
  if (url.pathname === '/api/v1/search/events' || /\/(opened|original-opened)$/.test(url.pathname)) return reply(null,204);
  throw new Error(`Unexpected request ${method} ${url.pathname}`);
};

async function until(condition) {
  for (let count = 0; count < 100; count++) {
    if (condition()) return;
    await new Promise(resolve => setImmediate(resolve));
  }
  throw new Error(`UI did not settle. ${$('message').textContent}`);
}
function press(el,key,extra = {}) { el.dispatchEvent(new w.KeyboardEvent('keydown',{key,bubbles:true,cancelable:true,...extra})); }
function submit(form) { form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); }

(async () => {
  try {
    w.eval(fs.readFileSync(path.join(staticRoot, 'app.js'), 'utf8'));
    await until(() => $('notes').querySelector('button'));
    $('notes').querySelector('button').click();
    await until(() => $('items').querySelector('.item-text') && !$('title').disabled);
    assert.equal($('preview').querySelector('script'),null);
    assert.ok($('preview').textContent.includes('<script>'));
    assert.equal(calls.filter(c => c.url.pathname.endsWith('/opened')).length,1);

    $('items').querySelector('.item-text').click();
    let editor = $('items').querySelector('textarea');
    assert.ok(editor,'A click on the text opens the inline editor');
    editor.value = 'Уточнить время звонка';
    pauseItem = true;
    press(editor,'Enter');
    await until(() => completeItem !== null);
    assert.equal($('title').disabled,true);
    assert.equal(editor.disabled,true);
    $('new-note').click();
    assert.equal($('note-card').hidden,false);
    assert.equal($('message').textContent,'Дождитесь сохранения.');
    completeItem();
    await until(() => $('note-mode').textContent.includes('v2') && !$('title').disabled);
    assert.equal($('items').querySelector('textarea'),null,'The editor closes after a save');
    assert.equal($('items').querySelector('.item-text').textContent,'Уточнить время звонка');
    assert.equal($('confirm-structure').disabled,true);

    $('items').querySelector('.item-text').click();
    editor = $('items').querySelector('textarea');
    editor.value = 'Не терять эту правку';
    const readsBefore = calls.filter(c => c.url.pathname === `/api/v1/notes/${note.id}`).length;
    $('notes').querySelector('button').click();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(calls.filter(c => c.url.pathname === `/api/v1/notes/${note.id}`).length,readsBefore);
    assert.equal(editor.value,'Не терять эту правку');
    pauseItem = false; conflictNextItem = true;
    press(editor,'Enter');
    await until(() => $('message').textContent.includes('уже изменена') && !editor.disabled);
    assert.equal(editor.value,'Не терять эту правку');
    assert.ok($('note-mode').textContent.includes('v2'));
    editor.value = note.items[0].text;

    $('search-query').value = 'РУССКОЕ'; $('category-filter').value = note.category_id;
    submit($('search-form'));
    await until(() => !$('more-notes').hidden);
    const search = calls.find(c => c.url.pathname === '/api/v1/search/events');
    assert.ok(search.body.operation_id);
    let list = calls.filter(c => c.url.pathname === '/api/v1/notes').at(-1);
    assert.equal(list.url.searchParams.get('q'),'РУССКОЕ');
    assert.equal(list.url.searchParams.get('category_id'),note.category_id);
    $('search-query').value = 'Ещё не отправлено'; $('more-notes').click();
    await until(() => $('more-notes').hidden);
    list = calls.filter(c => c.url.pathname === '/api/v1/notes').at(-1);
    assert.equal(list.url.searchParams.get('q'),'РУССКОЕ');
    assert.equal(list.url.searchParams.get('offset'),'1');
    $('notes').querySelector('button').click();
    await until(() => calls.filter(c => c.url.pathname.endsWith('/opened')).length === 2 && !$('title').disabled);
    assert.equal(calls.filter(c => c.url.pathname.endsWith('/opened')).at(-1).body.search_operation_id,search.body.operation_id);

    const row = $('categories').querySelector('form');
    row.querySelector('input').value = 'Проект'; submit(row);
    await until(() => $('note-category').selectedOptions[0].textContent === 'Проект');
    assert.equal($('note-category').value,note.category_id);
    assert.equal(calls.filter(c => c.url.pathname === '/api/v1/search/events').length,1);
    // Authentication input stays aligned with the API, including whitespace.
    const login = $('login');
    assert.equal(login.placeholder,'username');
    assert.equal(login.minLength,3); assert.equal(login.maxLength,64);
    const link = w.document.querySelector('a[href="https://t.me/beresta_app"]');
    assert.ok(link && link.textContent.includes('Telegram'));
    const authCalls = [];
    w.fetch = async (url, options) => {
      authCalls.push({url,body:JSON.parse(options.body)});
      return reply({detail:'Synthetic auth result'},401);
    };
    // Registration needs the privacy consent box. Password sign-in does not.
    login.value = 'abc'; $('password').value = 'test-only-password-123'; $('accept-policy').checked = false;
    await $('auth-form').onsubmit({preventDefault(){},target:$('auth-form'),submitter:{value:'register'}});
    assert.equal(authCalls.length,0);
    assert.match($('auth-message').textContent,/согласие на обработку персональных данных/);
    await $('auth-form').onsubmit({preventDefault(){},target:$('auth-form'),submitter:{value:'login'}});
    assert.equal(authCalls.length,1); authCalls.length = 0;
    $('accept-policy').checked = true; $('confirm-age').checked = false;
    await $('auth-form').onsubmit({preventDefault(){},target:$('auth-form'),submitter:{value:'register'}});
    assert.equal(authCalls.length,0);
    assert.match($('auth-message').textContent,/18 лет/);
    await $('auth-form').onsubmit({preventDefault(){},target:$('auth-form'),submitter:{value:'login'}});
    assert.equal(authCalls.length,1); authCalls.length = 0;
    $('confirm-age').checked = true;
    for (const action of ['register','login']) {
      for (const name of ['abc','User.Name_123-X','A'.repeat(64),'ab','a'.repeat(65),'Марк','MarkМ','Ёжик','abc\n',' abc','abc ','café','abc@']) {
        const valid = ['abc','User.Name_123-X','A'.repeat(64)].includes(name);
        login.value = name;
        $('password').value = 'test-only-password-123';
        // text inputs strip newlines natively; all other cases exercise HTML validation.
        if (!name.includes('\n')) assert.equal(login.checkValidity(), !/[^A-Za-z0-9_.-]/u.test(name),name);
        const before = authCalls.length;
        // Set the original value to test JS validation even for programmatic submits.
        Object.defineProperty(login,'value',{configurable:true,value:name,writable:true});
        await $('auth-form').onsubmit({preventDefault(){},target:$('auth-form'),submitter:{value:action}});
        delete login.value;
        assert.equal(authCalls.length,before + Number(valid),name);
        if (valid) assert.equal(authCalls.at(-1).body.username,name);
        if (valid && action === 'register') assert.deepEqual([authCalls.at(-1).body.accept_policy,authCalls.at(-1).body.policy_version,authCalls.at(-1).body.confirm_age],[true,'2026-10-11',true]);
        if (valid && action === 'login') assert.equal('accept_policy' in authCalls.at(-1).body || 'confirm_age' in authCalls.at(-1).body,false);
        if (!valid) assert.match($('auth-message').textContent,/Латинские буквы/);
      }
    }
    await require('./web_job_waiting.cjs')();
    await require('./web_workspace.cjs')();
    await require('./web_note_ux.cjs')();
    await require('./web_focus.cjs')();
    await require('./web_onboarding.cjs')();
    await require('./web_auth.cjs')();
    await require('./web_email_auth.cjs')();
    await require('./web_brain.cjs')();
    console.log('Web DOM checks passed: drafts, conflicts, version, XSS, search, pagination, events and category rename.');
  } finally { dom.window.close(); }
})().catch(error => {console.error(error);process.exitCode = 1;});
