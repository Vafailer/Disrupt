'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const node = (tag, text, className = '') => {
    const result = document.createElement(tag); result.textContent = text; result.className = className; return result;
  };
  let note = null, bridge = null, generation = 0, listRequest = 0, timeRequest = 0;
  let locked = false, busy = false, editing = null, baseline = '', preview = null, comparison = null, conflicted = false;
  let pendingCreate = null, uncertainEdit = null, rows = [], more = false, timer = null;
  let resolveTimer = null, pick = '', rememberedZone = '', telegram = {linked:null,error:false};
  const labels = {confirmed:'Подтверждено',sent:'Отправлено',blocked:'Telegram недоступен',
    unknown:'Результат отправки неизвестен',cancelled:'Отменено'};
  const deliveryLabels = {pending:'Ожидает отправки',leased:'Готовится к отправке',authorized:'Отправляется',
    retryable:'Ожидает повтора после отказа Telegram',sent:'Отправлено',blocked:'Telegram недоступен',
    unknown:'Результат отправки неизвестен',cancelled:'Попытка отменена'};
  const weekdaysTo = ['в воскресенье','в понедельник','во вторник','в среду','в четверг','в пятницу','в субботу'];
  const weekdaysShort = ['Вс','Пн','Вт','Ср','Чт','Пт','Сб'];
  const months = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря'];
  const monthsShort = ['янв','фев','мар','апр','мая','июн','июл','авг','сен','окт','ноя','дек'];
  const zoneNames = {'Europe/Moscow':'Москва','Europe/Berlin':'Берлин','Asia/Yekaterinburg':'Екатеринбург',
    'Asia/Novosibirsk':'Новосибирск','Asia/Vladivostok':'Владивосток',UTC:'UTC'};
  const zoneKey = 'beresta.reminderZone';
  function validZone(zone) {
    try { new Intl.DateTimeFormat('en-CA',{timeZone:zone}); return Boolean(zone); } catch (_) { return false; }
  }
  function defaultZone() {
    let saved = rememberedZone;
    if (!saved) { try { saved = localStorage.getItem(zoneKey) || ''; } catch (_) { saved = ''; } }
    if (saved && validZone(saved)) return saved;
    const browser = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return browser && validZone(browser) ? browser : 'Europe/Moscow';
  }
  function rememberZone(zone) {
    rememberedZone = zone;
    try { localStorage.setItem(zoneKey,zone); } catch (_) { /* storage may be blocked */ }
  }
  function zoneShort(zone) { return zoneNames[zone] || zone; }
  function zoneLine(zone) { return zoneNames[zone] ? `${zoneNames[zone]} (${zone})` : zone; }
  function debounceDelay() { return typeof window.reminderDebounceMs === 'number' ? window.reminderDebounceMs : 300; }
  // Wall-clock strings "YYYY-MM-DDTHH:MM[:SS]" are shifted as plain UTC numbers, so no browser zone leaks in.
  function wallMs(value) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/.exec(value);
    return Date.UTC(+m[1],+m[2]-1,+m[3],+m[4],+m[5],+(m[6] || 0));
  }
  function wallFromMs(ms) { return new Date(ms).toISOString().slice(0,16); }
  function roundUp5(value) { return wallFromMs(Math.ceil(wallMs(value) / 300000) * 300000); }
  function hm(value) { return `${+value.slice(11,13)}:${value.slice(14,16)}`; }
  function dayParts(value) { const d = new Date(wallMs(value.slice(0,10) + 'T00:00')); return {d:d.getUTCDate(),m:d.getUTCMonth(),y:d.getUTCFullYear(),w:d.getUTCDay()}; }
  function nowWall(zone) { return wallTime(Date.now(),zone); }
  function whenWords(local, zone) {
    const day = dayParts(local), year = dayParts(nowWall(zone)).y;
    return `${weekdaysTo[day.w]}, ${day.d} ${months[day.m]}${day.y === year ? '' : ' ' + day.y + ' года'}, в ${hm(local)} (${zoneShort(zone)})`;
  }
  function whenShort(local, zone) {
    const day = dayParts(local), year = dayParts(nowWall(zone)).y;
    return `${weekdaysShort[day.w]}, ${day.d} ${monthsShort[day.m]}${day.y === year ? '' : ' ' + day.y}, ${hm(local)}`;
  }
  function pickTime(kind, zone) {
    const now = nowWall(zone).slice(0,16), midnight = wallMs(now.slice(0,10) + 'T00:00');
    const at = (days, time) => `${wallFromMs(midnight + days * 86400000).slice(0,10)}T${time}`;
    if (kind === 'hour') return roundUp5(wallTime(Date.now() + 3600000,zone));
    if (kind === 'today') return at(0,'19:00');
    if (kind === 'monday') return at((8 - dayParts(now).w) % 7 || 7,'09:00');
    if (kind === 'week') return roundUp5(new Date(wallMs(nowWall(zone)) + 7 * 86400000).toISOString().slice(0,19));
    return at(1,'09:00');
  }
  function say(text = '') { $('reminders-message').textContent = text; }
  function fields() { return {item_id:$('reminder-target').value || null, text:$('reminder-text').value,
    local_time:$('reminder-local').value, timezone:$('reminder-zone').value.trim()}; }
  function fingerprint() { return JSON.stringify(fields()); }
  function dirty() { return !$('reminder-form').hidden && Boolean(pendingCreate || uncertainEdit || fingerprint() !== baseline); }
  function selectedTime() {
    if (!preview || preview.local_time !== fields().local_time || preview.timezone !== fields().timezone) return null;
    return preview.choices.find(row => row.scheduled_at === $('reminder-time-choice').value && row.is_future) || null;
  }
  function controls() {
    $('reminder-controls').disabled = locked || busy;
    $('reminder-fields').disabled = Boolean(pendingCreate || uncertainEdit);
    $('reminder-target').disabled = Boolean(editing);
    $('reminder-confirm').disabled = Boolean(conflicted || comparison || (!pendingCreate && !uncertainEdit && !selectedTime()));
    $('reminder-confirm').textContent = pendingCreate ? 'Повторить подтверждение' : uncertainEdit
      ? 'Проверить сохранение' : editing ? 'Сохранить изменения' : 'Напомнить';
    $('reminders-more').hidden = !more;
  }
  function setWork(value) { busy = value; bridge.onBusy(value); controls(); }
  function setLocked(value) { locked = value; controls(); }
  function invalidateTime() {
    timeRequest++; preview = null; clearTimeout(resolveTimer); resolveTimer = null;
    $('reminder-preview').textContent = ''; $('reminder-time-choices').hidden = true; controls();
  }
  function scheduleResolve(delay = debounceDelay()) {
    clearTimeout(resolveTimer);
    resolveTimer = setTimeout(() => { resolveTimer = null; checkTime(); },delay);
  }
  function timeChanged() { invalidateTime(); scheduleResolve(); }
  function drawPreview() {
    const choice = selectedTime();
    let text = '';
    if (choice) text = `Напомню ${whenWords(choice.local_at,preview.timezone)}.\n${fields().text}`;
    else if (preview && !preview.choices.some(row => row.is_future)) text = 'Это время уже прошло. Выберите другое.';
    else if (preview?.ambiguous) text = 'Это время наступит дважды из-за перевода часов. Выберите, какое нужно.';
    $('reminder-preview').textContent = text;
    controls();
  }
  function setLocal(value) {
    // Source of truth stays in the hidden #reminder-local as "YYYY-MM-DDTHH:MM:00".
    const minutes = value ? value.slice(0,16) : '';
    $('reminder-local').value = minutes ? minutes + ':00' : '';
    $('reminder-date').value = minutes.slice(0,10); $('reminder-time').value = minutes.slice(11,16);
    $('reminder-time').step = minutes && +minutes.slice(14,16) % 5 ? 60 : 300;
  }
  function syncLocal() {
    const date = $('reminder-date').value, time = $('reminder-time').value.slice(0,5);
    $('reminder-local').value = date && time ? `${date}T${time}:00` : '';
  }
  function setPick(kind) {
    pick = kind;
    for (const chip of $('reminder-chips').querySelectorAll('button')) chip.setAttribute('aria-pressed',String(chip.dataset.pick === kind));
    $('reminder-custom').hidden = kind !== 'custom';
  }
  function drawChips() {
    const zone = $('reminder-zone').value.trim();
    const now = validZone(zone) ? nowWall(zone) : null;
    for (const chip of $('reminder-chips').querySelectorAll('button')) {
      let hidden = false;
      if (now && chip.dataset.pick === 'today') hidden = +now.slice(11,13) * 60 + +now.slice(14,16) >= 18 * 60 + 30;
      if (now && chip.dataset.pick === 'monday') hidden = dayParts(now).w === 0;
      chip.hidden = hidden;
    }
  }
  function drawZone() {
    const zone = $('reminder-zone').value.trim();
    $('reminder-zone-name').textContent = zone ? zoneLine(zone) : 'не указан';
  }
  function applyPick(kind) {
    const zone = $('reminder-zone').value.trim();
    if (!validZone(zone)) { $('reminder-zone-edit').hidden = false; return say('Укажите часовой пояс, например Europe/Moscow.'); }
    if (kind === 'custom' && pick === 'custom') return $('reminder-date').focus();
    setPick(kind); setLocal(pickTime(kind,zone)); say(); invalidateTime();
    if (kind === 'custom') { $('reminder-date').focus(); scheduleResolve(0); } else checkTime();
  }
  function wallTime(value, timezone) {
    const parts = new Intl.DateTimeFormat('en-CA',{timeZone:timezone,year:'numeric',month:'2-digit',day:'2-digit',
      hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(new Date(value));
    const part = type => parts.find(p => p.type === type).value;
    return `${part('year')}-${part('month')}-${part('day')}T${part('hour')}:${part('minute')}:${part('second')}`;
  }
  function description(row) {
    try { return `${wallTime(row.scheduled_at,row.timezone).replace('T',' ')} · ${row.timezone}`; }
    catch (_) { return `${row.scheduled_at} · ${row.timezone}`; }
  }
  function clearForm() {
    editing = pendingCreate = uncertainEdit = comparison = null;
    conflicted = false;
    $('reminder-form').hidden = true; $('reminder-conflict').hidden = true;
    $('reminder-remote').hidden = true; $('reminder-use-version').hidden = true;
    $('reminder-text').value = ''; setLocal(''); setPick('');
    $('reminder-zone-edit').hidden = true; $('reminder-zone-change').setAttribute('aria-expanded','false');
    invalidateTime(); baseline = fingerprint();
  }
  function targets(value = '') {
    $('reminder-target').replaceChildren(new Option('Заметка целиком',''));
    let tasks = 0;
    for (const item of note.items) if (item.kind === 'task' && item.status === 'open') {
      $('reminder-target').append(new Option(item.text,item.id)); tasks++;
    }
    if (value && !Array.from($('reminder-target').options).some(option => option.value === value)) {
      $('reminder-target').append(new Option('Задача больше недоступна',value));
    }
    $('reminder-target').value = value;
    $('reminder-target-wrap').hidden = !tasks && !value;
  }
  // `proposal` is {local_time, timezone, focusTime} from a suggested reminder. The person still confirms it.
  function startForm(row = null, item = null, proposal = null) {
    if (busy || locked || !note) return;
    if (dirty() && !confirm('Есть несохранённое напоминание. Закрыть его и открыть другое?')) return;
    clearForm(); editing = row ? {...row} : null;
    targets(row?.item_id || item?.id || '');
    $('reminder-form-title').textContent = row ? 'Изменить напоминание' : 'Новое напоминание';
    $('reminder-text').value = row?.text || item?.text || note.title;
    const proposedZone = proposal && validZone(proposal.timezone) ? proposal.timezone : '';
    $('reminder-zone').value = row?.timezone || proposedZone || defaultZone(); drawZone(); drawChips();
    if (row) { setLocal(wallTime(row.scheduled_at,row.timezone)); setPick('custom'); }
    else if (proposedZone && proposal.local_time) { setLocal(proposal.local_time); setPick('custom'); }
    $('reminder-form').hidden = false; baseline = fingerprint(); controls();
    if (proposedZone && proposal.local_time && !row) {
      checkTime().then(() => {
        if (proposal.focusTime) $('reminder-time').focus();
        else if (!$('reminder-confirm').disabled) $('reminder-confirm').focus();
      });
      $('reminder-form').scrollIntoView?.({block:'nearest'});
    } else if (row) checkTime(); else $('reminder-chips').querySelector('button:not([hidden])').focus();
  }
  function rowTime(row) {
    try {
      const local = wallTime(row.scheduled_at,row.timezone);
      return whenShort(local,row.timezone) + (row.timezone === defaultZone() ? '' : ` (${zoneShort(row.timezone)})`);
    } catch (_) { return `${row.scheduled_at} · ${row.timezone}`; }
  }
  function drawRows() {
    $('reminders-list').replaceChildren();
    if (!rows.length) $('reminders-list').append(node('p','Напоминаний нет','muted reminders-empty'));
    for (const row of rows) {
      const card = node('article','','reminder-row'); card.dataset.id = row.id;
      if (row.status === 'cancelled') card.classList.add('is-cancelled');
      const task = note.items.find(item => item.id === row.item_id);
      const head = node('div','','reminder-line');
      head.append(node('strong',rowTime(row),'reminder-when'),node('span',
        `${labels[row.status] || 'Статус недоступен'}${row.delivery_status ? ' · ' + (deliveryLabels[row.delivery_status] || 'Статус доставки недоступен') : ''}`,'reminder-status'));
      card.append(head,node('p',row.text,'reminder-what'));
      if (row.item_id) card.append(node('p',`К задаче «${task?.text || 'Задача больше недоступна'}»`,'muted'));
      if (row.previous_attempt_unknown || row.status === 'unknown' || row.delivery_status === 'unknown') {
        card.append(node('p','Предыдущая отправка могла состояться. Новая дата или отмена не могут отозвать уже отправленное сообщение.','reminder-warning'));
      }
      const actions = node('div','','reminder-actions');
      const edit = node('button','Изменить','quiet reminder-small'); edit.type = 'button'; edit.dataset.action = 'edit';
      edit.disabled = Boolean(row.item_id && (!task || task.kind !== 'task' || task.status !== 'open'));
      edit.onclick = () => startForm(row);
      const cancel = node('button','Отменить','quiet reminder-small'); cancel.type = 'button'; cancel.dataset.action = 'cancel';
      cancel.disabled = row.status === 'cancelled'; cancel.onclick = () => cancelReminder(row);
      actions.append(edit,cancel); card.append(actions); $('reminders-list').append(card);
    }
    drawTelegram(); controls();
  }
  // The Telegram line shows only when something needs the person's attention.
  function drawTelegram() {
    const problem = rows.some(row => ['blocked','unknown'].includes(row.status) ||
      ['blocked','unknown','retryable'].includes(row.delivery_status));
    let text = '';
    if (telegram.error) text = 'Не удалось проверить Telegram.';
    else if (telegram.linked === false) text = 'Telegram не подключён. Напоминания не придут.';
    else if (problem) text = 'Есть напоминание с проблемой доставки. Проверьте Telegram.';
    $('reminder-telegram-text').textContent = text;
    $('reminder-telegram').hidden = !text; $('reminder-link').hidden = !text;
  }
  async function refresh(append = false, silent = false) {
    if (!note || (silent && (busy || locked || document.visibilityState === 'hidden'))) return;
    const id = note.id, view = generation, request = ++listRequest;
    const offset = append ? rows.length : 0, wanted = silent ? Math.max(20,rows.length) : 20;
    try {
      let data = [], page, limit;
      do {
        limit = Math.min(100,wanted - data.length);
        page = await bridge.request(`/api/v1/notes/${encodeURIComponent(id)}/reminders?limit=${limit}&offset=${offset + data.length}`);
        if (view !== generation || request !== listRequest) return;
        data = [...data,...page];
      } while (page.length === limit && data.length < wanted);
      rows = append ? [...new Map([...rows,...data].map(row => [row.id,row])).values()] : data;
      more = page.length === limit;
      drawRows(); if (!silent) say();
    } catch (_) {
      if (view === generation && request === listRequest) say('Не удалось обновить напоминания. Попробуйте ещё раз.');
    }
  }
  async function linkStatus() {
    const view = generation;
    try {
      const data = await bridge.request('/api/v1/telegram/links');
      if (view !== generation) return;
      telegram = {linked:data.identities.some(i => i.notifications_enabled && i.delivery_status === 'available'),error:false};
    } catch (_) {
      if (view !== generation) return;
      telegram = {linked:null,error:true};
    }
    drawTelegram();
  }
  async function checkTime() {
    if (!note || pendingCreate || uncertainEdit) return;
    clearTimeout(resolveTimer); resolveTimer = null;
    if (!$('reminder-local').value || !$('reminder-zone').value.trim()) return;
    if (!validZone($('reminder-zone').value.trim())) {
      invalidateTime(); return say('Не знаю такого часового пояса. Выберите из списка, например Europe/Moscow.');
    }
    const body = {local_time:fields().local_time,timezone:fields().timezone}, view = generation, request = ++timeRequest;
    say(); preview = null; $('reminder-preview').textContent = 'Проверяем время…'; controls();
    try {
      const result = await bridge.request('/api/v1/reminders/resolve-time',{method:'POST',body:JSON.stringify(body)});
      if (view !== generation || request !== timeRequest) return;
      preview = result; $('reminder-time-choice').replaceChildren();
      if (result.ambiguous) $('reminder-time-choice').append(new Option('Выберите',''));
      for (const [index,choice] of result.choices.entries()) {
        const turn = result.ambiguous ? `${index ? 'Второй' : 'Первый'} раз, ` : '';
        const option = new Option(`${turn}${hm(choice.local_at)}, UTC${choice.utc_offset}${choice.is_future ? '' : ' · уже прошло'}`,choice.scheduled_at);
        option.disabled = !choice.is_future; $('reminder-time-choice').append(option);
      }
      $('reminder-time-choices').hidden = !result.ambiguous;
      drawPreview();
    } catch (error) {
      if (view === generation && request === timeRequest) {
        $('reminder-preview').textContent = '';
        say(typeof error.message === 'string' ? error.message : 'Не удалось проверить время.');
      }
    }
  }
  async function compare() {
    if (!editing || busy || locked) return;
    const view = generation; setWork(true);
    try {
      const row = await bridge.request(`/api/v1/reminders/${encodeURIComponent(editing.id)}`);
      if (view !== generation) return;
      if (uncertainEdit && row.status !== 'cancelled' && row.generation === editing.generation + 1 && row.timezone === uncertainEdit.timezone &&
          row.text === uncertainEdit.text && new Date(row.scheduled_at).getTime() === new Date(uncertainEdit.scheduled_at).getTime()) {
        clearForm(); await refresh(); say('Изменения сохранены.'); bridge.notify('Изменения напоминания сохранены.'); return;
      }
      comparison = row; $('reminder-conflict').hidden = false;
      $('reminder-remote').textContent = `${description(row)}\n${row.text}\n${labels[row.status] || row.status}`;
      if (row.previous_attempt_unknown) $('reminder-remote').textContent += '\nПредыдущая отправка могла состояться.';
      $('reminder-remote').hidden = false; $('reminder-use-version').hidden = false;
    } catch (_) { if (view === generation) say('Не удалось загрузить текущий вариант. Ваши поля остались в форме.'); }
    finally { setWork(false); }
  }
  async function save(event) {
    event.preventDefault();
    if (busy || locked || !note || comparison || conflicted) return;
    if (uncertainEdit) return compare();
    if (bridge.otherDrafts()) return bridge.notify('Сначала сохраните остальные правки.');
    if (!fields().text.trim() || fields().text.includes('\0')) return say('Напишите текст напоминания.');
    if (!$('reminder-form').reportValidity()) return;
    const choice = selectedTime();
    if (!pendingCreate && !choice) return say('Выберите время напоминания.');
    const body = pendingCreate?.body || {scheduled_at:choice.scheduled_at,timezone:fields().timezone,text:fields().text,
      ...(editing ? {generation:editing.generation} : {item_id:fields().item_id})};
    const view = generation, id = note.id, edit = editing;
    if (!edit && !pendingCreate) pendingCreate = {body,key:window.berestaId()};
    setWork(true); say();
    try {
      const row = await bridge.request(edit ? `/api/v1/reminders/${encodeURIComponent(edit.id)}`
        : `/api/v1/notes/${encodeURIComponent(id)}/reminders`,{
        method:edit ? 'PATCH' : 'POST',body:JSON.stringify(body),
        headers:edit ? {} : {'Idempotency-Key':pendingCreate.key},
      });
      if (view !== generation) return;
      rememberZone(body.timezone); clearForm(); await refresh(); await linkStatus();
      const text = row.previous_attempt_unknown ? 'Сохранено. Предыдущая отправка могла состояться.' : 'Напоминание подтверждено.';
      say(text); bridge.notify(text);
    } catch (error) {
      if (view !== generation) return;
      if (edit && error.status === 409) {
        conflicted = true;
        $('reminder-conflict').hidden = false; say('Напоминание уже изменилось. Сравните текущий вариант, ваши поля сохранены.');
      } else if (error.status >= 400 && error.status < 500) {
        pendingCreate = null; say(error.message);
      } else if (edit) {
        uncertainEdit = body; $('reminder-conflict').hidden = false;
        say('Ответ потерян. Изменения могли сохраниться. Проверьте текущий вариант перед новой отправкой.');
      } else {
        say('Ответ потерян. Напоминание могло сохраниться. Повторите подтверждение с тем же текстом и временем.');
      }
    } finally { setWork(false); }
  }
  async function cancelReminder(row) {
    if (busy || locked || !note) return;
    if (dirty()) return say('Сначала сохраните или закройте форму напоминания.');
    if (bridge.otherDrafts()) return bridge.notify('Сначала сохраните остальные правки.');
    if (!confirm('Отменить это напоминание? Если отправка уже началась, сообщение ещё может прийти.')) return;
    const view = generation; setWork(true);
    try {
      const result = await bridge.request(`/api/v1/reminders/${encodeURIComponent(row.id)}/cancel`,{
        method:'POST',body:JSON.stringify({generation:row.generation}),
      });
      if (view !== generation) return;
      await refresh(); say(result.previous_attempt_unknown ? 'Напоминание отменено. Предыдущая отправка могла состояться.' : 'Напоминание отменено.');
    } catch (error) {
      if (view !== generation) return;
      await refresh(); say(error.status === 409 ? 'Напоминание изменилось. Проверьте новый вариант перед отменой.'
        : 'Не удалось подтвердить отмену. Обновите статусы и проверьте результат.');
    } finally { setWork(false); }
  }
  function hide() {
    generation++; listRequest++; timeRequest++; note = null; rows = [];
    clearInterval(timer); timer = null; clearForm();
  }
  function show(value, callbacks) {
    hide(); note = value; bridge = callbacks; targets(); telegram = {linked:null,error:false}; drawTelegram();
    $('reminders-list').replaceChildren(node('p','Загружаем напоминания…','muted')); more = false; controls();
    refresh(); linkStatus(); timer = setInterval(() => refresh(false,true),15000);
  }
  $('reminder-new').onclick = () => startForm();
  $('reminders-refresh').onclick = () => { refresh(); linkStatus(); };
  $('reminders-more').onclick = () => refresh(true);
  $('reminder-check-time').onclick = checkTime;
  $('reminder-time-choice').onchange = drawPreview;
  for (const chip of $('reminder-chips').querySelectorAll('button')) chip.onclick = () => applyPick(chip.dataset.pick);
  $('reminder-date').oninput = $('reminder-time').oninput = () => { syncLocal(); setPick('custom'); timeChanged(); };
  $('reminder-local').oninput = () => { setLocal($('reminder-local').value); if (!pick) setPick('custom'); timeChanged(); };
  $('reminder-zone').oninput = () => {
    drawZone(); drawChips();
    if (pick && pick !== 'custom' && validZone($('reminder-zone').value.trim())) applyPick(pick); else timeChanged();
  };
  $('reminder-zone-change').onclick = () => {
    const open = $('reminder-zone-edit').hidden; $('reminder-zone-edit').hidden = !open;
    $('reminder-zone-change').setAttribute('aria-expanded',String(open)); if (open) $('reminder-zone').focus();
  };
  $('reminder-text').oninput = () => { if (preview) drawPreview(); };
  $('reminder-form').onsubmit = save;
  $('reminder-discard').onclick = () => {
    if ((pendingCreate || uncertainEdit) && !confirm('Ответ сервера потерян. Напоминание могло сохраниться. Закрыть форму и проверить список?')) return;
    clearForm(); refresh();
  };
  $('reminder-compare').onclick = compare;
  $('reminder-use-version').onclick = () => {
    if (!comparison) return;
    editing = comparison; comparison = uncertainEdit = null;
    conflicted = false;
    $('reminder-conflict').hidden = true; $('reminder-use-version').hidden = true;
    invalidateTime(); say('Ваши поля сохранены. Подтвердите изменения текущей версии.'); checkTime();
  };
  $('reminder-link').onclick = () => { $('telegram-settings').open = true; $('link-code').focus(); };
  window.addEventListener('pagehide',hide);
  window.BerestaReminders = {show,hide,dirty,setLocked,startForTask:(item,proposal) => startForm(null,item,proposal)};
})();
