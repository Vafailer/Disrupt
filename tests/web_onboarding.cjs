'use strict';
const assert=require('node:assert/strict'),fs=require('fs'),path=require('path'),{JSDOM}=require('jsdom');
const root=path.join(__dirname,'../app/static');
const read=name=>fs.readFileSync(path.join(root,name),'utf8');
const reply=(data,status=200)=>({ok:status<400,status,headers:new Headers(),json:async()=>structuredClone(data)});
async function settled(){for(let i=0;i<25;i++)await new Promise(resolve=>setImmediate(resolve));}
function open(){
 const dom=new JSDOM(read('index.html'),{url:'https://beresta.invalid/',runScripts:'outside-only'});
 const w=dom.window;
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
 w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
 return {dom,w,$:id=>w.document.getElementById(id)};
}
function landing(){
 const {dom,w,$}=open();
 w.fetch=async()=>reply({detail:'Войдите'},401);
 w.eval(read('landing.js'));
 try{
  assert.equal($('auth').hidden,false);
  assert.ok($('landing-title'),'landing headline');
  assert.equal($('auth-form').querySelector('button').value,'login');
  $('landing-register').click();
  assert.equal($('auth').dataset.intent,'register');assert.equal($('auth-title').textContent,'Создать аккаунт');
  assert.equal($('password').getAttribute('autocomplete'),'new-password');
  assert.equal($('auth-form').querySelector('button').value,'register','Enter must submit the register action');
  assert.equal($('auth-form').querySelectorAll('button[type=submit]').length,2);
  $('landing-login').click();
  assert.equal($('auth').dataset.intent,'login');assert.equal($('auth-form').querySelector('button').value,'login');
  $('landing-register-bottom').click();assert.equal($('auth').dataset.intent,'register');
 }finally{dom.window.close();}
}
async function firstRun({storage='ok',notes=[],preset=false}={}){
 const {dom,w,$}=open();
 if(storage==='throws')for(const method of ['getItem','setItem'])Object.defineProperty(w.Storage.prototype,method,{configurable:true,value(){throw new Error('blocked');}});
 else if(preset)w.localStorage.setItem('beresta.onboarding.dismissed.v1','1');
 w.fetch=async(input)=>{
  const url=new URL(input,w.location.href);
  if(url.pathname==='/health'||url.pathname==='/api/v1/provider/usage')return reply({simulation:true});
  if(url.pathname==='/api/v1/auth/me')return reply({username:'tester',csrf_token:'csrf'});
  if(url.pathname==='/api/v1/categories')return reply([]);
  if(url.pathname==='/api/v1/jobs')return reply([]);
  if(url.pathname==='/api/v1/notes')return reply(notes);
  if(url.pathname==='/api/v1/telegram/links')return reply({pending:[],identities:[]});
  throw new Error('Unexpected '+url.pathname);
 };
 w.eval(read('app.js')+'\n'+read('workspace.js')+'\n'+read('onboarding.js'));await settled();
 return {dom,w,$};
}
module.exports=async()=>{
 landing();
 let t=await firstRun();
 try{
  assert.ok(t.$('onboarding'),'shown for zero notes');
  assert.equal(t.$('notes').querySelector('[data-empty=library]')!==null,true);
  assert.equal(t.$('onboarding-steps').children.length,3);
  t.$('onboarding-telegram').click();assert.equal(t.$('telegram-dialog').open,true);
  t.$('onboarding-dismiss').click();assert.equal(t.$('onboarding'),null,'hidden after dismiss');
  assert.equal(t.w.localStorage.getItem('beresta.onboarding.dismissed.v1'),'1');
 }finally{t.dom.window.close();}
 t=await firstRun({preset:true});
 try{assert.equal(t.$('onboarding'),null,'remembered dismissal');}finally{t.dom.window.close();}
 t=await firstRun({storage:'throws'});
 try{
  assert.ok(t.$('onboarding'),'renders when storage throws');
  t.$('onboarding-dismiss').click();assert.equal(t.$('onboarding'),null,'dismiss works without storage');
 }finally{t.dom.window.close();}
 t=await firstRun({notes:[{id:'n1',title:'Заметка'}]});
 try{assert.equal(t.$('onboarding'),null,'not shown when notes exist');}finally{t.dom.window.close();}
 console.log('Onboarding checks passed: landing intent, first-run panel, dismissal, blocked storage.');
};
if(require.main===module)module.exports().catch(e=>{console.error(e);process.exitCode=1;});
