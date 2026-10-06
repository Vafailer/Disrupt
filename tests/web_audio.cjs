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
w.URL.createObjectURL = () => 'blob:synthetic-preview';
w.URL.revokeObjectURL = () => {};
let stoppedTracks = 0, activeRecorder = null, rejectMicrophone = false, durationLimit = null;
Object.defineProperty(w.navigator,'mediaDevices',{value:{
  getUserMedia:async () => {
    if (rejectMicrophone) throw new Error('permission denied');
    return {getTracks:() => [{stop:() => stoppedTracks++}]};
  },
}});
w.MediaRecorder = class {
  static isTypeSupported(type) { return type === 'audio/webm;codecs=opus'; }
  constructor(stream,options) { this.state = 'inactive'; this.mimeType = options.mimeType; activeRecorder = this; }
  start() { this.state = 'recording'; }
  stop() {
    this.state = 'inactive';
    this.ondataavailable({data:new w.Blob(['synthetic recording'],{type:this.mimeType})});
    this.onstop();
  }
};
const setTimeout = w.setTimeout.bind(w);
w.setTimeout = (callback,delay,...args) => {
  if (delay === 180000) { durationLimit = callback; return setTimeout(() => {},180000); }
  return setTimeout(callback,delay,...args);
};
const capture = {
  capture_id:randomUUID(),input_kind:'audio',original_text:'',processing_mode:'ai',
  transcript:null,transcript_version:1,transcript_origin:null,audio_seconds:1,audio_media_type:'audio/wav',note_id:null,
};
const job = {
  id:randomUUID(),capture_id:capture.capture_id,status:'failed',error_code:'stt_not_configured',
  provider:'mock',note_id:null,original_text:'',created_at:1791280000,finished_at:1791280001,
};
capture.job = job;
const calls = [], history = [];
let lostUpload = true, uploaded = false, conflictEdit = false, pauseEdit = false, finishEdit = null;
function reply(data,status=200) {
  return {ok:status >= 200 && status < 300,status,headers:new Headers(),json:async () => structuredClone(data)};
}
w.fetch = async (url,options={}) => {
  const method = options.method || 'GET', body = typeof options.body === 'string' ? JSON.parse(options.body) : options.body;
  calls.push({url,options,body,method});
  if (url === '/health' || url === '/api/v1/provider/usage') return reply({simulation:true});
  if (url === '/api/v1/auth/me') return reply({username:'Тест',csrf_token:'synthetic-csrf'});
  if (url.startsWith('/api/v1/notes?') || url === '/api/v1/categories') return reply([]);
  if (url === '/api/v1/jobs') return reply(uploaded ? [job] : []);
  if (url === '/api/v1/captures/audio') {
    assert.ok(body instanceof w.FormData);
    assert.equal(options.headers['Content-Type'],undefined,'Browser must supply the multipart boundary');
    assert.equal(options.headers['X-CSRF-Token'],'synthetic-csrf');
    assert.equal(options.credentials,'same-origin');
    assert.equal(body.get('processing_mode'),'ai');
    assert.equal([...body.keys()].length,2);
    if (lostUpload) { lostUpload = false; throw new Error('Сеть оборвалась'); }
    uploaded = true; return reply(job,202);
  }
  if (url === `/api/v1/jobs/${job.id}`) return reply(job);
  if (url === `/api/v1/captures/${capture.capture_id}`) return reply(capture);
  if (url === `/api/v1/captures/${capture.capture_id}/transcript`) {
    assert.equal(body.version,capture.transcript_version);
    if (conflictEdit) {
      conflictEdit = false; capture.transcript_version++; capture.transcript = 'Правка другой вкладки';
      history.unshift({version:capture.transcript_version,text:capture.transcript,origin:'user',created_at:1791280025});
      return reply({detail:'Расшифровка уже изменена. Обновите её перед сохранением'},409);
    }
    const save = () => {
      capture.transcript = body.text; capture.transcript_version++; capture.transcript_origin = 'user';
      history.unshift({version:capture.transcript_version,text:body.text,origin:'user',created_at:1791280020});
      return reply(capture);
    };
    if (pauseEdit) return new Promise(resolve => {finishEdit = () => resolve(save());});
    return save();
  }
  if (url === `/api/v1/captures/${capture.capture_id}/transcript-revisions`) return reply(history);
  if (url.endsWith('/original-opened')) return reply(null,204);
  throw new Error(`Unexpected request ${method} ${url}`);
};
async function until(condition) {
  for (let n=0;n<100;n++) {
    if (condition()) return;
    await new Promise(resolve => setImmediate(resolve));
  }
  throw new Error(`UI did not settle: ${$('message').textContent}`);
}
function submit(id) { $(id).dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true})); }
function choose(file) {
  Object.defineProperty($('audio-file'),'files',{value:[file],configurable:true});
  $('audio-file').dispatchEvent(new w.Event('change',{bubbles:true}));
}

