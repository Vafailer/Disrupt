'use strict';
const $ = id => document.getElementById(id);
let csrf = '', currentNote = null, epoch = 0, notesOffset = 0, pendingCapture = null;
let categories = [], searchOperation = null, notesGeneration = 0, noteBusy = false;
let activeFilter = {q:'',category:''};
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
  const response = await fetch(path, {
    ...options, credentials:'same-origin', headers:{
      'Content-Type':'application/json', 'X-CSRF-Token':csrf, ...options.headers,
    },
  });
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Не удалось выполнить запрос');
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
  if (!list.length && notesOffset === 0) $('notes').append(element('p','Записей не найдено.'));
  for (const note of list) {
    const button = element('button', note.title);
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
  if (!currentNote) return false;
  if (except !== 'note' && ($('title').value !== currentNote.title || $('markdown').value !== currentNote.markdown)) return true;
  if (except !== 'new' && $('item-text').value.trim()) return true;
  for (const [id,editor] of itemEditors) {
    if (id === except) continue;
    if (editor.text.value !== editor.item.text || editor.kind.value !== editor.item.kind || editor.status.value !== editor.item.status) return true;
  }
  return false;
}
function dirty() { return hasDrafts(); }
function setNoteBusy(value) {
  noteBusy = value;
  if (value) {
    for (const control of $('note-card').querySelectorAll('input,textarea,select,button')) {
      if (!busyControls.has(control)) busyControls.set(control,control.disabled);
      control.disabled = true;
    }
  } else {
    for (const [control,disabled] of busyControls) control.disabled = disabled;
    busyControls.clear();
    $('confirm-structure').disabled = Boolean(currentNote?.structure_confirmed_at);
  }
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
  currentNote = note; $('capture-card').hidden = true; $('note-card').hidden = false;
  $('original').textContent = note.original_text; $('title').value = note.title;
  $('markdown').value = note.markdown; renderMarkdown(note.markdown);
  $('original-details').open = false;
  fillCategoryOptions($('note-category'),false,note.category_id || '');
  $('structure-status').textContent = note.structure_confirmed_at ? 'Вы проверили структуру этой записи.' : 'Проверьте текст, задачи и категорию.';
  $('confirm-structure').disabled = Boolean(note.structure_confirmed_at);
  renderItems(note);
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
async function openNote(id, {userAction = false, search = null} = {}) {
  if (noteBusy) return message('Дождитесь сохранения.');
  if (dirty() && !confirm('Есть несохранённые правки. Открыть другую заметку?')) return;
  const currentEpoch = epoch;
  setNoteBusy(true);
  try {
    await loadCategories();
    const note = await api(`/api/v1/notes/${id}`);
    if (currentEpoch !== epoch) return;
    renderNote(note); setNoteBusy(true); message();
    if (userAction) await api(`/api/v1/notes/${id}/opened`,{
      method:'POST',body:JSON.stringify({operation_id:crypto.randomUUID(),search_operation_id:search}),
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
    $('jobs').append(row);
  }
  return jobs;
}
async function pollJob(id, currentEpoch) {
  for (let i=0; i<120 && currentEpoch===epoch; i++) {
    const job = await api(`/api/v1/jobs/${id}`);
    if (currentEpoch!==epoch) return;
    $('job-status').textContent = statusLabels[job.status];
    if (job.status === 'succeeded') {
      $('thought').value = ''; pendingCapture = null;
      await loadNotes(); await loadJobs(); await openNote(job.note_id); return;
    }
    if (job.status === 'failed') {
      pendingCapture = null;
      await loadJobs(); await loadProviderUsage();
      throw new Error(errors[job.error_code] || `Не удалось обработать запись. Она сохранена. Код: ${job.error_code}.`);
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  if (currentEpoch===epoch) message('Запись сохранена. Обработка ещё идёт.');
}
$('auth-form').onsubmit = async event => {
  event.preventDefault();
  const username = $('login').value.trim();
  const password = $('password').value;
  const action = event.submitter?.value || 'login';
  if (!/^[A-Za-zА-Яа-яЁё0-9_.-]{3,64}$/u.test(username)) {
    authMessage('Имя должно содержать от 3 до 64 символов. Можно по-русски.');
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
  const processing_mode = $('processing-mode').value;
  if (!text.trim()) return message('Напишите что-нибудь.');
  // Keep the same key after a network error: retrying must not create another paid job.
  if (!pendingCapture || pendingCapture.text !== text || pendingCapture.processing_mode !== processing_mode) {
    pendingCapture = {text,processing_mode,key:crypto.randomUUID()};
  }
  $('capture-submit').disabled = true; $('thought').disabled=true; $('example').disabled=true;
  $('processing-mode').disabled = true;
  try {
    const job = await api('/api/v1/captures/text',{method:'POST',headers:{'Idempotency-Key':pendingCapture.key},body:JSON.stringify({text,processing_mode})});
    message('Запись сохранена.');
    if (processing_mode === 'manual') {
      $('thought').value = ''; pendingCapture = null;
      await loadNotes(); await openNote(job.note_id);
    } else { await loadJobs(); await pollJob(job.id,epoch); }
  } catch(e) { message(e.message); } finally {
    $('capture-submit').disabled=false; $('thought').disabled=false; $('example').disabled=false;
    $('processing-mode').disabled = false;
  }
};
$('edit-form').onsubmit = async event => {
  event.preventDefault();
  await mutateNote('','PATCH',{title:$('title').value,markdown:$('markdown').value},'Правки сохранены.','note');
};
$('markdown').oninput = () => renderMarkdown($('markdown').value);
$('new-note').onclick = () => {
  if (noteBusy) return message('Дождитесь сохранения.');
  if (dirty() && !confirm('Есть несохранённые правки. Перейти к новой записи?')) return;
  currentNote=null; $('note-card').hidden=true; $('capture-card').hidden=false; message();
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
  if (capture.note_id) await openNote(capture.note_id);
  else if (capture.job) await pollJob(capture.job.id, epoch);
}
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
