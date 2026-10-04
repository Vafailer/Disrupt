'use strict';
const $ = id => document.getElementById(id);
let csrf = '', currentNote = null, epoch = 0, notesOffset = 0, pendingCapture = null;
const statusLabels = {queued:'В очереди', running:'Обработка', succeeded:'Готово', failed:'Ошибка'};
const conclusionLabels = {proposed:'Предложен', accepted:'Принят', rejected:'Отклонён'};
const errors = {
  provider_bad_request:'Cloud.ru отклонил параметры запроса (HTTP 400/422). Проверьте совместимость выбранной модели.',
  provider_auth:'Нет доступа к модели. Проверьте настройки сервера.',
  provider_model_not_found:'Cloud.ru не нашёл выбранную модель (HTTP 404). Проверьте ID модели и доступ команды.',
  provider_rate_limit:'Провайдер ограничил запросы. Автоматического повтора не будет.',
  provider_unavailable:'Cloud.ru временно недоступен (HTTP 5xx). Оригинал сохранён; автоматического повтора не было.',
  provider_conflict:'Cloud.ru отклонил запрос из-за конфликта. Автоматического повтора не было.',
  budget_exhausted:'Достигнут лимит обращений, заданный при запуске приложения. Исходный текст сохранён. Чтобы сделать ещё один запрос, перезапустите сервер и укажите лимит больше числа уже учтённых обращений.',
  execution_unknown:'Обработка прервалась. Оригинал сохранён; автоматического повтора не будет.',
  provider_timeout_unknown:'Модель не ответила вовремя. Запрос мог быть оплачен; повтор не выполнялся.',
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
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options, credentials:'same-origin', headers:{
      'Content-Type':'application/json', 'X-CSRF-Token':csrf, ...options.headers,
    },
  });
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Не удалось выполнить запрос');
  return data;
}
function showUser(user) {
  csrf = user.csrf_token; $('username').textContent = user.username;
  $('auth').hidden = true; $('account').hidden = false; $('workspace').hidden = false;
}
async function loadProviderUsage() {
  const usage = await api('/api/v1/provider/usage');
  if (usage.simulation) return;
  $('mode').textContent = `Режим Cloud.ru · модель ${usage.model} · использовано ${usage.global_used} из ${usage.global_limit}, осталось ${usage.global_remaining}. Подключение подтвердится после первой готовой заметки.`;
}
async function loadNotes(reset = true) {
  if (reset) { notesOffset = 0; $('notes').replaceChildren(); }
  const list = await api(`/api/v1/notes?limit=20&offset=${notesOffset}`);
  if (!list.length && notesOffset === 0) $('notes').append(element('p','Здесь появятся ваши заметки.'));
  for (const note of list) {
    const button = element('button', note.title);
    button.onclick = () => openNote(note.id).catch(e => message(e.message));
    $('notes').append(button);
  }
  notesOffset += list.length; $('more-notes').hidden = list.length < 20;
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
function dirty() {
  return currentNote && ($('title').value !== currentNote.title || $('markdown').value !== currentNote.markdown);
}
function renderNote(note) {
  currentNote = note; $('capture-card').hidden = true; $('note-card').hidden = false;
  $('original').textContent = note.original_text; $('title').value = note.title;
  $('markdown').value = note.markdown; renderMarkdown(note.markdown);
  $('note-mode').textContent = `Версия ${note.version} · ${note.provider === 'mock' ? 'Демонстрационная обработка' : 'Cloud.ru'}`;
  $('history').replaceChildren(); $('conclusions').replaceChildren();
  if (!note.conclusions.length) $('conclusions').append(element('p', note.provider === 'mock'
    ? 'Выводов нет. В режиме имитации они подготовлены только для встроенного примера.'
    : 'Для этой записи модель не предложила дополнительных выводов.'));
  for (const c of note.conclusions) {
    const block = element('div','', 'conclusion');
    block.append(element('div', conclusionLabels[c.status], 'status'), element('p',c.text), element('p',c.source_quote,'quote'));
    const actions = element('div','', 'actions');
    for (const [status,label] of [['accepted','Принять'],['rejected','Отклонить']]) {
      const button = element('button',label,'secondary'); button.disabled = c.status === status;
      button.onclick = async () => {
        if (dirty()) return message('Сначала сохраните правки заметки, затем выберите статус вывода.');
        button.disabled = true;
        try {
          renderNote(await api(`/api/v1/notes/${note.id}/conclusions/${c.id}`, {
            method:'PATCH',body:JSON.stringify({version:currentNote.version,status}),
          })); message('Статус вывода сохранён.');
        } catch (e) { message(e.message); button.disabled = false; }
      };
      actions.append(button);
    }
    block.append(actions); $('conclusions').append(block);
  }
}
async function openNote(id) {
  if (dirty() && !confirm('Есть несохранённые правки. Открыть другую заметку?')) return;
  renderNote(await api(`/api/v1/notes/${id}`)); message();
}
async function loadJobs() {
  const jobs = await api('/api/v1/jobs'); $('jobs').replaceChildren();
  for (const job of jobs) {
    const row = element('div','','job');
    row.append(element('p',`${statusLabels[job.status]} · ${new Date(job.created_at*1000).toLocaleString('ru-RU')}`));
    if (job.status === 'failed') row.append(element('p',errors[job.error_code] || `Обработка не завершилась (${job.error_code}). Оригинал сохранён.`));
    if (job.note_id) {
      const button = element('button','Открыть заметку','quiet');
      button.onclick = () => openNote(job.note_id).catch(e => message(e.message)); row.append(button);
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
      throw new Error(errors[job.error_code] || `Ошибка обработки: ${job.error_code}. Оригинал сохранён. Повторная отправка создаст новый запрос.`);
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  if (currentEpoch===epoch) message('Запись сохранена. Обработка ещё продолжается — проверьте последние задания позже.');
}
$('auth-form').onsubmit = async event => {
  event.preventDefault();
  const username = $('login').value.trim();
  const password = $('password').value;
  const action = event.submitter?.value || 'login';
  if (!/^[A-Za-zА-Яа-яЁё0-9_.-]{3,64}$/u.test(username)) {
    authMessage('Имя должно содержать от 3 до 64 символов. Можно использовать русские и латинские буквы, цифры, точку, дефис и подчёркивание.');
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
    $('password').value=''; showUser(user); await loadProviderUsage(); await loadNotes(); await loadJobs(); message();
  } catch(e) {
    authMessage(action === 'login' && e.message === 'Неверное имя или пароль'
      ? 'Неверное имя или пароль. Если вы впервые вошли в этот режим, нажмите «Создать аккаунт».'
      : e.message);
  } finally { buttons.forEach(b => b.disabled=false); }
};
$('logout').onclick = async () => {
  if (dirty() && !confirm('Выйти без сохранения правок?')) return;
  try { await api('/api/v1/auth/logout',{method:'POST'}); epoch++; location.reload(); }
  catch(e) { message(e.message); }
};
$('example').onclick = async () => {
  try {
    if ($('thought').value.trim() && !confirm('Заменить введённый текст демонстрационным примером?')) return;
    $('thought').value = (await api('/api/v1/example')).text;
  } catch(e) { message(e.message); }
};
$('capture-form').onsubmit = async event => {
  event.preventDefault(); const text = $('thought').value;
  if (!text.trim()) return message('Добавьте хотя бы одну мысль.');
  // Keep the same key after a network error: retrying must not create another paid job.
  if (!pendingCapture || pendingCapture.text !== text) pendingCapture = {text,key:crypto.randomUUID()};
  $('capture-submit').disabled = true; $('thought').disabled=true; $('example').disabled=true;
  try {
    const job = await api('/api/v1/captures/text',{method:'POST',headers:{'Idempotency-Key':pendingCapture.key},body:JSON.stringify({text})});
    message('Оригинал сохранён.'); await loadJobs(); await pollJob(job.id,epoch);
  } catch(e) { message(e.message); } finally {
    $('capture-submit').disabled=false; $('thought').disabled=false; $('example').disabled=false;
  }
};
$('edit-form').onsubmit = async event => {
  event.preventDefault(); const button = event.target.querySelector('button'); button.disabled=true;
  try {
    renderNote(await api(`/api/v1/notes/${currentNote.id}`,{method:'PATCH',body:JSON.stringify({version:currentNote.version,title:$('title').value,markdown:$('markdown').value})}));
    await loadNotes(); message('Правки сохранены. Оригинал не изменён.');
  } catch(e) { message(e.message); } finally { button.disabled=false; }
};
$('markdown').oninput = () => renderMarkdown($('markdown').value);
$('new-note').onclick = () => {
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
(async () => {
  try {
    const health = await api('/health');
    $('mode').textContent = health.simulation
      ? 'Демонстрационный режим: запросы к ИИ не отправляются. Встроенный пример использует подготовленный ответ; другой текст получает только простую разметку.'
      : 'Режим Cloud.ru выбран. Подключение подтвердится после первой готовой заметки.';
    try { showUser(await api('/api/v1/auth/me')); await loadProviderUsage(); await loadNotes(); await loadJobs(); }
    catch(e) { if (!e.message.includes('Войдите') && !e.message.includes('Сессия')) message(e.message); }
  } catch(e) { $('mode').textContent='Не удалось связаться с приложением.'; message(e.message); }
})();
