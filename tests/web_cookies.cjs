'use strict';
// Cookie banner: first visit, both choices, remembered choice, blocked storage, reopening and the
// rule that optional storage reaches localStorage only after "Принять все".
const assert=require('node:assert/strict'),fs=require('fs'),path=require('path'),{JSDOM}=require('jsdom');
const root=path.join(__dirname,'../app/static');
const read=name=>fs.readFileSync(path.join(root,name),'utf8');
const reply=(data,status=200)=>({ok:status<400,status,headers:new Headers(),json:async()=>structuredClone(data)});
async function settled(){for(let i=0;i<25;i++)await new Promise(resolve=>setImmediate(resolve));}
const KEY='beresta.cookie-consent.v1',DISMISSED='beresta.onboarding.dismissed.v1',ZONE='beresta.reminderZone';

function page({stored=null,blocked=false,file='index.html'}={}){
 const dom=new JSDOM(read(file),{url:'https://beresta.invalid/',runScripts:'outside-only'});
 const w=dom.window;
 if(stored!==null)w.localStorage.setItem(KEY,stored);
 if(blocked)for(const method of ['getItem','setItem','removeItem'])Object.defineProperty(w.Storage.prototype,method,{configurable:true,value(){throw new Error('blocked');}});
 w.eval(read('cookie-consent.js'));
 return {dom,w,$:id=>w.document.getElementById(id)};
}
const record=choice=>JSON.stringify({choice,version:1,at:'2026-10-12T09:00:00.000Z'});

