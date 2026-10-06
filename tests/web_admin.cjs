'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const root = path.join(__dirname,'..');
const fixture = JSON.parse(fs.readFileSync(path.join(root,'docs/fixtures/admin-summary.json'),'utf8'));
const html = fs.readFileSync(path.join(root,'app/static/admin.html'),'utf8');
const js = fs.readFileSync(path.join(root,'app/static/admin.js'),'utf8');
const dom = new JSDOM(html,{url:'https://beresta.invalid/admin',runScripts:'outside-only'});
const w = dom.window, $ = id => w.document.getElementById(id), calls = [], downloads = [];
w.Date = class extends Date {constructor(...args){super(...(args.length ? args : ['2026-10-07T09:00:00Z']));}};
w.URL.createObjectURL = blob => {downloads.push(blob);return 'blob:synthetic';};
w.URL.revokeObjectURL = () => {};
w.HTMLAnchorElement.prototype.click = function(){ downloads.push(this.download); };
let nextResponse = null;
function reply(data=fixture,status=200,headers={}) {
  return {ok:status>=200&&status<300,status,headers:new Headers(headers),json:async()=>structuredClone(data),blob:async()=>new Blob(['synthetic CSV'])};
}
w.fetch = async (input,options) => {
  const url = new URL(input,w.location.href);calls.push({url,options});
  assert.equal(url.origin,'https://beresta.invalid');
  assert.equal(options.credentials,'same-origin');assert.equal(options.cache,'no-store');
  assert.ok(['/api/admin/summary','/api/admin/export'].includes(url.pathname));
  if (nextResponse) {const fn=nextResponse;nextResponse=null;return fn(url);}
  if (url.pathname.endsWith('/export')) return reply(null,200,{'Content-Type':'text/csv; charset=utf-8'});
  const offset = Number(url.searchParams.get('usage_offset'));
  return offset ? reply({...fixture,usage:[]}) : reply(fixture,200,{'X-Next-Usage-Offset':'50'});
};
const tick = () => new Promise(resolve=>setImmediate(resolve));
async function settled(){for(let i=0;i<8;i++)await tick();}
async function reload(fn){if(fn)nextResponse=fn;$('filters').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await settled();}
(async()=>{
  w.eval(js);await settled();
  assert.equal($('dashboard').hidden,false);
  assert.equal($('to').value,'2026-10-06');
  assert.equal(calls[0].url.searchParams.get('usage_limit'),'50');
  assert.match($('cards').textContent,/DAU за 2026-10-06/);
  assert.match($('costs').textContent,/LLM · полная стоимостьНет данных/);
  assert.match($('costs').textContent,/LLM · известная часть0,25/);
  assert.match($('retention').textContent,/D7Нет данных0 из 0/);
  assert.match($('retention-pending').textContent,/D7 — 2/);
  const initialCards=$('cards').textContent;
  $('next').click();await settled();
  assert.equal(calls.at(-1).url.searchParams.get('usage_offset'),'50');
  assert.equal($('cards').textContent,initialCards);
  assert.equal($('next').disabled,true);assert.equal($('previous').disabled,false);
  $('previous').click();await settled();
  assert.equal(calls.at(-1).url.searchParams.get('usage_offset'),'0');
  $('export').click();await settled();
  assert.equal(calls.at(-1).url.pathname,'/api/admin/export');
  assert.equal(calls.at(-1).url.searchParams.has('usage_offset'),false);
  assert.ok(downloads.includes('beresta-2026-09-30-2026-10-06.csv'));

  const malicious=structuredClone(fixture);malicious.usage[0].model='<img src=x onerror="alert(1)">';
  await reload(()=>reply(malicious));
  assert.equal($('usage').querySelector('img'),null);
  assert.match($('usage').textContent,/<img src=x/);

  for (const code of [401,403,404,503]) {
    await reload(()=>reply(null,code));
    assert.equal($('dashboard').hidden,true);
    assert.equal($('usage').textContent,'');
    assert.equal($('export').disabled,true);
    assert.equal($('login').hidden,code!==401);
    if(code===404)assert.match($('status').textContent,/ещё не подключён/);
  }
  await reload(()=>reply({...fixture,cards:{...fixture.cards,dau:null}}));
  assert.equal($('dashboard').hidden,true);
  await reload(()=>reply(fixture,200,{'X-Next-Usage-Offset':'javascript:bad'}));
  assert.equal($('dashboard').hidden,true);

  let resolveOld;
  nextResponse=()=>new Promise(resolve=>{resolveOld=resolve;});
  $('filters').dispatchEvent(new w.Event('submit',{cancelable:true}));await settled();
  $('channel').value='telegram';$('channel').dispatchEvent(new w.Event('input',{bubbles:true}));
  assert.equal($('dashboard').hidden,true);
  await reload();
  assert.equal(calls.at(-1).url.searchParams.get('channel'),'telegram');
  assert.equal(calls.at(-1).url.searchParams.get('usage_offset'),'0');
  const newSnapshot=$('snapshot').textContent;
  resolveOld(reply({...fixture,cards:{...fixture.cards,dau:999}}));await settled();
  assert.equal($('snapshot').textContent,newSnapshot);
  assert.doesNotMatch($('cards').textContent,/999/);

  $('from').value='2026-10-08';const before=calls.length;await reload();
  assert.equal(calls.length,before);assert.equal($('dashboard').hidden,true);
  $('from').value='2026-09-30';await reload();
  const sameDay=structuredClone(fixture);
  $('from').value='2026-10-07';$('to').value='2026-10-07';await reload(()=>reply(sameDay));
  assert.match($('cards').textContent,/текущего дня · предварительно/);
  nextResponse=()=>reply(null,403);$('export').click();await settled();
  assert.equal($('dashboard').hidden,true);assert.equal($('export').disabled,true);
  assert.equal(w.localStorage.length,0);
  dom.window.close();
  console.log('Admin DOM checks passed: filters, null/zero, cohorts, pagination, CSV, role failures, XSS, stale responses.');
})().catch(error=>{console.error(error);dom.window.close();process.exitCode=1;});
