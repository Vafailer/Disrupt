'use strict';
// Telegram sign-in, recovery, reset page, policy banner and account deletion in the browser.
// Timers are replaced with a manual queue so the 2 second polling runs on demand.
const assert=require('node:assert/strict'),fs=require('fs'),path=require('path'),{JSDOM}=require('jsdom');
const root=path.join(__dirname,'../app/static');
const read=name=>fs.readFileSync(path.join(root,name),'utf8');
const reply=(data,status=200)=>({ok:status<400,status,headers:new Headers(),json:async()=>structuredClone(data)});
async function settled(){for(let i=0;i<25;i++)await new Promise(resolve=>setImmediate(resolve));}
const VERSION='2026-10-10', TOKEN='R'.repeat(43);
const user=(extra={})=>({id:'u1',username:'ivan',csrf_token:'csrf-1',policy_current:true,policy_version:VERSION,deletion_requested:false,...extra});

async function open({hash='',me=null,routes={}}={}){
 const dom=new JSDOM(read('index.html'),{url:'https://beresta.invalid/'+hash,runScripts:'outside-only'});
 const w=dom.window,$=id=>w.document.getElementById(id);
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
 w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
 const timers=new Map();let next=1;
 w.setTimeout=fn=>{const id=next++;timers.set(id,fn);return id;};
 w.clearTimeout=id=>{timers.delete(id);};
 const calls=[];
 w.fetch=async(input,options={})=>{
  const url=new URL(input,w.location.href),body=options.body?JSON.parse(options.body):null;
  calls.push({path:url.pathname,method:options.method||'GET',body,headers:options.headers||{}});
  if(url.pathname==='/health'||url.pathname==='/api/v1/provider/usage')return reply({simulation:true});
  if(url.pathname==='/api/v1/auth/me')return me?reply(me):reply({detail:'Войдите в приложение'},401);
  if(url.pathname==='/api/v1/categories'||url.pathname==='/api/v1/jobs'||url.pathname==='/api/v1/notes')return reply([]);
  const route=routes[url.pathname]||Object.entries(routes).find(([key])=>key.endsWith('*')&&url.pathname.startsWith(key.slice(0,-1)))?.[1];
  if(route)return route(body,calls.at(-1));
  throw new Error('Unexpected '+url.pathname);
 };
 w.eval(read('app.js')+'\n'+read('auth-extra.js'));
 await settled();
 // One round of due timers. Everything they schedule waits for the next call.
 const tick=async()=>{const due=[...timers.values()];timers.clear();for(const fn of due)await fn();await settled();};
 return {dom,w,$,calls,timers,tick,closeAll:()=>dom.window.close()};
}
const callsTo=(t,suffix)=>t.calls.filter(c=>c.path.endsWith(suffix));

async function telegramLogin(){
 const login={status:'pending'};
 const t=await open({routes:{
  '/api/v1/auth/telegram/start':()=>reply({login_id:'login-1',deep_link:'https://t.me/beresta_ru_bot?start=login_'+TOKEN,expires_at:new Date(Date.now()+300000).toISOString()},201),
  '/api/v1/auth/telegram/status/*':()=>login.status==='ok'?reply({...user({username:'tg123'}),status:'ok',new_account:true}):login.status==='404'?reply({detail:'Вход не найден. Начните заново'},404):reply({status:login.status}),
 }});
 try{
  assert.equal(t.$('auth').hidden,false);
  t.$('tg-login').click();await settled();
  assert.match(t.$('auth-message').textContent,/политику конфиденциальности/);
  assert.equal(callsTo(t,'/telegram/start').length,0,'no request without consent');
  t.$('accept-policy').checked=true;
  t.$('tg-login').click();await settled();
  const start=callsTo(t,'/telegram/start')[0];
  assert.deepEqual(start.body,{accept_policy:true,policy_version:VERSION});
  assert.equal(t.$('tg-login-box').hidden,false);
  assert.equal(t.$('tg-login-link').href,'https://t.me/beresta_ru_bot?start=login_'+TOKEN);
  assert.equal(t.$('tg-login').disabled,true);
  assert.equal(t.timers.size,1,'polling is scheduled');
  await t.tick();
  assert.equal(callsTo(t,'/status/login-1').length,1);assert.equal(t.timers.size,1,'still waiting');
  assert.equal(t.$('workspace').hidden,true);
  login.status='ok';await t.tick();
  assert.equal(t.$('workspace').hidden,false);assert.equal(t.$('auth').hidden,true);
  assert.equal(t.$('username').textContent,'tg123');
  assert.equal(t.$('message').textContent,'Аккаунт создан. Добро пожаловать!');
  assert.equal(t.timers.size,0,'polling stops after sign-in');
  assert.equal(t.$('policy-banner').hidden,true);
  assert.equal(JSON.stringify(t.w.localStorage.length),'0','nothing is kept in storage');
 }finally{t.closeAll();}
}

