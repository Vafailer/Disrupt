'use strict';
const jsdom=require('jsdom');const Orig=jsdom.JSDOM;const counts=new Map();let windows=0;const log=[];
jsdom.JSDOM=function(...a){const d=new Orig(...a);const w=d.window;const id=++windows;
 for(const name of ['setTimeout','setInterval']){const f=w[name].bind(w);w[name]=(fn,ms,...r)=>{const s=(new Error().stack.split('\n')[2]||'').trim();const k=`win${id} ${name} ${ms} ${s}`;counts.set(k,(counts.get(k)||0)+1);return f(fn,ms,...r);};}
 const origEval=w.eval.bind(w);
 const fetchDesc=()=>{};log.push('window '+id);return d;};
const file=process.argv[2];
setTimeout(()=>{console.log('WATCHDOG '+file);console.log(log.join('\n'));
 const top=[...counts.entries()].sort((a,b)=>b[1]-a[1]).slice(0,15);for(const [k,v] of top)console.log(v,k);
 console.log('handles',process._getActiveHandles().map(h=>h.constructor.name).join(','));process.exit(3);},20000).unref?.();
const t=setTimeout(()=>{},1);
Promise.resolve(require('./tests/'+file)()).then(()=>console.log('DONE')).catch(e=>{console.error('ERR',e);});
