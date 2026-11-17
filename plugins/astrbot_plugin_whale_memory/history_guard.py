"""Normalize a provider-facing history copy; never edit persisted conversations."""
from __future__ import annotations
import copy


def normalize_history(items):
    result=[]
    for raw in items or []:
        if not isinstance(raw,dict) or raw.get('role') not in ('user','assistant'):
            continue
        if raw.get('tool_calls') or raw.get('role')=='tool':
            continue
        item=copy.deepcopy(raw)
        content=item.get('content')
        if not isinstance(content,(str,list)):
            continue
        if isinstance(content,list) and item['role']=='assistant':
            content=[part for part in content if isinstance(part,dict) and part.get('type')=='text']
            item['content']=content
        if not content or (isinstance(content,str) and not content.strip()):
            continue
        if not result and item['role']=='assistant':
            continue
        if result and result[-1]['role']==item['role']:
            previous=result[-1].get('content')
            if isinstance(previous,str) and isinstance(content,str):
                result[-1]['content']=previous+'\n'+content
            else:
                left=[{'type':'text','text':previous}] if isinstance(previous,str) else list(previous)
                right=[{'type':'text','text':content}] if isinstance(content,str) else list(content)
                result[-1]['content']=left+[{'type':'text','text':'\n'}]+right
        else:
            result.append(item)
    return result
