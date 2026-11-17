"""Back up and install 1.4.0: bounded pages, optional polish, real native vision."""
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile

ROOT=Path('/opt/qq-deepseek')
archive=ROOT/'artifacts/whale-memory-1.4.1.zip'
expected=archive.with_suffix('.zip.sha256').read_text(encoding='ascii').split()[0]
if hashlib.sha256(archive.read_bytes()).hexdigest()!=expected:
    raise SystemExit('Package checksum mismatch')
plugin_root=(ROOT/'data/astrbot/plugins/astrbot_plugin_whale_memory').resolve()
with zipfile.ZipFile(str(archive)) as bundle:
    for member in bundle.namelist():
        destination=(plugin_root.parent/member).resolve()
        if destination!=plugin_root and plugin_root not in destination.parents:
            raise SystemExit('Archive path outside target plugin')
core_path=ROOT/'data/astrbot/cmd_config.json'
plugin_path=ROOT/'data/astrbot/config/astrbot_plugin_whale_memory_config.json'
core=json.loads(core_path.read_text(encoding='utf-8-sig'))
plugin=json.loads(plugin_path.read_text(encoding='utf-8-sig'))
if core.get('admins_id')!=['1000000002'] or plugin.get('group_ids')!=['1000000003']:
    raise SystemExit('Unexpected formal group/admin')
if core['provider_settings']['streaming_response']:
    raise SystemExit('Expected existing nonstreaming mode')
style_before=plugin.get('base_style')
core_after=copy.deepcopy(core)
plugin_after=copy.deepcopy(plugin)
model=next(x for x in core_after['provider'] if x['id']=='deepseek/deepseek-flash')
if model['model']!='deepseek-flash':
    raise SystemExit('Configured vision model changed')
model['modalities']=['text','image']
model.setdefault('custom_extra_body',{})['safety_check']={'level':'high'}
core_after['platform_settings']['segmented_reply']['regex']=r'^[\s\S]{1,60}$|[\s\S]{1,59}?[。！？!?\n]|[\s\S]{1,60}'
plugin_after.update({'delivery_guard_enabled':True,'reply_polish_enabled':True,'vision_enabled':True})
assert plugin_after.get('base_style')==style_before
os.umask(0o077)
stamp=datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
backup=ROOT/'backups'/('before-enhancements-'+stamp+'.tar.gz')
subprocess.run(['docker','compose','stop','astrbot'],cwd=str(ROOT),check=True)
try:
    subprocess.run(['tar','-czf',str(backup),'-C',str(ROOT/'data'),'astrbot'],check=True)
    os.chmod(str(backup),0o600)
    with zipfile.ZipFile(str(archive)) as bundle:
        bundle.extractall(str(plugin_root.parent))
    for path,value in ((core_path,core_after),(plugin_path,plugin_after)):
        pending=path.with_suffix('.pending')
        pending.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        os.chmod(str(pending),0o600)
        pending.replace(path)
    print('Installed 1.4.1 with protected private/group pages and history normalization')
    print('Backup:',backup.name)
    print('Only native segment regex/model image capability/safety and three enhancement switches changed.')
    print('Existing key, style, identity, whitelist, time, cooldown and memory settings preserved.')
except BaseException:
    # Restore the exact backed-up data before starting a service after a partial
    # package/config write; extraction target is fixed to this deployment.
    data_target=(ROOT/'data').resolve()
    if data_target!=Path('/opt/qq-deepseek/data'):
        raise RuntimeError('Unexpected restoration target')
    subprocess.run(['tar','-xzf',str(backup),'-C',str(data_target)],check=True)
    print('Installation failed; original deployment data restored.')
    raise
finally:
    subprocess.run(['docker','compose','up','-d','astrbot'],cwd=str(ROOT),check=True)
