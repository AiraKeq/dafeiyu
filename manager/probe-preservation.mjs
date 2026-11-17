// Read-only production fingerprint. No message text, credentials, or Docker Env leaves the cloud.
import fs from 'node:fs';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
const name=process.argv[2];
if(!['before','after'].includes(name)) throw new Error('Use before or after');
const runtime=path.join(here,'.runtime');fs.mkdirSync(runtime,{recursive:true,mode:0o700});
const program=`import os,json,hashlib,subprocess
root='/opt/qq-deepseek'
files=['.speech-paused','data/astrbot/cmd_config.json','data/astrbot/config/astrbot_plugin_whale_memory_config.json']
base='data/astrbot/plugin_data/whale_memory/'
files += [base+n for n in ['memory.sqlite3','memory.sqlite3-wal','memory.sqlite3-shm','pagination.sqlite3','pagination.sqlite3-wal','pagination.sqlite3-shm','reply-queue.json','review-state.json','segment-score-state.json']]
result={'files':{},'containers':{}}
for name in files:
 p=os.path.join(root,name)
 if not os.path.isfile(p):
  result['files'][name]=None
  continue
 s=os.stat(p)
 with open(p,'rb') as f: h=hashlib.sha256(f.read()).hexdigest()
 result['files'][name]={'sha256':h,'size':s.st_size,'mtime_ns':s.st_mtime_ns,'mode':s.st_mode & 4095}
for name in ['qq-deepseek-astrbot-1','qq-deepseek-napcat-1']:
 raw=subprocess.check_output(['docker','inspect','--format','{{.Id}}|{{.State.Status}}|{{.State.StartedAt}}|{{.State.FinishedAt}}|{{.RestartCount}}',name]).decode().strip()
 result['containers'][name]=raw
print(json.dumps(result,sort_keys=True))
`;
const command=`python3 -c "import base64;exec(base64.b64decode('${Buffer.from(program).toString('base64')}'))"`;
const dest=path.join(runtime,`preservation-${name}.json`), err=path.join(runtime,`preservation-${name}.err`);
const fds=[fs.openSync(dest,'w',0o600),fs.openSync(err,'w',0o600)];
try {
 const code=await new Promise((resolve,reject)=>{const child=spawn('C:\\Windows\\System32\\OpenSSH\\ssh.exe',['-i','C:\\Users\\ASUS\\.ssh\\qq_deepseek_20261113','-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10','root@8.217.178.5',command],{stdio:['ignore',...fds],windowsHide:true});child.once('error',reject);child.once('close',resolve);});
 if(code!==0) throw new Error(`Fingerprint SSH failed (${code}); diagnostics stored privately.`);
} finally {fds.forEach(fd=>fs.closeSync(fd));}
const result=JSON.parse(fs.readFileSync(dest,'utf8'));
if(name==='after') {
 const before=JSON.parse(fs.readFileSync(path.join(runtime,'preservation-before.json'),'utf8'));
 const same=JSON.stringify(before)===JSON.stringify(result);
 console.log(JSON.stringify({unchanged:same,files_checked:Object.keys(result.files).length,containers_checked:Object.keys(result.containers).length}));
 if(!same){for(const group of ['files','containers']) for(const key of Object.keys(result[group])) if(JSON.stringify(result[group][key])!==JSON.stringify(before[group][key])) console.log('Changed:',group,key);process.exitCode=1;}
} else console.log('Baseline recorded: production data and container fingerprints; no body or credential output.');
