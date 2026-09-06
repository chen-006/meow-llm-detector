// Original handler execution with synthetic DOM/network; no live user data.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(__dirname,'../../..');
const desktopRoot=fs.existsSync(path.join(root,'gpt56_vnext'))?root:path.resolve(__dirname,'..');
const desk=fs.readFileSync(path.join(desktopRoot,'gpt56_vnext/web/workbench.js'),'utf8');
const results=[];
(async()=>{
 const fields={}; const $=id=>fields[id]||=( {value:'',checked:false,textContent:'',disabled:true} );
 $('collect-url').value='https://b.example/v1';$('collect-key').value='synthetic-B';$('collect-http').checked=true;
 let handler,sent;
 const start=desk.indexOf('      resume.addEventListener("click", async () => {');
 const end=desk.indexOf('\n      });',start)+'\n      });'.length;
 vm.runInNewContext(desk.slice(start,end),{$,row:{base_url:'https://a.example/v1',session_id:'A'},resume:{addEventListener:(_,f)=>handler=f},
   workbench:{sessions:[],timer:null},t:x=>x,errorMessage:e=>e.message,
   post:async(_,body)=>{sent=body;return{session_id:'A'};},clearInterval(){},setInterval(){return 1},pollCollection(){}});
 await handler();assert.equal(sent,undefined);assert.equal($('collect-key').value,'');assert.equal($('collect-url').value,'https://a.example/v1');
 $('collect-key').value='synthetic-A';await handler();assert.equal(sent.key,'synthetic-A');assert.equal(sent.base_url,'https://a.example/v1');assert(sent.allow_insecure);
 results.push('resume never sends B key to historical A; explicit re-entry succeeds');
 let exporter,download;const state={sessionId:'A'},requests=[];
 const eStart=desk.indexOf('action("retention-export",');const eEnd=desk.indexOf('}, "progress");',eStart)+'}, "progress");'.length;
 vm.runInNewContext(desk.slice(eStart,eEnd),{state,TextEncoder,t:x=>x,action:(_,fn)=>exporter=fn,
   json:async(url)=>{requests.push(url);state.sessionId='B';return requests.length===1?{coverage:{},records:[{attempt_id:1}]}:{coverage:{},records:[]};},
   download:(data,name)=>download={data,name}});
 await exporter();assert(requests.every(x=>x.includes('/A?')));assert.equal(download.data.session_id,'A');assert.equal(download.name,'meow-evidence-A.json');
 results.push('export pagination and filename stay pinned to A while current report changes to B');
 const html=fs.readFileSync(path.join(desktopRoot,'gpt56_vnext/web/index.html'),'utf8');
 for(const id of ['allow-http','collect-http','preset-http'])assert(html.includes('id="'+id+'"'));
 assert(desk.includes('allow_insecure: $("preset-http").checked'));
 results.push('all three explicit HTTP controls and preset save wiring exist');
 const webRoot=path.join(root,'meow-web');
 if(fs.existsSync(webRoot)){
  const web=fs.readFileSync(path.join(webRoot,'web/app.js'),'utf8');let fail=true,cleared=0;
  $('key').value='synthetic';
  const ctx=vm.createContext({$,state:{csrf:'synthetic'},message:String,AbortController,setTimeout:()=>1,clearTimeout:()=>cleared++,
    fetch:async()=>{if(fail)throw Error('simulated disconnect');return{ok:true,json:async()=>({ok:true})};}});
  vm.runInContext(web.slice(web.indexOf('async function api('),web.indexOf('function errorAt(')),ctx);
  try{await ctx.api('/synthetic');}catch{}
  assert.equal($('key').value,'');fail=false;await ctx.api('/synthetic');assert.equal($('connection').textContent,'后台已连接');assert.equal(cleared,2);
  results.push('website key clearing remains and successful recovery restores connected label');
  vm.runInContext(web.slice(web.indexOf('function labelFor('),web.indexOf('function colorFor(')),ctx);
  ctx.verdictNames={yellow:'证据不足'};ctx.statusNames={};
  assert.equal(ctx.labelFor({status:'complete',fingerprint:{color:'yellow'}}),'证据不足');
  assert(ctx.labelFor({status:'complete',quality_status:'insufficient_valid_samples'}).includes('有效样本不足'));
  ctx.verdictNames.green='强指向申报模型';
  assert.equal(ctx.labelFor({status:'cancelled',fingerprint:{color:'green',quality_status:'sufficient'}}),'强指向申报模型');
  results.push('stopped qualified reports retain strong verdict; shortages stay explicit');
 }
 console.log(JSON.stringify({passed:results.length,checks:results},null,2));
})().catch(e=>{console.error(e);process.exitCode=1;});
