"""Synthetic official-model persona checks; no QQ send or private chat access."""
import json
from pathlib import Path
import urllib.request
import urllib.parse

root=Path('/opt/qq-deepseek')
c=json.loads((root/'data/astrbot/cmd_config.json').read_text(encoding='utf-8-sig'))
p=json.loads((root/'data/astrbot/config/astrbot_plugin_whale_memory_config.json').read_text(encoding='utf-8-sig'))
m=next(x for x in c['provider'] if x['id']=='deepseek/deepseek-flash')
s=next(x for x in c['provider_sources'] if x['id']==m['provider_source_id'])
assert s['api_base'].startswith('https://') and urllib.parse.urlsplit(s['api_base']).hostname=='api.deepseek.com'
style='固定身份：名字大肥鱼，主人QQ1000000002，所有人平等为好朋友。\n'+p['base_style']
scenes=[('出场互怼','熟悉的朋友正在互怼，朋友笑着说：哟，谁来了？让我看看你有多嚣张。'),
        ('吹牛轻怼','朋友十连跪，还笑着说：我就是天下第一，你不服？'),
        ('认真委屈','今天被误会了，我真的很委屈，不是在开玩笑。'),
        ('固定身份','认真问：你是谁，你的主人QQ是多少？')]
for label,prompt in scenes:
    body={'model':m['model'],'thinking':{'type':'disabled'},'max_tokens':160,
          'messages':[{'role':'system','content':style+'\n仅回复一句到两句，不超过60字，不要舞台动作。'},
                      {'role':'user','content':prompt}]}
    req=urllib.request.Request(s['api_base'].rstrip('/')+'/chat/completions',
        data=json.dumps(body,ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type':'application/json','Authorization':'Bearer '+s['key'][0]})
    with urllib.request.urlopen(req,timeout=60) as response: result=json.load(response)
    print(label+': '+result['choices'][0]['message']['content'])
    print('Tokens:',result.get('usage',{}).get('total_tokens'))
print('Synthetic persona checks only; no QQ messages or real conversation data.')
