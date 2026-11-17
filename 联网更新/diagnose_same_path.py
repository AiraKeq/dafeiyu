"""Same production search broker, public synthetic queries, no QQ or fact writes."""
import asyncio
import json
import sys
import time
sys.path.insert(0,'/plugin')
from lookup_client import LookupClient

async def run():
    client=LookupClient('/data',enabled=True,timeout=8)
    reports=[]
    for queries in [['鸡你太美 网络用语 含义'],['鸡你太美','不存在的犄角旮旯同构咔咔语']]:
        start=time.monotonic()
        result=await client.search(queries,'synthetic-diagnostic')
        reports.append({'query_count':len(queries),'state':result['state'],'results':len(result['results']),
                        'error_type':client.error_type,'elapsed':time.monotonic()-start})
    await client.close()
    print(json.dumps(reports,ensure_ascii=False))
asyncio.run(run())
