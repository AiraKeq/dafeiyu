"""Install the tested plugin with a server-side backup and bounded config changes."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile

ROOT = Path('/opt/qq-deepseek')
archive_path = ROOT / 'artifacts/whale-memory-1.1.1.zip'
expected = (ROOT / 'artifacts/whale-memory-1.1.1.zip.sha256').read_text().split()[0]
if hashlib.sha256(archive_path.read_bytes()).hexdigest() != expected:
    raise SystemExit('Plugin package checksum did not match.')
os.umask(0o077)
stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
backup = ROOT / 'backups' / ('before-memory-' + stamp + '.tar.gz')
subprocess.run(['docker', 'compose', 'stop', 'astrbot'], cwd=str(ROOT), check=True)
try:
    subprocess.run(['tar', '-czf', str(backup), '-C', str(ROOT / 'data'), 'astrbot'], check=True)
    os.chmod(str(backup), 0o600)
    plugins = ROOT / 'data/astrbot/plugins'
    plugins.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(archive_path)) as archive:
        for name in archive.namelist():
            target = (plugins / name).resolve()
            allowed = (plugins / 'astrbot_plugin_whale_memory').resolve()
            if target != allowed and allowed not in target.parents:
                raise SystemExit('Archive path was outside the intended plugin.')
        archive.extractall(str(plugins))
    path = ROOT / 'data/astrbot/cmd_config.json'
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    assert config['admins_id'] == ['1000000002'], 'Unexpected administrator configuration'
    config['platform_settings']['rate_limit']['count'] = 0
    config['provider_settings']['streaming_response'] = False
    config['provider_settings']['display_reasoning_text'] = False
    config['t2i'] = False
    config['platform_settings']['forward_threshold'] = 1000000
    config['platform_settings']['segmented_reply'].update({
        'enable': True, 'only_llm_result': False,
        'interval_method': 'random', 'interval': '0.8,1.8',
        'words_count_threshold': 1000000, 'split_mode': 'regex',
        'regex': r'^[\s\S]{1,20}$|[\s\S]{1,19}?[。！？!?\n]|[\s\S]{1,20}',
        'content_cleanup_rule': '',
    })
    compression = config['agent_runner']['config']['compression']
    compression.update({'max_turns': 12, 'trim_turns': 1, 'overflow_strategy': 'truncate_by_turns'})
    pending = path.with_suffix('.pending')
    pending.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(str(pending), 0o600)
    pending.replace(path)
    plugin_path = ROOT / 'data/astrbot/config/astrbot_plugin_whale_memory_config.json'
    plugin_config = json.loads(plugin_path.read_text(encoding='utf-8-sig')) if plugin_path.exists() else {}
    plugin_config['group_ids'] = [
        item.rsplit(':', 1)[1]
        for item in config['platform_settings']['id_whitelist']
        if item.startswith('aiocqhttp:GroupMessage:')
    ]
    plugin_config['names'] = list(dict.fromkeys([
        *plugin_config.get('names', ['大鲸鱼', '小鲸鱼']), '大肥鱼', '鲸鱼娘', '鲸鲸',
    ]))
    plugin_config['proactive_gap_seconds'] = 10
    plugin_pending = plugin_path.with_suffix('.pending')
    plugin_pending.parent.mkdir(parents=True, exist_ok=True)
    plugin_pending.write_text(json.dumps(plugin_config, ensure_ascii=False, indent=2), encoding='utf-8')
    os.chmod(str(plugin_pending), 0o600)
    plugin_pending.replace(plugin_path)
    print('Plugin installed. Backup:', backup.name)
finally:
    subprocess.run(['docker', 'compose', 'up', '-d', 'astrbot'], cwd=str(ROOT), check=True)
