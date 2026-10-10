'use strict';
// Focus mode. The note is a field for typing, every other function sits behind an icon button.
// Loaded after app.js, workspace.js and brain.js. It uses their globals (api, currentNote, noteBusy, message ...).
(() => {
  const get = id => document.getElementById(id);
  const title = get('title'), body = get('markdown');
  const heading = title.closest('.note-heading');

  /* ---------- Icon panels and popovers ---------- */
  const tools = new Map();
  for (const tool of document.querySelectorAll('.tool[data-tool]')) {
    const key = tool.dataset.tool;
    tools.set(key, {key, tool, btn: tool.querySelector('.tool-btn'), pop: tool.querySelector('.tool-pop'), label: tool.querySelector('.tool-btn').dataset.tip});
  }
  let current = null;
  function focusInto(pop) {
    pop.focus();
  }
  function placePop(entry) {
    // Desktop popovers hang from the right edge of their button. If that runs off the left edge, hang from the left.
    entry.pop.classList.remove('is-left');
    const box = entry.pop.getBoundingClientRect();
    if (box.width && box.left < 8) entry.pop.classList.add('is-left');
  }
  function openTool(key, {focus = true} = {}) {
    const entry = tools.get(key);
    if (!entry || entry.tool.hidden) return false;
    if (current && current.key === key) return true;
    if (current) closeTool({restore: false});
    current = entry;
    entry.pop.hidden = false; entry.btn.setAttribute('aria-expanded', 'true');
    get('pop-scrim').hidden = false; document.body.classList.add('has-pop');
    placePop(entry);
    if (key === 'original' && !get('original-details').hidden) get('original-details').open = true;
    if (key === 'history' && !get('history').children.length && !get('load-history').disabled) get('load-history').click();
    if (key === 'jobs' && typeof loadJobs === 'function') loadJobs().catch(() => {});
    if (focus) focusInto(entry.pop);
    return true;
  }
  function closeTool({restore = true} = {}) {
    if (!current) return;
    const entry = current; current = null;
    const inside = entry.pop.contains(document.activeElement);
    entry.pop.hidden = true; entry.btn.setAttribute('aria-expanded', 'false');
    get('pop-scrim').hidden = true; document.body.classList.remove('has-pop');
    if (restore && (inside || !document.activeElement || document.activeElement === document.body)) entry.btn.focus();
  }
  function closeAll() { closeTool({restore: false}); }
  for (const entry of tools.values()) {
    entry.btn.addEventListener('click', () => { if (current && current.key === entry.key) closeTool(); else openTool(entry.key); });
    entry.pop.querySelector('.pop-close').addEventListener('click', () => closeTool());
  }
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && current && !event.defaultPrevented) { event.preventDefault(); closeTool(); }
  });
  // Capture phase: the clicked element is still in the page, even if its own handler redraws the list.
  document.addEventListener('click', event => {
    if (!current || !event.target.isConnected) return;
    if (current.pop.contains(event.target) || current.btn.contains(event.target)) return;
    closeTool({restore: false});
  }, true);

  /* ---------- Badges on the icons ---------- */
  // Every write here is guarded: this observer watches the note card, an unguarded write would queue itself forever.
  function setText(el, text) { if (el.textContent !== text) el.textContent = text; if (el.hidden === Boolean(text)) el.hidden = !text; }
  function setBadge(key, text) {
    const entry = tools.get(key), badge = get(`badge-${key}`);
    if (!entry || !badge) return;
    setText(badge, text);
    const label = text ? `${entry.label}: ${text}` : entry.label;
    if (entry.btn.getAttribute('aria-label') !== label) entry.btn.setAttribute('aria-label', label);
  }
  function syncBadges() {
    const active = get('reminders-list').querySelectorAll('.reminder-row:not(.is-cancelled)').length;
    setBadge('reminders', active ? String(active) : '');
    const proposed = Boolean(get('items').querySelector('.br-propose'));
    if (get('dot-reminders').hidden === proposed) get('dot-reminders').hidden = !proposed;
    const taskRows = [...get('items').querySelectorAll('.item')].filter(row => row.querySelector('.item-done'));
    setBadge('tasks', taskRows.length ? `${taskRows.filter(row => row.classList.contains('is-done')).length}/${taskRows.length}` : '');
    const related = get('related-list').children.length;
    setBadge('related', related ? String(related) : '');
    const hideRelated = get('view-related').hidden || !related;
    if (tools.get('related').tool.hidden !== hideRelated) tools.get('related').tool.hidden = hideRelated;
    if (hideRelated && current && current.key === 'related') closeTool();
    const proposedConclusions = [...get('conclusions').querySelectorAll('.conclusion .status')].filter(el => el.textContent === 'Предложен').length;
    setBadge('insights', proposedConclusions ? String(proposedConclusions) : '');
  }

  /* ---------- Where the audio source lives ---------- */
  const sourceCard = get('source-card'), sourceMarker = document.createComment('source-card');
  sourceCard.before(sourceMarker);
  function placeSource() {
    const inNote = !get('note-card').hidden && !sourceCard.hidden, slot = get('source-slot');
    if (inNote && sourceCard.parentNode !== slot) slot.append(sourceCard);
    else if (!inNote && sourceCard.parentNode === slot) sourceMarker.after(sourceCard);
  }

  /* ---------- Observers ---------- */
  function syncAll() {
    syncBadges(); placeSource();
    if (current && current.pop.closest('.card').hidden) closeAll();
  }
  const note = get('note-card');
  new MutationObserver(syncAll).observe(note, {childList: true, subtree: true, attributes: true, attributeFilter: ['hidden']});
  new MutationObserver(syncAll).observe(sourceCard, {attributes: true, attributeFilter: ['hidden']});
  new MutationObserver(syncAll).observe(get('capture-card'), {attributes: true, attributeFilter: ['hidden']});
  // A reminder form that opens (from the bell on a task, or from a suggestion) brings its popover along.
  new MutationObserver(() => {
    if (get('reminder-form').hidden) return;
    openTool('reminders', {focus: false});
    if (!tools.get('reminders').pop.contains(document.activeElement)) {
      const target = !get('reminder-confirm').disabled ? get('reminder-confirm') : get('reminder-chips').querySelector('button:not([hidden])');
      target?.focus();
    }
  }).observe(get('reminder-form'), {attributes: true, attributeFilter: ['hidden']});

  /* ---------- Title that fits ---------- */
  const isNarrow = () => (window.matchMedia ? window.matchMedia('(max-width:640px)').matches : window.innerWidth <= 640);
  const titleSizes = () => (isNarrow() ? {max: 26, min: 18} : {max: 36, min: 20});
  function measureTitleLines(size) {
    title.style.height = 'auto';
    const cs = window.getComputedStyle(title), pad = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
    const line = parseFloat(cs.lineHeight) || size * 1.2;
    return Math.round(Math.max(0, title.scrollHeight - pad) / line);
  }
  // `measure(size)` returns how many lines the title takes at that font size. Tests replace it, there is no layout in jsdom.
  function fitTitle(measure = measureTitleLines) {
    const {max, min} = titleSizes();
    let size = max, clamped = false;
    for (;;) {
      title.style.fontSize = `${size}px`;
      if (measure(size) <= 2) break;
      if (size <= min) { clamped = true; break; }
      size = Math.max(min, size - 1);
    }
    heading.classList.toggle('is-clamped', clamped);
    get('title-display').textContent = title.value;
    get('title-display').style.fontSize = `${size}px`;
    title.style.height = 'auto';
    if (title.scrollHeight) {
      const cs = window.getComputedStyle(title), pad = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
      const limit = clamped ? Math.ceil(2 * (parseFloat(cs.lineHeight) || size * 1.2) + pad) : Infinity;
      title.style.height = `${Math.min(title.scrollHeight, limit)}px`;
    }
    return {size, clamped};
  }
  let fitWidth = -1;
  function refit() { fitTitle(); grow(); }
  if (window.ResizeObserver) {
    new window.ResizeObserver(entries => {
      const width = Math.round(entries[0].contentRect.width);
      if (width !== fitWidth) { fitWidth = width; refit(); }
    }).observe(heading);
  } else window.addEventListener('resize', refit);

  function grow() {
    body.style.height = 'auto';
    if (body.scrollHeight) body.style.height = `${body.scrollHeight}px`;
  }

  /* ---------- Autosave ---------- */
  let timer = 0, retryTimer = 0, running = null, conflict = false, saved = false;
  const delay = () => (Number.isFinite(window.noteAutosaveMs) ? window.noteAutosaveMs : 1000);
  const retryDelay = () => (Number.isFinite(window.noteAutosaveRetryMs) ? window.noteAutosaveRetryMs : 5000);
  const differs = () => Boolean(currentNote) && (title.value !== currentNote.title || body.value !== currentNote.markdown);
  // True while typed text waits for the server and there is a chance to send it.
  const pending = () => differs() && !conflict;
  function setStatus(kind, text = '') {
    const line = get('note-save-status');
    if (line.dataset.state !== kind) line.dataset.state = kind;
    if (get('note-save-text').textContent !== text) get('note-save-text').textContent = text;
    const reload = kind === 'conflict';
    if (get('note-reload').hidden === reload) get('note-reload').hidden = !reload;
  }
  function schedule() {
    clearTimeout(timer); clearTimeout(retryTimer); retryTimer = 0;
    if (conflict) return;
    timer = window.setTimeout(() => { timer = 0; run(); }, delay());
  }
  function rowUpdate(note) {
    const row = get('notes').querySelector(`button[data-note-id="${note.id}"]`);
    if (!row) return;
    const label = row.querySelector('.note-row-title');
    if (label && label.textContent !== note.title) label.textContent = note.title;
    if (row !== get('notes').firstElementChild && !activeFilter.q && !activeFilter.category && !activeFilter.source) get('notes').prepend(row);
  }
  // One request at a time. The loop reads the fields again after every answer, so the latest text wins.
  async function sendOnce() {
    const target = currentNote, id = target.id, version = target.version;
    const payload = {title: title.value, markdown: body.value};
    if (!payload.title.trim()) { setStatus('invalid', 'Не сохранено: нужно название'); return false; }
    if (!payload.markdown.trim()) { setStatus('invalid', 'Не сохранено: заметка пустая'); return false; }
    if (noteBusy) { timer = window.setTimeout(() => { timer = 0; run(); }, 300); return false; }
    setStatus('saving', 'Сохраняется…');
    try {
      const result = await api(`/api/v1/notes/${id}`, {method: 'PATCH', body: JSON.stringify({...payload, version})});
      if (currentNote !== target) return true;
      Object.assign(target, {version: result.version, title: result.title, markdown: result.markdown,
        updated_at: result.updated_at, structure_confirmed_at: result.structure_confirmed_at});
      renderNoteMeta(target); renderStructure(target); rowUpdate(target);
      saved = true;
      setStatus('saved', differs() ? 'Сохраняется…' : 'Сохранено');
      return true;
    } catch (error) {
      if (currentNote !== target) return false;
      if (error.status === 409) {
        conflict = true; setStatus('conflict', 'Заметку изменили в другом месте. Ваш текст остался здесь, но не сохранён.');
        message(error.message);
      } else {
        setStatus('failed', 'Не сохранено, повторим');
        // A refusal by the server (4xx) will not change by itself. Network trouble and 5xx are tried again.
        if (!(error.status >= 400 && error.status < 500)) retryTimer = window.setTimeout(() => { retryTimer = 0; run(); }, retryDelay());
      }
      return false;
    }
  }
  function run() {
    if (running) return running;
    running = (async () => {
      while (pending()) { if (!(await sendOnce())) break; }
    })().finally(() => { running = null; });
    return running;
  }
  async function flush() {
    clearTimeout(timer); timer = 0; clearTimeout(retryTimer); retryTimer = 0;
    await run();
    return !differs();
  }
  function onEdit() {
    if (!currentNote) return;
    if (/\n/.test(title.value)) title.value = title.value.replace(/\s*\n\s*/g, ' ');
    fitTitle(); grow();
    if (differs()) { if (!conflict) { setStatus('saving', 'Сохраняется…'); schedule(); } }
    else { clearTimeout(timer); timer = 0; if (!conflict) setStatus(saved ? 'saved' : '', saved ? 'Сохранено' : ''); }
  }
  title.addEventListener('input', onEdit);
  body.addEventListener('input', onEdit);
  for (const field of [title, body]) field.addEventListener('blur', () => { if (pending()) flush(); });
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden' && pending()) flush(); });
  title.addEventListener('keydown', event => {
    if (event.key !== 'Enter' || event.isComposing) return;
    event.preventDefault(); body.focus(); body.setSelectionRange?.(0, 0);
  });
  get('note-reload').onclick = async () => {
    if (!currentNote || !confirm('Загрузить версию с сервера? Текст, который не сохранился, будет заменён.')) return;
    const id = currentNote.id;
    setNoteBusy(true);
    try {
      const fresh = await api(`/api/v1/notes/${id}`);
      renderNote(fresh); setNoteBusy(true); await loadNotes(); message('Загружена версия с сервера.');
    } catch (error) { message(error.message); } finally { setNoteBusy(false); }
  };
  // A fresh render (another note, or the same one after a tool changed it) starts from the stored text.
  document.addEventListener('beresta:note-rendered', () => {
    clearTimeout(timer); clearTimeout(retryTimer); timer = 0; retryTimer = 0; conflict = false; saved = false;
    setStatus(''); get('title-display').textContent = title.value; refit();
  });

  /* ---------- Import into the capture field ---------- */
  const thought = get('thought'), importStatus = get('import-status');
  const limit = () => (thought.maxLength > 0 ? thought.maxLength : 12000);
  function importError(text) { importStatus.textContent = text; importStatus.classList.add('is-error'); }
  function addText(raw, source) {
    const text = String(raw).replace(/^﻿/, '').replace(/\r\n?/g, '\n').trim();
    if (!text) return importError(source === 'file' ? 'Файл пустой.' : 'В буфере нет текста.');
    if (text.length > limit()) return importError(`Текст длиннее ${limit()} знаков. Сократите его или разбейте на части.`);
    const before = thought.value.replace(/\s+$/, '');
    const next = before ? `${before}\n\n${text}` : text;
    if (next.length > limit()) return importError(`В поле уже есть текст, вместе получится больше ${limit()} знаков.`);
    thought.value = next; thought.dispatchEvent(new Event('input', {bubbles: true}));
    importStatus.textContent = ''; importStatus.classList.remove('is-error');
    closeAll(); thought.focus(); thought.setSelectionRange?.(next.length, next.length);
    message(source === 'file' ? 'Текст из файла добавлен в запись. Проверьте его и сохраните.' : 'Текст из буфера добавлен в запись. Проверьте его и сохраните.');
  }
  function importFile(file) {
    importStatus.classList.remove('is-error'); importStatus.textContent = '';
    if (!file) return;
    if (thought.disabled) return importError('Подождите, запись ещё сохраняется.');
    const textual = /\.(txt|md|markdown)$/i.test(file.name || '') || /^text\//.test(file.type || '');
    if (!textual) return importError('Нужен текстовый файл .txt или .md.');
    // UTF-8 takes at most four bytes per character.
    if (file.size > limit() * 4) return importError(`Файл слишком большой. В запись помещается до ${limit()} знаков.`);
    const reader = new FileReader();
    reader.onerror = () => importError('Не удалось прочитать файл.');
    reader.onload = () => {
      const text = String(reader.result || '');
      if (text.includes('\u0000') || text.includes('�')) return importError('Это не текст в кодировке UTF-8. Сохраните файл как .txt в UTF-8.');
      addText(text, 'file');
    };
    reader.readAsText(file, 'utf-8');
  }
  get('import-file').addEventListener('change', event => {
    importFile(event.target.files && event.target.files[0]);
    try { event.target.value = ''; } catch (_) { /* some browsers refuse to clear a file input */ }
  });
  function pasteFallback() {
    importStatus.classList.remove('is-error');
    importStatus.textContent = 'Браузер не даёт прочитать буфер. Нажмите Ctrl+V или ⌘V в поле записи.';
    message('Нажмите Ctrl+V или ⌘V в поле записи, чтобы вставить текст.');
    closeAll(); if (!thought.disabled) thought.focus();
  }
  get('import-paste').addEventListener('click', async () => {
    if (thought.disabled) return importError('Подождите, запись ещё сохраняется.');
    const clipboard = window.navigator && window.navigator.clipboard;
    if (!clipboard || typeof clipboard.readText !== 'function') return pasteFallback();
    try { addText(await clipboard.readText(), 'clipboard'); } catch (_) { pasteFallback(); }
  });

  window.BerestaFocus = {pending, flush, closeAll, open: openTool, close: closeTool, fitTitle, importFile, tools};
  syncAll();
})();