async function telegramLoginEnds(){
 for(const [how,expected] of [['expired',/Время вышло/],['404',/Вход не найден/]]){
  const login={status:how==='404'?'404':'pending'};
  const t=await open({routes:{
   '/api/v1/auth/telegram/start':()=>reply({login_id:'login-2',deep_link:'https://t.me/beresta_ru_bot?start=login_'+TOKEN,expires_at:new Date(Date.now()+300000).toISOString()},201),
   '/api/v1/auth/telegram/status/*':()=>login.status==='404'?reply({detail:'Вход не найден. Начните заново'},404):reply({status:login.status}),
  }});
  try{
   t.$('accept-policy').checked=true;t.$('tg-login').click();await settled();
   login.status=how==='404'?'404':'expired';await t.tick();
   assert.match(t.$('auth-message').textContent,expected,how);
   assert.equal(t.$('tg-login-box').hidden,true);assert.equal(t.$('tg-login').disabled,false);
   assert.equal(t.timers.size,0);assert.equal(t.$('workspace').hidden,true);
  }finally{t.closeAll();}
 }
 // A start that has already run out locally ends on the first check, without asking the server.
 let t=await open({routes:{
  '/api/v1/auth/telegram/start':()=>reply({login_id:'login-3',deep_link:'https://t.me/beresta_ru_bot?start=login_'+TOKEN,expires_at:new Date(Date.now()-10000).toISOString()},201),
 }});
 try{
  t.$('accept-policy').checked=true;t.$('tg-login').click();await settled();await t.tick();
  assert.match(t.$('auth-message').textContent,/Время вышло/);assert.equal(callsTo(t,'/status/login-3').length,0);
 }finally{t.closeAll();}
 // Cancel stops polling and a foreign link is never used.
 t=await open({routes:{
  '/api/v1/auth/telegram/start':()=>reply({login_id:'login-4',deep_link:'javascript:alert(1)',expires_at:new Date(Date.now()+300000).toISOString()},201),
 }});
 try{
  t.$('accept-policy').checked=true;t.$('tg-login').click();await settled();
  assert.equal(t.$('tg-login-box').hidden,true);assert.equal(t.timers.size,0);assert.equal(t.$('tg-login').disabled,false);
  assert.ok(t.$('auth-message').textContent.length>0);
 }finally{t.closeAll();}
 t=await open({routes:{
  '/api/v1/auth/telegram/start':()=>reply({login_id:'login-5',deep_link:'https://t.me/beresta_ru_bot?start=login_'+TOKEN,expires_at:new Date(Date.now()+300000).toISOString()},201),
 }});
 try{
  t.$('accept-policy').checked=true;t.$('tg-login').click();await settled();
  assert.equal(t.timers.size,1);t.$('tg-login-cancel').click();
  assert.equal(t.timers.size,0);assert.equal(t.$('tg-login-box').hidden,true);assert.equal(t.$('tg-login').disabled,false);
 }finally{t.closeAll();}
}

async function recovery(){
 const t=await open({routes:{
  '/api/v1/auth/recovery/telegram':body=>body.username==='limited'?reply({detail:'Слишком много попыток. Подождите минуту.'},429):reply({status:'ok',message:'Если у аккаунта подключён Telegram, мы отправили туда ссылку для нового пароля. Она действует 15 минут.'}),
 }});
 try{
  t.$('forgot-password').click();await settled();
  assert.match(t.$('auth-message').textContent,/имя пользователя/);assert.equal(callsTo(t,'/recovery/telegram').length,0);
  t.$('login').value='  ivan ';t.$('forgot-password').click();await settled();
  assert.deepEqual(callsTo(t,'/recovery/telegram')[0].body,{username:'ivan'});
  assert.equal(t.$('auth-info').hidden,false);assert.match(t.$('auth-info').textContent,/Если у аккаунта подключён Telegram/);
  assert.equal(t.$('auth-message').hidden,true);assert.equal(t.$('forgot-password').disabled,false);
  t.$('login').value='limited';t.$('forgot-password').click();await settled();
  assert.match(t.$('auth-message').textContent,/Слишком много попыток/);assert.equal(t.$('auth-info').hidden,true);
 }finally{t.closeAll();}
}

