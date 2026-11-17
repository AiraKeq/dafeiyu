import {SshBridge} from 'file:///C:/Users/ASUS/.ssh/whale-manager-app/manager/server.mjs';
const bridge=new SshBridge();
const before=await bridge.call({action:'status'});
if(!before.ok || before.data.service.state==='unknown')throw new Error('Cannot safely verify existing AstrBot state.');
let result=before.data.service;
if(result.state==='running'){
 const stopped=await bridge.call({action:'service_stop',confirm:'停止大肥鱼',revision:result.id+':'+result.started_at});
 if(!stopped.ok)throw new Error(JSON.stringify(stopped.error));
 result=stopped.data.service;
}
if(result.state!=='stopped')throw new Error('AstrBot pause not verified.');
console.log(JSON.stringify({service:result.state,cid:result.id,napcat_touched:false,dsh_touched:false,reason:'User confirmed pause for semantic upgrade'}));
