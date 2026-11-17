// Read-only cloud contract verification. Never saves, starts/stops, backs up, or prints message text.
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {SshBridge} from './server.mjs';
const here=path.dirname(fileURLToPath(import.meta.url));
const bridge=new SshBridge();
const summaries=[];
async function get(action,fields={}){const r=await bridge.call({action,...fields});assert.equal(r.ok,true,JSON.stringify(r.error));return r.data;}
const status=await get('status');assert.equal(status.service.state,'stopped');assert.equal(status.version,'1.4.1');assert.ok(status.scopes.length>0);
summaries.push({action:'status',service:status.service.state,qq:status.qq.state,version:status.version,scopes:status.scopes.length,queue:status.queue,model:status.model,warnings:status.warnings});
const personality=await get('personality');assert.equal(personality.characters,1797);assert.equal(personality.combined_characters,1949);
summaries.push({action:'personality',characters:personality.characters,combined_characters:personality.combined_characters});
for(const scope of status.scopes.map(s=>s.scope)){
 const style=await get('style_get',{scope});summaries.push({action:'style_get',scope,characters:Array.from(style.notes).length,day:style.day});
 for(const kind of ['events','messages']){
  const result=await get('memory_list',{scope,kind,limit:2});summaries.push({action:'memory_list',scope,kind,items:result.items.length,has_more:Boolean(result.next_cursor)});
  if(result.next_cursor){const next=await get('memory_list',{scope,kind,limit:2,cursor:result.next_cursor});assert.ok(next.items.every(row=>!result.items.some(old=>old.id===row.id)));}
 }
}
const backups=await get('backups');summaries.push({action:'backups',count:backups.items.length});
fs.writeFileSync(path.join(here,'.runtime','live-verification.json'),JSON.stringify({verified_at:new Date().toISOString(),summaries},null,2),{mode:0o600});
console.log(JSON.stringify(summaries,null,2));