async function resetPage(){
 let outcome='ok';
 const routes={'/api/v1/auth/password-reset':()=>outcome==='ok'?reply({status:'ok'}):reply({detail:'Ссылка недействительна или истекла. Запросите новую'},400)};
 let t=await open({hash:'#reset='+TOKEN,routes});
 try{
  assert.equal(t.$('reset-dialog').open,true,'the link opens the password form');
  assert.equal(t.w.location.hash,'','the token leaves the address bar');
  assert.ok(!t.w.location.href.includes(TOKEN));
  const submit=()=>t.$('reset-form').dispatchEvent(new t.w.Event('submit',{cancelable:true}));
  t.$('reset-password').value='short';t.$('reset-password2').value='short';submit();await settled();
  assert.match(t.$('reset-status').textContent,/от 10 до 128/);
  t.$('reset-password').value='brand-new-password-456';t.$('reset-password2').value='different-password-789';submit();await settled();
  assert.match(t.$('reset-status').textContent,/не совпадают/);assert.equal(callsTo(t,'/password-reset').length,0);
  outcome='dead';t.$('reset-password2').value='brand-new-password-456';submit();await settled();
  assert.match(t.$('reset-status').textContent,/недействительна или истекла/);assert.equal(t.$('reset-dialog').open,true);
  outcome='ok';submit();await settled();
  const sent=callsTo(t,'/password-reset');
  assert.deepEqual(sent.at(-1).body,{token:TOKEN,password:'brand-new-password-456'});
  assert.equal(t.$('reset-dialog').open,false);assert.equal(t.$('reset-password').value,'');
  assert.match(t.$('auth-info').textContent,/Пароль изменён/);
  submit();await settled();
  assert.match(t.$('reset-status').textContent,/Ссылка устарела|от 10 до 128/,'the token is not reused from memory');
  assert.equal(callsTo(t,'/password-reset').length,sent.length);
 }finally{t.closeAll();}
 t=await open({hash:'#reset=short',routes});
 try{assert.equal(t.$('reset-dialog').open,false,'a malformed link opens nothing');}finally{t.closeAll();}
 t=await open({routes});
 try{
  t.w.location.hash='#reset='+TOKEN;t.w.dispatchEvent(new t.w.HashChangeEvent('hashchange'));
  assert.equal(t.$('reset-dialog').open,true,'a link opened in a running page works too');
 }finally{t.closeAll();}
}

