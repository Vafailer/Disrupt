'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {randomUUID} = require('node:crypto');
const {JSDOM} = require('jsdom');

async function until(condition) {
  for (let i=0;i<150;i++) {
    if (condition()) return;
    await new Promise(resolve => setImmediate(resolve));
  }
  throw new Error('Queued-note UI did not settle');
}
async function settle() { for (let i=0;i<20;i++) await new Promise(resolve => setImmediate(resolve)); }

async function scenario({lostAck = false, audio = false} = {}) {
  const root = path.join(__dirname,'../app/static');
  const dom = new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{
    url:'https://beresta.invalid/',runScripts:'outside-only',
  });
  const w = dom.window, $ = id => w.document.getElementById(id);
  Object.defineProperty(w.crypto,'randomUUID',{value:randomUUID});
  w.confirm = () => false;
  w.URL.createObjectURL = () => 'blob:synthetic'; w.URL.revokeObjectURL = () => {};
  const job = {id:randomUUID(),capture_id:randomUUID(),status:'queued',provider:'mock',note_id:null,
    original_text:'Synthetic queued text',created_at:1791280000,error_code:null};
  const note = id => ({id,capture_id:job.capture_id,title:'Synthetic note',original_text:'Synthetic',
    markdown:'Synthetic',conclusions:[],items:[],provider:'mock',version:1,category_id:null,
    structure_confirmed_at:null,created_at:1791280000,updated_at:1791280000});
  const calls = [], keys = new Set();
  let saved = false, manual = false, releasePoll = null;
  function reply(data,status=200) {
    return {ok:status>=200 && status<300,status,headers:new Headers(),json:async () => structuredClone(data)};
  }
  w.fetch = async (input,options={}) => {
    const url = new URL(input,w.location.href), method = options.method || 'GET';
    calls.push({path:url.pathname,method,options});
    if (url.pathname === '/health' || url.pathname === '/api/v1/provider/usage') return reply({simulation:true});
    if (url.pathname === '/api/v1/auth/me') return reply({username:'synthetic',csrf_token:'synthetic-csrf'});
    if (url.pathname === '/api/v1/categories') return reply([]);
    if (url.pathname === '/api/v1/jobs') return reply(saved ? [job] : []);
    if (url.pathname === '/api/v1/notes') return reply(manual ? [{id:'manual-note',title:'Manual note'}] : []);
    if (url.pathname.startsWith('/api/v1/notes/')) return reply(note(url.pathname.split('/').at(-1)));
    if (url.pathname === '/api/v1/captures/text' || url.pathname === '/api/v1/captures/audio') {
      if (url.pathname.endsWith('/text') && JSON.parse(options.body).processing_mode === 'manual') {
        manual = true; return reply({note_id:'manual-note'},201);
      }
      keys.add(options.headers['Idempotency-Key']); saved = true;
      if (lostAck) { lostAck = false; throw new Error('Synthetic lost acknowledgement'); }
      return reply(job,202);
    }
    if (url.pathname === `/api/v1/jobs/${job.id}`) return new Promise(resolve => { releasePoll = resolve; });
    if (url.pathname.endsWith('/original-opened')) return reply(null,204);
    throw new Error(`Unexpected synthetic request ${method} ${url.pathname}`);
  };
  const submit = id => $(id).dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
  function choose(name) {
    Object.defineProperty($('audio-file'),'files',{value:[new w.File(['synthetic'],name,{type:'audio/wav'})],configurable:true});
    $('audio-file').dispatchEvent(new w.Event('change',{bubbles:true}));
  }
  w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8'));
  await until(() => !$('workspace').hidden);
  if (audio) { choose('first.wav'); submit('audio-form'); }
  else {
    $('thought').value = 'Synthetic queued text'; submit('capture-form');
    if (lostAck) await until(() => !$('capture-submit').disabled);
    // A lost POST response must keep the draft and retry the same durable operation.
    if (!releasePoll) {
      await settle();
      if ($('message').textContent.includes('Synthetic lost')) {
        assert.equal($('thought').value,'Synthetic queued text'); submit('capture-form');
      }
    }
  }
  await until(() => releasePoll !== null);
  assert.equal($('capture-submit').disabled,false,'Polling must not lock the form');
  assert.equal($('thought').disabled,false);
  assert.equal($('processing-mode').disabled,false);
  assert.equal(keys.size,1,'A lost acknowledgement must not create another operation');
  if (!audio) assert.equal($('thought').value,'','Clear only after storage acknowledgement');
  return {dom,$,w,job,calls,submit,choose,reply,finish:() => releasePoll(reply(job)),
    failPoll:() => releasePoll(reply({detail:'Synthetic status outage'},503))};
}

