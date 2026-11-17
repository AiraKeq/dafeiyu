"""Prepare isolated plugin rehearsal data; never copy production credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--archive', type=Path, default=Path('/opt/qq-deepseek/artifacts/whale-memory-1.2.0.zip'))
args = parser.parse_args()
root = Path('/opt/qq-deepseek/rehearsal')
data = root / 'data'
plugins = data / 'plugins'
expected = args.archive.with_suffix('.zip.sha256').read_text(encoding='ascii').split()[0]
if hashlib.sha256(args.archive.read_bytes()).hexdigest() != expected:
    raise SystemExit('Rehearsal package checksum did not match.')
plugins.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(str(args.archive)) as archive:
    allowed = (plugins / 'astrbot_plugin_whale_memory').resolve()
    for name in archive.namelist():
        destination = (plugins / name).resolve()
        if destination != allowed and allowed not in destination.parents:
            raise SystemExit('Archive path outside rehearsal plugin directory.')
    archive.extractall(str(plugins))
config = {
    'config_version': 5,
    'dashboard': {'enable': False},
    'provider_sources': [], 'provider': [], 'platform': [],
    'disable_metrics': True,
    'pypi_index_url': 'https://pypi.org/simple/',
    'provider_settings': {'proactive_capability': {'add_cron_tools': False}},
}
(data / 'cmd_config.json').write_text(json.dumps(config), encoding='utf-8')
print('Isolated rehearsal data prepared; no production configuration copied.')
