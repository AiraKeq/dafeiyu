"""Real native provider path regression on synthetic chat; no QQ/real chat data."""
import asyncio
import copy
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
sys.path.insert(0,'/plugin')
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from routing_adapter import classify
from lookup_policy import PLAN_SYSTEM,parse_plan,ordinary_chat,fallback_required

async def run():
    root=Path('/opt/qq-deepseek')
    config=json.loads((root/'data/astrbot/cmd_config.json').read_text(encoding='utf-8-sig'))
    model=next(x for x in config['provider'] if x['id']=='deepseek/deepseek-flash')
    source=next(x for x in config['provider_sources'] if x['id']==model['provider_source_id'])
    effective=dict(source);effective.update(model)
    provider=ProviderOpenAIOfficial(effective,copy.deepcopy(config.get('provider_settings',{})))
    context=SimpleNamespace(get_provider_by_id=lambda _:provider)
    try:
        dropped,_=await provider._prepare_chat_payload(prompt='synthetic',contexts=[],system_prompt='JSON only',
            max_tokens=384,response_format={'type':'json_object'},thinking={'type':'disabled'})
        reports=[{'native_kwargs_dropped':all(k not in dropped for k in ('max_tokens','response_format','thinking'))}]
        for text in ('？','你干嘛凶我','这不是梗','尊嘟假嘟是什么意思？'):
            start=time.monotonic()
            if ordinary_chat(text):
                reports.append({'text':text,'state':'skip','forced_fallback':False,'router_called':False})
                continue
            raw=await asyncio.wait_for(classify(context,model['id'],text,[],PLAN_SYSTEM),timeout=8)
            plan=parse_plan(raw,text=text)
            reports.append({'text':text,'state':plan['state'],'forced_fallback_if_search_failed':fallback_required(text,False,plan),
                            'router_called':True,'elapsed':time.monotonic()-start,'content_length':len(raw)})
            if plan['state']!='search':raise AssertionError('Explicit public term did not route to search.')
        if not reports[0]['native_kwargs_dropped']:raise AssertionError('Old native kwargs-drop regression not reproduced.')
        print(json.dumps({'existing_native_provider':True,'synthetic_only':True,'reports':reports},ensure_ascii=False))
    finally:await provider.client.close()
asyncio.run(run())