module.exports = async function checkJobWaiting() {
  let s;
  try {
    s = await scenario({lostAck:true});
    assert.equal(s.calls.filter(c => c.path === '/api/v1/captures/text').length,2);
    s.$('thought').value = 'New unsent draft';
    s.job.status = 'succeeded'; s.job.note_id = 'ai-note'; s.finish(); await settle();
    assert.equal(s.$('thought').value,'New unsent draft');
    assert.equal(s.$('note-card').hidden,true,'A finished job must not navigate over a new draft');
    assert.equal(s.calls.some(c => c.path === '/api/v1/notes/ai-note'),false);
  } finally { s?.dom.window.close(); }
  try {
    s = await scenario();
    s.$('processing-mode').value = 'manual'; s.$('thought').value = 'Manual while queued';
    s.submit('capture-form');
    await until(() => !s.$('note-card').hidden && !s.$('capture-submit').disabled);
    const before = s.$('title').value;
    s.job.status = 'succeeded'; s.job.note_id = 'old-ai-note'; s.finish(); await settle();
    assert.equal(s.$('title').value,before,'A stale poll must not replace a newer manual note');
    assert.equal(s.calls.some(c => c.path === '/api/v1/notes/old-ai-note'),false);
    assert.equal(s.calls.filter(c => c.path === '/api/v1/captures/text').length,2);
  } finally { s?.dom.window.close(); }
  try {
    s = await scenario({audio:true});
    s.choose('new-draft.wav');
    s.job.status = 'succeeded'; s.job.note_id = 'audio-note'; s.finish(); await settle();
    assert.ok(s.$('audio-selected').textContent.includes('new-draft.wav'));
    assert.equal(s.$('audio-submit').disabled,false);
    assert.equal(s.calls.filter(c => c.path === '/api/v1/captures/audio').length,1);
    assert.equal(s.calls.some(c => c.path === '/api/v1/notes/audio-note'),false);
  } finally { s?.dom.window.close(); }
  for (const status of ['queued','running']) {
    try {
      s = await scenario(); s.job.status = status; s.finish();
      await until(() => s.$('job-status').textContent.includes(status === 'queued' ? 'В очереди' : 'Разбираем'));
      assert.equal(s.$('capture-submit').disabled,false);
      assert.equal(s.$('thought').disabled,false);
      assert.equal(s.calls.filter(c => c.path === '/api/v1/captures/text').length,1);
    } finally { s?.dom.window.close(); }
  }
  try {
    s = await scenario(); s.$('thought').value = 'Draft during status outage'; s.failPoll();
    await until(() => s.$('message').textContent.includes('Исходник сохранён. Не удалось обновить статус'));
    assert.equal(s.$('thought').value,'Draft during status outage');
    assert.equal(s.$('capture-submit').disabled,false);
    assert.equal(s.calls.filter(c => c.path === '/api/v1/captures/text').length,1);
  } finally { s?.dom.window.close(); }
  console.log('Queued-job checks passed: unlocked form, lost-ack idempotency, manual saves, draft and navigation preservation.');
};

if (require.main === module) module.exports().catch(error => {console.error(error);process.exitCode=1;});
