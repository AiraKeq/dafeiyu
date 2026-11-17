"""Cold deployment only. No start/restart/compose up, no model calls or QQ send.
Host Python3.6. Fixed trusted paths, backups before updates, original settings preserved.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = Path('/opt/qq-deepseek')
STAGE = ROOT / 'semantic-build-20261116'
PLUGIN = ROOT / 'data/astrbot/plugins/astrbot_plugin_whale_memory'
CONFIG = ROOT / 'data/astrbot/config/astrbot_plugin_whale_memory_config.json'
DATA = ROOT / 'data/astrbot/plugin_data/whale_memory'
SEMANTIC = {'semantic_enabled': True, 'semantic_similarity_threshold': 0.42,
            'semantic_top_k': 12, 'semantic_query_timeout_seconds': 3.0,
            'semantic_sync_interval_seconds': 60.0}


def container(role):
    return subprocess.check_output(['docker', 'inspect', '--format',
        '{{.Id}}|{{.State.Status}}|{{.State.StartedAt}}|{{.State.FinishedAt}}|{{.RestartCount}}',
        'qq-deepseek-' + role + '-1']).decode().strip()


def signatures():
    output = {}
    for name in ('memory.sqlite3','memory.sqlite3-wal','memory.sqlite3-shm','pagination.sqlite3',
                 'pagination.sqlite3-wal','pagination.sqlite3-shm','reply-queue.json',
                 'review-state.json','segment-score-state.json'):
        file = DATA / name
        if file.is_file():
            stat = file.stat()
            output[name] = {'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),
                            'mtime_ns':stat.st_mtime_ns,'size':stat.st_size,'mode':stat.st_mode & 4095}
        else:
            output[name] = None
    for name in ('data/astrbot/cmd_config.json','.speech-paused'):
        file = ROOT / name
        stat = file.stat()
        output[name] = {'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),
                        'mtime_ns':stat.st_mtime_ns,'size':stat.st_size,'mode':stat.st_mode & 4095}
    return output


def perform_deployment():
    os.umask(0o077)
    if ROOT.resolve() != Path('/opt/qq-deepseek') or PLUGIN.is_symlink() or DATA.is_symlink():
        raise RuntimeError('Unexpected fixed deployment path.')
    before = {role:container(role) for role in ('astrbot','napcat')}
    if '|exited|' not in before['astrbot']:
        raise RuntimeError('AstrBot must be stopped before deployment.')
    original = signatures()
    config_raw = CONFIG.read_bytes()
    config = json.loads(config_raw.decode('utf-8-sig'))
    style = config.get('base_style')
    if not isinstance(style,str) or not style:
        raise RuntimeError('Current style not verified.')
    quality = json.loads((STAGE/'semantic-quality.json').read_text(encoding='utf-8'))
    if not quality.get('deleted_vector_rejected') or not all(x.get('semantic_hit') for x in quality.get('cases',[])):
        raise RuntimeError('Real synthetic semantic quality validation not passed.')
    stamp = time.strftime('%Y%m%d-%H%M%S',time.localtime())
    backup = ROOT/'backups'/('before-semantic-'+stamp+'.tar.gz')
    if backup.exists():
        raise RuntimeError('Backup target already exists.')
    subprocess.check_call(['tar','-czf',str(backup),'-C',str(ROOT),'data/astrbot','.speech-paused','manager'])
    os.chmod(str(backup),0o600)
    if signatures()!=original or {role:container(role) for role in before}!=before or CONFIG.read_bytes()!=config_raw:
        raise RuntimeError('Production changed during backup; no deployment applied.')
    bundle = STAGE/'whale-memory-1.5.0.zip'
    expected = json.loads((STAGE/'bundle-manifest.json').read_text(encoding='utf-8'))
    if hashlib.sha256(bundle.read_bytes()).hexdigest()!=expected.get('zip_sha256'):
        raise RuntimeError('Package hash not verified.')
    with zipfile.ZipFile(str(bundle)) as archive:
        names = archive.namelist()
        if set(names)!=set(expected.get('files',{})) or any(hashlib.sha256(archive.read(name)).hexdigest()!=expected['files'][name] for name in names):
            raise RuntimeError('Package file manifest does not match.')
        if any(name.startswith('/') or '..' in Path(name).parts or '\\' in name for name in names):
            raise RuntimeError('Unsafe package entry.')
        for name in names:
            target = PLUGIN/name
            if target.is_symlink():
                raise RuntimeError('Unsafe plugin target.')
        archive.extractall(str(PLUGIN))
    for source_name,target_name in (('deps','.semantic-deps'),('model','semantic-model')):
        source = STAGE/source_name
        target = DATA/target_name
        if target.exists() or target.is_symlink():
            raise RuntimeError('Refuse to overwrite existing semantic resources.')
        shutil.copytree(str(source),str(target))
    updated = dict(config)
    updated.update(SEMANTIC)
    temp = CONFIG.with_name(CONFIG.name+'.semantic-install-pending')
    if temp.exists():
        raise RuntimeError('Unexpected config pending file.')
    with temp.open('xb') as handle:
        handle.write((json.dumps(updated,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))
        handle.flush();os.fsync(handle.fileno())
    os.chmod(str(temp),CONFIG.stat().st_mode & 4095)
    if temp.parent.resolve()!=CONFIG.parent.resolve():
        raise RuntimeError('Unsafe config publication path.')
    os.replace(str(temp),str(CONFIG))
    readback=json.loads(CONFIG.read_text(encoding='utf-8-sig'))
    if readback.get('base_style')!=style or {k:v for k,v in readback.items() if k not in SEMANTIC} != {k:v for k,v in config.items() if k not in SEMANTIC}:
        raise RuntimeError('Existing config unexpectedly changed; retain pause and backup for recovery.')
    if signatures()!=original or {role:container(role) for role in before}!=before:
        raise RuntimeError('Preserved source files or containers changed; keep paused for review.')
    report={'version':'1.5.0','backup':str(backup),'zip_sha256':hashlib.sha256(bundle.read_bytes()).hexdigest(),
            'source_files_preserved':True,'containers_preserved':True,'style_characters':len(style),
            'semantic_settings':SEMANTIC,'astrbot':'stopped'}
    (STAGE/'deployment.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))

def main():
    import fcntl
    lock = ROOT/'backups/whale-manager/operation.lock'
    if lock.is_symlink() or not lock.parent.is_dir():
        raise RuntimeError('Manager operation lock not verified.')
    with lock.open('a+b') as handle:
        fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            perform_deployment()
        finally:
            fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

if __name__=='__main__':
    main()
