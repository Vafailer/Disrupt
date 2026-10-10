'use strict';
(() => {
  const get=id=>document.getElementById(id);
  const kinds={bug:'Ошибка',idea:'Предложение',question:'Вопрос',other:'Другое'};
  const statuses={new:'Новое',in_progress:'В работе',resolved:'Решено'};
  const views={statistics:'statistics-view',feedback:'feedback-inbox',archive:'archive-view',audit:'audit-view'};
  let offset=0,next=null,generation=0;
  const text=(tag,value,cls)=>{const el=document.createElement(tag);el.textContent=value;if(cls)el.className=cls;return el;};
  const auth=()=>window.adminSession||{csrf:'',expired(){}};
  function failure(error){get('inbox-list').replaceChildren();get('inbox-next').disabled=true;get('inbox-previous').disabled=true;get('inbox-status').textContent=error.status===401?'Сессия завершилась. Войдите заново.':error.status===403?'Доступ запрещён.':error.message || 'Не удалось загрузить обращения.';if(error.status===401)auth().expired();}
  async function request(path,options={}) {
    const response=await fetch(path,{credentials:'same-origin',cache:'no-store',redirect:'error',...options});
    const data=await response.json();if(!response.ok){const error=new Error(typeof data.detail==='string'?data.detail:'Не удалось выполнить запрос.');error.status=response.status;throw error;}
    return {data,response};
  }
  async function load(target=0){
    const serial=++generation;get('inbox-list').replaceChildren();get('inbox-status').textContent='Загружаем обращения…';get('inbox-next').disabled=get('inbox-previous').disabled=true;
    try{
      const {data,response}=await request(`/admin-api/v1/feedback?${new URLSearchParams({status:get('inbox-filter').value,limit:'20',offset:String(target)})}`);
      if(serial!==generation)return;
      offset=target;const cursor=response.headers.get('X-Next-Feedback-Offset');next=cursor===null?null:Number(cursor);
      if(!Array.isArray(data) || (next!==null && (!Number.isSafeInteger(next)||next<=offset)))throw new Error('Некорректный ответ сервера.');
      for(const row of data){
        const card=text('article','', 'feedback-card panel');
        card.append(text('p',`${kinds[row.kind] || row.kind} · ${statuses[row.status]} · ${new Date(row.created_at*1000).toLocaleString('ru-RU')}`,'muted'),text('h2',row.subject));
        for(const [label,value] of [['Описание',row.description],['Как повторить',row.steps],['Ожидание',row.expected],['Контакт',row.contact]])if(value){card.append(text('h3',label),text('p',value,'feedback-body'));}
        card.append(text('p',`Обращение ${row.id} · Пользователь ${row.user_id}`,'muted'));
        const actions=text('div','', 'feedback-actions');
        for(const [status,label] of Object.entries(statuses)){
          const button=text('button',label,'secondary');button.type='button';button.disabled=status===row.status;
          button.onclick=async()=>{
            for(const control of actions.querySelectorAll('button'))control.disabled=true;
            try{
              await request(`/admin-api/v1/feedback/${row.id}`,{method:'PATCH',headers:{'Content-Type':'application/json','X-CSRF-Token':auth().csrf},body:JSON.stringify({status,version:row.version})});
              await load(offset);
            }catch(error){if(error.status===401 || error.status===403)failure(error);else{get('inbox-status').textContent=error.message;for(const control of actions.querySelectorAll('button'))control.disabled=false;}}
          };actions.append(button);
        }
        card.append(actions);get('inbox-list').append(card);
      }
      get('inbox-status').textContent=data.length?`Показано обращений: ${data.length}`:'В этом разделе обращений пока нет.';
      get('inbox-next').disabled=next===null;get('inbox-previous').disabled=offset===0;
    }catch(error){if(serial===generation)failure(error);}
  }
  // One place switches the sections. Other scripts listen for the `admin:view` event.
  function show(name){
    generation++;
    for(const [view,id] of Object.entries(views)){
      const section=get(id),button=get('show-'+view);
      if(section)section.hidden=view!==name;
      if(button)button.setAttribute('aria-pressed',String(view===name));
    }
    document.dispatchEvent(new CustomEvent('admin:view',{detail:name}));
    if(name==='feedback')load();
  }
  for(const name of Object.keys(views)){const button=get('show-'+name);if(button)button.onclick=()=>show(name);}
  if(window.adminSession)window.adminSession.onLogout(()=>{generation++;get('inbox-list').replaceChildren();get('inbox-status').textContent='';show('statistics');});
  get('feedback-filters').onsubmit=event=>{event.preventDefault();load();};
  get('inbox-filter').onchange=()=>load();
  get('inbox-next').onclick=()=>{if(next!==null)load(next);};
  get('inbox-previous').onclick=()=>load(Math.max(0,offset-20));
})();
