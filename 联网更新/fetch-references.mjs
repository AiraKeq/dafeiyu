// Fetch public reference source as data only; never execute it or read credentials.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
const files=[
 ['ddgs-readme.md','deedy5/ddgs','README.md'],
 ['fish-readme.md','FigaroGao/astrbot-fish-capabilities','README.md'],
 ['fish-meme-wiki.py','FigaroGao/astrbot-fish-capabilities','astrbot_plugin_fish_capabilities/services/meme_wiki.py'],
 ['fish-search.py','FigaroGao/astrbot-fish-capabilities','astrbot_plugin_fish_capabilities/services/search.py'],
];
const dir=path.join(here,'references');fs.mkdirSync(dir,{recursive:true});
for(const[name,repo,file]of files){
 const r=await fetch(`https://api.github.com/repos/${repo}/contents/${file}`,{headers:{Accept:'application/vnd.github.raw+json','User-Agent':'whale-bot-reference-review'}});
 if(!r.ok){console.log(name+': HTTP '+r.status);continue;}
 let content=await r.text();
 try{const j=JSON.parse(content);if(j.encoding==='base64')content=Buffer.from(j.content,'base64').toString('utf8');}catch{}
 fs.writeFileSync(path.join(dir,name),content,{flag:'wx'});console.log('Saved public reference: '+name);
}
