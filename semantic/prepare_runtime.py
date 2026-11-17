"""Prepare public semantic runtime in isolated staging; never start AstrBot.
Host Python 3.6+. No production config/database/provider/credential is read.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path('/opt/qq-deepseek')
STAGE = ROOT / 'semantic-build-20261116'
IMAGE = 'soulter/astrbot@sha256:72f341f0ffcf6621ef820277642c5d63955f7beeac3683433ce09cac96379c78'
MODEL = 'Qdrant/bge-small-zh-v1.5'
REVISION = '46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59'
FILES = {
    'config.json': (739, '60938626ad1097a0c1a14be4f8340e32c714a056'),
    'model_optimized.onnx': (94781076, '1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38'),
    'ort_config.json': (1234, '31d3edac186bcfc7fb617662b7e0f750c7fef47a'),
    'special_tokens_map.json': (125, 'a8b3208c2884c4efb86e49300fdd3dc877220cdf'),
    'tokenizer.json': (439125, 'cdb3043fc938fc918c06e66cf704c2ba58f88747'),
    'tokenizer_config.json': (367, '3a59388f0fd1bd22dec2ce7902c1be8e1fb84107'),
    'vocab.txt': (109540, 'ca4f9781030019ab9b253c6dcb8c7878b6dc87a5'),
}

def paused():
    state = subprocess.check_output(['docker', 'inspect', '--format', '{{.State.Running}}', 'qq-deepseek-astrbot-1']).decode().strip()
    if state != 'false':
        raise RuntimeError('Production AstrBot must remain stopped.')


def verify(path, expected):
    size, sha = expected
    if not path.is_file() or path.stat().st_size != size:
        return False
    raw_hash = hashlib.sha256()
    git_hash = hashlib.sha1(('blob %d\0' % size).encode())
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            raw_hash.update(chunk)
            git_hash.update(chunk)
    return (raw_hash.hexdigest() if len(sha) == 64 else git_hash.hexdigest()) == sha


def main():
    os.umask(0o077)
    paused()
    if STAGE.resolve() != Path('/opt/qq-deepseek/semantic-build-20261116') or STAGE.is_symlink():
        raise RuntimeError('Unexpected staging path.')
    STAGE.mkdir(mode=0o700, exist_ok=True)
    model_dir = STAGE / 'model'
    model_dir.mkdir(mode=0o700, exist_ok=True)
    manifest = {'model': 'BAAI/bge-small-zh-v1.5', 'repository': MODEL,
                'revision': REVISION, 'dimension': 512, 'license': 'MIT', 'files': {}}
    for name, expected in FILES.items():
        target = model_dir / name
        if not verify(target, expected):
            if target.exists():
                raise RuntimeError('Existing model file does not match pinned source: ' + name)
            part = model_dir / (name + '.download-part')
            if part.exists():
                raise RuntimeError('Partial download retained; inspect before resuming: ' + name)
            url = 'https://huggingface.co/' + MODEL + '/resolve/' + REVISION + '/' + name
            request = urllib.request.Request(url, headers={'User-Agent': 'whale-semantic-model-preparation/1.0'})
            print('Downloading public pinned model file:', name, flush=True)
            with urllib.request.urlopen(request, timeout=90) as response, part.open('xb') as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if not verify(part, expected):
                raise RuntimeError('Downloaded model hash mismatch: ' + name)
            # Both absolute targets are the pinned staging directory's known files.
            if part.parent.resolve() != model_dir.resolve() or target.parent.resolve() != model_dir.resolve():
                raise RuntimeError('Unsafe model publish path.')
            os.rename(str(part), str(target))
        manifest['files'][name] = {'size': target.stat().st_size, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
    (model_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    requirements = 'sqlite-vec==0.1.6\nfastembed==0.9.1\n'
    requirement_path = STAGE / 'requirements.txt'
    if requirement_path.exists() and requirement_path.read_text() != requirements:
        raise RuntimeError('Unexpected existing staging requirements.')
    requirement_path.write_text(requirements, encoding='utf-8')
    paused()
    install = ('set -e; if command -v uv >/dev/null 2>&1; then '
               'uv pip install --target /work/deps --no-compile --only-binary :all: -r /work/requirements.txt; '
               'else python -m pip install --target /work/deps --no-compile --only-binary :all: -r /work/requirements.txt; fi')
    command = ['docker', 'run', '--rm', '--pull=never', '--name', 'whale-semantic-deps',
               '--memory', '768m', '--cpus', '1', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--volume', str(STAGE) + ':/work', '--workdir', '/work', '--entrypoint', '/bin/sh', IMAGE, '-c', install]
    print('Installing isolated optional dependency directory; production image unchanged.', flush=True)
    subprocess.check_call(command)
    paused()
    print('Public model hashes verified and optional dependencies staged. QQ remains stopped.', flush=True)

if __name__ == '__main__':
    main()
