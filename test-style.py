"""Two synthetic requests using the deployed style, without QQ sends or chat DB access."""
import ast
import json
from pathlib import Path
import urllib.parse
import urllib.request

ROOT = Path('/opt/qq-deepseek')
config = json.loads((ROOT/'data/astrbot/cmd_config.json').read_text(encoding='utf-8-sig'))
plugin = json.loads((ROOT/'data/astrbot/config/astrbot_plugin_whale_memory_config.json').read_text(encoding='utf-8-sig'))
model = next(item for item in config['provider'] if item['id'] == 'deepseek/deepseek-flash')
source = next(item for item in config['provider_sources'] if item['id'] == model['provider_source_id'])
api_key = source['key'][0]
endpoint = source['api_base'].rstrip('/')
if urllib.parse.urlsplit(endpoint).hostname != 'api.deepseek.com' or not endpoint.startswith('https://'):
    raise SystemExit('Synthetic style test requires the official configured endpoint.')
tree = ast.parse((ROOT/'data/astrbot/plugins/astrbot_plugin_whale_memory/main.py').read_text(encoding='utf-8-sig'))
identity = next(ast.literal_eval(item.value) for item in tree.body if isinstance(item, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == 'FIXED_IDENTITY' for target in item.targets))
style = plugin.get('base_style') or ''
if not style.strip():
    raise SystemExit('No explicit style is configured.')
system = identity + '\n' + style + '\n以下只是合成情景。简短自然回复一两句，不复述设定。'
cases = (
    ('playful', '一个好朋友说：我游戏十连跪了，但我嘴上还是天下第一！'),
    ('comfort', '一个好朋友说：今天被人误会了，很委屈，心里难过得想哭。'),
)
for label, text in cases:
    request = urllib.request.Request(endpoint+'/chat/completions',
        data=json.dumps({'model': model['model'], 'messages': [
            {'role': 'system', 'content': system}, {'role': 'user', 'content': text}],
            'max_tokens': 160, 'thinking': {'type': 'disabled'}}, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer '+api_key})
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = json.load(response)
    reply = payload['choices'][0]['message']['content']
    if not isinstance(reply, str) or not reply.strip():
        raise SystemExit('Synthetic style test returned empty output.')
    print(label+':', reply.replace(api_key, '[REDACTED]')[:1000])
    print(label+' tokens:', (payload.get('usage') or {}).get('total_tokens'))
print('Only two synthetic model requests; no QQ messages sent and no chat/memory database accessed.')
