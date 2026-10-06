'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const node = (tag, text, className = '') => {
    const result = document.createElement(tag); result.textContent = text; result.className = className; return result;
  };
  let note = null, bridge = null, generation = 0, listRequest = 0, timeRequest = 0;
  let locked = false, busy = false, editing = null, baseline = '', preview = null, comparison = null, conflicted = false;
  let pendingCreate = null, uncertainEdit = null, rows = [], more = false, timer = null;
  const labels = {confirmed:'Подтверждено',sent:'Отправлено',blocked:'Telegram недоступен',
    unknown:'Результат отправки неизвестен',cancelled:'Отменено'};
  const deliveryLabels = {pending:'Ожидает отправки',leased:'Готовится к отправке',authorized:'Отправляется',
    retryable:'Ожидает повтора после отказа Telegram',sent:'Отправлено',blocked:'Telegram недоступен',
    unknown:'Результат отправки неизвестен',cancelled:'Попытка отменена'};
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
      ? 'Проверить сохранение' : editing ? 'Подтвердить изменения' : 'Подтвердить напоминание';
    $('reminders-more').hidden = !more;
  }
  function setWork(value) { busy = value; bridge.onBusy(value); controls(); }
  function setLocked(value) { locked = value; controls(); }
  function invalidateTime() {
    timeRequest++; preview = null; $('reminder-preview').textContent = '';
    $('reminder-time-choices').hidden = true; controls();
  }
  function drawPreview() {
    const choice = selectedTime();
    $('reminder-preview').textContent = choice
      ? `${choice.local_at.replace('T',' ')} · ${preview.timezone} · UTC${choice.utc_offset}\n${fields().text}`
      : preview?.ambiguous ? 'Это время наступит дважды. Выберите нужное смещение UTC.' : '';
    controls();
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
    $('reminder-text').value = ''; $('reminder-local').value = '';
    invalidateTime(); baseline = fingerprint();
  }
  function targets(value = '') {
    $('reminder-target').replaceChildren(new Option('К заметке',''));
    for (const item of note.items) if (item.kind === 'task' && item.status === 'open') {
      $('reminder-target').append(new Option(item.text,item.id));
    }
    if (value && !Array.from($('reminder-target').options).some(option => option.value === value)) {
      $('reminder-target').append(new Option('Задача больше недоступна',value));
    }
    $('reminder-target').value = value;
  }
  function startForm(row = null, item = null) {
    if (busy || locked || !note) return;
    if (dirty() && !confirm('Есть несохранённое напоминание. Закрыть его и открыть другое?')) return;
    clearForm(); editing = row ? {...row} : null;
    targets(row?.item_id || item?.id || '');
    $('reminder-form-title').textContent = row ? 'Изменить напоминание' : 'Новое напоминание';
    $('reminder-text').value = row?.text || item?.text || note.title;
    $('reminder-zone').value = row?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'Europe/Moscow';
    $('reminder-local').value = row ? wallTime(row.scheduled_at,row.timezone) : '';
    $('reminder-form').hidden = false; baseline = fingerprint(); controls();
    $('reminder-local').focus();
  }
  function drawRows() {
    $('reminders-list').replaceChildren();
    if (!rows.length) $('reminders-list').append(node('p','Напоминаний пока нет.','muted'));
    for (const row of rows) {
      const card = node('article','','reminder-row'); card.dataset.id = row.id;
      const task = note.items.find(item => item.id === row.item_id);
      card.append(node('p',row.text),node('p',description(row),'muted'),node('p',
        row.item_id ? `К задаче «${task?.text || 'Задача больше недоступна'}»` : 'К заметке','muted'),
      node('p',`${labels[row.status] || 'Статус недоступен'}${row.delivery_status ? ' · ' + (deliveryLabels[row.delivery_status] || 'Статус доставки недоступен') : ''}`));
      if (row.previous_attempt_unknown || row.status === 'unknown' || row.delivery_status === 'unknown') {
        card.append(node('p','Предыдущая отправка могла состояться. Новая дата или отмена не могут отозвать уже отправленное сообщение.','reminder-warning'));
      }
      const actions = node('div','','actions');
      const edit = node('button','Изменить','secondary'); edit.type = 'button'; edit.dataset.action = 'edit';
      edit.disabled = Boolean(row.item_id && (!task || task.kind !== 'task' || task.status !== 'open'));
      edit.onclick = () => startForm(row);
      const cancel = node('button','Отменить','quiet'); cancel.type = 'button'; cancel.dataset.action = 'cancel';
      cancel.disabled = row.status === 'cancelled'; cancel.onclick = () => cancelReminder(row);
      actions.append(edit,cancel); card.append(actions); $('reminders-list').append(card);
    }
    controls();
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
      $('reminder-telegram').textContent = data.identities.some(i => i.notifications_enabled && i.delivery_status === 'available')
        ? 'Telegram подключён. Статус отправки появится в списке.'
        : 'Для доставки нужен доступный Telegram. Напоминание можно сохранить сейчас, затем подключить бота.';
    } catch (_) {
      if (view === generation) $('reminder-telegram').textContent = 'Не удалось проверить подключение Telegram. Проверьте его перед сроком напоминания.';
    }
  }
  async function checkTime() {
    if (busy || locked || !note || pendingCreate || uncertainEdit) return;
    if (!$('reminder-local').value || !$('reminder-zone').value.trim()) return say('Укажите точную дату, время и часовой пояс.');
    const body = {local_time:fields().local_time,timezone:fields().timezone}, view = generation, request = ++timeRequest;
    setWork(true); say();
    try {
      const result = await bridge.request('/api/v1/reminders/resolve-time',{method:'POST',body:JSON.stringify(body)});
      if (view !== generation || request !== timeRequest) return;
      preview = result; $('reminder-time-choice').replaceChildren();
      if (result.ambiguous) $('reminder-time-choice').append(new Option('Выберите смещение UTC',''));
      for (const choice of result.choices) {
        const option = new Option(`UTC${choice.utc_offset}${choice.is_future ? '' : ' · уже прошло'}`,choice.scheduled_at);
        option.disabled = !choice.is_future; $('reminder-time-choice').append(option);
      }
      $('reminder-time-choices').hidden = !result.ambiguous;
      drawPreview();
    } catch (error) { if (view === generation) say(typeof error.message === 'string' ? error.message : 'Не удалось проверить время.'); }
    finally { setWork(false); }
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
    if (!pendingCreate && !choice) return say('Сначала проверьте время.');
    const body = pendingCreate?.body || {scheduled_at:choice.scheduled_at,timezone:fields().timezone,text:fields().text,
      ...(editing ? {generation:editing.generation} : {item_id:fields().item_id})};
    const view = generation, id = note.id, edit = editing;
    if (!edit && !pendingCreate) pendingCreate = {body,key:crypto.randomUUID()};
    setWork(true); say();
    try {
      const row = await bridge.request(edit ? `/api/v1/reminders/${encodeURIComponent(edit.id)}`
        : `/api/v1/notes/${encodeURIComponent(id)}/reminders`,{
        method:edit ? 'PATCH' : 'POST',body:JSON.stringify(body),
        headers:edit ? {} : {'Idempotency-Key':pendingCreate.key},
      });
      if (view !== generation) return;
      clearForm(); await refresh(); await linkStatus();
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
    hide(); note = value; bridge = callbacks; targets();
    $('reminders-list').replaceChildren(node('p','Загружаем напоминания…','muted')); more = false; controls();
    refresh(); linkStatus(); timer = setInterval(() => refresh(false,true),15000);
  }
  $('reminder-new').onclick = () => startForm();
  $('reminders-refresh').onclick = () => { refresh(); linkStatus(); };
  $('reminders-more').onclick = () => refresh(true);
  $('reminder-check-time').onclick = checkTime;
  $('reminder-time-choice').onchange = drawPreview;
  $('reminder-local').oninput = $('reminder-zone').oninput = invalidateTime;
  $('reminder-text').oninput = drawPreview;
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
    invalidateTime(); say('Ваши поля сохранены. Проверьте время и подтвердите изменения текущей версии.');
  };
  $('reminder-link').onclick = () => { $('telegram-settings').open = true; $('link-code').focus(); };
  window.addEventListener('pagehide',hide);
  window.BerestaReminders = {show,hide,dirty,setLocked,startForTask:item => startForm(null,item)};
})();
