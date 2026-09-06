const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'), source=fs.readFileSync(path.join(root,'gpt56_vnext/web/app.js'),'utf8');
const nodes=new Map(), node=()=>({textContent:'',hidden:true,append(){},replaceChildren(){},setAttribute(){}});
const context={$:id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);},document:{documentElement:{lang:'en'},createElement:node}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(root,'gpt56_vnext/web/i18n.js'),'utf8'),context);
vm.runInContext(source.slice(source.indexOf('function renderReportNote('),source.indexOf('function renderHistory(')),context);
const report={claimed_model:'a',request_model:'alias-a',endpoint:'https://tested.invalid/v1',
 benchmark:{id:'b',version:'1.0.0',publisher:'maintainer',collection:{sources:[{url:'https://reference.invalid/v1'},{url:'https://reference.invalid/v1'}]}},
 fingerprint:{color:'green',reasons:[],matches:{a:.9},thresholds:{a:.8}}};
context.showReport(report);
let text=nodes.get('report-summary').textContent;
assert.equal(text.split('https://reference.invalid/v1').length-1,1);
assert(text.includes("This run's URL (API base): https://tested.invalid/v1"));
assert(text.includes('Benchmark collection URL (API base): https://reference.invalid/v1'));
vm.runInContext("locale='zh-CN'",context); context.showReport(report);
text=nodes.get('report-summary').textContent;
assert(text.includes('本次检测网址（API 根地址）: https://tested.invalid/v1'));
assert(text.includes('基准采集网址（API 根地址）: https://reference.invalid/v1'));
delete report.endpoint; context.showReport(report);
assert(nodes.get('report-summary').textContent.includes('本次检测网址（API 根地址）: 未提供'));
console.log('3 report URL cases passed (English, Chinese, historical missing endpoint)');
