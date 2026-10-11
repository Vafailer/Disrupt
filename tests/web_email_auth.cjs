'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const root=path.join(__dirname,'../app/static');
const settle=async()=>{for(let i=0;i<10;i++)await new Promise(resolve=>setImmediate(resolve));};
async function boot(enabled=true){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://beresta.invalid/',runScripts:'outside-only'});
 const w=dom.window,$=id=>w.document.getElementById(id),calls=[],entered=[];
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
 w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
 w.localStorage.setItem=()=>{throw new Error('Never store a password or code');};
 let error=null;
 w.enterApp=async user=>{entered.push(user);};
 w.api=async(url,options={})=>{
  const body=options.body?JSON.parse(options.body):null;calls.push({url,body});
  if(url.endsWith('/options'))return{login_enabled:enabled,registration_enabled:enabled};
  if(error)throw new Error(error);
  if(url.endsWith('/start'))return{registration_id:'synthetic-registration-id'};
  if(url.endsWith('/recovery/email'))return{message:'Если адрес подтверждён, письмо отправлено.'};
  if(url.endsWith('/confirm')||url.endsWith('/login'))return{id:'same-user',username:'owner'};
  throw new Error('Unexpected '+url);
 };
 w.eval(fs.readFileSync(path.join(root,'email-auth.js'),'utf8'));
 await settle();
 return{w,$,calls,entered,setError:value=>{error=value;},close:()=>dom.window.close()};
}
async function registration(){
 const t=await boot();const{w,$,calls,entered}=t;
 try{
  $('landing-register').click();assert.equal($('email-auth-dialog').open,true);
  $('email-auth-address').value='owner@example.test';$('email-auth-password').value='synthetic-password-1';
  await $('email-auth-form').onsubmit({preventDefault(){}});
  assert.equal(calls.length,1,'Consent is needed before sending');
  $('email-auth-consent').checked=true;
  await $('email-auth-form').onsubmit({preventDefault(){}});
  assert.equal(calls.length,1,'Age confirmation is needed before sending');
  assert.match($('email-auth-message').textContent,/18 лет/);
  $('email-auth-age').checked=true;
  await $('email-auth-form').onsubmit({preventDefault(){}});
  assert.equal(calls.at(-1).body.policy_version,'2026-10-11');assert.equal(calls.at(-1).body.confirm_age,true);
  assert.equal($('email-auth-password').value,'');assert.equal($('email-auth-code-form').hidden,false);
  $('email-auth-code').value='000001';
  t.setError('<img src=x onerror=alert(1)>Код не подошёл');
  await $('email-auth-code-form').onsubmit({preventDefault(){}});
  assert.equal(entered.length,0);assert.equal($('email-auth-message').querySelector('img'),null);
  t.setError(null);await $('email-auth-code-form').onsubmit({preventDefault(){}});
  assert.deepEqual(calls.at(-1).body,{registration_id:'synthetic-registration-id',code:'000001'});
  assert.equal(entered[0].id,'same-user');assert.equal($('email-auth-code').value,'');
  assert.equal($('email-auth-dialog').open,false);
 }finally{t.close();}
}
async function retryAndLogin(){
 const t=await boot();const{$,calls,entered}=t;
 try{
  $('landing-register-bottom').click();$('email-auth-consent').checked=true;$('email-auth-age').checked=true;
  $('email-auth-address').value='owner@example.test';$('email-auth-password').value='synthetic-password-1';
  await $('email-auth-form').onsubmit({preventDefault(){}});
  const before=calls.length;$('email-auth-retry').click();
  assert.equal(calls.length,before,'No automatic email resend');assert.equal($('email-auth-form').hidden,false);
  $('email-auth-close').click();$('email-login').click();
  assert.equal($('email-auth-consent-label').hidden,true);assert.equal($('email-auth-age-label').hidden,true);
  $('email-auth-address').value='owner@example.test';$('email-auth-password').value='synthetic-password-1';
  $('email-auth-recover').click();await settle();
  assert.ok(calls.at(-1).url.endsWith('/recovery/email'));
  assert.deepEqual(calls.at(-1).body,{email:'owner@example.test'});
  await $('email-auth-form').onsubmit({preventDefault(){}});
  assert.ok(calls.at(-1).url.endsWith('/login'));assert.equal(entered[0].id,'same-user');
 }finally{t.close();}
}
module.exports=async()=>{
 await registration();await retryAndLogin();
 const t=await boot(false);
 try{assert.equal(t.$('email-login').hidden,true);t.$('landing-register').click();assert.equal(t.$('email-auth-dialog').open,false);}
 finally{t.close();}
 console.log('Email DOM checks passed: consent, code, leading zero, safe errors, explicit resend, login, hidden unavailable feature.');
};