function firstVisit(){
 const {dom,w,$}=page();
 try{
  const bar=$('cookie-consent');
  assert.ok(bar,'banner on first visit');
  assert.equal(bar.getAttribute('role'),'dialog');
  assert.ok(bar.getAttribute('aria-labelledby')&&$(bar.getAttribute('aria-labelledby')),'labelled');
  const buttons=[...bar.querySelectorAll('button')];
  assert.deepEqual(buttons.map(b=>b.textContent),['Принять все','Только необходимые']);
  assert.equal(buttons[0].className,buttons[1].className,'both buttons look the same');
  assert.ok(buttons.every(b=>b.type==='button'&&!b.disabled&&b.tabIndex>=0),'focusable buttons');
  assert.equal(bar.querySelectorAll('input').length,0,'no pre-ticked options');
  assert.equal(bar.querySelector('a').getAttribute('href'),'/cookies');
  assert.equal(w.berestaConsent.choice(),null);
  assert.equal(w.localStorage.getItem(KEY),null,'nothing saved before a choice');
  assert.equal(w.document.body.firstElementChild,bar,'first in tab order');
  assert.equal(w.document.body.classList.contains('cookie-bar-open'),true);
 }finally{dom.window.close();}
}
function choices(){
 for(const [id,choice] of [['cookie-accept-all','all'],['cookie-necessary','necessary']]){
  const {dom,w,$}=page();
  try{
   $(id).click();
   assert.equal($('cookie-consent'),null,'hidden after '+choice);
   assert.equal(w.document.body.classList.contains('cookie-bar-open'),false);
   const saved=JSON.parse(w.localStorage.getItem(KEY));
   assert.equal(saved.choice,choice);assert.equal(saved.version,1);assert.ok(!Number.isNaN(Date.parse(saved.at)),'time saved');
   assert.equal(w.berestaConsent.choice(),choice);assert.equal(w.berestaConsent.allowsOptional(),choice==='all');
   // Same stored record on a fresh page load: the banner stays away.
   const again=page({stored:w.localStorage.getItem(KEY)});
   try{assert.equal(again.$('cookie-consent'),null,'reload keeps it hidden');assert.equal(again.w.berestaConsent.choice(),choice);}
   finally{again.dom.window.close();}
  }finally{dom.window.close();}
 }
 for(const bad of ['not json','{"choice":"maybe","version":1}',record('all').replace('"version":1','"version":0')]){
  const {dom,$}=page({stored:bad});
  try{assert.ok($('cookie-consent'),'unusable record asks again: '+bad);}finally{dom.window.close();}
 }
}
function optionalStorage(){
 let t=page();
 try{
  t.$('cookie-necessary').click();
  t.w.berestaConsent.write(DISMISSED,'1');t.w.berestaConsent.write(ZONE,'Asia/Yekaterinburg');
  assert.equal(t.w.localStorage.getItem(DISMISSED),null,'necessary keeps the dismissal out of localStorage');
  assert.equal(t.w.localStorage.getItem(ZONE),null);
  assert.equal(t.w.sessionStorage.getItem(DISMISSED),'1');
  assert.equal(t.w.berestaConsent.read(DISMISSED),'1');assert.equal(t.w.berestaConsent.read(ZONE),'Asia/Yekaterinburg');
 }finally{t.dom.window.close();}
 t=page();
 try{
  t.w.berestaConsent.write(DISMISSED,'1');
  assert.equal(t.w.localStorage.getItem(DISMISSED),null,'no choice yet means no localStorage');
  t.$('cookie-accept-all').click();
  t.w.berestaConsent.write(DISMISSED,'1');
  assert.equal(t.w.localStorage.getItem(DISMISSED),'1','all allows localStorage');
  assert.equal(t.w.berestaConsent.read(DISMISSED),'1');
 }finally{t.dom.window.close();}
}
function reopen(){
 const {dom,w,$}=page({stored:record('all')});
 try{
  assert.equal($('cookie-consent'),null);
  w.berestaConsent.write(DISMISSED,'1');w.berestaConsent.write(ZONE,'Europe/Berlin');
  assert.equal(w.localStorage.getItem(DISMISSED),'1');
  const footer=w.document.querySelector('#landing-footer [data-cookie-settings]'),menu=$('open-cookie-settings');
  assert.ok(footer&&menu,'settings link in landing footer and app menu');
  assert.equal(menu.closest('.nav-bottom')!==null,true);
  footer.click();
  assert.ok($('cookie-consent'),'settings link reopens the banner');
  assert.equal(w.document.activeElement.id,'cookie-accept-all','focus moves to the banner');
  menu.click();assert.equal(w.document.querySelectorAll('#cookie-consent').length,1,'never two banners');
  $('cookie-necessary').click();
  assert.equal($('cookie-consent'),null);
  assert.equal(JSON.parse(w.localStorage.getItem(KEY)).choice,'necessary','choice can be changed');
  assert.equal(w.localStorage.getItem(DISMISSED),null,'switching to necessary removes optional keys');
  assert.equal(w.localStorage.getItem(ZONE),null);
 }finally{dom.window.close();}
}
function blockedStorage(){
 const {dom,w,$}=page({blocked:true});
 try{
  assert.ok($('cookie-consent'),'banner works without storage');
  $('cookie-necessary').click();
  assert.equal($('cookie-consent'),null,'hidden for this page');
  assert.equal(w.berestaConsent.choice(),'necessary','choice kept in memory');
  w.berestaConsent.write(DISMISSED,'1');
  assert.equal(w.berestaConsent.read(DISMISSED),'1','value kept in memory');
  w.berestaConsent.reopen();assert.ok($('cookie-consent'));
  $('cookie-accept-all').click();assert.equal(w.berestaConsent.allowsOptional(),true);
  w.berestaConsent.write(ZONE,'UTC');assert.equal(w.berestaConsent.read(ZONE),'UTC');
 }finally{dom.window.close();}
}
async function onboardingFollowsChoice(choice){
 const dom=new JSDOM(read('index.html'),{url:'https://beresta.invalid/',runScripts:'outside-only'});
 const w=dom.window,$=id=>w.document.getElementById(id);
 try{
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
  w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
  w.fetch=async(input)=>{
   const url=new URL(input,w.location.href);
   if(url.pathname==='/health'||url.pathname==='/api/v1/provider/usage')return reply({simulation:true});
   if(url.pathname==='/api/v1/auth/me')return reply({username:'tester',csrf_token:'csrf'});
   if(url.pathname==='/api/v1/categories'||url.pathname==='/api/v1/jobs'||url.pathname==='/api/v1/notes')return reply([]);
   if(url.pathname==='/api/v1/telegram/links')return reply({pending:[],identities:[]});
   throw new Error('Unexpected '+url.pathname);
  };
  w.eval(read('cookie-consent.js')+'\n'+read('app.js')+'\n'+read('workspace.js')+'\n'+read('onboarding.js')+'\n'+read('focus.js'));await settled();
  $(choice==='all'?'cookie-accept-all':'cookie-necessary').click();
  assert.ok($('onboarding'),'first-run panel is shown');
  $('onboarding-dismiss').click();
  assert.equal($('onboarding'),null,'hidden after dismiss');
  assert.equal(w.localStorage.getItem(DISMISSED),choice==='all'?'1':null);
  assert.equal(w.sessionStorage.getItem(DISMISSED),choice==='all'?null:'1');
 }finally{dom.window.close();}
}
function staticPages(){
 for(const file of ['index.html','privacy.html','terms.html','data-deletion.html','cookies.html']){
  const html=read(file);
  assert.match(html,/<script src="\/static\/cookie-consent\.js"/,file+' loads the banner');
  assert.match(html,/href="\/static\/cookie-consent\.css"/,file+' loads banner styles');
  assert.match(html,/data-cookie-settings/,file+' has a settings link');
  assert.equal(/<script(?![^>]*\bsrc=)/.test(html),false,file+' has no inline script');
  assert.equal(/\sstyle=/.test(html),false,file+' has no inline style');
  if(file!=='cookies.html')assert.match(html,/href="\/cookies"/,file+' links to the cookie page');
 }
 const cookies=read('cookies.html');
 for(const name of ['notes_session','notes_tg_login','notes_email_registration','beresta_admin',KEY,DISMISSED,ZONE])assert.ok(cookies.includes(name),name+' is listed');
 assert.match(cookies,/Рекламные/);
}
module.exports=async()=>{
 firstVisit();choices();optionalStorage();reopen();blockedStorage();staticPages();
 await onboardingFollowsChoice('all');await onboardingFollowsChoice('necessary');
 console.log('Cookie checks passed: banner, both choices, remembered choice, blocked storage, reopening, optional storage.');
};
if(require.main===module)module.exports().catch(e=>{console.error(e);process.exitCode=1;});
