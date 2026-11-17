// Minimal CDP client, adapted from F:/work/_tools/cdp-shot.mjs. Uses the isolated browser only.
import fs from 'node:fs';
export async function browserPage(url='about:blank') {
 const version=await (await fetch('http://127.0.0.1:9333/json/version')).json();
 const browser=await connect(version.webSocketDebuggerUrl);
 const {browserContextId}=await browser.send('Target.createBrowserContext');
 const {targetId}=await browser.send('Target.createTarget',{url:'about:blank',browserContextId});
 const targets=await (await fetch('http://127.0.0.1:9333/json/list')).json();
 const target=targets.find(t=>t.id===targetId);
 const page=await connect(target.webSocketDebuggerUrl);
 const errors=[];
 page.onEvent=m=>{if(m.method==='Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.text);if(m.method==='Log.entryAdded' && m.params.entry.level==='error')errors.push(m.params.entry.text);};
 await page.send('Page.enable');await page.send('Runtime.enable');await page.send('Log.enable');
 const evaluate=async expression=>{const r=await page.send('Runtime.evaluate',{expression:'{\n'+expression+'\n}',awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw new Error(r.exceptionDetails.exception?.description??r.exceptionDetails.text);return r.result?.value;};
 const waitFor=expression=>evaluate(`new Promise((resolve,reject)=>{const start=Date.now();const tick=()=>{try{if(${expression})return resolve(true);if(Date.now()-start>45000)return reject(new Error('UI condition timed out'));setTimeout(tick,50);}catch(e){reject(e)}};tick();})`);
 const navigate=async to=>{await page.send('Page.navigate',{url:to});await waitFor("document.readyState==='complete'");};
 const size=async(width,height)=>{await page.send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<600});};
 const shot=async dest=>{const r=await page.send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(dest,Buffer.from(r.data,'base64'));};
 const close=async()=>{page.close();await browser.send('Target.closeTarget',{targetId});await browser.send('Target.disposeBrowserContext',{browserContextId});browser.close();};
 if(url!=='about:blank')await navigate(url);
 return {evaluate,waitFor,navigate,size,shot,close,errors};
}
async function connect(url){
 const socket=new WebSocket(url),pending=new Map();let seq=0;
 const api={send:(method,params={})=>new Promise((resolve,reject)=>{const id=++seq;const timer=setTimeout(()=>{pending.delete(id);reject(new Error(`CDP timeout: ${method}`));},55000);pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params}));}),close:()=>socket.close(),onEvent:()=>{}};
 socket.addEventListener('message',event=>{const m=JSON.parse(event.data);if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);clearTimeout(p.timer);if(m.error)p.reject(new Error(m.error.message));else p.resolve(m.result);}else api.onEvent(m);});
 await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',()=>reject(new Error('CDP unavailable')),{once:true});});return api;
}
