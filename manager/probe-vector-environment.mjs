// Sanitized environment probe only: no keys, tokens, configuration bodies, or conversation text.
import fs from 'node:fs';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
const program=`import os,json,subprocess,hashlib
root='/opt/qq-deepseek'
result={}
for name in ['qq-deepseek-astrbot-1','qq-deepseek-napcat-1']:
 result[name]=subprocess.check_output(['docker','inspect','--format','{{.Id}}|{{.State.Status}}|{{.State.StartedAt}}|{{.State.Running}}',name]).decode().strip()
with open(root+'/data/astrbot/cmd_config.json','r',encoding='utf-8-sig') as f:c=json.load(f)
result['provider_types']=[{'id':str(p.get('id','')),'type':str(p.get('type','')),'model':str(p.get('model','')),'enable':p.get('enable')} for p in c.get('provider',[]) if isinstance(p,dict)]
result['provider_sources']=[{'id':str(p.get('id','')),'type':str(p.get('type','')),'key_configured':bool(p.get('key'))} for p in c.get('provider_sources',[]) if isinstance(p,dict)]
with open('/proc/meminfo') as f:
 result['memory']={line.split(':')[0]:line.split(':')[1].strip() for line in f if line.startswith(('MemTotal:','MemAvailable:','SwapTotal:','SwapFree:'))}
result['docker_stats']=subprocess.check_output(['docker','stats','--no-stream','--format','{{.Name}}|{{.MemUsage}}|{{.MemPerc}}|{{.CPUPerc}}']).decode().strip().splitlines()
print(json.dumps(result,ensure_ascii=False))
`;
const command=`python3 -c "import base64;exec(base64.b64decode('${Buffer.from(program).toString('base64')}'))"`;
const fd=fs.openSync(path.join(here,'.runtime','vector-environment.json'),'w');
try{
 const code=await new Promise((resolve,reject)=>{const child=spawn('C:\\Windows\\System32\\OpenSSH\\ssh.exe',['-i','C:\\Users\\ASUS\\.ssh\\qq_deepseek_20261113','-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10','root@8.217.178.5',command],{stdio:['ignore',fd,'inherit'],windowsHide:true});child.once('error',reject);child.once('close',resolve);});
 if(code!==0)throw new Error('Sanitized environment probe failed: '+code);
}finally{fs.closeSync(fd);}
console.log(fs.readFileSync(path.join(here,'.runtime','vector-environment.json'),'utf8'));
