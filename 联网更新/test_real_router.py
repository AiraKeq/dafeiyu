"""Synthetic text/vision routing using the already configured official provider.
No QQ messages, real chat/image text, API keys, or private config are printed.
"""
import base64
import importlib.util
import json
from pathlib import Path
import sys
import urllib.parse
import urllib.request

ROOT=Path('/opt/qq-deepseek')
STAGE=ROOT/'lookup-build-20261116'
module_path=STAGE/'plugin/lookup_policy.py'
module=type(sys)('whale_lookup_policy_test')
exec(compile(module_path.read_bytes(),str(module_path),'exec'),module.__dict__)
config=json.loads((ROOT/'data/astrbot/cmd_config.json').read_text(encoding='utf-8-sig'))
model=next(x for x in config['provider'] if x['id']=='deepseek/deepseek-flash')
source=next(x for x in config['provider_sources'] if x['id']==model['provider_source_id'])
url=source['api_base'].rstrip('/')+'/chat/completions'
if urllib.parse.urlsplit(url).hostname!='api.deepseek.com' or not url.startswith('https://'):
    raise RuntimeError('Existing official provider identity not verified.')
scenarios=[('public_text','尊嘟假嘟是什么意思？',None),('private_chat','今天被误会了，我很难受。',None)]
image=STAGE/'synthetic-meme.png'
if image.is_file():scenarios.append(('synthetic_image','这张表情包什么意思？',image))
results=[]
for label,text,picture in scenarios:
    content=[{'type':'text','text':json.dumps({'text':text,'image_count':int(picture is not None)},ensure_ascii=False)}]
    if picture is not None:
        content.append({'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(picture.read_bytes()).decode()}})
    body={'model':model['model'],'thinking':{'type':'disabled'},'max_tokens':384,
          'response_format':{'type':'json_object'},'messages':[{'role':'system','content':module.PLAN_SYSTEM},{'role':'user','content':content}]}
    request=urllib.request.Request(url,data=json.dumps(body,ensure_ascii=False).encode(),
          headers={'Content-Type':'application/json','Authorization':'Bearer '+source['key'][0]})
    with urllib.request.urlopen(request,timeout=30) as response:raw=json.load(response)
    plan=module.parse_plan(raw['choices'][0]['message']['content'],text=text,has_images=picture is not None)
    results.append({'label':label,'plan':plan,'tokens':raw.get('usage',{}).get('total_tokens')})
    if label=='public_text' and plan['state']!='search':raise AssertionError('Public meaning query not routed to search.')
    if label=='private_chat' and plan['state']!='skip':raise AssertionError('Private emotional chat must not be sent to public search.')
    if label=='synthetic_image' and plan['state']!='search':raise AssertionError('Synthetic public image was not routed to search.')
(STAGE/'router-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'synthetic_only':True,'existing_provider_only':True,'results':results},ensure_ascii=False))
