'use strict';
const assert=require('node:assert/strict'),fs=require('fs'),path=require('path'),{JSDOM}=require('jsdom');
const root=path.join(__dirname,'../app/static');
module.exports=async()=>{
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://beresta.invalid/',runScripts:'outside-only'}),w=dom.window,$=id=>w.document.getElementById(id);
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
 let lost=true,posts=[];
 const reply=(data,status=200)=>({ok:status<400,status,headers:new Headers(),json:async()=>structuredClone(data)});
 const note={id:'n1',title:'Synthetic note',markdown:'## Heading\nText',original_text:'Original',items:[],conclusions:[],version:1,provider:'manual',category_id:'c1'};
 w.fetch=async(input,options={})=>{
  const url=new URL(input,w.location.href);
  if(url.pathname==='/health'||url.pathname==='/api/v1/provider/usage')return reply({simulation:true});
  if(url.pathname==='/api/v1/auth/me')return reply({username:'tester',csrf_token:'csrf'});
  if(url.pathname==='/api/v1/categories')return reply([{id:'c1',name:'Research',version:1}]);
  if(url.pathname==='/api/v1/jobs')return reply([]);
  if(url.pathname==='/api/v1/notes')return reply([note]);
  if(url.pathname==='/api/v1/notes/n1')return reply(note);
  if(url.pathname.endsWith('/opened')||url.pathname.endsWith('/original-opened')||url.pathname==='/api/v1/search/events')return reply(null,204);
  if(url.pathname==='/api/v1/feedback'){posts.push(options);if(lost){lost=false;throw new Error('Network lost');}return reply({id:'12345678-abcd'},201);}
  throw new Error('Unexpected '+url.pathname);
 };
 async function settled(){for(let i=0;i<25;i++)await new Promise(resolve=>setImmediate(resolve));}
 try{
  w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8')+'\n'+fs.readFileSync(path.join(root,'workspace.js'),'utf8'));await settled();
  assert.equal($('workspace').hidden,false);assert.equal($('category-navigation').children.length,3);
  $('category-navigation').lastChild.click();await settled();assert.equal($('library-title').textContent,'Research');
  $('notes').querySelector('button').click();await settled();
  assert.equal($('note-heading-title').textContent,'Synthetic note');assert.equal($('view-tasks').hidden,false);assert.equal($('view-reminders').hidden,false);assert.equal($('markdown').hidden,true);assert.equal(w.document.querySelector('#note-card [data-note-view]'),null);
  $('note-edit-start').click();assert.equal($('markdown').hidden,false);assert.equal($('preview').hidden,true);$('markdown').value='Unsaved';
  w.document.querySelector('[data-note-panel=original]').click();await settled();assert.equal($('original-details').open,true);assert.equal($('view-original').hidden,false);assert.equal($('markdown').value,'Unsaved');assert.equal($('markdown').hidden,false);
  $('mobile-library-toggle').click();assert.equal($('mobile-library-toggle').getAttribute('aria-expanded'),'true');
  $('mobile-library-toggle').click();assert.equal($('workspace').classList.contains('library-open'),false);
  $('open-feedback').click();assert.equal($('feedback-dialog').open,true);
  $('feedback-subject').value='Bug';$('feedback-description').value='Synthetic reproduction';
  await $('feedback-form').onsubmit({preventDefault(){}});assert.match($('feedback-status').textContent,/Текст сохранён/);assert.equal($('feedback-description').value,'Synthetic reproduction');
  await $('feedback-form').onsubmit({preventDefault(){}});assert.match($('feedback-status').textContent,/передано команде/);
  assert.equal(posts[0].headers['Idempotency-Key'],posts[1].headers['Idempotency-Key']);assert.equal(posts[0].headers['X-CSRF-Token'],'csrf');assert.equal($('feedback-description').value,'');
  assert.equal($('markdown').value,'Unsaved');
  $('feedback-description').value='Private draft';$('workspace').hidden=true;await settled();assert.equal($('feedback-description').value,'');assert.equal($('feedback-dialog').open,false);
  console.log('Workspace checks passed: categories, inline edit, original panel, preserved drafts, feedback lost-response replay.');
 }finally{await new Promise(resolve=>w.setTimeout(resolve,10));dom.window.close();}
};
