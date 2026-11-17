"""Install only the verified 1.3.0 extension and its segment-scoring fields.

Preserve all other plugin/core/provider/QQ configuration and back up data first.
Run on the deployment host; credentials are never printed.
"""
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile

ROOT = Path('/opt/qq-deepseek')
archive_path = ROOT / 'artifacts/whale-memory-1.3.0.zip'
expected = archive_path.with_suffix('.zip.sha256').read_text(encoding='ascii').split()[0]
if hashlib.sha256(archive_path.read_bytes()).hexdigest() != expected:
    raise SystemExit('Package checksum mismatch; no service was changed.')
allowed = (ROOT/'data/astrbot/plugins/astrbot_plugin_whale_memory').resolve()
with zipfile.ZipFile(str(archive_path)) as archive:
    for member in archive.namelist():
        target = (allowed.parent/member).resolve()
        if target != allowed and allowed not in target.parents:
            raise SystemExit('Package path outside the intended plugin.')
plugin_path = ROOT/'data/astrbot/config/astrbot_plugin_whale_memory_config.json'
core_path = ROOT/'data/astrbot/cmd_config.json'
core = json.loads(core_path.read_text(encoding='utf-8-sig'))
seg = core['platform_settings']['segmented_reply']
if seg.get('regex') != r'^[\s\S]{1,20}$|[\s\S]{1,19}?[。！？!?\n]|[\s\S]{1,20}' or not seg.get('enable'):
    raise SystemExit('Native 20-character segmentation contract changed; review before installing.')
if core['provider_settings']['streaming_response']:
    raise SystemExit('This extension requires the existing non-streaming mode.')
os.umask(0o077)
stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
backup = ROOT/'backups'/('before-segment-score-'+stamp+'.tar.gz')
subprocess.run(['docker','compose','stop','astrbot'],cwd=str(ROOT),check=True)
try:
    subprocess.run(['tar','-czf',str(backup),'-C',str(ROOT/'data'),'astrbot'],check=True)
    os.chmod(str(backup),0o600)
    before = json.loads(plugin_path.read_text(encoding='utf-8-sig'))
    core_before = core_path.read_bytes()
    after = copy.deepcopy(before)
    new_fields = {'segment_score_enabled':True, 'segment_weight_1':60.0,
                  'segment_weight_2':30.0, 'segment_weight_3':10.0,
                  'segment_score_micro_random':True}
    after.update(new_fields)
    if {key:value for key,value in after.items() if key not in new_fields} != {
            key:value for key,value in before.items() if key not in new_fields}:
        raise SystemExit('Unexpected change outside segment-scoring settings.')
    with zipfile.ZipFile(str(archive_path)) as archive:
        archive.extractall(str(allowed.parent))
    pending = plugin_path.with_suffix('.pending')
    pending.write_text(json.dumps(after,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    os.chmod(str(pending),0o600)
    pending.replace(plugin_path)
    assert core_before == core_path.read_bytes(), 'Core configuration changed unexpectedly'
    assert before.get('base_style') == after.get('base_style'), 'Style must not change'
    print('Segment score extension installed; version 1.3.0')
    print('Backup:',backup.name)
    print('Added/updated fields:',sorted(new_fields))
    print('Other core/plugin rules and approved style preserved.')
finally:
    subprocess.run(['docker','compose','up','-d','astrbot'],cwd=str(ROOT),check=True)
