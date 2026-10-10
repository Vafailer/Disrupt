'use strict';
(() => {
  const get = id => document.getElementById(id);
  const panelButtons = [...document.querySelectorAll('[data-note-panel]')];
  let lastNote = null, editing = false;
  const card = get('note-card'), body = get('markdown');
  // Only one secondary panel is open at a time. An empty name closes them all.
  function showPanel(name) {
    for (const button of panelButtons) {
      const open = button.dataset.notePanel === name;
      button.setAttribute('aria-expanded',String(open));
      get(`view-${button.dataset.notePanel}`).hidden = !open;
    }
    get('workspace').dataset.noteView = name || 'read';
    if (name === 'original' && !get('original-details').hidden) get('original-details').open = true;
  }
  for (const button of panelButtons) {
    button.onclick = () => {
      const name = button.dataset.notePanel, open = button.getAttribute('aria-expanded') === 'true';
      showPanel(open ? '' : name);
      if (!open) get(`view-${name}`).scrollIntoView?.({block:'nearest',behavior:'smooth'});
    };
  }
  function grow() {
    body.style.height = 'auto';
    if (body.scrollHeight) body.style.height = `${body.scrollHeight}px`;
  }
  // The title is a textarea so a long one wraps. It never holds a line break.
  function growTitle() {
    const title = get('title');
    if (/\n/.test(title.value)) title.value = title.value.replace(/\s*\n\s*/g,' ');
    title.style.height = 'auto';
    if (title.scrollHeight) title.style.height = `${title.scrollHeight}px`;
  }
  window.addEventListener('resize',growTitle);
  function titleChanged() { return Boolean(currentNote) && get('title').value !== currentNote.title; }
  function syncEditActions() { get('note-edit-actions').hidden = !(editing || titleChanged()); }
  // The editor replaces the rendered text in place. Fields and the save handler are the old edit form's.
  function setEditing(on, focus = true) {
    editing = on; card.classList.toggle('is-editing',on);
    get('preview').hidden = on; body.hidden = !on; get('note-edit-start').hidden = on;
    syncEditActions();
    if (on) {
      grow();
      if (focus) { body.focus(); body.setSelectionRange?.(body.value.length,body.value.length); }
    }
  }
  function cancelEditing() {
    if (!currentNote) return;
    const changed = titleChanged() || body.value !== currentNote.markdown;
    if (changed && !confirm('Отменить несохранённые правки?')) return;
    get('title').value = currentNote.title; body.value = currentNote.markdown; renderMarkdown(currentNote.markdown);
    get('note-heading-title').textContent = currentNote.title || 'Без названия';
    const wasEditing = editing;
    setEditing(false);
    if (wasEditing) get('note-edit-start').focus();
  }
  get('note-edit-start').onclick = () => { if (currentNote && !noteBusy) setEditing(true); };
  get('preview').addEventListener('click',event => {
    if (editing || !currentNote || noteBusy || event.target.closest('a')) return;
    if (window.getSelection?.().toString()) return;
    setEditing(true);
  });
  body.addEventListener('input',grow);
  get('title').addEventListener('input',() => { growTitle(); syncEditActions(); });
  get('note-edit-cancel').onclick = cancelEditing;
  get('edit-form').addEventListener('keydown',event => {
    if (event.target !== get('title') && event.target !== body) return;
    if (event.key === 'Escape' && (editing || titleChanged())) { event.preventDefault(); cancelEditing(); }
    else if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      if (editing || titleChanged()) get('edit-form').requestSubmit?.(get('note-save'));
    } else if (event.key === 'Enter' && event.target === get('title')) {
      event.preventDefault(); if (!noteBusy) setEditing(true);
    }
  });
  // app.js renders again after a save or a note switch, so the editor closes with fresh text.
  let refocus = false;
  document.addEventListener('beresta:note-rendered',() => { refocus = editing; setEditing(false,false); growTitle(); });
  // Controls are locked while saving, so focus returns to "Изменить" only once they are free again.
  document.addEventListener('beresta:note-idle',() => {
    if (!refocus) return;
    refocus = false;
    if (!document.activeElement || document.activeElement === document.body) get('note-edit-start').focus();
  });
  function syncNote() {
    const title = get('title').value || 'Без названия';
    if(get('note-heading-title').textContent !== title)get('note-heading-title').textContent = title;
    if (currentNote?.id !== lastNote) {lastNote = currentNote?.id; showPanel('');get('workspace').classList.remove('library-open');get('mobile-library-toggle').setAttribute('aria-expanded','false');}
    get('workspace').classList.toggle('has-note',!get('note-card').hidden);
    get('workspace').classList.toggle('has-source',!get('source-card').hidden);
    // This observer watches the note card subtree: write the attribute only when it changes,
    // otherwise every write queues a new mutation and the page spins forever.
    const audioHint = !(get('original-details').hidden && !get('source-card').hidden);
    if (get('original-audio-hint').hidden !== audioHint) get('original-audio-hint').hidden = audioHint;
    for (const button of get('notes').querySelectorAll('button')) {
      button.setAttribute('aria-current',String(button.dataset.noteId === currentNote?.id));
    }
  }
  new MutationObserver(syncNote).observe(get('note-card'),{attributes:true,attributeFilter:['hidden'],childList:true,subtree:true});
  get('title').addEventListener('input',()=>{get('note-heading-title').textContent=get('title').value || 'Без названия';});
  new MutationObserver(syncNote).observe(get('source-card'),{attributes:true,attributeFilter:['hidden']});
  new MutationObserver(syncNote).observe(get('notes'),{childList:true});
  get('mobile-library-toggle').onclick=()=>{const open=get('workspace').classList.toggle('library-open');get('mobile-library-toggle').setAttribute('aria-expanded',String(open));};
  function showCapture(view) {
    for (const button of get('capture-tabs').querySelectorAll('button')) {
      const active = button.dataset.captureView === view;
      button.setAttribute('aria-pressed',String(active));get(button.dataset.captureView).hidden=!active;
    }
  }
  for(const button of get('capture-tabs').querySelectorAll('button'))button.onclick=()=>showCapture(button.dataset.captureView);
  get('new-note').addEventListener('click',()=>{if(get('capture-card').hidden)return;showCapture('capture-form');if(!get('thought').disabled)get('thought').focus();});
  function categoriesNavigation() {
    const list=get('category-navigation');list.replaceChildren();
    for(const option of get('category-filter').options) {
      const button=document.createElement('button');button.type='button';button.className='category-button';
      button.textContent=option.value===''?'Все заметки':option.textContent;
      button.setAttribute('aria-pressed',String(option.selected));
      button.onclick=()=>{
        get('category-filter').value=option.value;
        get('library-title').textContent=button.textContent;
        get('search-form').requestSubmit();categoriesNavigation();
      };
      list.append(button);
    }
  }
  new MutationObserver(categoriesNavigation).observe(get('category-filter'),{childList:true});
  get('clear-search').addEventListener('click',()=>{get('library-title').textContent='Все заметки';categoriesNavigation();});
  function openDialog(id,opener) {
    const dialog=get(id);dialog.showModal();dialog._opener=opener;
  }
  for(const [button,id] of [['manage-categories','category-dialog'],['open-telegram','telegram-dialog'],['open-feedback','feedback-dialog']])get(button).onclick=event=>openDialog(id,event.currentTarget);
  for(const button of document.querySelectorAll('[data-close-dialog]'))button.onclick=()=>button.closest('dialog').close();
  for(const dialog of document.querySelectorAll('dialog'))dialog.addEventListener('close',()=>dialog._opener?.focus());
  // The existing reminder action opens the Telegram settings; reveal its new dialog too.
  get('reminder-link').addEventListener('click',()=>{if(!get('telegram-dialog').open)openDialog('telegram-dialog',get('reminder-link'));});
  get('feedback-kind').onchange=()=>{get('feedback-bug-fields').hidden=get('feedback-kind').value!=='bug';};
  let pendingFeedback=null,feedbackSession=0;
  new MutationObserver(()=>{
    if(!get('workspace').hidden)return;
    feedbackSession++;
    for(const dialog of document.querySelectorAll('dialog[open]'))dialog.close();
    get('feedback-form').reset();get('feedback-bug-fields').hidden=false;get('feedback-status').textContent='';pendingFeedback=null;
  }).observe(get('workspace'),{attributes:true,attributeFilter:['hidden']});
  get('feedback-form').onsubmit=async event=>{
    event.preventDefault();const button=get('feedback-submit');if(button.disabled)return;
    const payload={kind:get('feedback-kind').value,subject:get('feedback-subject').value.trim(),description:get('feedback-description').value.trim(),steps:get('feedback-kind').value==='bug'?get('feedback-steps').value.trim():'',expected:get('feedback-kind').value==='bug'?get('feedback-expected').value.trim():'',contact:get('feedback-contact').value.trim()};
    if(!payload.subject || !payload.description){get('feedback-status').textContent='Заполните тему и описание.';return;}
    const session=feedbackSession;
    const serialized=JSON.stringify(payload);
    if(!pendingFeedback || pendingFeedback.serialized!==serialized)pendingFeedback={serialized,key:window.berestaId()};
    const fields=[...get('feedback-form').querySelectorAll('input,textarea,select')];fields.forEach(field=>field.disabled=true);
    button.disabled=true;get('feedback-status').textContent='Отправляем…';
    try {
      const result=await api('/api/v1/feedback',{method:'POST',headers:{'Idempotency-Key':pendingFeedback.key},body:serialized});
      if(session!==feedbackSession)return;
      get('feedback-form').reset();pendingFeedback=null;get('feedback-bug-fields').hidden=false;
      get('feedback-status').textContent=`Спасибо! Обращение ${result.id.slice(0,8)} передано команде.`;
    }catch(error){if(session!==feedbackSession)return;get('feedback-status').textContent=`${error.message} Текст сохранён в форме. Попробуйте отправить ещё раз.`;}
    finally{button.disabled=false;fields.forEach(field=>field.disabled=false);}
  };
  // On phones the three secondary actions live behind one menu button next to the category chips.
  const navMore = get('nav-more'), navMenu = get('nav-menu');
  function setNavMenu(open) {
    navMenu.classList.toggle('is-open',open); navMore.setAttribute('aria-expanded',String(open));
  }
  navMore.onclick = () => setNavMenu(!navMenu.classList.contains('is-open'));
  navMenu.addEventListener('click',event => { if (event.target.closest('button')) setNavMenu(false); });
  document.addEventListener('click',event => {
    if (navMenu.classList.contains('is-open') && !event.target.closest('.nav-row')) setNavMenu(false);
  });
  document.addEventListener('keydown',event => {
    if (event.key === 'Escape' && navMenu.classList.contains('is-open')) { setNavMenu(false); navMore.focus(); }
  });
  showCapture('capture-form');showPanel('');setEditing(false,false);categoriesNavigation();syncNote();
})();
