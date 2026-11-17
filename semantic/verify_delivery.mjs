import fs from 'node:fs';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {SshBridge} from 'file:///C:/Users/ASUS/.ssh/whale-manager-app/manager/server.mjs';
const here=path.dirname(fileURLToPath(import.meta.url));
const r=await new SshBridge().call({action:'status'});
if(!r.ok || r.data.service.state!=='stopped' || r.data.version!=='1.5.0' || r.data.semantic?.state!=='ready' || !r.data.semantic.enabled)throw new Error('Final paused semantic state not verified: '+JSON.stringify(r.error??r.data.semantic));
console.log(JSON.stringify({service:r.data.service.state,qq:r.data.qq.state,version:r.data.version,semantic:r.data.semantic}));
const program=`import json,hashlib,tarfile,os
from pathlib import Path
root=Path('/opt/qq-deepseek'); stage=root/'semantic-build-20261116'
report=json.loads((stage/'deployment.json').read_text())
files=['data/astrbot/cmd_config.json','.speech-paused']
base='data/astrbot/plugin_data/whale_memory/'
files += [base+n for n in ['memory.sqlite3','memory.sqlite3-wal','memory.sqlite3-shm','pagination.sqlite3','pagination.sqlite3-wal','pagination.sqlite3-shm','reply-queue.json','review-state.json','segment-score-state.json']]
checks={}
with tarfile.open(report['backup'],'r:gz') as archive:
 for name in files:
  try: member=archive.getmember(name)
  except KeyError: checks[name]=not(root/name).exists(); continue
  raw=archive.extractfile(member).read()
  checks[name]=(root/name).is_file() and hashlib.sha256(raw).hexdigest()==hashlib.sha256((root/name).read_bytes()).hexdigest()
 old=json.loads(archive.extractfile('data/astrbot/config/astrbot_plugin_whale_memory_config.json').read().decode('utf-8-sig'))
new=json.loads((root/'data/astrbot/config/astrbot_plugin_whale_memory_config.json').read_text(encoding='utf-8-sig'))
keys={'semantic_enabled','semantic_similarity_threshold','semantic_top_k','semantic_query_timeout_seconds','semantic_sync_interval_seconds'}
checks['original_plugin_settings']= {k:v for k,v in old.items() if k not in keys} == {k:v for k,v in new.items() if k not in keys}
output={'backup':report['backup'],'preservation_checks':len(checks),'all_preserved':all(checks.values()),'failed':[k for k,v in checks.items() if not v]}
print(json.dumps(output)); assert output['all_preserved']
`;
const out=path.join(here,'preservation-final.json'),fd=fs.openSync(out,'w');
const command=`python3 -c "import base64;exec(base64.b64decode('${Buffer.from(program).toString('base64')}'))"`;
try{
 const code=await new Promise((resolve,reject)=>{const child=spawn('C:\\Windows\\System32\\OpenSSH\\ssh.exe',['-i','C:\\Users\\ASUS\\.ssh\\qq_deepseek_20261113','-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','root@8.217.178.5',command],{stdio:['ignore',fd,'inherit'],windowsHide:true});child.once('error',reject);child.once('close',resolve);});if(code!==0)throw new Error('Preservation verification failed.');
}finally{fs.closeSync(fd);}
console.log(fs.readFileSync(out,'utf8'));
