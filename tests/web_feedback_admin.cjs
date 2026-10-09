'use strict';
const assert=require('node:assert/strict'),fs=require('fs'),path=require('path'),{JSDOM}=require('jsdom');
module.exports=async()=>{
 const root=path.join(__dirname,'../app/static'),dom=new JSDOM(fs.readFileSync(path.join(root,'admin.html'),'utf8'),{url:'https://beresta.invalid/admin',runScripts:'outside-only'}),w=dom.window,$=id=>w.document.getElementById(id);
 let denied=false,patched=false;const record={id:'case1',user_id:'synthetic',kind:'bug',subject:'<img src=x onerror=alert(1)>',description:'<script>bad</script>',steps:'Steps',expected:'Works',contact:'@test',created_at:1,status:'new',version:1};
 w.fetch=async(url,options={})=>{let data=[record],status=200;if(denied)status=403;
  if(url==='/api/v1/auth/me')data={csrf_token:'test-csrf'};
  if(options.method==='PATCH'){assert.equal(options.headers['X-CSRF-Token'],'test-csrf');assert.equal(JSON.parse(options.body).version,1);patched=true;data=record;}
  return {ok:status===200,status,headers:new Headers(),json:async()=>status===200?data:{detail:'Forbidden'}};
 };
 async function settle(){for(let i=0;i<25;i++)await new Promise(resolve=>setImmediate(resolve));}
 try{w.eval(fs.readFileSync(path.join(root,'admin.feedback.js'),'utf8'));$('show-feedback').click();await settle();
  assert.equal($('feedback-inbox').hidden,false);assert.equal($('inbox-list').querySelector('img,script'),null);assert.match($('inbox-list').textContent,/<script>/);
  [...$('inbox-list').querySelectorAll('button')].find(x=>x.textContent==='В работе').click();await settle();assert.equal(patched,true);
  denied=true;$('feedback-filters').dispatchEvent(new w.Event('submit',{cancelable:true}));await settle();assert.equal($('inbox-list').textContent,'');assert.match($('inbox-status').textContent,/администратора/);
  console.log('Feedback admin checks passed: safe rendering, status/CSRF and access revocation.');
 }finally{dom.window.close();}
};