async function policyAndDeletion(){
 const routes={
  '/api/v1/account/accept-policy':body=>body.policy_version===VERSION?reply({status:'ok',policy_version:VERSION}):reply({detail:'Политика обновилась'},409),
  '/api/v1/account/delete-request/cancel':()=>reply({status:'cancelled'}),
 };
 let t=await open({me:user({policy_current:false,deletion_requested:true}),routes});
 try{
  assert.equal(t.$('workspace').hidden,false);
  assert.equal(t.$('policy-banner').hidden,false);assert.equal(t.$('deletion-banner').hidden,false);
  assert.equal(t.$('notes').hidden,false,'nothing else is blocked');
  t.$('policy-accept').click();await settled();
  assert.deepEqual(callsTo(t,'/accept-policy')[0].body,{policy_version:VERSION});
  assert.equal(callsTo(t,'/accept-policy')[0].headers['X-CSRF-Token'],'csrf-1');
  assert.equal(t.$('policy-banner').hidden,true);
  t.$('deletion-cancel').click();await settled();
  assert.equal(t.$('deletion-banner').hidden,true);assert.equal(t.$('message').textContent,'Запрос на удаление отменён.');
 }finally{t.closeAll();}
 t=await open({me:user(),routes});
 try{assert.equal(t.$('policy-banner').hidden,true);assert.equal(t.$('deletion-banner').hidden,true);}finally{t.closeAll();}

 const deletion={status:'pending',password:'right'};
 t=await open({me:user(),routes:{
  '/api/v1/account/delete-request':body=>body.password===deletion.password?reply({status:'requested'}):reply({detail:'Неверный пароль'},403),
  '/api/v1/account/delete-request/telegram':()=>reply({login_id:'del-1',deep_link:'https://t.me/beresta_ru_bot?start=login_'+TOKEN,expires_at:new Date(Date.now()+300000).toISOString()},201),
  '/api/v1/account/delete-request/telegram/*':()=>reply({status:deletion.status}),
 }});
 try{
  const link=t.$('open-privacy');assert.equal(link.getAttribute('href'),'/privacy');
  t.$('open-delete').click();assert.equal(t.$('delete-dialog').open,true);
  const submit=()=>t.$('delete-form').dispatchEvent(new t.w.Event('submit',{cancelable:true}));
  submit();await settled();assert.match(t.$('delete-status').textContent,/Введите пароль/);
  t.$('delete-password').value='wrong';submit();await settled();
  assert.equal(t.$('delete-status').textContent,'Неверный пароль');assert.equal(t.timers.size,0);
  t.$('delete-telegram-start').click();await settled();
  assert.equal(t.$('delete-telegram-box').hidden,false);
  assert.equal(t.$('delete-telegram-link').href,'https://t.me/beresta_ru_bot?start=login_'+TOKEN);
  await t.tick();assert.equal(t.timers.size,1);assert.equal(t.$('delete-status').textContent.includes('Запрос принят'),false);
  deletion.status='requested';await t.tick();
  assert.match(t.$('delete-status').textContent,/Запрос принят/);
  assert.equal(t.timers.size,1,'only the page reload is pending');
  assert.equal(callsTo(t,'/delete-request/telegram/del-1').length,2);
 }finally{t.closeAll();}
 t=await open({me:user(),routes:{'/api/v1/account/delete-request':()=>reply({status:'requested'})}});
 try{
  t.$('open-delete').click();t.$('delete-password').value='right';
  t.$('delete-form').dispatchEvent(new t.w.Event('submit',{cancelable:true}));await settled();
  assert.match(t.$('delete-status').textContent,/Запрос принят/);
  assert.deepEqual(callsTo(t,'/delete-request')[0].body,{password:'right'});
 }finally{t.closeAll();}
}

function staticChecks(){
 const index=read('index.html'),privacy=read('privacy.html');
 for(const [name,html] of [['index.html',index],['privacy.html',privacy]]){
  assert.ok(!/\sstyle\s*=/.test(html),name+' has no inline styles');
  assert.ok(!/<script(?![^>]*\ssrc=)/.test(html),name+' has no inline scripts');
  assert.ok(!/data:/.test(html.replace(/data-[a-z-]+/g,'')),name+' has no data: URLs');
 }
 const dom=new JSDOM(index);const doc=dom.window.document;
 const consent=doc.getElementById('accept-policy');
 assert.equal(consent.type,'checkbox');assert.equal(consent.dataset.policyVersion,VERSION);
 assert.ok(consent.closest('label').querySelector('a[href="/privacy"]'),'consent links the policy');
 assert.ok(doc.querySelector('#landing-footer a[href="/privacy"]'),'landing footer links the policy');
 assert.ok(doc.querySelector('#nav-menu a[href="/privacy"]'),'account menu links the policy');
 assert.ok(doc.getElementById('open-delete'));
 assert.ok(doc.querySelector('script[src="/static/auth-extra.js"]')&&doc.querySelector('link[href="/static/auth-extra.css"]'));
 assert.equal(doc.getElementById('forgot-password').type,'button');
 const privacyDoc=new JSDOM(privacy).window.document;
 assert.equal(privacyDoc.querySelector('main').dataset.policyVersion,VERSION);
 assert.ok(privacy.indexOf('Черновик, требует проверки юристом')<privacy.indexOf('<html'),'the draft note stays in a comment');
 assert.equal(privacyDoc.body.textContent.includes('Черновик, требует проверки юристом'),false,'and is not visible');
 assert.ok(privacyDoc.body.textContent.includes('[Оператор: ФИО/ИП, контакт]'));
 dom.window.close();privacyDoc.defaultView.close();
}

module.exports=async()=>{
 staticChecks();
 await telegramLogin();
 await telegramLoginEnds();
 await recovery();
 await resetPage();
 await policyAndDeletion();
 console.log('Auth checks passed: Telegram sign-in, recovery, reset page, policy banner, deletion.');
};
if(require.main===module)module.exports().catch(e=>{console.error(e);process.exitCode=1;});
