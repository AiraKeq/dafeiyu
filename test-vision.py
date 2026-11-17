"""Send one non-private test image to the configured official model; never QQ-send."""
import base64
import json
from pathlib import Path
import urllib.parse
import urllib.request

ROOT=Path('/opt/qq-deepseek')
c=json.loads((ROOT/'data/astrbot/cmd_config.json').read_text(encoding='utf-8-sig'))
m=next(x for x in c['provider'] if x['id']=='deepseek/deepseek-flash')
s=next(x for x in c['provider_sources'] if x['id']==m['provider_source_id'])
base=s['api_base'].rstrip('/')
if urllib.parse.urlsplit(base).hostname!='api.deepseek.com' or not base.startswith('https://'):
    raise SystemExit('The test requires the configured official DeepSeek endpoint.')
image=(ROOT/'fixtures/vision-test.png').read_bytes()
if not image.startswith(b'\x89PNG\r\n\x1a\n'):
    raise SystemExit('Fixture was not a PNG image.')
body={'model':m['model'],'messages':[
    {'role':'system','content':'读取图片实际内容，只输出JSON，格式为{"number":"四位数字","objects":[{"color":"中文颜色","shape":"中文形状"}]}。不要猜测不可见内容。'},
    {'role':'user','content':[{'type':'text','text':'请读出图中的四位数字，并从左到右列出三个彩色图形的颜色和形状。'},
                            {'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(image).decode('ascii')}}]}],
    'max_tokens':256,'thinking':{'type':'disabled'},'response_format':{'type':'json_object'}}
request=urllib.request.Request(base+'/chat/completions',data=json.dumps(body,ensure_ascii=False).encode('utf-8'),
                               headers={'Content-Type':'application/json','Authorization':'Bearer '+s['key'][0]})
with urllib.request.urlopen(request,timeout=90) as response:
    result=json.load(response)
text=result['choices'][0]['message']['content']
parsed=json.loads(text)
number=str(parsed.get('number',''))
objects=parsed.get('objects') or []
colors=[str(x.get('color','')) for x in objects]
shapes=[str(x.get('shape','')) for x in objects]
passed=(number=='7384' and len(objects)==3 and '红' in colors[0] and '三角' in shapes[0]
        and '蓝' in colors[1] and any(k in shapes[1] for k in ('正方','方形','方块'))
        and '黄' in colors[2] and '圆' in shapes[2])
print('Actual image response:',json.dumps(parsed,ensure_ascii=False))
print('Image bytes sent:',len(image))
print('Vision recognition passed:',passed)
print('Usage:',result.get('usage'))
print('One synthetic image request; no QQ messages or private chat data were sent.')
raise SystemExit(0 if passed else 1)
