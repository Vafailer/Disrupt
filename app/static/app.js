'use strict';
// v4 UUID. Older Safari and non-secure pages (plain http) have no crypto.randomUUID.
window.berestaId = () => {
  const c = window.crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  const bytes = new Uint8Array(16);
  if (c && typeof c.getRandomValues === 'function') c.getRandomValues(bytes);
  else for (let i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40; bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map(b => b.toString(16).padStart(2,'0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
};
const $ = id => document.getElementById(id);
let csrf = '', currentNote = null, epoch = 0, notesOffset = 0, pendingCapture = null;
let categories = [], searchOperation = null, notesGeneration = 0, noteBusy = false;
let activeFilter = {q:'',category:'',source:''};
const sourceQuery = {telegram:{channel:'telegram'},voice:{input_kind:'audio'},text:{input_kind:'text'}};
let currentCapture = null, selectedAudio = null, pendingAudio = null, captureBusy = false, viewGeneration = 0;
let recorder = null, recordStream = null, recordTimer = null, recordBytes = 0, recordChunks = [], recordInvalid = false, microphonePending = false;
const audioMaximum = 10 * 1024 * 1024;
let localAudioUrl = null;
let comparisonCapture = null;
const itemEditors = new Map(), busyControls = new Map();
const itemLabels = {note:'Заметка', idea:'Идея', task:'Задача', goal:'Цель', plan:'План'};
const statusLabels = {queued:'В очереди', running:'Разбираем запись', succeeded:'Готово', failed:'Не получилось'};
const conclusionLabels = {proposed:'Предложен', accepted:'Принят', rejected:'Отклонён'};
const errors = {
  provider_bad_request:'Cloud.ru отклонил запрос. Проверьте выбранную модель.',
  provider_auth:'Нет доступа к модели. Проверьте настройки сервера.',
  provider_model_not_found:'Модель не найдена. Проверьте её название и доступ команды.',
  provider_invalid_response:'ИИ ответил, но формат результата не удалось принять. Исходник сохранён.',
  provider_rate_limit:'Cloud.ru ограничил запросы. Повтора не было.',
  provider_unavailable:'Cloud.ru сейчас недоступен. Запись сохранена, повтора не было.',
  provider_conflict:'Cloud.ru отклонил запрос. Повтора не было.',
  budget_exhausted:'Лимит приложения исчерпан. Запись сохранена. Для нового запроса увеличьте лимит при запуске.',
  execution_unknown:'Обработка прервалась. Запись сохранена, повтора не было.',
  provider_timeout_unknown:'Модель не ответила вовремя. Запрос мог быть учтён. Повтора не было.',
  stt_not_configured:'Распознавание пока не подключено. Аудио сохранено, можно вписать расшифровку вручную.',
  stt_invalid_response:'Не удалось получить расшифровку. Аудио сохранено, можно вписать текст вручную.',
  stt_failed:'Не удалось распознать запись. Оригинал сохранён.',
  audio_storage_unavailable:'Не удалось прочитать аудио. Попробуйте открыть оригинал позже.',
};
function message(text = '') { $('message').textContent = text; }
function authMessage(text = '') {
  $('auth-message').textContent = text;
  $('auth-message').hidden = !text;
}
const iconPaths = {
  telegram:'m21 3-6 18-4-8-8-4 18-6Zm-10 10 5-5',
  mic:'M12 3a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3ZM6 11a6 6 0 0 0 12 0M12 17v4',
  bell:'M6 9a6 6 0 0 1 12 0c0 6 2 7 2 7H4s2-1 2-7ZM10 20a2 2 0 0 0 4 0',
  bulb:'M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3Z',
  dot:'M12 12h.01',
};
function icon(name, size = 12) {
  const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns,'svg'), path = document.createElementNS(ns,'path');
  for (const [key,value] of Object.entries({viewBox:'0 0 24 24',width:String(size),height:String(size),'aria-hidden':'true',focusable:'false'})) svg.setAttribute(key,value);
  for (const [key,value] of Object.entries({d:iconPaths[name],fill:'none',stroke:'currentColor','stroke-width':'1.8','stroke-linecap':'round','stroke-linejoin':'round'})) path.setAttribute(key,value);
  svg.append(path); return svg;
}
// Web notes written as text carry no tag. Only the Telegram channel and voice input are marked.
function sourceTags(note) {
  const tags = [];
  if (note.channel === 'telegram') {
    const tag = element('span','','tag tag-telegram'); tag.append(icon('telegram'),'Telegram'); tags.push(tag);
  }
  if (note.input_kind === 'audio') {
    const tag = element('span','','tag tag-voice'); tag.append(icon('mic'),'Голос'); tags.push(tag);
  }
  return tags;
}
// Library rows show the source as small icons at the end of the meta line.
function sourceMarks(note) {
  const marks = [];
  if (note.channel === 'telegram') marks.push(['telegram','Из Telegram']);
  if (note.input_kind === 'audio') marks.push(['mic','Голос']);
  return marks.map(([name,label]) => {
    const mark = element('span','',`note-mark note-mark-${name === 'mic' ? 'voice' : 'telegram'}`);
    mark.setAttribute('role','img'); mark.setAttribute('aria-label',label); mark.title = label;
    mark.append(icon(name)); return mark;
  });
}
function element(tag, text, className) {
  const el = document.createElement(tag); el.textContent = text;
  if (className) el.className = className;
  return el;
}
async function api(path, options = {}, withHeaders = false) {
  const headers = {'X-CSRF-Token':csrf,...options.headers};
  if (!(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {
    ...options, credentials:'same-origin', headers,
  });
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : response.status === 422
      ? 'Проверьте заполненные поля.' : 'Не удалось выполнить запрос');
    error.status = response.status; throw error;
  }
  return withHeaders ? {data,headers:response.headers} : data;
}
function showUser(user) {
  csrf = user.csrf_token; $('username').textContent = user.username;
  $('auth').hidden = true; $('account').hidden = false; $('workspace').hidden = false;
}
const LIMIT_SPENT = 'Лимит ИИ на сегодня исчерпан. Запись сохранена без ИИ, разобрать её можно завтра.';
function setMode(text = '') { $('mode').textContent = text; }
// Daily AI limit. The counter is a quiet line. The notice replaces it when the day's units are spent.
const LIMIT_TICK_MS = 30000;
let limitTimer = null, limitResetsAt = null, limitHit = null;
function formatWait(ms) {
  const minutes = Math.ceil(ms / 60000);
  if (!(ms >= 60000)) return 'меньше минуты';
  const hours = Math.floor(minutes / 60), rest = minutes % 60;
  return hours ? `${hours} ч ${rest} мин` : `${rest} мин`;
}
function stopLimitTimer() { if (limitTimer !== null) { clearInterval(limitTimer); limitTimer = null; } }
function renderLimitWait() {
  const ms = Date.parse(limitResetsAt) - Date.now();
  $('ai-limit-wait').textContent = Number.isNaN(ms) ? '' : ms > 0 ? `Обновится через ${formatWait(ms)}.` : 'Скоро обновится.';
  return ms;
}
function limitTick() {
  if (renderLimitWait() > 0) return;
  stopLimitTimer(); limitHit = null;
  loadProviderUsage().catch(() => {});
}
// The switch is the visible control. The hidden #processing-mode select keeps the value and the limit logic.
function syncModeSwitch() {
  const select = $('processing-mode'), toggle = $('ai-switch'), ai = select.querySelector('option[value="ai"]');
  toggle.checked = select.value === 'ai' && !ai.disabled;
  toggle.disabled = select.disabled || ai.disabled;
  $('ai-switch-label').classList.toggle('is-disabled',toggle.disabled);
}
$('ai-switch').onchange = () => {
  const select = $('processing-mode');
  select.value = $('ai-switch').checked && !select.querySelector('option[value="ai"]').disabled ? 'ai' : 'manual';
  syncModeSwitch();
};
function showAiLimit({counter = '', title = '', warn = false, spent = false, appLimit = false} = {}) {
  const note = $('ai-limit-note'), line = $('ai-limit-counter');
  line.hidden = !counter; $('ai-limit-count').textContent = counter;
  line.title = title; $('ai-limit-cost').textContent = title; line.classList.toggle('is-warning',warn);
  note.hidden = !spent; note.classList.toggle('is-warning',spent);
  // Only the first sentence is a live region, and only when it changes. The countdown is never announced.
  const text = spent ? 'Лимит ИИ на сегодня исчерпан.' : '';
  if ($('ai-limit-text').textContent !== text) $('ai-limit-text').textContent = text;
  $('ai-limit-tail').textContent = spent ? 'Записи сохраняются без ИИ.' : '';
  if (spent && limitResetsAt && !appLimit) {
    renderLimitWait();
    if (limitTimer === null && !$('workspace').hidden) limitTimer = setInterval(limitTick,LIMIT_TICK_MS);
  } else { stopLimitTimer(); $('ai-limit-wait').textContent = ''; }
  syncModeSwitch();
}
function markLimitHit() {
  limitHit = {resetsAt: limitResetsAt};
  $('processing-mode').value = 'manual';
  $('processing-mode').querySelector('option[value="ai"]').disabled = true;
  showAiLimit({spent: true});
  loadProviderUsage().catch(() => {});
}
async function loadProviderUsage() {
  const usage = await api('/api/v1/provider/usage');
  const daily = typeof usage.daily_units_remaining === 'number' ? usage.daily_units_remaining : null;
  const total = typeof usage.daily_unit_limit === 'number' && usage.daily_unit_limit > 0 ? usage.daily_unit_limit : null;
  limitResetsAt = typeof usage.limit_resets_at === 'string' ? usage.limit_resets_at : null;
  // A capture that came back without AI keeps the notice until the day changes.
  if (limitHit && limitHit.resetsAt !== limitResetsAt) limitHit = null;
  // A long voice note can cost more than what is left. Units that remain stay usable for text.
  if (limitHit && daily !== null && daily > 0) limitHit = null;
  const appLimit = !usage.simulation && (usage.global_remaining === 0 || usage.user_remaining === 0);
  const spent = daily === 0 || limitHit !== null;
  const exhausted = appLimit || spent;
  // The banner stays empty for the real provider. It speaks only for the demo and for limits.
  if (!usage.simulation) setMode(appLimit ? 'Лимит ИИ исчерпан. Сохранение без ИИ доступно.' : '');
  $('processing-mode').querySelector('option[value="ai"]').disabled = exhausted;
  if (exhausted) $('processing-mode').value = 'manual';
  const showCounter = daily !== null && !exhausted;
  showAiLimit({
    counter: showCounter ? `ИИ на сегодня: осталось ${daily}${total ? ` из ${total}` : ''}` : '',
    title: showCounter && typeof usage.text_unit_cost === 'number'
      ? `Текст ${usage.text_unit_cost}, голос ${usage.audio_unit_base} + ${usage.audio_unit_per_minute} за минуту` : '',
    warn: showCounter && daily <= 3,
    spent: spent && !appLimit,
    appLimit,
  });
}
// A refresh after a save must never break the save itself.
const refreshUsage = () => loadProviderUsage().catch(() => {});
new MutationObserver(() => { if ($('workspace').hidden) { stopLimitTimer(); limitHit = null; } })
  .observe($('workspace'),{attributes:true,attributeFilter:['hidden']});
async function loadNotes(reset = true) {
  if (reset) { notesGeneration++; notesOffset = 0; $('notes').replaceChildren(); }
  const generation = notesGeneration, currentEpoch = epoch, context = searchOperation;
  const query = new URLSearchParams({limit:'20',offset:String(notesOffset)});
  if (activeFilter.q) query.set('q',activeFilter.q);
  if (activeFilter.category) query.set('category_id',activeFilter.category);
  for (const [key,value] of Object.entries(sourceQuery[activeFilter.source] || {})) query.set(key,value);
  const {data:list,headers} = await api(`/api/v1/notes?${query}`,{},true);
  if (generation !== notesGeneration || currentEpoch !== epoch) return;
  if (!list.length && notesOffset === 0) {
    // data-empty tells onboarding.js a truly empty library from an empty search.
    const kind = activeFilter.q || activeFilter.source ? 'search' : activeFilter.category ? 'category' : 'library';
    const searchText = activeFilter.q ? 'Ничего не нашли. Измените запрос или сбросьте поиск.' : 'С этим фильтром записей нет. Выберите «Все».';
    const hint = element('p',{search:searchText,category:'В этой категории пока нет записей.',library:'Пока пусто. Первая запись появится здесь.'}[kind],'empty-hint');
    hint.dataset.empty = kind; $('notes').append(hint);
  }
  for (const note of list) {
    const button = element('button','');
    button.dataset.noteId = note.id;
    button.append(element('span',note.title,'note-row-title'));
    const category = categories.find(c => c.id === note.category_id);
    const when = typeof note.updated_at === 'number' ? new Date(note.updated_at*1000).toLocaleDateString('ru-RU',{day:'numeric',month:'short'}) : '';
    const meta = element('span',[when, category?.name].filter(Boolean).join(' · '),'note-meta');
    for (const mark of sourceMarks(note)) meta.append(mark);
    if (meta.childNodes.length) button.append(meta);
    button.onclick = () => openNote(note.id,{userAction:true,search:context}).catch(e => message(e.message));
    $('notes').append(button);
  }
  notesOffset += list.length; $('more-notes').hidden = !headers.has('X-Next-Notes-Offset');
}
function renderMarkdown(text) {
  const preview = $('preview'); preview.replaceChildren();
  // Deliberately small renderer: text nodes only, no raw HTML or executable links.
  let list = null;
  for (const line of text.split('\n')) {
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    const bullet = line.match(/^[-*]\s+(.+)$/);
    if (bullet) {
      if (!list) { list = document.createElement('ul'); preview.append(list); }
      list.append(element('li',bullet[1]));
    } else {
      list = null;
      if (heading) preview.append(element(`h${heading[1].length}`, heading[2]));
      else if (line.trim()) preview.append(element('p',line));
    }
  }
}
function hasDrafts(except = null) {
  if (except !== 'reminder' && window.BerestaReminders?.dirty()) return true;
  if (except !== 'transcript' && currentCapture?.input_kind === 'audio' &&
      $('transcript-text').value !== (currentCapture.transcript || '')) return true;
  if (!currentNote) return false;
  if (except !== 'note' && ($('title').value !== currentNote.title || $('markdown').value !== currentNote.markdown)) return true;
  if (except !== 'new' && $('item-text').value.trim()) return true;
  for (const [id,editor] of itemEditors) {
    if (id === except) continue;
    if (editor.input && editor.input.value !== editor.item.text) return true;
  }
  return false;
}
function dirty() { return hasDrafts() || Boolean(recorder || microphonePending || selectedAudio || captureBusy || $('thought').value.trim()); }
function setNoteBusy(value) {
  noteBusy = value;
  if (value) {
    for (const control of document.querySelectorAll('#note-card input,#note-card textarea,#note-card select,#note-card button,#source-card textarea,#source-card button')) {
      if (!busyControls.has(control)) busyControls.set(control,control.disabled);
      control.disabled = true;
    }
  } else {
    for (const [control,disabled] of busyControls) control.disabled = disabled;
    busyControls.clear();
    $('confirm-structure').disabled = Boolean(currentNote?.structure_confirmed_at);
    setTranscriptControls();
    document.dispatchEvent(new CustomEvent('beresta:note-idle'));
  }
  window.BerestaReminders?.setLocked(value);
}
async function mutateNote(suffix, method, body, text, except = null) {
  if (noteBusy) return;
  if (hasDrafts(except)) return message('Сначала сохраните остальные правки.');
  const id = currentNote.id, currentEpoch = epoch;
  setNoteBusy(true);
  try {
    const note = await api(`/api/v1/notes/${id}${suffix}`,{method,body:JSON.stringify({...body,version:currentNote.version})});
    if (currentEpoch !== epoch || currentNote?.id !== id) return;
    renderNote(note); setNoteBusy(true);
    await loadNotes(); message(text);
  } catch(e) { message(e.message); }
  finally { setNoteBusy(false); }
}
function renderNoteMeta(note) {
  const when = typeof note.updated_at === 'number'
    ? new Date(note.updated_at*1000).toLocaleDateString('ru-RU',{day:'numeric',month:'long',year:'numeric'}) : '';
  $('note-date').textContent = when; $('note-date').hidden = !when;
  $('note-tags').replaceChildren(...sourceTags(note));
  $('note-mode').replaceChildren(note.provider === 'manual' ? 'Без ИИ' : note.provider === 'mock' ? 'Демо' : 'Обработано ИИ');
  if (note.version > 1) $('note-mode').append(' ',element('span',`v${note.version}`,'note-version'));
}
function renderNote(note) {
  if (note.input_kind === 'audio') renderSource({...note,note_id:note.id,job:null});
  else { currentCapture = null; hideSource(); }
  currentNote = note; $('capture-card').hidden = true; $('note-card').hidden = false;
  $('original').textContent = note.original_text; $('title').value = note.title;
  $('original-details').hidden = note.input_kind === 'audio';
  $('markdown').value = note.markdown; renderMarkdown(note.markdown);
  $('original-details').open = false;
  fillCategoryOptions($('note-category'),false,note.category_id || '');
  const checked = 'Вы проверили структуру этой записи.';
  $('structure-status').textContent = note.structure_confirmed_at ? checked : '';
  $('confirm-structure').title = note.structure_confirmed_at ? checked : 'Проверьте текст, задачи и категорию.';
  $('confirm-structure').classList.toggle('is-confirmed',Boolean(note.structure_confirmed_at));
  $('confirm-structure').disabled = Boolean(note.structure_confirmed_at);
  renderItems(note);
  window.BerestaReminders?.show(note,{request:api,onBusy:setNoteBusy,
    otherDrafts:() => hasDrafts('reminder'),notify:message});
  renderNoteMeta(note);
  $('history').replaceChildren(); $('conclusions').replaceChildren();
  if (!note.conclusions.length) $('conclusions').append(element('p', note.provider === 'mock'
    ? 'В демо выводы есть только у примера.'
    : 'Для этой записи выводов нет.'));
  document.dispatchEvent(new CustomEvent('beresta:note-rendered'));
  for (const c of note.conclusions) {
    const block = element('div','', 'conclusion');
    block.append(element('div', conclusionLabels[c.status], 'status'), element('p',c.text), element('p',c.source_quote,'quote'));
    const actions = element('div','', 'actions');
    for (const [status,label] of [['accepted','Принять'],['rejected','Отклонить']]) {
      const button = element('button',label,'secondary'); button.disabled = c.status === status;
      button.onclick = async () => {
        await mutateNote(`/conclusions/${c.id}`,'PATCH',{status},'Статус вывода сохранён.');
      };
      actions.append(button);
    }
    block.append(actions); $('conclusions').append(block);
  }
}
async function openNote(id, {userAction = false, search = null, reminder = null, backgroundDraft} = {}) {
  if (noteBusy) return message('Дождитесь сохранения.');
  if (recorder || microphonePending) return message('Сначала завершите запись голоса.');
  if (hasDrafts() && !confirm('Есть несохранённые правки. Открыть другую заметку?')) return;
  const generation = ++viewGeneration;
  const currentEpoch = epoch;
  setNoteBusy(true);
  try {
    await loadCategories();
    const note = await api(`/api/v1/notes/${id}`);
    if (currentEpoch !== epoch || generation !== viewGeneration) return;
    if (backgroundDraft !== undefined && !jobDraftUnchanged(backgroundDraft)) return;
    renderNote(note); setNoteBusy(true); message();
    if (userAction) await api(`/api/v1/notes/${id}/opened`,{
      method:'POST',body:JSON.stringify({operation_id:window.berestaId(),search_operation_id:search,reminder_id:reminder}),
    });
  } finally { setNoteBusy(false); }
}
async function loadJobs() {
  const jobs = await api('/api/v1/jobs'); $('jobs').replaceChildren();
  for (const job of jobs) {
    const row = element('div','','job');
    row.append(element('p',`${statusLabels[job.status]} · ${new Date(job.created_at*1000).toLocaleString('ru-RU')}`));
    if (job.status === 'failed') row.append(element('p',errors[job.error_code] || `Не удалось обработать запись. Она сохранена. Код: ${job.error_code}.`));
    if (job.note_id) {
      const button = element('button','Открыть заметку','quiet');
      button.onclick = () => openNote(job.note_id,{userAction:true}).catch(e => message(e.message)); row.append(button);
    } else row.append(element('pre',job.original_text));
    const original = element('button','Открыть исходник','quiet');
    original.onclick = () => openCapture(job.capture_id,{userAction:true}).catch(e => message(e.message));
    row.append(original);
    $('jobs').append(row);
  }
  return jobs;
}
function jobDraftUnchanged(draft) {
  return !captureBusy && !recorder && !microphonePending &&
    !selectedAudio && !hasDrafts() && $('thought').value === draft;
}
function canShowJobResult(generation, draft) {
  return generation === viewGeneration && !noteBusy && jobDraftUnchanged(draft);
}
function watchSavedJob(job, generation, kind = 'text', draft = '') {
  const currentEpoch = epoch;
  (async () => {
    await loadJobs();
    await pollJob(job.id, currentEpoch, {kind,generation,draft});
  })().catch(error => {
    if (currentEpoch === epoch && generation === viewGeneration) {
      message(`Исходник сохранён. Не удалось обновить статус: ${error.message}. Посмотрите последние записи.`);
    }
  });
}
async function pollJob(id, currentEpoch, {kind = 'text', generation = viewGeneration, draft = $('thought').value} = {}) {
  let lastStatus = 'queued';
  for (let i=0; i<120 && currentEpoch===epoch && generation===viewGeneration; i++) {
    const job = await api(`/api/v1/jobs/${id}`);
    if (currentEpoch!==epoch || generation!==viewGeneration) return;
    lastStatus = job.status;
    $('job-status').textContent = job.status === 'queued'
      ? 'В очереди. Исходник сохранён. Можно продолжить работу.' : statusLabels[job.status];
    if (job.status === 'succeeded') {
      await loadNotes(); await loadJobs(); await loadProviderUsage();
      if (currentEpoch!==epoch || generation!==viewGeneration) return;
      if (canShowJobResult(generation,draft)) await openNote(job.note_id,{backgroundDraft:draft});
      else message('Запись обработана. Она доступна в списке заметок.');
      return;
    }
    if (job.status === 'failed') {
      await loadJobs(); await loadProviderUsage();
      if (currentEpoch!==epoch || generation!==viewGeneration) return;
      const error = errors[job.error_code] || `Не удалось обработать запись. Она сохранена. Код: ${job.error_code}.`;
      if (kind === 'audio' && canShowJobResult(generation,draft)) {
        await openCapture(job.capture_id,{backgroundDraft:draft});
        if (currentEpoch===epoch && currentCapture?.capture_id===job.capture_id) message(error);
      } else message(error);
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  if (currentEpoch===epoch && generation===viewGeneration) {
    message(lastStatus === 'queued'
      ? 'Исходник сохранён. Задание пока в очереди. Статус доступен в последних записях.'
      : 'Исходник сохранён. Обработка ещё не завершилась. Статус доступен в последних записях.');
  }
}
$('auth-form').onsubmit = async event => {
  event.preventDefault();
  const username = $('login').value;
  const password = $('password').value;
  const action = event.submitter?.value || 'login';
  if (username.length < 3 || username.length > 64 || /[^A-Za-z0-9_.-]/u.test(username)) {
    authMessage('От 3 до 64 символов. Латинские буквы, цифры и символы _ . -');
    $('login').focus(); return;
  }
  if ([...password].length < 10 || [...password].length > 128) {
    authMessage('Пароль должен содержать от 10 до 128 символов.');
    $('password').focus(); return;
  }
  authMessage();
  const buttons = [...event.target.querySelectorAll('button')];
  buttons.forEach(b => b.disabled=true);
  try {
    const user = await api(`/api/v1/auth/${action}`, {method:'POST',body:JSON.stringify({username,password})});
    $('password').value=''; showUser(user); await loadProviderUsage(); await loadCategories(); await loadNotes(); await loadJobs(); await openLinkedCapture(); message();
  } catch(e) {
    authMessage(e.message);
  } finally { buttons.forEach(b => b.disabled=false); }
};
$('logout').onclick = async () => {
  if (noteBusy || captureBusy || recorder || microphonePending) return message('Дождитесь завершения записи или сохранения.');
  if (dirty() && !confirm('Выйти без сохранения правок?')) return;
  try { await api('/api/v1/auth/logout',{method:'POST'}); epoch++; location.reload(); }
  catch(e) { message(e.message); }
};
$('example').onclick = async () => {
  try {
    if ($('thought').value.trim() && !confirm('Заменить ваш текст примером?')) return;
    $('thought').value = (await api('/api/v1/example')).text;
  } catch(e) { message(e.message); }
};
$('capture-form').onsubmit = async event => {
  event.preventDefault(); const text = $('thought').value;
  if (captureBusy || recorder || microphonePending) return;
  const processing_mode = $('processing-mode').value;
  if (!text.trim()) return message('Напишите что-нибудь.');
  // Keep the same key after a network error: retrying must not create another paid job.
  if (!pendingCapture || pendingCapture.text !== text || pendingCapture.processing_mode !== processing_mode) {
    pendingCapture = {text,processing_mode,key:window.berestaId()};
  }
  const generation = ++viewGeneration;
  let savedJob = null;
  setCaptureBusy(true);
  try {
    const job = await api('/api/v1/captures/text',{method:'POST',headers:{'Idempotency-Key':pendingCapture.key},body:JSON.stringify({text,processing_mode})});
    message('Запись сохранена.');
    // The server may save without AI when the daily limit ends. Tell the person, even if the flag shape changes.
    const downgraded = processing_mode === 'ai' && (job.ai_limit_exceeded === true || job.ai_limit_reached === true || job.processing_mode === 'manual');
    if (downgraded) {
      markLimitHit();
    } else { refreshUsage(); }
    if (processing_mode === 'manual' || downgraded) {
      $('thought').value = ''; pendingCapture = null;
      await loadNotes(); await openNote(job.note_id);
      if (downgraded) message(LIMIT_SPENT);
    } else {
      // The POST acknowledgement confirms storage; a later poll must never clear a new draft.
      $('thought').value = ''; pendingCapture = null; savedJob = job;
      $('job-status').textContent = 'Исходник сохранён. Статус обработки появится в последних записях.';
    }
  } catch(e) { message(e.message); } finally {
    setCaptureBusy(false);
  }
  if (savedJob) watchSavedJob(savedJob,generation);
};
$('thought').addEventListener('keydown',event => {
  if (event.key !== 'Enter' || !(event.ctrlKey || event.metaKey) || event.isComposing) return;
  event.preventDefault();
  if ($('capture-submit').disabled) return;
  if ($('capture-form').requestSubmit) $('capture-form').requestSubmit($('capture-submit'));
  else $('capture-form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
});
$('edit-form').onsubmit = async event => {
  event.preventDefault();
  await mutateNote('','PATCH',{title:$('title').value,markdown:$('markdown').value},'Правки сохранены.','note');
};
$('markdown').oninput = () => renderMarkdown($('markdown').value);
$('new-note').onclick = () => {
  if (noteBusy) return message('Дождитесь сохранения.');
  if (recorder || microphonePending) return message('Сначала завершите запись голоса.');
  if (hasDrafts() && !confirm('Есть несохранённые правки. Перейти к новой записи?')) return;
  viewGeneration++; currentNote=null; currentCapture=null; hideSource();
  window.BerestaReminders?.hide();
  $('note-card').hidden=true; $('capture-card').hidden=false; message();
  if (!$('thought').disabled) $('thought').focus();
  loadJobs().catch(e=>message(e.message));
};
$('more-notes').onclick = () => loadNotes(false).catch(e=>message(e.message));
$('load-history').onclick = async () => {
  try {
    const revisions = await api(`/api/v1/notes/${currentNote.id}/revisions`); $('history').replaceChildren();
    for (const rev of revisions) {
      const detail = document.createElement('details');
      detail.append(element('summary',`Версия ${rev.version} · ${rev.title}`),element('pre',rev.markdown));
      for (const c of rev.conclusions) detail.append(element('p',`${conclusionLabels[c.status]}: ${c.text}`));
      $('history').append(detail);
    }
  } catch(e) { message(e.message); }
};
window.addEventListener('beforeunload',event=>{if(dirty()){event.preventDefault();event.returnValue='';}});
function fillCategoryOptions(select, all, value = select.value) {
  select.replaceChildren();
  if (all) select.append(new Option('Все категории',''));
  select.append(new Option('Без категории',all ? 'none' : ''));
  for (const category of categories) select.append(new Option(category.name,category.id));
  select.value = value;
}
async function loadCategories() {
  categories = await api('/api/v1/categories');
  fillCategoryOptions($('category-filter'),true);
  if (currentNote) fillCategoryOptions($('note-category'),false,currentNote.category_id || '');
  $('categories').replaceChildren();
  for (const category of categories) {
    const row = element('form','','category-row'), input = document.createElement('input');
    input.value = category.name; input.maxLength = 100; input.required = true;
    input.setAttribute('aria-label',`Название категории ${category.name}`);
    const button = element('button','Переименовать','quiet'); button.type = 'submit';
    row.append(input,button);
    row.onsubmit = async event => {
      event.preventDefault(); button.disabled = true;
      try {
        await api(`/api/v1/categories/${category.id}`,{method:'PATCH',body:JSON.stringify({version:category.version,name:input.value})});
        await loadCategories(); message('Название категории сохранено.');
      } catch(e) { message(e.message); } finally { button.disabled = false; }
    };
    $('categories').append(row);
  }
}
$('category-form').onsubmit = async event => {
  event.preventDefault(); const button = event.target.querySelector('button'); button.disabled = true;
  try {
    await api('/api/v1/categories',{method:'POST',body:JSON.stringify({name:$('category-name').value})});
    $('category-name').value = ''; await loadCategories(); message('Категория добавлена.');
  } catch(e) { message(e.message); } finally { button.disabled = false; }
};
let searchSeq = 0, searchTimer = null;
window.searchDebounceMs = 300;
$('search-form').onsubmit = async event => {
  event.preventDefault(); clearTimeout(searchTimer);
  // Typing sends many searches. Only the newest one may change the list.
  const seq = ++searchSeq;
  try {
    const operation = window.berestaId();
    await api('/api/v1/search/events',{method:'POST',body:JSON.stringify({operation_id:operation})});
    if (seq !== searchSeq) return;
    searchOperation = operation;
    activeFilter = {q:$('search-query').value.trim(),category:$('category-filter').value,source:activeFilter.source};
    syncSearchClear();
    await loadNotes(); if (seq === searchSeq) message();
  } catch(e) { if (seq === searchSeq) message(e.message); }
};
function syncSearchClear() { $('clear-search').hidden = !$('search-query').value && !activeFilter.q; }
$('search-query').addEventListener('input',() => {
  syncSearchClear(); clearTimeout(searchTimer);
  if ($('search-query').value.trim() === activeFilter.q) return;
  searchTimer = setTimeout(() => $('search-form').requestSubmit(),window.searchDebounceMs);
});
$('clear-search').onclick = () => {
  clearTimeout(searchTimer); searchSeq++;
  $('search-query').value = ''; $('category-filter').value = ''; searchOperation = null;
  activeFilter = {q:'',category:'',source:''}; syncSourceFilter(); syncSearchClear();
  loadNotes().catch(e => message(e.message));
  $('search-query').focus();
};
syncSearchClear();
function syncSourceFilter() {
  for (const button of $('source-filter').querySelectorAll('button')) {
    button.setAttribute('aria-pressed',String(button.dataset.source === activeFilter.source));
  }
}
for (const button of $('source-filter').querySelectorAll('button')) {
  button.onclick = () => {
    activeFilter = {...activeFilter,source:button.dataset.source}; syncSourceFilter();
    loadNotes().catch(e => message(e.message));
  };
}
$('note-category').onchange = async () => {
  await mutateNote('/category','PATCH',{category_id:$('note-category').value || null},'Категория записи сохранена.');
  $('note-category').value = currentNote.category_id || '';
};
$('confirm-structure').onclick = () => mutateNote('/confirm-structure','POST',{},'Структура подтверждена.');
// Tasks and ideas are a checklist. A row is plain text until it is clicked, then it edits in place.
function saveItem(item, patch, row, restore) {
  const next = {kind:item.kind,text:item.text,status:item.status,...patch};
  if (next.kind !== 'task') next.status = 'open';
  return mutateNote(`/items/${item.id}`,'PATCH',next,'Элемент сохранён.',item.id).then(() => {
    // A refused or failed save re-renders nothing. Put the control back to what is stored.
    if (row.isConnected) restore();
  });
}
function growField(field) {
  field.style.height = 'auto';
  if (field.scrollHeight) field.style.height = `${field.scrollHeight}px`;
}
function updateItemsCount(note) {
  const tasks = note.items.filter(item => item.kind === 'task'), done = tasks.filter(item => item.status === 'completed').length;
  $('items-count').textContent = tasks.length ? `${done} из ${tasks.length} выполнено` : '';
}
function renderItems(note) {
  itemEditors.clear(); $('items').replaceChildren(); $('item-text').value = '';
  updateItemsCount(note);
  if (!note.items.length) $('items').append(element('p','Пока пусто. Добавьте задачу или идею ниже.','muted items-empty'));
  const ordered = [...note.items].sort((a,b) => (a.status === 'completed') - (b.status === 'completed'));
  for (const item of ordered) {
    const row = element('div','','item'), entry = {item,input:null};
    row.dataset.itemId = item.id;
    const task = item.kind === 'task', completed = task && item.status === 'completed';
    row.classList.toggle('is-done',completed); row.classList.toggle('is-idea',!task);
    let lead;
    if (task) {
      lead = document.createElement('input'); lead.type = 'checkbox'; lead.className = 'item-done'; lead.checked = completed;
      lead.setAttribute('aria-label','Задача выполнена');
      lead.onchange = () => saveItem(item,{status:lead.checked ? 'completed' : 'open'},row,() => { lead.checked = completed; });
    } else {
      lead = element('span','','item-mark'); lead.setAttribute('aria-hidden','true');
      lead.append(icon(item.kind === 'idea' ? 'bulb' : 'dot',item.kind === 'idea' ? 15 : 18));
    }
    const text = element('button',item.text,'item-text'); text.type = 'button';
    text.title = 'Нажмите, чтобы изменить';
    const main = element('div','','item-main'); main.append(text);
    if (item.due_text) main.append(element('span',`Срок из записи: «${item.due_text}»`,'item-note muted'));
    if (item.source_quote) {
      const quote = document.createElement('details'); quote.className = 'item-quote';
      quote.append(element('summary','Фрагмент исходника'),element('pre',item.source_quote)); main.append(quote);
    }
    const tools = element('div','','item-tools');
    if (task && item.status === 'open') {
      const remind = element('button','','item-remind'); remind.type = 'button';
      remind.setAttribute('aria-label','Напомнить'); remind.title = 'Напомнить'; remind.append(icon('bell',15));
      remind.onclick = () => window.BerestaReminders?.startForTask(item); tools.append(remind);
    }
    const kind = document.createElement('select'); kind.id = `kind-${item.id}`; kind.className = 'item-kind';
    kind.setAttribute('aria-label','Тип');
    for (const [value,label] of Object.entries(itemLabels)) kind.append(new Option(label,value));
    kind.value = item.kind;
    kind.onchange = () => saveItem(item,{kind:kind.value},row,() => { kind.value = item.kind; });
    tools.append(kind);
    const startEdit = () => {
      if (entry.input || noteBusy) return;
      const input = document.createElement('textarea'); input.className = 'item-edit'; input.rows = 1;
      input.maxLength = 1500; input.value = item.text; input.id = `text-${item.id}`;
      input.setAttribute('aria-label','Текст');
      let finished = false;
      const close = () => { entry.input = null; input.replaceWith(text); };
      const commit = () => {
        if (finished) return; finished = true;
        const value = input.value.trim();
        if (!value || value === item.text) { close(); if (!value) message('Текст не может быть пустым.'); return; }
        input.value = value;
        // If the save did not go through, the editor stays open with the typed text.
        saveItem(item,{text:value},row,() => { finished = false; if (entry.input === input) input.focus(); });
      };
      input.addEventListener('keydown',event => {
        if (event.isComposing) return;
        if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); commit(); }
        else if (event.key === 'Escape') { event.preventDefault(); finished = true; close(); text.focus(); }
      });
      input.addEventListener('input',() => growField(input));
      input.addEventListener('blur',commit);
      entry.input = input; text.replaceWith(input); growField(input); input.focus();
      input.setSelectionRange?.(input.value.length,input.value.length);
    };
    text.onclick = startEdit;
    row.append(lead,main,tools);
    itemEditors.set(item.id,entry); $('items').append(row);
  }
}
function syncItemKind() {
  const kind = $('item-kind').value;
  for (const button of $('item-kind-toggle').querySelectorAll('button')) button.setAttribute('aria-pressed',String(button.dataset.kind === kind));
  $('item-text').placeholder = kind === 'idea' ? '+ Добавить идею' : '+ Добавить задачу';
}
for (const button of $('item-kind-toggle').querySelectorAll('button')) {
  button.onclick = () => { $('item-kind').value = button.dataset.kind; syncItemKind(); $('item-text').focus(); };
}
syncItemKind();
$('item-form').onsubmit = async event => {
  event.preventDefault();
  const text = $('item-text').value.trim();
  if (!text) return;
  await mutateNote('/items','POST',{kind:$('item-kind').value,text},'Элемент добавлен.','new');
};
$('original-details').ontoggle = () => {
  if (!$('original-details').open || !currentNote) return;
  api(`/api/v1/notes/${currentNote.id}/original-opened`,{method:'POST',body:JSON.stringify({operation_id:window.berestaId()})}).catch(e => message(e.message));
};
// Telegram linking lives in telegram-link.js.
async function openLinkedCapture() {
  const id = new URLSearchParams(location.search).get('capture');
  if (!id) return;
  const capture = await api(`/api/v1/captures/${encodeURIComponent(id)}`);
  if (capture.note_id) await openNote(capture.note_id,{userAction:true,reminder:new URLSearchParams(location.search).get('reminder')});
  else if (capture.input_kind === 'audio') {
    await openCapture(id);
    if (capture.job && ['queued','running'].includes(capture.job.status)) {
      watchSavedJob(capture.job,viewGeneration,'audio',$('thought').value);
    }
  } else if (capture.job) watchSavedJob(capture.job,viewGeneration,'text',$('thought').value);
}
function setAudioControls() {
  const recording = Boolean(recorder || microphonePending);
  $('capture-submit').disabled = captureBusy || recording;
  $('audio-file').disabled = captureBusy || recording;
  $('audio-submit').disabled = captureBusy || recording || !selectedAudio;
  $('audio-clear').disabled = captureBusy || recording || !selectedAudio;
  $('record-start').disabled = captureBusy || recording || !recordingMime();
  $('record-stop').disabled = !recorder || recorder.state !== 'recording';
}
function setCaptureBusy(value) {
  captureBusy = value;
  for (const id of ['capture-submit','thought','example','processing-mode']) $(id).disabled = value;
  syncModeSwitch();
  setAudioControls();
}
function clearAudio() {
  selectedAudio = null; pendingAudio = null; $('audio-file').value = '';
  $('audio-selected').textContent = 'Файл не выбран.';
  if (localAudioUrl) URL.revokeObjectURL(localAudioUrl);
  localAudioUrl = null; $('audio-preview').removeAttribute('src'); $('audio-preview').hidden = true;
  setAudioControls();
}
function selectAudio(file) {
  if (!file || file.size === 0 || file.size > audioMaximum) {
    message(file?.size > audioMaximum ? 'Файл должен быть не больше 10 МиБ.' : 'В файле нет аудио.');
    return false;
  }
  clearAudio(); selectedAudio = file;
  $('audio-selected').textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} МиБ`;
  if (typeof URL.createObjectURL === 'function') {
    localAudioUrl = URL.createObjectURL(file);
    $('audio-preview').src = localAudioUrl; $('audio-preview').hidden = false;
  }
  setAudioControls(); return true;
}
$('audio-file').onchange = () => {
  const file = $('audio-file').files[0];
  if (!file) return;
  selectAudio(file);
};
$('audio-clear').onclick = () => { if (!captureBusy && !recorder) clearAudio(); };
$('audio-form').onsubmit = async event => {
  event.preventDefault();
  if (!selectedAudio || captureBusy || recorder || microphonePending) return;
  if (!pendingAudio || pendingAudio.file !== selectedAudio) pendingAudio = {file:selectedAudio,key:window.berestaId()};
  const form = new FormData();
  form.append('audio',pendingAudio.file); form.append('processing_mode','ai');
  const generation = ++viewGeneration, draft = $('thought').value;
  let savedJob = null;
  setCaptureBusy(true); $('job-status').textContent = 'Загружаем аудио…';
  try {
    const job = await api('/api/v1/captures/audio',{method:'POST',headers:{'Idempotency-Key':pendingAudio.key},body:form});
    message('Аудио сохранено.');
    // A successful response confirms durable storage even if polling later loses the network.
    clearAudio();
    if (job.ai_limit_exceeded) {
      message(LIMIT_SPENT); markLimitHit(); $('job-status').textContent = ''; await loadNotes();
    } else {
      savedJob = job; refreshUsage();
      $('job-status').textContent = 'Аудио сохранено. Статус обработки появится в последних записях.';
    }
  } catch(e) {
    message(selectedAudio ? `${e.message}. Файл остался выбранным. Можно повторить загрузку.` : e.message);
  } finally { setCaptureBusy(false); }
  if (savedJob) watchSavedJob(savedJob,generation,'audio',draft);
};
function recordingMime() {
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') return null;
  return ['audio/webm;codecs=opus','audio/ogg;codecs=opus'].find(type => MediaRecorder.isTypeSupported(type)) || null;
}
function releaseMicrophone() {
  clearTimeout(recordTimer); recordTimer = null;
  if (recordStream) for (const track of recordStream.getTracks()) track.stop();
  recordStream = null;
}
function stopRecording() {
  if (recorder?.state === 'recording') { recorder.stop(); $('record-stop').disabled = true; }
}
$('record-start').onclick = async () => {
  const mime = recordingMime(), recordingEpoch = epoch;
  if (!mime || recorder || microphonePending || captureBusy) return;
  if (selectedAudio && !confirm('Заменить выбранный файл новой записью?')) return;
  microphonePending = true; setAudioControls();
  try {
    recordStream = await navigator.mediaDevices.getUserMedia({audio:true});
    if (recordingEpoch !== epoch) { releaseMicrophone(); return; }
    const active = new MediaRecorder(recordStream,{mimeType:mime});
    clearAudio(); recorder = active; recordChunks = []; recordBytes = 0; recordInvalid = false;
    active.ondataavailable = event => {
      if (!event.data.size) return;
      recordBytes += event.data.size;
      if (recordBytes > audioMaximum) {
        recordInvalid = true; $('record-status').textContent = 'Запись превысила 10 МиБ. Запишите более короткий фрагмент.';
        stopRecording(); return;
      }
      if (!recordInvalid) recordChunks.push(event.data);
    };
    active.onerror = () => {
      recordInvalid = true; $('record-status').textContent = 'Не удалось записать голос. Можно загрузить файл.';
      stopRecording(); releaseMicrophone();
    };
    active.onstop = () => {
      if (recorder !== active) return;
      releaseMicrophone(); recorder = null;
      if (!recordInvalid && recordingEpoch === epoch) {
        const file = new File(recordChunks,`recording.${mime.startsWith('audio/ogg') ? 'ogg' : 'webm'}`,{type:mime});
        if (selectAudio(file)) $('record-status').textContent = 'Голос записан. Прослушайте и загрузите файл.';
      }
      recordChunks = []; setAudioControls();
    };
    active.start(1000);
    recordTimer = setTimeout(() => {
      $('record-status').textContent = 'Достигнут предел 3 минуты. Завершаем запись.'; stopRecording();
    },180000);
    $('record-status').textContent = 'Идёт запись. Нажмите «Остановить», когда закончите.';
  } catch(e) {
    recorder = null; releaseMicrophone();
    $('record-status').textContent = 'Не удалось открыть микрофон. Проверьте разрешение браузера или загрузите файл.';
  } finally { microphonePending = false; setAudioControls(); }
};
$('record-stop').onclick = stopRecording;
window.addEventListener('pagehide',() => { epoch++; stopLimitTimer(); stopRecording(); releaseMicrophone(); });

function hideSource() {
  $('source-card').hidden = true;
  $('audio-player').removeAttribute('src'); $('audio-download').removeAttribute('href');
}
function setTranscriptControls() {
  const editable = currentCapture?.input_kind === 'audio' &&
    (currentCapture.note_id || ['failed','succeeded'].includes(currentCapture.job?.status));
  $('transcript-text').disabled = noteBusy || !editable;
  $('transcript-save').disabled = noteBusy || !editable;
}
function renderSource(capture) {
  comparisonCapture = null; $('transcript-conflict').hidden = true;
  $('transcript-remote').hidden = true; $('transcript-use-version').hidden = true;
  currentCapture = capture; $('source-card').hidden = false;
  const audio = capture.input_kind === 'audio';
  $('source-audio').hidden = !audio; $('transcript-editor').hidden = !audio;
  $('source-original').hidden = audio;
  $('source-original').textContent = audio ? '' : capture.original_text;
  if (audio) {
    const url = `/api/v1/captures/${encodeURIComponent(capture.capture_id)}/audio`;
    if ($('audio-player').getAttribute('src') !== url) $('audio-player').src = url;
    $('audio-download').href = url;
    $('audio-info').textContent = capture.audio_seconds == null ? 'Исходный аудиофайл.' : `Длительность ${Math.ceil(capture.audio_seconds)} сек.`;
    $('transcript-text').value = capture.transcript || '';
    const origin = {stt:'Результат распознавания',user:'Текст, исправленный вручную',legacy:'Сохранённая расшифровка'};
    $('transcript-label').textContent = capture.transcript == null
      ? (['queued','running'].includes(capture.job?.status) ? 'Дождитесь завершения обработки.' : 'Расшифровка пока не получена. Можно вписать текст вручную.')
      : `${origin[capture.transcript_origin] || 'Расшифровка'} · версия ${capture.transcript_version}`;
    $('transcript-history').replaceChildren();
  } else { $('audio-player').removeAttribute('src'); $('audio-download').removeAttribute('href'); }
  setTranscriptControls();
}
async function openCapture(id, {userAction = false, backgroundDraft} = {}) {
  if (noteBusy) return message('Дождитесь сохранения.');
  if (recorder || microphonePending) return message('Сначала завершите запись голоса.');
  if (hasDrafts() && !confirm('Есть несохранённые правки. Открыть исходник?')) return;
  const generation = ++viewGeneration, currentEpoch = epoch;
  setNoteBusy(true);
  try {
    const capture = await api(`/api/v1/captures/${encodeURIComponent(id)}`);
    if (generation !== viewGeneration || currentEpoch !== epoch) return;
    if (backgroundDraft !== undefined && !jobDraftUnchanged(backgroundDraft)) return;
    if (currentNote?.capture_id !== id) { currentNote = null; $('note-card').hidden = true; window.BerestaReminders?.hide(); }
    $('capture-card').hidden = true; renderSource(capture);
    if (userAction) await api(`/api/v1/captures/${encodeURIComponent(id)}/original-opened`,{
      method:'POST',body:JSON.stringify({operation_id:window.berestaId()}),
    });
  } finally { setNoteBusy(false); }
}
$('source-refresh').onclick = () => {
  if (hasDrafts()) return message('Сначала сохраните правки.');
  if (currentCapture) openCapture(currentCapture.capture_id).catch(e => message(e.message));
};
$('transcript-form').onsubmit = async event => {
  event.preventDefault();
  if (noteBusy || currentCapture?.input_kind !== 'audio' || $('transcript-save').disabled) return;
  if (hasDrafts('transcript')) return message('Сначала сохраните остальные правки.');
  const text = $('transcript-text').value;
  if (!text.trim() || text.includes('\0')) return message('Расшифровка не должна быть пустой.');
  const id = currentCapture.capture_id, generation = viewGeneration;
  setNoteBusy(true);
  try {
    const capture = await api(`/api/v1/captures/${encodeURIComponent(id)}/transcript`,{
      method:'PATCH',body:JSON.stringify({version:currentCapture.transcript_version,text}),
    });
    if (generation !== viewGeneration || currentCapture?.capture_id !== id) return;
    renderSource(capture);
    if (currentNote?.capture_id === id) Object.assign(currentNote,{
      transcript:capture.transcript,transcript_version:capture.transcript_version,transcript_origin:capture.transcript_origin,
    });
    message('Расшифровка сохранена.');
  } catch(e) {
    if (e.status === 409 && generation === viewGeneration && currentCapture?.capture_id === id) $('transcript-conflict').hidden = false;
    message(e.message);
  }
  finally { setNoteBusy(false); }
};
$('transcript-compare').onclick = async () => {
  if (!currentCapture || noteBusy) return;
  const id = currentCapture.capture_id, generation = viewGeneration;
  setNoteBusy(true);
  try {
    const capture = await api(`/api/v1/captures/${encodeURIComponent(id)}`);
    if (generation !== viewGeneration || currentCapture?.capture_id !== id) return;
    comparisonCapture = capture;
    $('transcript-remote').textContent = `Версия ${capture.transcript_version}\n\n${capture.transcript || 'Расшифровка пока не получена.'}`;
    $('transcript-remote').hidden = false; $('transcript-use-version').hidden = false;
  } catch(e) { message(e.message); }
  finally { setNoteBusy(false); }
};
$('transcript-use-version').onclick = () => {
  if (!comparisonCapture || noteBusy || comparisonCapture.capture_id !== currentCapture?.capture_id) return;
  currentCapture = comparisonCapture; comparisonCapture = null;
  $('transcript-use-version').hidden = true;
  $('transcript-label').textContent = `Правка версии ${currentCapture.transcript_version}. Ваш текст остался в поле. Сравните и сохраните.`;
  setTranscriptControls();
};
$('transcript-history-load').onclick = async () => {
  if (!currentCapture) return;
  const id = currentCapture.capture_id, generation = viewGeneration;
  try {
    const rows = await api(`/api/v1/captures/${encodeURIComponent(id)}/transcript-revisions`);
    if (generation !== viewGeneration || currentCapture?.capture_id !== id) return;
    $('transcript-history').replaceChildren();
    for (const row of rows) {
      const detail = document.createElement('details');
      const date = row.created_at == null ? 'Дата неизвестна' : new Date(row.created_at * 1000).toLocaleString('ru-RU');
      detail.append(element('summary',`Версия ${row.version} · ${date}`),element('pre',row.text));
      $('transcript-history').append(detail);
    }
    if (!rows.length) $('transcript-history').append(element('p','Сохранённых версий пока нет.'));
  } catch(e) { message(e.message); }
};
setAudioControls();
if (!recordingMime()) $('record-status').textContent = 'В этом браузере запись недоступна. Можно загрузить Ogg, WebM или WAV.';

(async () => {
  try {
    const health = await api('/health');
    setMode(health.simulation
      ? 'Демо без ИИ. Для примера есть готовый ответ, остальные записи просто размечаются.' : '');
    try { showUser(await api('/api/v1/auth/me')); await loadProviderUsage(); await loadCategories(); await loadNotes(); await loadJobs(); await openLinkedCapture(); }
    catch(e) { if (!e.message.includes('Войдите') && !e.message.includes('Сессия')) message(e.message); }
  } catch(e) { $('mode').textContent='Не удалось связаться с приложением.'; message(e.message); }
})();
