"""Cold, locked v1.6 update. Never start/restart AstrBot, NapCat or DSH."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time
import zipfile

ROOT=Path('/opt/qq-deepseek'); STAGE=ROOT/'lookup-build-20261116'
PLUGIN=ROOT/'data/astrbot/plugins/astrbot_plugin_whale_memory'
DATA=ROOT/'data/astrbot/plugin_data/whale_memory'
CONFIG=ROOT/'data/astrbot/config/astrbot_plugin_whale_memory_config.json'
CHANGES={'proactive_gap_seconds':25,'ignore_commands':True,'web_lookup_enabled':True}


def state(role):
    return subprocess.check_output(['docker','inspect','--format','{{.Id}}|{{.State.Status}}|{{.State.StartedAt}}|{{.State.FinishedAt}}|{{.RestartCount}}','qq-deepseek-'+role+'-1']).decode().strip()


def files():
    names=['memory.sqlite3','memory.sqlite3-wal','memory.sqlite3-shm','pagination.sqlite3',
           'pagination.sqlite3-wal','pagination.sqlite3-shm','reply-queue.json','review-state.json',
           'segment-score-state.json','semantic-index.sqlite3']
    selected=[DATA/name for name in names]+[ROOT/'data/astrbot/cmd_config.json',ROOT/'.speech-paused']
    return {str(p.relative_to(ROOT)):(hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None) for p in selected}


def main():
    os.umask(0o077)
    lock=ROOT/'backups/whale-manager/operation.lock'
    if lock.is_symlink():raise RuntimeError('Unsafe operation lock.')
    with lock.open('a+b') as handle:
        fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        before={role:state(role) for role in ('astrbot','napcat')}
        if '|exited|' not in before['astrbot']:raise RuntimeError('AstrBot must remain paused.')
        original=files(); raw=CONFIG.read_bytes(); config=json.loads(raw.decode('utf-8-sig'))
        manifest=json.loads((STAGE/'bundle-manifest.json').read_text())
        bundle=STAGE/'whale-memory-1.6.0.zip'
        if hashlib.sha256(bundle.read_bytes()).hexdigest()!=manifest['zip_sha256']:raise RuntimeError('Package digest mismatch.')
        backup=ROOT/'backups'/('before-online-'+time.strftime('%Y%m%d-%H%M%S')+'.tar.gz')
        subprocess.check_call(['tar','-czf',str(backup),
            '--exclude=data/astrbot/plugin_data/whale_memory/.semantic-deps',
            '--exclude=data/astrbot/plugin_data/whale_memory/semantic-model',
            '-C',str(ROOT),'data/astrbot/plugins/astrbot_plugin_whale_memory',
            'data/astrbot/config/astrbot_plugin_whale_memory_config.json','data/astrbot/cmd_config.json',
            'data/astrbot/plugin_data/whale_memory','.speech-paused','manager'])
        os.chmod(str(backup),0o600)
        if original!=files() or CONFIG.read_bytes()!=raw or before!={role:state(role) for role in before}:raise RuntimeError('Source changed during backup.')
        with zipfile.ZipFile(str(bundle)) as archive:
            names=archive.namelist()
            if set(names)!=set(manifest['files']) or any(name.startswith('/') or '..' in Path(name).parts or '\\' in name for name in names):raise RuntimeError('Package path mismatch.')
            for name in names:
                if PLUGIN.joinpath(name).is_symlink() or hashlib.sha256(archive.read(name)).hexdigest()!=manifest['files'][name]:raise RuntimeError('Package file mismatch.')
            archive.extractall(str(PLUGIN))
        target=DATA/'.lookup-deps'
        if target.exists():raise RuntimeError('Refuse to overwrite unknown lookup deps.')
        shutil.copytree(str(STAGE/'deps'),str(target))
        updated=dict(config); updated.update(CHANGES)
        pending=CONFIG.with_name(CONFIG.name+'.online-pending')
        if pending.exists():raise RuntimeError('Unexpected pending config.')
        with pending.open('xb') as output:
            output.write((json.dumps(updated,ensure_ascii=False,indent=2)+'\n').encode());output.flush();os.fsync(output.fileno())
        os.chmod(str(pending),CONFIG.stat().st_mode&4095)
        if pending.parent.resolve()!=CONFIG.parent.resolve():raise RuntimeError('Unsafe config target.')
        os.replace(str(pending),str(CONFIG))
        readback=json.loads(CONFIG.read_text(encoding='utf-8-sig'))
        if {k:v for k,v in readback.items() if k not in CHANGES}!={k:v for k,v in config.items() if k not in CHANGES}:raise RuntimeError('Original settings changed.')
        if original!=files() or before!={role:state(role) for role in before}:raise RuntimeError('Original data/container changed.')
        report={'version':'1.6.0','backup':str(backup),'changes':CHANGES,'source_preserved':True,
                'containers_preserved':True,'preservation':original,'astrbot':'stopped','zip_sha256':manifest['zip_sha256']}
        (STAGE/'deployment.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k!='preservation'}))

if __name__=='__main__':main()