(async () => {
  try {
    w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8'));
    await until(() => !$('workspace').hidden);
    $('thought').value = 'Не терять текст во время загрузки аудио';
    choose(new w.File(['synthetic file'],'<script>audio.wav',{type:'audio/wav'}));
    assert.equal($('audio-selected').querySelector('script'),null);
    assert.equal($('audio-submit').disabled,false);
    submit('audio-form');
    await until(() => $('message').textContent.includes('Файл остался') && !$('audio-submit').disabled);
    const first = calls.find(c => c.url === '/api/v1/captures/audio');
    assert.equal($('audio-selected').textContent.includes('audio.wav'),true);
    submit('audio-form');
    await until(() => !$('source-card').hidden && $('message').textContent.includes('Распознавание пока') && !$('thought').disabled);
    const uploads = calls.filter(c => c.url === '/api/v1/captures/audio');
    assert.equal(uploads.length,2);
    assert.equal(uploads[1].options.headers['Idempotency-Key'],first.options.headers['Idempotency-Key']);
    assert.equal($('thought').value,'Не терять текст во время загрузки аудио');
    assert.equal($('audio-download').getAttribute('href'),`/api/v1/captures/${capture.capture_id}/audio`);
    assert.equal($('audio-player').getAttribute('src'),$('audio-download').getAttribute('href'));
    assert.equal($('transcript-save').disabled,false);

    $('transcript-text').value = 'Ручная расшифровка'; pauseEdit = true;
    submit('transcript-form'); await until(() => finishEdit !== null);
    assert.equal($('transcript-text').disabled,true);
    $('new-note').click(); assert.equal($('message').textContent,'Дождитесь сохранения.');
    finishEdit(); await until(() => $('transcript-label').textContent.includes('версия 2') && !$('transcript-text').disabled);
    assert.equal($('transcript-text').value,'Ручная расшифровка');
    assert.equal($('source-original').hidden,true);

    pauseEdit = false; conflictEdit = true; $('transcript-text').value = 'Не терять конфликтующую правку';
    submit('transcript-form');
    await until(() => $('message').textContent.includes('уже изменена') && !$('transcript-text').disabled);
    assert.equal($('transcript-text').value,'Не терять конфликтующую правку');
    $('source-refresh').click();
    assert.equal($('message').textContent,'Сначала сохраните правки.');
    $('transcript-compare').click(); await until(() => !$('transcript-remote').hidden && !$('transcript-compare').disabled);
    assert.equal($('transcript-text').value,'Не терять конфликтующую правку');
    assert.ok($('transcript-remote').textContent.includes('Правка другой вкладки'));
    $('transcript-use-version').click();
    assert.equal($('transcript-text').value,'Не терять конфликтующую правку');
    $('transcript-text').value = 'Правка другой вкладки. Моя правка';
    submit('transcript-form');
    await until(() => $('transcript-label').textContent.includes('версия 4') && !$('transcript-save').disabled);
    history.push({version:1,text:'<script>alert(1)</script>',origin:'legacy',created_at:null});
    $('transcript-history-load').click(); await until(() => $('transcript-history').querySelectorAll('details').length === history.length);
    assert.equal($('transcript-history').querySelector('script'),null);
    assert.ok($('transcript-history').textContent.includes('Дата неизвестна'));
    assert.equal(calls.filter(c => c.url === '/api/v1/captures/audio').length,2,'Editing must not call capture/AI again');

    w.confirm = () => true; $('new-note').click();
    $('record-start').click(); await until(() => activeRecorder?.state === 'recording');
    assert.equal($('capture-submit').disabled,true);
    $('record-stop').click();
    assert.equal(stoppedTracks,1);
    assert.ok($('audio-selected').textContent.includes('recording.webm'));
    assert.equal($('audio-submit').disabled,false);
    assert.equal(calls.filter(c => c.url === '/api/v1/captures/audio').length,2,'Stopping recording must not upload');
    $('audio-clear').click();
    $('record-start').click(); await until(() => activeRecorder?.state === 'recording');
    durationLimit();
    assert.equal(stoppedTracks,2);
    assert.equal(activeRecorder.state,'inactive');
    $('audio-clear').click();
    rejectMicrophone = true; $('record-start').click();
    await until(() => $('record-status').textContent.includes('Не удалось открыть') && !$('record-start').disabled);
    assert.equal($('audio-submit').disabled,true);
    choose(new w.File(['x'.repeat(10 * 1024 * 1024 + 1)],'large.wav'));
    assert.equal($('audio-submit').disabled,true);
    assert.ok($('message').textContent.includes('10 МиБ'));
    rejectMicrophone = false; $('record-start').click();
    await until(() => activeRecorder?.state === 'recording');
    activeRecorder.ondataavailable({data:new w.Blob(['x'.repeat(10 * 1024 * 1024 + 1)])});
    assert.equal(stoppedTracks,3);
    assert.equal($('audio-submit').disabled,true);
    assert.ok($('record-status').textContent.includes('10 МиБ'));
    job.status = 'running'; $('new-note').click();
    await until(() => $('jobs').querySelector('button'));
    $('jobs').querySelector('button').click();
    await until(() => !$('source-card').hidden);
    assert.equal($('transcript-save').disabled,true);
    assert.equal($('transcript-text').disabled,true);
    console.log('Audio DOM checks passed: multipart, lost-response replay, text preservation, transcript conflicts/history, safe DOM, recording and microphone cleanup.');
  } finally { dom.window.close(); }
})().catch(error => {console.error(error);process.exitCode = 1;});
