"""Switch the allowed QQ group without moving memories or exposing secrets."""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess


def write_json(path, value):
    pending = path.with_suffix(path.suffix + '.pending')
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(str(pending), 0o600)
    pending.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('group', help='QQ group number')
    args = parser.parse_args()
    if not 5 <= len(args.group) <= 12 or any(char not in '0123456789' for char in args.group):
        parser.error('The group must contain 5 to 12 ASCII digits.')

    root = Path('/opt/qq-deepseek')
    paths = (
        root / 'data/astrbot/cmd_config.json',
        root / 'data/astrbot/config/astrbot_plugin_whale_memory_config.json',
    )
    os.umask(0o077)
    backup = root / 'backups' / ('group-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    subprocess.run(['docker', 'compose', 'stop', 'astrbot'], cwd=str(root), check=True)
    try:
        backup.mkdir(parents=True, exist_ok=False)
        for path in paths:
            shutil.copy2(str(path), str(backup / path.name))
            os.chmod(str(backup / path.name), 0o600)
        config, plugin = (json.loads(path.read_text(encoding='utf-8-sig')) for path in paths)
        settings = config['platform_settings']
        previous = settings.get('id_whitelist', [])
        settings['enable_id_white_list'] = True
        settings['id_whitelist'] = [
            item for item in previous
            if not str(item).startswith('aiocqhttp:GroupMessage:')
        ] + ['aiocqhttp:GroupMessage:' + args.group]
        plugin['group_ids'] = [args.group]
        plugin['names'] = list(dict.fromkeys([*plugin.get('names', []), '大肥鱼', '鲸鱼娘', '鲸鲸']))
        try:
            write_json(paths[0], config)
            write_json(paths[1], plugin)
        except Exception:
            for path in paths:
                shutil.copy2(str(backup / path.name), str(path))
            raise
        print('Allowed group:', args.group)
        print('Wake names:', ', '.join(plugin['names']))
        print('Previous group memories retained in their original scopes.')
        print('Configuration backup:', backup.name)
    finally:
        subprocess.run(['docker', 'compose', 'up', '-d', 'astrbot'], cwd=str(root), check=True)


if __name__ == '__main__':
    main()
