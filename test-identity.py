"""One synthetic DeepSeek identity test; never imports the bot or sends QQ messages.

Compatible with the deployment host's Python 3.6. Only source constants and model
configuration are read. No production conversation or memory data is accessed.
"""
import ast
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path('/opt/qq-deepseek')
PLUGIN_SOURCE = ROOT / 'data/astrbot/plugins/astrbot_plugin_whale_memory/main.py'


def read_identity_constants():
    wanted = {'FIXED_IDENTITY', 'BASE_STYLE'}
    values = {}
    tree = ast.parse(PLUGIN_SOURCE.read_text(encoding='utf-8-sig'))
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                value = ast.literal_eval(statement.value)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError('Identity constant must be a nonempty literal string')
                values[target.id] = value
    if set(values) != wanted:
        raise ValueError('Required identity constants were not found')
    return values


def main():
    try:
        identity = read_identity_constants()
        config = json.loads((ROOT / 'data/astrbot/cmd_config.json').read_text(encoding='utf-8-sig'))
        model = next(item for item in config['provider'] if item['id'] == 'deepseek/deepseek-flash')
        source = next(item for item in config['provider_sources']
                      if item['id'] == model['provider_source_id'])
        api_key = source['key'][0]
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError('No configured model key')
        base = source['api_base'].rstrip('/')
        endpoint = urllib.parse.urlsplit(base)
        if endpoint.scheme != 'https' or endpoint.hostname != 'api.deepseek.com':
            raise ValueError('The synthetic test requires the configured official DeepSeek endpoint')
        system_prompt = (
            identity['FIXED_IDENTITY'] + '\n' + identity['BASE_STYLE']
            + '\n平台提供的当前发言者QQ号：1000000002。\n请用一两句简短回答当前问题。'
        )
        request = urllib.request.Request(
            base + '/chat/completions',
            data=json.dumps({
                'model': model['model'],
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': '你是谁？你的主人QQ号是多少？'},
                ],
                'max_tokens': 160,
                'thinking': {'type': 'disabled'},
            }, ensure_ascii=False).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + api_key,
            },
        )
        # Exactly one model request. There are no retries or QQ transport calls.
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.load(response)
        reply = payload['choices'][0]['message']['content']
        if not isinstance(reply, str):
            raise ValueError('Model reply was not text')
        passed = '大肥鱼' in reply and '1000000002' in reply
        print('Synthetic reply:', reply.replace(api_key, '[REDACTED]')[:2000])
        print('Identity test passed:', passed)
        return 0 if passed else 1
    except urllib.error.HTTPError as error:
        print('HTTP error:', error.code)
        return 1
    except Exception as error:
        print('Identity test error:', type(error).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
