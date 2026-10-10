'use strict';
(() => {
  const get = id => document.getElementById(id);
  let lastNote = null;
  function syncNote() {
    const title = get('title').value || 'Без названия';
    if(get('note-heading-title').textContent !== title)get('note-heading-title').textContent = title;
    if (currentNote?.id !== lastNote) {lastNote = currentNote?.id; window.BerestaFocus?.closeAll();get('workspace').classList.remove('library-open');get('mobile-library-toggle').setAttribute('aria-expanded','false');}
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
  get('new-note').addEventListener('click',()=>{if(get('capture-card').hidden)return;window.BerestaFocus?.closeAll();if(!get('thought').disabled)get('thought').focus();});
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
  categoriesNavigation();syncNote();
})();
