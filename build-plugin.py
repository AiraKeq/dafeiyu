"""Build a versioned source package and SHA-256 sidecar, excluding runtime data."""
import hashlib
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parent
if not (root / 'plugins').exists():
    root = root.parent
plugin = root / 'plugins/astrbot_plugin_whale_memory'
metadata = (plugin / 'metadata.yaml').read_text(encoding='utf-8')
version = next(line.split(':', 1)[1].strip().strip('"\'') for line in metadata.splitlines() if line.startswith('version:'))
target = root / 'artifacts' / ('whale-memory-' + version + '.zip')
target.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(plugin.rglob('*')):
        if not path.is_file() or any(part in ('.codegraph', '__pycache__', '.git') for part in path.parts):
            continue
        if '.sqlite3' in path.name or path.suffix == '.log':
            continue
        archive.write(path, Path(plugin.name) / path.relative_to(plugin))
digest = hashlib.sha256(target.read_bytes()).hexdigest()
target.with_suffix('.zip.sha256').write_text(digest + '  ' + target.name + '\n', encoding='ascii')
print('Built:', target.name)
print('SHA256:', digest)
