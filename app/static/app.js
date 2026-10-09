'use strict';
const $ = id => document.getElementById(id);
let csrf = '', currentNote = null, epoch = 0, notesOffset = 0, pendingCapture = null;
let categories = [], searchOperation = null, notesGeneration = 0, noteBusy = false;
let activeFilter = {q:'',category:''};
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
async function loadProviderUsage() {
  const usage = await api('/api/v1/provider/usage');
  if (usage.simulation) return;
  $('mode').textContent = `Cloud.ru · ${usage.model} · обращений в приложении ${usage.global_used}/${usage.global_limit}`;
}
async function loadNotes(reset = true) {
  if (reset) { notesGeneration++; notesOffset = 0; $('notes').replaceChildren(); }
  const generation = notesGeneration, currentEpoch = epoch, context = searchOperation;
  const query = new URLSearchParams({limit:'20',offset:String(notesOffset)});
  if (activeFilter.q) query.set('q',activeFilter.q);
  if (activeFilter.category) query.set('category_id',activeFilter.category);
  const {data:list,headers} = await api(`/api/v1/notes?${query}`,{},true);
  if (generation !== notesGeneration || currentEpoch !== epoch) return;
  if (!list.length && notesOffset === 0) {
    // data-empty tells onboarding.js a truly empty library from an empty search.
    const kind = activeFilter.q ? 'search' : activeFilter.category ? 'category' : 'library';
    const hint = element('p',{search:'Ничего не нашли. Измените запрос или сбросьте поиск.',category:'В этой категории пока нет записей.',library:'Пока пусто. Первая запись появится здесь.'}[kind],'empty-hint');
    hint.dataset.empty = kind; $('notes').append(hint);
  }
  for (const note of list) {
    const button = element('button', note.title);
    button.dataset.noteId = note.id;
    const category = categories.find(c => c.id === note.category_id);
    const when = typeof note.updated_at === 'number' ? new Date(note.updated_at*1000).toLocaleDateString('ru-RU',{day:'numeric',month:'short'}) : '';
    const meta = [when, category?.name].filter(Boolean).join(' · ');
    if (meta) button.append(element('span',meta,'note-meta'));
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
    if (editor.text.value !== editor.item.text || editor.kind.value !== editor.item.kind || editor.status.value !== editor.item.status) return true;
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
function renderNote(note) {
  if (note.input_kind === 'audio') renderSource({...note,note_id:note.id,job:null});
  else { currentCapture = null; hideSource(); }
  currentNote = note; $('capture-card').hidden = true; $('note-card').hidden = false;
  $('original').textContent = note.original_text; $('title').value = note.title;
  $('original-details').hidden = note.input_kind === 'audio';
  $('markdown').value = note.markdown; renderMarkdown(note.markdown);
  $('original-details').open = false;
  fillCategoryOptions($('note-category'),false,note.category_id || '');
  $('structure-status').textContent = note.structure_confirmed_at ? 'Вы проверили структуру этой записи.' : 'Проверьте текст, задачи и категорию.';
  $('confirm-structure').disabled = Boolean(note.structure_confirmed_at);
  renderItems(note);
  window.BerestaReminders?.show(note,{request:api,onBusy:setNoteBusy,
    otherDrafts:() => hasDrafts('reminder'),notify:message});
  $('note-mode').textContent = `Версия ${note.version} · ${note.provider === 'manual' ? 'Без ИИ' : note.provider === 'mock' ? 'Демо' : 'Cloud.ru'}`;
  $('history').replaceChildren(); $('conclusions').replaceChildren();
  if (!note.conclusions.length) $('conclusions').append(element('p', note.provider === 'mock'
    ? 'В демо выводы есть только у примера.'
    : 'Для этой записи выводов нет.'));
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
      method:'POST',body:JSON.stringify({operation_id:crypto.randomUUID(),search_operation_id:search,reminder_id:reminder}),
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
      await loadNotes(); await loadJobs();
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
    pendingCapture = {text,processing_mode,key:crypto.randomUUID()};
  }
  const generation = ++viewGeneration;
  let savedJob = null;
  setCaptureBusy(true);
  try {
    const job = await api('/api/v1/captures/text',{method:'POST',headers:{'Idempotency-Key':pendingCapture.key},body:JSON.stringify({text,processing_mode})});
    message('Запись сохранена.');
    if (processing_mode === 'manual') {
      $('thought').value = ''; pendingCapture = null;
      await loadNotes(); await openNote(job.note_id);
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
$('search-form').onsubmit = async event => {
  event.preventDefault(); const button = event.target.querySelector('button'); button.disabled = true;
  try {
    const operation = crypto.randomUUID();
    await api('/api/v1/search/events',{method:'POST',body:JSON.stringify({operation_id:operation})});
    searchOperation = operation;
    activeFilter = {q:$('search-query').value.trim(),category:$('category-filter').value};
    await loadNotes(); message();
  } catch(e) { message(e.message); } finally { button.disabled = false; }
};
$('clear-search').onclick = () => {
  $('search-query').value = ''; $('category-filter').value = ''; searchOperation = null;
  activeFilter = {q:'',category:''};
  loadNotes().catch(e => message(e.message));
};
$('note-category').onchange = async () => {
  await mutateNote('/category','PATCH',{category_id:$('note-category').value || null},'Категория записи сохранена.');
  $('note-category').value = currentNote.category_id || '';
};
$('confirm-structure').onclick = () => mutateNote('/confirm-structure','POST',{},'Структура подтверждена.');
function renderItems(note) {
  itemEditors.clear(); $('items').replaceChildren(); $('item-text').value = '';
  if (!note.items.length) $('items').append(element('p','Можно добавить задачу или идею вручную.'));
  for (const item of note.items) {
    const form = element('form','','item'), kind = document.createElement('select');
    kind.id = `kind-${item.id}`;
    for (const [value,label] of Object.entries(itemLabels)) kind.append(new Option(label,value));
    kind.value = item.kind;
    const kindLabel = element('label','Тип'); kindLabel.htmlFor = kind.id;
    const text = document.createElement('textarea'); text.id = `text-${item.id}`;
    text.rows = 2; text.maxLength = 1500; text.required = true; text.value = item.text;
    const textLabel = element('label','Текст'); textLabel.htmlFor = text.id;
    const status = document.createElement('select'); status.id = `status-${item.id}`;
    status.append(new Option('В работе','open'),new Option('Выполнена','completed')); status.value = item.status;
    const statusLabel = element('label','Статус'); statusLabel.htmlFor = status.id;
    const updateStatus = () => {
      status.hidden = statusLabel.hidden = kind.value !== 'task';
      if (kind.value !== 'task') status.value = 'open';
    };
    kind.onchange = updateStatus; updateStatus();
    form.append(kindLabel,kind,textLabel,text,statusLabel,status);
    if (item.due_text) form.append(element('p',`Срок из записи «${item.due_text}». Время напоминания ещё не подтверждено.`,'muted'));
    if (item.source_quote) {
      const quote = document.createElement('details');
      quote.append(element('summary','Фрагмент исходника'),element('pre',item.source_quote)); form.append(quote);
    }
    const button = element('button','Сохранить элемент','secondary'); button.type = 'submit'; form.append(button);
    if (item.kind === 'task' && item.status === 'open') {
      const remind = element('button','Напомнить','quiet'); remind.type = 'button';
      remind.onclick = () => window.BerestaReminders?.startForTask(item); form.append(remind);
    }
    form.onsubmit = async event => {
      event.preventDefault();
      await mutateNote(`/items/${item.id}`,'PATCH',{kind:kind.value,text:text.value,status:status.value},'Элемент сохранён.',item.id);
    };
    itemEditors.set(item.id,{item,text,kind,status}); $('items').append(form);
  }
}
$('item-form').onsubmit = async event => {
  event.preventDefault();
  await mutateNote('/items','POST',{kind:$('item-kind').value,text:$('item-text').value},'Элемент добавлен.','new');
};
$('original-details').ontoggle = () => {
  if (!$('original-details').open || !currentNote) return;
  api(`/api/v1/notes/${currentNote.id}/original-opened`,{method:'POST',body:JSON.stringify({operation_id:crypto.randomUUID()})}).catch(e => message(e.message));
};
async function loadTelegramLinks() {
  const links = await api('/api/v1/telegram/links'); $('telegram-links').replaceChildren();
  for (const identity of links.identities) {
    $('telegram-links').append(element('p', `Связан Telegram ID ${identity.telegram_user_id}, бот ${identity.bot_id}.`));
  }
  for (const link of links.pending) {
    const button = element('button', `Подтвердить мой Telegram ID ${link.telegram_user_id}`, 'secondary');
    button.onclick = async () => {
      button.disabled = true;
      try {
        await api(`/api/v1/telegram/links/${link.link_request_id}/confirm`, {method:'POST'});
        $('telegram-code').textContent = ''; await loadTelegramLinks(); message('Telegram связан.');
      } catch(e) { message(e.message); button.disabled = false; }
    };
    $('telegram-links').append(button);
  }
  if (!links.pending.length && !links.identities.length) $('telegram-links').append(element('p', 'Запросов пока нет.'));
}
$('link-code').onclick = async () => {
  try {
    const link = await api('/api/v1/telegram/link-code', {method:'POST'});
    $('telegram-code').textContent = `/start ${link.code}`;
    await loadTelegramLinks();
  } catch(e) { message(e.message); }
};
$('refresh-links').onclick = () => loadTelegramLinks().catch(e => message(e.message));
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
  if (!pendingAudio || pendingAudio.file !== selectedAudio) pendingAudio = {file:selectedAudio,key:crypto.randomUUID()};
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
    savedJob = job;
    $('job-status').textContent = 'Аудио сохранено. Статус обработки появится в последних записях.';
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
window.addEventListener('pagehide',() => { epoch++; stopRecording(); releaseMicrophone(); });

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
      method:'POST',body:JSON.stringify({operation_id:crypto.randomUUID()}),
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
    $('mode').textContent = health.simulation
      ? 'Демо без ИИ. Для примера есть готовый ответ, остальные записи просто размечаются.'
      : 'Cloud.ru выбран. Подключение проверится после первой готовой заметки.';
    try { showUser(await api('/api/v1/auth/me')); await loadProviderUsage(); await loadCategories(); await loadNotes(); await loadJobs(); await openLinkedCapture(); }
    catch(e) { if (!e.message.includes('Войдите') && !e.message.includes('Сессия')) message(e.message); }
  } catch(e) { $('mode').textContent='Не удалось связаться с приложением.'; message(e.message); }
})();
