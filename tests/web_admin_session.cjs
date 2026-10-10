'use strict';
const assert=require('node:assert/strict'),fs=require('fs'),path=require('path'),{JSDOM}=require('jsdom');
module.exports=async()=>{
 const root=path.join(__dirname,'../app/static'),dom=new JSDOM(fs.readFileSync(path.join(root,'admin.html'),'utf8'),{url:'http://127.0.0.1:18000/admin',runScripts:'outside-only'}),w=dom.window,$=id=>w.document.getElementById(id);
 const calls=[],downloads=[];
 let signedIn=false,loginStatus=200,exportReply=null,auditRows=[];
 w.URL.createObjectURL=blob=>{downloads.push(blob);return 'blob:synthetic';};
 w.URL.revokeObjectURL=()=>{};
 w.HTMLAnchorElement.prototype.click=function(){downloads.push(this.download);};
 const answer=(status,data,headers={})=>({ok:status>=200&&status<300,status,headers:new Headers(headers),json:async()=>structuredClone(data),blob:async()=>new Blob(['synthetic zip'])});
 w.fetch=async(input,options={})=>{
  const url=new URL(input,w.location.href);calls.push({url,options});
  assert.equal(url.origin,'http://127.0.0.1:18000');
  assert.equal(options.credentials,'same-origin');assert.equal(options.cache,'no-store');
  assert.ok(url.pathname.startsWith('/admin-api/v1/'));
  if(url.pathname.endsWith('/me'))return signedIn?answer(200,{username:'owner',csrf_token:'csrf-1'}):answer(401,{detail:'x'});
  if(url.pathname.endsWith('/login')){
   assert.equal(options.method,'POST');
   const body=JSON.parse(options.body);assert.deepEqual(Object.keys(body).sort(),['password','totp','username']);
   if(loginStatus===200)signedIn=true;
   return loginStatus===200?answer(200,{username:'owner',csrf_token:'csrf-1'}):answer(loginStatus,{detail:'server detail must not be shown'});
  }
  if(url.pathname.endsWith('/logout')){assert.equal(options.method,'POST');assert.equal(options.headers['X-CSRF-Token'],'csrf-1');signedIn=false;return answer(204,null);}
  if(url.pathname.endsWith('/export.zip'))return exportReply?exportReply():answer(200,null,{'Content-Type':'application/zip'});
  if(url.pathname.endsWith('/audit'))return answer(200,auditRows);
  throw new Error('unexpected '+url.pathname);
 };
 async function settle(){for(let i=0;i<25;i++)await new Promise(resolve=>setImmediate(resolve));}
 const submit=form=>$(form).dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
 async function signIn(){
  $('admin-username').value='owner';$('admin-password').value='synthetic-password';$('admin-code').value='123456';
  submit('login-form');await settle();
 }
 try{
  for(const file of ['admin.auth.js','admin.feedback.js','admin.tools.js'])w.eval(fs.readFileSync(path.join(root,file),'utf8'));
  await settle();
  // Not signed in: only the form and the network note are shown.
  assert.equal($('login-view').hidden,false);assert.equal($('admin-app').hidden,true);assert.equal($('logout').hidden,true);
  assert.match(w.document.querySelector('.network-note').textContent,/Доступ только из приватной сети/);
  // Every failure gets the same message, and the password is not kept in the field.
  loginStatus=401;await signIn();
  assert.equal($('login-message').hidden,false);assert.match($('login-message').textContent,/Не удалось войти/);
  assert.doesNotMatch($('login-message').textContent,/server detail/);assert.equal($('admin-password').value,'');
  const failure=$('login-message').textContent;
  loginStatus=403;await signIn();assert.equal($('login-message').textContent,failure);
  loginStatus=429;await signIn();assert.match($('login-message').textContent,/Слишком много попыток/);
  assert.equal($('admin-app').hidden,true);
  loginStatus=200;await signIn();
  assert.equal($('admin-app').hidden,false);assert.equal($('login-view').hidden,true);assert.equal($('logout').hidden,false);
  assert.equal($('admin-user').textContent,'owner');assert.equal(w.adminSession.csrf,'csrf-1');assert.equal($('admin-password').value,'');
  // Archive: valid range is downloaded, wrong ranges never reach the server.
  $('archive-from').value='2026-09-01';$('archive-to').value='2026-09-07';
  const before=calls.length;submit('archive-form');await settle();
  const request=calls.at(-1);assert.equal(calls.length,before+1);
  assert.equal(request.url.pathname,'/admin-api/v1/export.zip');
  assert.equal(request.url.searchParams.get('from'),'2026-09-01');assert.equal(request.url.searchParams.get('to'),'2026-09-07');
  assert.ok(downloads.includes('beresta-metrics-2026-09-01-2026-09-07.zip'));assert.match($('archive-status').textContent,/скачан/);
  $('archive-from').value='2026-06-01';$('archive-to').value='2026-09-07';
  const count=calls.length;submit('archive-form');await settle();
  assert.equal(calls.length,count);assert.match($('archive-status').textContent,/92/);
  $('archive-from').value='2026-09-08';$('archive-to').value='2026-09-07';submit('archive-form');await settle();
  assert.equal(calls.length,count);assert.match($('archive-status').textContent,/Проверьте даты/);
  $('archive-from').value='2026-09-01';$('archive-to').value='2026-09-07';
  exportReply=()=>answer(422,{detail:'Конец периода не может быть в будущем'});submit('archive-form');await settle();
  assert.match($('archive-status').textContent,/в будущем/);
  exportReply=()=>answer(200,null,{'Content-Type':'text/html'});submit('archive-form');await settle();
  assert.match($('archive-status').textContent,/Не удалось собрать архив/);assert.equal(downloads.filter(x=>String(x).endsWith('.zip')).length,1);
  // Audit log is drawn as text only.
  auditRows=[{id:'a',created_at:1790000000,admin:'owner',action:'export_zip',target:null,ip:'10.77.0.5',details:{from:'2026-09-01'}},
             {id:'b',created_at:1790000100,admin:null,action:'<img src=x onerror=alert(1)>',target:'<b>x</b>',ip:null,details:{}}];
  $('show-audit').click();await settle();
  assert.equal($('audit-view').hidden,false);assert.equal($('audit-list').querySelectorAll('tbody tr').length,2);
  assert.equal($('audit-list').querySelector('img,b'),null);assert.match($('audit-list').textContent,/Выгрузка архива метрик/);assert.match($('audit-list').textContent,/<img src=x/);
  // An expired session on any call returns to the form and clears what was shown.
  exportReply=()=>answer(401,{detail:'x'});submit('archive-form');await settle();
  assert.equal($('login-view').hidden,false);assert.equal($('admin-app').hidden,true);assert.equal($('audit-list').textContent,'');
  assert.match($('login-message').textContent,/Сессия завершилась/);
  loginStatus=200;await signIn();assert.equal($('admin-app').hidden,false);
  // Logout sends the CSRF header, closes the screen and drops the data.
  $('show-audit').click();await settle();assert.notEqual($('audit-list').textContent,'');
  $('logout').click();await settle();
  assert.equal($('login-view').hidden,false);assert.equal($('admin-app').hidden,true);assert.equal($('audit-list').textContent,'');
  assert.equal(w.adminSession.csrf,'');assert.equal($('admin-user').textContent,'');
  assert.equal(w.localStorage.length,0);assert.equal(w.sessionStorage.length,0);
  console.log('Admin session checks passed: sign-in, generic errors, archive, audit log, expiry and logout.');
 }finally{dom.window.close();}
};
