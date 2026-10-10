'use strict';
const assert=require('node:assert/strict'),fs=require('fs'),path=require('path'),{JSDOM}=require('jsdom');
module.exports=async()=>{
 const root=path.join(__dirname,'../app/static'),dom=new JSDOM(fs.readFileSync(path.join(root,'admin.html'),'utf8'),{url:'https://beresta.invalid/admin',runScripts:'outside-only'}),w=dom.window,$=id=>w.document.getElementById(id);
 let status=200,patched=false,expired=0,wiped=null;const record={id:'case1',user_id:'synthetic',kind:'bug',subject:'<img src=x onerror=alert(1)>',description:'<script>bad</script>',steps:'Steps',expected:'Works',contact:'@test',created_at:1,status:'new',version:1};
 // Sign-in itself is covered by web_admin_session.cjs. Here the page only needs a session object.
 w.adminSession={csrf:'test-csrf',expired(){expired++;},onLogout(fn){wiped=fn;}};
 w.fetch=async(url,options={})=>{let data=[record];
  assert.match(String(url),/^\/admin-api\/v1\/feedback/);
  assert.equal(options.credentials,'same-origin');
  if(options.method==='PATCH'){assert.equal(url,'/admin-api/v1/feedback/case1');assert.equal(options.headers['X-CSRF-Token'],'test-csrf');assert.equal(JSON.parse(options.body).version,1);patched=true;data=record;}
  return {ok:status===200,status,headers:new Headers(),json:async()=>status===200?data:{detail:'Forbidden'}};
 };
 async function settle(){for(let i=0;i<25;i++)await new Promise(resolve=>setImmediate(resolve));}
 try{w.eval(fs.readFileSync(path.join(root,'admin.feedback.js'),'utf8'));$('show-feedback').click();await settle();
  assert.equal($('feedback-inbox').hidden,false);assert.equal($('statistics-view').hidden,true);assert.equal($('inbox-list').querySelector('img,script'),null);assert.match($('inbox-list').textContent,/<script>/);
  assert.equal($('show-feedback').getAttribute('aria-pressed'),'true');assert.equal($('show-statistics').getAttribute('aria-pressed'),'false');
  [...$('inbox-list').querySelectorAll('button')].find(x=>x.textContent==='В работе').click();await settle();assert.equal(patched,true);
  status=403;$('feedback-filters').dispatchEvent(new w.Event('submit',{cancelable:true}));await settle();assert.equal($('inbox-list').textContent,'');assert.match($('inbox-status').textContent,/запрещён/);assert.equal(expired,0);
  status=401;$('feedback-filters').dispatchEvent(new w.Event('submit',{cancelable:true}));await settle();assert.equal($('inbox-list').textContent,'');assert.equal(expired,1);
  status=200;$('feedback-filters').dispatchEvent(new w.Event('submit',{cancelable:true}));await settle();assert.notEqual($('inbox-list').textContent,'');
  // Logout wipes the inbox and returns to statistics.
  assert.equal(typeof wiped,'function');wiped();assert.equal($('inbox-list').textContent,'');assert.equal($('feedback-inbox').hidden,true);assert.equal($('statistics-view').hidden,false);
  for(const name of ['archive','audit']){$('show-'+name).click();assert.equal($(name+'-view').hidden,false);assert.equal($('feedback-inbox').hidden,true);assert.equal($('statistics-view').hidden,true);}
  console.log('Feedback admin checks passed: safe rendering, status/CSRF, access revocation and sections.');
 }finally{dom.window.close();}
};
