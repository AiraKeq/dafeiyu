"""Apply v1.6.1 code only, under the existing cold-operation lock."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import zipfile
ROOT=Path('/opt/qq-deepseek');STAGE=ROOT/'lookup-fix-20261116'
PLUGIN=ROOT/'data/astrbot/plugins/astrbot_plugin_whale_memory'
DATA=ROOT/'data/astrbot/plugin_data/whale_memory'

def state(role):
    return subprocess.check_output(['docker','inspect','--format','{{.Id}}|{{.State.Status}}|{{.State.StartedAt}}|{{.State.FinishedAt}}|{{.RestartCount}}','qq-deepseek-'+role+'-1']).decode().strip()

def hashes():
    paths=[DATA/name for name in ('memory.sqlite3','memory.sqlite3-wal','memory.sqlite3-shm','pagination.sqlite3','pagination.sqlite3-wal','pagination.sqlite3-shm','reply-queue.json','review-state.json','segment-score-state.json','semantic-index.sqlite3')]
    paths += [ROOT/'data/astrbot/cmd_config.json',ROOT/'data/astrbot/config/astrbot_plugin_whale_memory_config.json',ROOT/'.speech-paused']
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None for p in paths}

def main():
    os.umask(0o077)
    lock=ROOT/'backups/whale-manager/operation.lock'
    if lock.is_symlink():raise RuntimeError('Unsafe lock path.')
    with lock.open('a+b') as handle:
        fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        before={role:state(role) for role in ('astrbot','napcat')}
        if '|exited|' not in before['astrbot']:raise RuntimeError('Must remain paused for code fix.')
        original=hashes()
        manifest=json.loads((STAGE/'bundle-manifest.json').read_text())
        package=STAGE/'whale-memory-1.6.1.zip'
        if hashlib.sha256(package.read_bytes()).hexdigest()!=manifest['zip_sha256']:raise RuntimeError('Package not verified.')
        backup=ROOT/'backups'/('before-routing-fix-'+time.strftime('%Y%m%d-%H%M%S')+'.tar.gz')
        subprocess.check_call(['tar','-czf',str(backup),'-C',str(ROOT),
            'data/astrbot/plugins/astrbot_plugin_whale_memory','data/astrbot/config/astrbot_plugin_whale_memory_config.json',
            'data/astrbot/cmd_config.json','manager'])
        os.chmod(str(backup),0o600)
        if original!=hashes() or before!={role:state(role) for role in before}:raise RuntimeError('Source changed during backup.')
        with zipfile.ZipFile(str(package)) as archive:
            names=archive.namelist()
            if set(names)!=set(manifest['files']) or any(name.startswith('/') or '..' in Path(name).parts or '\\' in name for name in names):raise RuntimeError('Unsafe package.')
            for name in names:
                if PLUGIN.joinpath(name).is_symlink() or hashlib.sha256(archive.read(name)).hexdigest()!=manifest['files'][name]:raise RuntimeError('Unverified package file.')
            archive.extractall(str(PLUGIN))
        if original!=hashes() or before!={role:state(role) for role in before}:raise RuntimeError('Facts/config/containers unexpectedly changed.')
        report={'version':'1.6.1','backup':str(backup),'source_preserved':True,'config_changed':False,
                'containers_preserved':True,'astrbot':'stopped','package_sha256':manifest['zip_sha256']}
        (STAGE/'deployment.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
if __name__=='__main__':main()
