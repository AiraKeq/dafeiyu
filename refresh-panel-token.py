"""Refresh only the saved local NapCat credential, keeping the dashboard password."""
import json
from pathlib import Path
import subprocess

key = Path.home() / '.ssh/qq_deepseek_20261113'
command = '''python3 -c 'import json; print(json.dumps({"token":json.load(open("/opt/qq-deepseek/data/napcat/config/webui.json",encoding="utf-8-sig"))["token"]}))' '''
result = subprocess.run(
    ['ssh', '-i', str(key), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', 'root@8.217.178.5', command],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
)
token = json.loads(result.stdout)['token']
path = Path.home() / '.ssh/qq-deepseek-panels.txt'
lines = path.read_text(encoding='utf-8-sig').splitlines()
lines = [('Token：' + token) if line.startswith('Token：') else line for line in lines]
path.write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')
print('Saved NapCat management credential updated; its value was not printed.')
