import fs from 'node:fs';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {PRIVATE_RUNTIME, ensurePrivateRuntime} from './runtime-security.mjs';
const HERE=path.dirname(fileURLToPath(import.meta.url));
const RUNTIME=PRIVATE_RUNTIME;
const PORT=17865;

async function existing() {
  let state;
  try {state=JSON.parse(fs.readFileSync(path.join(RUNTIME,'access.json'),'utf8'));} catch {return null;}
  if (state.port!==PORT || typeof state.launchKey!=='string' || typeof state.instance!=='string') return null;
  try {
    const r=await fetch(`http://127.0.0.1:${PORT}/health`,{signal:AbortSignal.timeout(1500)});
    const health=await r.json();
    if (health.app!=='whale-manager' || health.instance!==state.instance) return null;
    return state;
  } catch {return null;}
}
export async function getLaunchUrl({startIfNeeded=true}={}) {
  await ensurePrivateRuntime(RUNTIME);
  let state=await existing();
  if (!state && startIfNeeded) {
    fs.mkdirSync(RUNTIME,{recursive:true,mode:0o700});
    const fd=fs.openSync(path.join(RUNTIME,'server.log'),'a',0o600);
    try {
      const child=spawn(process.execPath,[path.join(HERE,'server.mjs')],{cwd:HERE,detached:true,windowsHide:true,stdio:['ignore',fd,fd]});
      await new Promise((resolve,reject)=>{child.once('spawn',resolve);child.once('error',reject);});
      child.unref();
    } finally {fs.closeSync(fd);}
    for(let attempt=0;attempt<25;attempt++) {
      state=await existing(); if(state) break;
      await new Promise(resolve=>setTimeout(resolve,200));
    }
  }
  if(!state) throw new Error('管理器尚未就绪或17865端口被占用。请查看 manager/.runtime/server.log。');
  const r=await fetch(`http://127.0.0.1:${PORT}/api/launch`,{method:'POST',headers:{Authorization:`Bearer ${state.launchKey}`},signal:AbortSignal.timeout(3000)});
  const answer=await r.json();
  if(!r.ok||!answer.ok||typeof answer.data?.token!=='string') throw new Error('本机启动授权失败，请重新启动管理器。');
  return `http://127.0.0.1:${PORT}/#token=${encodeURIComponent(answer.data.token)}`;
}
if(process.argv[1] && path.resolve(process.argv[1])===fileURLToPath(import.meta.url)) {
  try {
    const url=await getLaunchUrl();
    if(process.argv.includes('--check')) console.log('管理器启动入口可用；未启用QQ聊天。');
    else {
      const child=spawn('powershell.exe',['-NoProfile','-NonInteractive','-Command','Start-Process -FilePath $env:WHALE_MANAGER_URL'],{stdio:'ignore',windowsHide:true,env:{...process.env,PSModulePath:'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\Modules',WHALE_MANAGER_URL:url}});
      await new Promise((resolve,reject)=>{child.once('error',reject);child.once('close',code=>code===0?resolve():reject(new Error('浏览器未能打开。')));});
      console.log('已打开本机大肥鱼管理器。打开页面不会启用QQ聊天。');
    }
  } catch(error) {console.error(error.message);process.exitCode=1;}
}
