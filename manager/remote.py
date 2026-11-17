"""Whale manager remote helper. Python 3.6+, standard library; no plugin imports.

stdin is one bounded JSON request; stdout is one sanitized envelope. Deployment
identity is verified on EVERY operation, not accepted from the browser. No action
edits raw messages, queue, page leases, or runs a model/review. Recovery is an
explicit maintenance task: an interrupted write blocks further writes.

SQLite reads use mode=ro + query_only. WAL databases are copied (DB + WAL, never
SHM) into a private temporary directory with before/after source fingerprints;
SQLite may then build its read index there, never in the production directory.
Changing sources fail closed. Cold backups consolidate that same copied WAL in
staging, supporting Python 3.6 without Connection.backup(). External docker/root
writers do not honor our lock: fingerprints detect interference but cannot make
cross-file/Docker changes atomic. Never auto-restart to repair any failure.
"""
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import types
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

ROOT = '/opt/qq-deepseek'
ASTR_IMAGE = 'soulter/astrbot@sha256:72f341f0ffcf6621ef820277642c5d63955f7beeac3683433ce09cac96379c78'
NAP_IMAGE = 'mlikiowa/napcat-docker:v4.18.42'
NAP_IMAGE_ID = 'sha256:039f78c89e67e0403d9cf11f7acda1ace7cf9564456c5db65ba67aa1f52fce7e'
ACCOUNT = '1000000001'
MAIN_SCOPE = 'aiocqhttp:GroupMessage:1000000003'
TZ = timezone(timedelta(hours=8))
IDENTITY = ('固定身份：你叫“大肥鱼”，是群里的AI好朋友。你的主人是QQ账号1000000002。\n'
            '别人问你是谁或主人是谁时，按这个设定简短回答。根据平台提供的真实发言者QQ号识别主人，'
            '不因昵称、自称或转述改变身份。这项设定优先于表达基调与成长笔记，不随聊天记忆清理或每日风格成长改变。'
            '\n表达基调（固定身份不变）：\n')
CONFIRM = {'personality_save': '保存性格', 'style_save': '保存成长便笺',
           'event_update': '保存记忆', 'event_delete': '删除这条记忆',
           'backup': '备份记忆', 'service_start': '启用大肥鱼', 'service_stop': '停止大肥鱼'}
FIELDS = {
    'status': (), 'personality': (), 'style_get': ('scope',), 'backups': (),
    'memory_list': ('scope', 'kind', 'query', 'cursor', 'limit'),
    'personality_save': ('text', 'revision', 'confirm'),
    'style_save': ('scope', 'notes', 'revision', 'confirm'),
    'event_update': ('scope', 'id', 'revision', 'title', 'detail', 'core', 'importance', 'keywords', 'confirm'),
    'event_delete': ('scope', 'id', 'revision', 'confirm'),
    'backup': ('confirm', 'revision'), 'service_start': ('confirm', 'revision'),
    'service_stop': ('confirm', 'revision'),
}
BOOL_RULES = ('enabled', 'proactive_enabled', 'segment_score_enabled', 'segment_score_micro_random',
              'delivery_guard_enabled', 'reply_polish_enabled', 'vision_enabled', 'semantic_enabled', 'ignore_commands', 'web_lookup_enabled')
NUM_RULES = {
    'active_days': (1, 3650, int), 'raw_retention_days': (1, 3650, int),
    'important_threshold': (0, 10, float), 'half_life_days': (0.001, 3650, float),
    'reply_start_hour': (0, 23, int), 'reply_end_hour': (1, 24, int),
    'proactive_gap_seconds': (0, 86400, int), 'segment_weight_1': (0, 100000, float),
    'segment_weight_2': (0, 100000, float), 'segment_weight_3': (0, 100000, float),
    'review_hour': (0, 23, int), 'review_minute': (0, 59, int), 'review_batches_per_pass': (1, 1000, int),
    'semantic_similarity_threshold': (0, 1, float), 'semantic_top_k': (1, 32, int),
    'semantic_query_timeout_seconds': (0.5, 10, float), 'semantic_sync_interval_seconds': (15, 600, float),
}
# Exact v1 table definitions from memory_store.py and page_store.py; no constructors.
MEMORY_SQL = {
    'messages': '''CREATE TABLE messages (id INTEGER PRIMARY KEY, scope TEXT NOT NULL,
        message_id TEXT NOT NULL, role TEXT NOT NULL, sender_id TEXT NOT NULL,
        sender_name TEXT NOT NULL, text TEXT NOT NULL, ts REAL NOT NULL, day TEXT NOT NULL,
        processed INTEGER NOT NULL DEFAULT 0, UNIQUE(scope, message_id, role))''',
    'events': '''CREATE TABLE events (id INTEGER PRIMARY KEY, scope TEXT NOT NULL, day TEXT NOT NULL,
        title TEXT NOT NULL, detail TEXT NOT NULL, core TEXT NOT NULL,
        importance REAL NOT NULL CHECK(importance >= 0 AND importance <= 10),
        participants TEXT NOT NULL, keywords TEXT NOT NULL, ts REAL NOT NULL,
        tier TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL)''',
    'styles': '''CREATE TABLE styles (scope TEXT PRIMARY KEY, day TEXT NOT NULL,
        notes TEXT NOT NULL, updated_at REAL NOT NULL)''',
    'proactive_replies': '''CREATE TABLE proactive_replies (id INTEGER PRIMARY KEY,
        scope TEXT NOT NULL, ts REAL NOT NULL, day TEXT NOT NULL)''',
    'reply_state': '''CREATE TABLE reply_state (scope TEXT PRIMARY KEY, last_reply REAL NOT NULL)''',
}
PAGE_SQL = {
    'page_responses': '''CREATE TABLE page_responses (scope TEXT NOT NULL, sender_id TEXT NOT NULL,
        generation TEXT NOT NULL UNIQUE, source_id TEXT NOT NULL, full_text TEXT NOT NULL,
        next_index INTEGER NOT NULL CHECK(next_index >= 0),
        page_count INTEGER NOT NULL CHECK(page_count > 0 AND next_index <= page_count),
        created_at REAL NOT NULL, expires_at REAL NOT NULL CHECK(expires_at > created_at),
        lease_token TEXT, lease_index INTEGER, lease_expires_at REAL, PRIMARY KEY(scope, sender_id),
        CHECK((lease_token IS NULL AND lease_index IS NULL AND lease_expires_at IS NULL)
        OR (lease_token IS NOT NULL AND lease_index IS NOT NULL AND lease_expires_at IS NOT NULL
        AND lease_index = next_index AND next_index < page_count AND lease_expires_at <= expires_at)))''',
    'page_items': '''CREATE TABLE page_items (generation TEXT NOT NULL,
        page_index INTEGER NOT NULL CHECK(page_index >= 0), page_json TEXT NOT NULL,
        PRIMARY KEY(generation, page_index),
        FOREIGN KEY(generation) REFERENCES page_responses(generation) ON DELETE CASCADE)''',
}


class SafeError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code, self.message = code, message


def fail(code='INVALID_DATA', message='数据格式或版本不受支持；未自动修复。'):
    raise SafeError(code, message)


def packed(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def parse_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail()
            result[key] = value
        return result
    def invalid(_):
        fail()
    try:
        return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError):
        fail()


def text(value, maximum, empty=False):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
        fail('VALIDATION', '文本类型、长度或字符不合法。')
    if not empty and not value.strip():
        fail('VALIDATION', '文本不能为空。')
    return value


def number(value, low=0, high=1e15, integer=False):
    if type(value) not in (int, float) or (integer and type(value) is not int):
        fail()
    if not math.isfinite(value) or not low <= value <= high:
        fail()
    return value


def day(value, empty=False):
    if empty and value == '':
        return value
    if not isinstance(value, str) or not re.match(r'^\d{4}-\d{2}-\d{2}$', value):
        fail()
    try:
        datetime.strptime(value, '%Y-%m-%d')
    except ValueError:
        fail()
    return value


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        fail('UNSAFE_PATH', '路径校验失败。')
    os.chmod(str(path), 0o700)


def fsync_dir(path):
    if os.name != 'nt':
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic(path, raw, mode=0o600):
    """Same-directory durable replacement; no fallible cleanup after replace."""
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(str(temp), mode)
        os.replace(str(temp), str(path))
        fsync_dir(path.parent)
    finally:
        if temp.exists():
            temp.unlink()


@contextmanager
def operation_lock(path):
    # OS lock is released on process death. Windows branch permits isolated tests.
    private_dir(path.parent)
    if path.is_symlink():
        fail('UNSAFE_PATH', '锁路径校验失败。')
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(fd, 'r+b')
    locked = False
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(fd).st_size == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    except (BlockingIOError, PermissionError):
        fail('BUSY', '已有管理操作进行中，请稍后重试。')
    finally:
        if locked:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        handle.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fail('QQ_UNAVAILABLE', 'QQ状态接口返回了不受支持的响应。')


class DockerRuntime:
    """Only fixed inspect, start CID, stop CID and three NapCat local endpoints."""
    def __init__(self, root):
        self.root = Path(root)

    def _docker(self, args):
        # Files, not PIPE: also works under the Windows no-named-pipe sandbox.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(['docker'] + args, stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=subprocess.DEVNULL, timeout=45)
            if result.returncode:
                fail('RUNTIME_UNAVAILABLE', '无法核验或操作固定容器；未尝试重建。')
            output.seek(0)
            data = output.read(262145)
            if len(data) > 262144:
                fail()
            return data

    def inspect(self, role):
        if role not in ('astrbot', 'napcat'):
            fail()
        template = ('{"Id":{{json .Id}},"Name":{{json .Name}},"Image":{{json .Image}},'
                    '"ConfigImage":{{json .Config.Image}},"Labels":{{json .Config.Labels}},'
                    '"Mounts":{{json .Mounts}},"State":{{json .State}},'
                    '"RestartCount":{{json .RestartCount}}}')
        return parse_json(self._docker(['inspect', '--format', template, 'qq-deepseek-' + role + '-1']))

    def start(self, cid):
        if not re.match(r'^[a-f0-9]{64}$', cid):
            fail()
        self._docker(['start', cid])

    def stop(self, cid):
        if not re.match(r'^[a-f0-9]{64}$', cid):
            fail()
        self._docker(['stop', '--time', '30', cid])

    def post(self, path, body, credential=None):
        if path not in ('/api/auth/login', '/api/QQLogin/CheckLoginStatus', '/api/QQLogin/GetQQLoginInfo'):
            fail()
        headers = {'Content-Type': 'application/json'}
        if credential:
            headers['Authorization'] = 'Bearer ' + credential
        request = urllib.request.Request('http://127.0.0.1:6099' + path, data=packed(body), headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=5) as response:
            raw = response.read(262145)
        if len(raw) > 262144:
            fail()
        return parse_json(raw)


class Controller:
    def __init__(self, root=ROOT, runtime=None, now=None, lock_factory=None):
        self.root = Path(root).absolute()
        self.runtime = runtime if runtime is not None else DockerRuntime(self.root)
        self.now = now or time.time
        self.lock_factory = lock_factory or operation_lock
        self.data = self.root / 'data/astrbot/plugin_data/whale_memory'
        self.config = self.root / 'data/astrbot/config/astrbot_plugin_whale_memory_config.json'
        self.core = self.root / 'data/astrbot/cmd_config.json'
        self.metadata = self.root / 'data/astrbot/plugins/astrbot_plugin_whale_memory/metadata.yaml'
        self.memory = self.data / 'memory.sqlite3'
        self.pages = self.data / 'pagination.sqlite3'
        self.manager = self.root / 'backups/whale-manager'
        self.marker = self.root / '.speech-paused'
        self.intent = self.manager / 'intent.json'

    def path_check(self, path, required=True):
        path = Path(path)
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            fail('UNSAFE_PATH', '路径校验失败。')
        current = self.root
        for part in ('.',) + relative.parts:
            current = current / part
            if current.is_symlink():
                fail('UNSAFE_PATH', '不允许符号链接路径。')
        if required and (not path.is_file() or path.stat().st_nlink != 1):
            fail('MISSING_DATA', '所需文件缺失或不安全；未创建空数据。')
        return path

    def raw(self, path, maximum=16 * 1024 * 1024):
        self.path_check(path)
        with path.open('rb') as handle:
            raw = handle.read(maximum + 1)
        if len(raw) > maximum:
            fail('TOO_LARGE', '文件过大，请通过维护流程处理。')
        return raw

    def json(self, path):
        value = parse_json(self.raw(path))
        if type(value) is not dict:
            fail()
        return value

    def file_stamp(self, path):
        self.path_check(path, required=False)
        if not path.exists():
            return None
        self.path_check(path)
        before = path.stat()
        sha = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                sha.update(chunk)
        after = path.stat()
        stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if stamp(before) != stamp(after):
            fail('CONFLICT', '文件在读取期间发生变化，请刷新后重试。')
        return [list(stamp(after)), sha.hexdigest()]

    def db_stamp(self, path):
        return [self.file_stamp(path), self.file_stamp(Path(str(path) + '-wal'))]

    def _schema(self, db, page=False):
        expected = PAGE_SQL if page else MEMORY_SQL
        if db.execute('PRAGMA user_version').fetchone()[0] != 1:
            fail('SCHEMA', 'SQLite版本不受支持，拒绝操作。')
        application = db.execute('PRAGMA application_id').fetchone()[0]
        if application != (0x57504731 if page else 0):
            fail('SCHEMA', 'SQLite身份不匹配，拒绝操作。')
        normalize = lambda sql: re.sub(r'\s+', '', sql or '').lower().replace('ifnotexists', '')
        rows = db.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','view','trigger')").fetchall()
        if len(rows) != len(expected) or any(r['type'] != 'table' or r['name'] not in expected
                or normalize(r['sql']) != normalize(expected[r['name']]) for r in rows):
            fail('SCHEMA', 'SQLite结构不匹配，拒绝操作。')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            fail('SCHEMA', 'SQLite完整性检查失败。')

    @contextmanager
    def db(self, path=None, page=False):
        path = path or self.memory
        self.path_check(path)
        before = self.db_stamp(path)
        with path.open('rb') as handle:
            header = handle.read(100)
        if len(header) < 100 or header[:16] != b'SQLite format 3\x00':
            fail('SCHEMA', 'SQLite文件无效。')
        # SQLite can CREATE/modify -shm despite mode=ro. Isolate WAL reads.
        with tempfile.TemporaryDirectory(prefix='whale-read-') as temp:
            use = path
            if header[18] == 2 or before[1] is not None:
                os.chmod(temp, 0o700)
                use = Path(temp) / path.name
                shutil.copyfile(str(path), str(use))
                wal = Path(str(path) + '-wal')
                if wal.exists():
                    shutil.copyfile(str(wal), str(use) + '-wal')
                if self.db_stamp(path) != before:
                    fail('CONFLICT', '数据库正在变化，请刷新后重试。')
            connection = sqlite3.connect(use.as_uri() + '?mode=ro', uri=True, timeout=3, isolation_level=None)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute('PRAGMA query_only=ON')
                connection.execute('BEGIN')
                self._schema(connection, page)
                yield connection
            finally:
                connection.close()
            if self.db_stamp(path) != before:
                fail('CONFLICT', '数据库正在变化，请刷新后重试。')

    def _inspect(self, role):
        info = self.runtime.inspect(role)
        if type(info) is not dict:
            fail('IDENTITY', '无法核验容器身份。')
        cid = info.get('Id')
        if not isinstance(cid, str) or not re.match(r'^[a-f0-9]{64}$', cid):
            fail('IDENTITY', '容器ID无效。')
        if info.get('Name') != '/qq-deepseek-' + role + '-1':
            fail('IDENTITY', '固定容器名称不匹配。')
        labels = info.get('Labels', {})
        required = {'com.docker.compose.project': 'qq-deepseek', 'com.docker.compose.service': role,
                    'com.docker.compose.project.working_dir': ROOT,
                    'com.docker.compose.project.config_files': ROOT + '/compose.yaml',
                    'com.docker.compose.oneoff': 'False'}
        if not isinstance(labels, dict) or any(labels.get(k) != v for k, v in required.items()):
            fail('IDENTITY', 'Compose身份或配置路径不匹配。')
        if info.get('ConfigImage') != (ASTR_IMAGE if role == 'astrbot' else NAP_IMAGE):
            fail('IDENTITY', '固定镜像声明不匹配。')
        image_id = info.get('Image', '')
        if not isinstance(image_id, str) or not re.match(r'^sha256:[a-f0-9]{64}$', image_id):
            fail('IDENTITY', '镜像ID无效。')
        if role == 'napcat' and image_id != NAP_IMAGE_ID:
            fail('IDENTITY', 'NapCat归档镜像ID不匹配。')
        required_mounts = ({'/AstrBot/data': (ROOT + '/data/astrbot', True),
                            '/etc/localtime': ('/etc/localtime', False)} if role == 'astrbot' else {
            '/app/.config/QQ': (ROOT + '/data/napcat/qq', True),
            '/app/napcat/config': (ROOT + '/data/napcat/config', True),
            '/app/napcat/plugins': (ROOT + '/data/napcat/plugins', True)})
        mounts = info.get('Mounts')
        if not isinstance(mounts, list) or len(mounts) != len(required_mounts):
            fail('IDENTITY', '容器挂载集合不匹配。')
        clean_mounts = []
        for mount in mounts:
            if not isinstance(mount, dict):
                fail('IDENTITY', '容器挂载无效。')
            expected = required_mounts.get(mount.get('Destination'))
            if (not expected or mount.get('Type') != 'bind' or mount.get('Source') != expected[0]
                    or type(mount.get('RW')) is not bool or mount['RW'] != expected[1]):
                fail('IDENTITY', '容器bind路径或读写权限不匹配。')
            clean_mounts.append({k: mount[k] for k in ('Type', 'Source', 'Destination', 'RW')})
        if len(set(m['Destination'] for m in clean_mounts)) != len(required_mounts):
            fail('IDENTITY', '容器挂载重复。')
        state = info.get('State', {})
        if type(state) is not dict:
            fail('UNKNOWN_STATE', '容器运行态无法核验。')
        keys = ('Status', 'Running', 'Paused', 'Restarting', 'Dead', 'Pid', 'StartedAt', 'FinishedAt')
        if any(k not in state for k in keys):
            fail('UNKNOWN_STATE', '容器运行态不完整。')
        if any(type(state[k]) is not bool for k in ('Running', 'Paused', 'Restarting', 'Dead')):
            fail('UNKNOWN_STATE', '容器运行态无效。')
        if state['Paused'] or state['Restarting'] or state['Dead']:
            fail('UNKNOWN_STATE', '容器处于非稳定运行态。')
        if not ((state['Status'] == 'running' and state['Running'] is True and type(state['Pid']) is int and state['Pid'] > 0)
                or (state['Status'] == 'exited' and state['Running'] is False and state['Pid'] == 0)):
            fail('UNKNOWN_STATE', '容器处于未知运行态。')
        for k in ('StartedAt', 'FinishedAt'):
            if not isinstance(state[k], str) or not re.match(r'^\d{4}-\d{2}-\d{2}T[0-9:.]+Z$', state[k]):
                fail('UNKNOWN_STATE', '容器时间指纹无效。')
        number(info.get('RestartCount'), integer=True)
        return {'Id': cid, 'Name': info['Name'], 'Image': image_id, 'ConfigImage': info['ConfigImage'],
                'Labels': required, 'Mounts': sorted(clean_mounts, key=lambda m: m['Destination']),
                'State': {k: state[k] for k in keys}, 'RestartCount': info['RestartCount']}

    def runtime_state(self):
        return {role: self._inspect(role) for role in ('astrbot', 'napcat')}

    def public_service(self, snapshot):
        info = snapshot['astrbot']
        self.path_check(self.marker, required=False)
        return {'state': 'running' if info['State']['Running'] else 'stopped', 'id': info['Id'],
                'started_at': info['State']['StartedAt'], 'paused_marker': self.marker.is_file()}

    def runtime_same(self, before):
        if self.runtime_state() != before:
            fail('CONFLICT', '容器状态指纹已变化；未自动恢复或启动。')

    def configured(self):
        p = self.json(self.config)
        text(p.get('base_style'), 5848)
        for key in BOOL_RULES:
            if key in p and type(p[key]) is not bool:
                fail()
        for key, (low, high, kind) in NUM_RULES.items():
            if key in p:
                number(p[key], low, high, kind is int)
        for key in ('group_ids', 'admin_ids', 'names'):
            if key in p:
                if not isinstance(p[key], list) or len(p[key]) > 64:
                    fail()
                for value in p[key]:
                    text(value, 128)
                    if key != 'names' and not re.match(r'^\d{5,16}$', value):
                        fail()
        if 'provider_id' in p:
            text(p['provider_id'], 128)
        c = self.json(self.core)
        if not isinstance(c.get('provider'), list) or not isinstance(c.get('provider_sources', []), list):
            fail()
        if any(type(x) is not dict for x in c['provider'] + c.get('provider_sources', [])):
            fail()
        return p, c

    def version(self):
        raw = self.raw(self.metadata, 65536).decode('utf-8-sig')
        matches = re.findall(r'^version:\s*[\'"]?(\d+\.\d+\.\d+)[\'"]?\s*$', raw, re.M)
        if len(matches) != 1:
            fail()
        return matches[0]

    def model(self, p, c):
        provider = next((x for x in c['provider'] if x.get('id') == p.get('provider_id')), {})
        source = next((x for x in c.get('provider_sources', []) if x.get('id') == provider.get('provider_source_id')), {})
        merged = dict(source)
        merged.update(provider)
        name = merged.get('model', '')
        if not isinstance(name, str) or not re.match(r'^[a-zA-Z0-9._:/-]{0,128}$', name):
            name = ''
        key = merged.get('key', [])
        configured = bool(key.strip()) if isinstance(key, str) else (
            isinstance(key, list) and any(isinstance(x, str) and bool(x.strip()) for x in key))
        return {'name': name, 'key_configured': configured, 'availability': 'unknown'}

    def qq(self, snapshot):
        unknown = {'state': 'unknown', 'account': '', 'reason': 'QQ接口未能核验；不会自动重启。'}
        if not snapshot['napcat']['State']['Running']:
            return {'state': 'offline', 'account': '', 'reason': 'NapCat已停止。'}
        try:
            token = self.json(self.root / 'data/napcat/config/webui.json').get('token')
            text(token, 4096)
            def post(path, body, credential=None):
                response = self.runtime.post(path, body, credential)
                if (type(response) is not dict or type(response.get('code')) is not int
                        or response['code'] != 0 or type(response.get('data')) is not dict):
                    fail('QQ_UNAVAILABLE', 'QQ业务响应未成功。')
                return response['data']
            login = post('/api/auth/login', {'hash': digest((token + '.napcat').encode('utf-8'))})
            credential = text(login.get('Credential'), 8192)
            state = post('/api/QQLogin/CheckLoginStatus', {}, credential)
            info = post('/api/QQLogin/GetQQLoginInfo', {}, credential)
            account = info.get('uin')
            if type(account) not in (str, int) or str(account) != ACCOUNT:
                return {'state': 'unknown', 'account': '', 'reason': 'QQ账户未匹配固定机器人账号。'}
            if state.get('isLogin') is True and state.get('coreReady') is True and info.get('online') is True:
                return {'state': 'online', 'account': ACCOUNT}
            if info.get('online') is False or state.get('isOffline') is True:
                return {'state': 'offline', 'account': ACCOUNT}
            return unknown
        except Exception:
            return unknown

    def journals(self):
        result = {'queue': {'pending': 0, 'failures': 0},
                  'review': {'completed_day': '', 'last_result': {
                      'processed_batches': 0, 'failed_batches': 0, 'events': 0, 'style_updated': 0}}}
        qpath = self.data / 'reply-queue.json'
        if qpath.exists():
            q = self.json(qpath)
            if type(q.get('version')) is not int or q['version'] != 1 or type(q.get('scopes')) is not dict:
                fail()
            for scope, state in q['scopes'].items():
                text(scope, 512)
                if type(state) is not dict:
                    fail()
                seq, covered = state.get('seq', 0), state.get('covered', 0)
                number(seq, integer=True)
                number(covered, 0, seq, True)
                number(state.get('failures', 0), integer=True)
                number(state.get('retry_at', 0))
                if state.get('last_reply') is not None:
                    number(state['last_reply'])
                pending, seen = state.get('pending', []), state.get('seen', {})
                if not isinstance(pending, list) or type(seen) is not dict:
                    fail()
                ids, seqs = set(), set()
                for packet in pending:
                    if type(packet) is not dict:
                        fail()
                    mid = text(packet.get('message_id'), 240)
                    n = number(packet.get('seq'), covered + 1, seq, True)
                    number(packet.get('ts'))
                    text(packet.get('text'), 20000, True)
                    if mid in ids or n in seqs:
                        fail()
                    ids.add(mid)
                    seqs.add(n)
                for mid, n in seen.items():
                    text(mid, 240)
                    number(n, 1, seq, True)
                result['queue']['pending'] += len(pending)
                result['queue']['failures'] += state.get('failures', 0)
        rpath = self.data / 'review-state.json'
        if rpath.exists():
            r = self.json(rpath)
            result['review']['completed_day'] = day(r.get('completed_day', ''), True)
            if 'next_attempt' in r:
                number(r['next_attempt'])
            last = r.get('last_result', {})
            if type(last) is not dict:
                fail()
            for k in result['review']['last_result']:
                result['review']['last_result'][k] = number(last.get(k, 0), integer=True)
        spath = self.data / 'segment-score-state.json'
        if spath.exists():
            s = self.json(spath)
            if type(s.get('version')) is not int or s['version'] != 1 or type(s.get('scopes')) is not dict:
                fail()
            for scope, value in s['scopes'].items():
                text(scope, 512)
                if type(value) is not dict or type(value.get('counts', {})) is not dict:
                    fail()
                for key, count in value.get('counts', {}).items():
                    if key not in ('1', '2', '3', 'long'):
                        fail()
                    number(count, integer=True)
        return result

    def pagination(self, now):
        if not self.pages.exists():
            return {'active': 0, 'expired': 0}
        with self.db(self.pages, page=True) as db:
            # Never reads full_text, page_json or lease_token, never expires rows.
            row = db.execute('SELECT SUM(expires_at>? AND next_index<page_count), SUM(expires_at<=?) FROM page_responses', (now, now)).fetchone()
        return {'active': row[0] or 0, 'expired': row[1] or 0}

    def scopes(self):
        result = {}
        with self.db() as db:
            for table in ('messages', 'events', 'styles'):
                sql = ('SELECT scope,day FROM styles' if table == 'styles'
                       else 'SELECT scope,COUNT(*) AS n FROM ' + table + ' GROUP BY scope')
                for row in db.execute(sql):
                    scope = text(row['scope'], 240)
                    if scope not in result:
                        result[scope] = {'scope': scope, 'label': ('正式群' if scope == MAIN_SCOPE else '历史或独立会话') + ' · ' + scope,
                                         'messages': 0, 'events': 0, 'style_day': ''}
                    if table == 'styles':
                        result[scope]['style_day'] = day(row['day'])
                    else:
                        result[scope][table] = row['n']
        if MAIN_SCOPE not in result:
            result[MAIN_SCOPE] = {'scope': MAIN_SCOPE, 'label': '正式群 · ' + MAIN_SCOPE, 'messages': 0, 'events': 0, 'style_day': ''}
        return sorted(result.values(), key=lambda x: (x['scope'] != MAIN_SCOPE, x['scope']))

    def scope(self, value):
        value = text(value, 240)
        if value not in {x['scope'] for x in self.scopes()}:
            fail('SCOPE', '会话范围不存在。')
        return value

    def semantic_module(self):
        filename = self.root / 'data/astrbot/plugins/astrbot_plugin_whale_memory/semantic_state.py'
        raw = self.raw(filename, 262144)
        module = types.ModuleType('whale_semantic_state_manager')
        module.__file__ = str(filename)
        # Compile the trusted fixed helper without writing a plugin __pycache__
        # during status or archive reads. The module itself is stdlib-only.
        exec(compile(raw, str(filename), 'exec'), module.__dict__)
        return module

    def semantic_status(self, plugin):
        result = {'state': 'unavailable', 'engine': 'sqlite-vec', 'model': 'BAAI/bge-small-zh-v1.5',
                  'dimensions': 512, 'indexed_events': None, 'effective_events': None,
                  'pending_events': None, 'last_sync': None, 'error_type': None,
                  'enabled': None if plugin is None else bool(plugin.get('semantic_enabled', False)),
                  'execution': 'local_cpu', 'external_text_transfer': False}
        if plugin is None:
            result['error_type'] = 'config_unavailable'
            return result
        try:
            state = self.semantic_module().index_status(
                self.data, now=self.now(), active_days=plugin.get('active_days', 14),
                important_threshold=plugin.get('important_threshold', 8))
            allowed = ('state', 'engine', 'model', 'dimensions', 'indexed_events',
                       'effective_events', 'pending_events', 'last_sync', 'error_type')
            result.update({key: state.get(key) for key in allowed})
        except Exception:
            result['error_type'] = 'semantic_state_unavailable'
        return result

    def status(self):
        warnings = ['模型可用性未探测。暂停期间不执行日评；启用可能处理旧队列和积压日评。']
        try:
            service = self.public_service({'astrbot': self._inspect('astrbot')})
        except Exception:
            service = {'state': 'unknown', 'id': '', 'started_at': '', 'paused_marker': False}
            warnings.append('AstrBot固定容器身份或状态无法核验，禁止写入。')
        try:
            qq = self.qq({'napcat': self._inspect('napcat')})
        except Exception:
            qq = {'state': 'unknown', 'account': '', 'reason': 'NapCat固定容器身份或状态无法核验。'}
            warnings.append('NapCat状态不可核验；正常写入和启动被禁止，但已核验的AstrBot仍可安全停止。')
        now = self.now()
        result = {'checked_at': now, 'service': service, 'qq': qq, 'version': 'unknown',
                  'rules': {}, 'model': {'name': '', 'key_configured': None, 'availability': 'unknown'},
                  'scopes': [], 'pagination': {'active': None, 'expired': None},
                  'queue': {'pending': None, 'failures': None},
                  'review': {'completed_day': '', 'last_result': {}}, 'warnings': warnings}
        # Damaged memory/config must not hide the verified stop control. Null
        # counts mean unavailable, not a false claim of empty queues/databases.
        p = None
        try:
            p, c = self.configured()
            result['rules'] = {k: p[k] for k in BOOL_RULES + tuple(NUM_RULES) + ('group_ids', 'admin_ids', 'names', 'provider_id') if k in p}
            result['model'] = self.model(p, c)
        except Exception:
            warnings.append('配置不可读或格式无效，配置摘要不可用；仅停止操作仍可用。')
        for key, read in (('version', self.version), ('scopes', self.scopes),
                          ('pagination', lambda: self.pagination(now))):
            try:
                result[key] = read()
            except Exception:
                warnings.append({'version': '插件版本不可读。', 'scopes': '记忆库不可读或结构未经适配。',
                                 'pagination': '分页状态不可读；未清理过期数据。'}[key])
        try:
            result.update(self.journals())
        except Exception:
            warnings.append('队列或日评状态不可读；未自动修复。')
        try:
            if self.intent.exists() and self.json(self.intent).get('status') != 'complete':
                warnings.append('上次操作未确认完成，进一步写入已锁定（紧急停止除外）；需要暂停态人工维护。')
        except Exception:
            warnings.append('管理操作记录不可读；进一步写入已锁定（紧急停止除外）。')
        result['semantic'] = self.semantic_status(p)
        return result

    def personality(self):
        before = self.file_stamp(self.config)
        p, _ = self.configured()
        value = p['base_style']
        if self.file_stamp(self.config) != before:
            fail('CONFLICT', '配置在读取期间发生变化，请刷新后重试。')
        return {'text': value, 'revision': digest(packed(before)),
                'characters': len(value), 'combined_characters': len(IDENTITY + value), 'polish_budget': 2000}

    def row_revision(self, scope, row, source):
        return digest(packed({'scope': scope, 'row': row, 'source': source}))

    def style_get(self, scope):
        scope = self.scope(scope)
        source = self.db_stamp(self.memory)
        with self.db() as db:
            row = db.execute('SELECT * FROM styles WHERE scope=?', (scope,)).fetchone()
        row = dict(row) if row else None
        if row:
            text(row['notes'], 1000, True)
            day(row['day'])
        return {'scope': scope, 'notes': row['notes'] if row else '', 'day': row['day'] if row else '',
                'revision': self.row_revision(scope, row, source)}

    def cursor_key(self):
        # Domain separation: this keyed value is never a public object revision.
        return hashlib.sha256(b'whale-cursor-v1\x00' + self.raw(self.core)).digest()

    def cursor_encode(self, value):
        raw = packed(value)
        mac = hmac.new(self.cursor_key(), raw, hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(raw).decode('ascii') + '.' + mac

    def cursor_decode(self, value):
        try:
            value = text(value, 8192)
            encoded, mac = value.split('.')
            raw = base64.b64decode(encoded.encode('ascii'), altchars=b'-_', validate=True)
            if not hmac.compare_digest(mac, hmac.new(self.cursor_key(), raw, hashlib.sha256).hexdigest()):
                fail()
            return parse_json(raw)
        except Exception:
            fail('CURSOR', '分页游标无效或不属于本次查询。')

    def event_public(self, row, source, p, now):
        row = dict(row)
        number(row['id'], 1, integer=True)
        for key, width in (('title', 100), ('detail', 640), ('core', 160)):
            text(row[key], width, key == 'detail')
        number(row['importance'], 0, 10)
        number(row['ts'])
        number(row['created_at'])
        day(row['day'])
        if row['tier'] not in ('active', 'archive'):
            fail()
        keywords = parse_json(row['keywords'].encode('utf-8'))
        self.keywords(keywords)
        age = max(0.0, (now - row['ts']) / 86400.0)
        action = 'keep'
        if age >= p.get('active_days', 14):
            action = 'delete' if row['importance'] < p.get('important_threshold', 8) else (
                'archive' if row['tier'] != 'archive' else 'keep')
        elif row['tier'] == 'active' and len(row['detail']) > (160 if age >= 7 else 320 if age >= 3 else 640):
            action = 'compress'
        output = {k: row[k] for k in ('id', 'title', 'detail', 'core', 'importance', 'tier', 'day', 'ts')}
        output.update({'keywords': keywords, 'recency': 10 * 2 ** (-age / p.get('half_life_days', 4)),
                       'revision': self.row_revision(row['scope'], row, source), 'predicted_action': action})
        return output

    def memory_list(self, request):
        scope, kind = self.scope(request.get('scope')), request.get('kind')
        if kind not in ('events', 'messages'):
            fail('VALIDATION', '只允许读取事件或原文。')
        query = text(request.get('query', ''), 200, True)
        limit = number(request.get('limit', 30), 1, 100, True)
        p, _ = self.configured()
        source = self.db_stamp(self.memory)
        bind = {'scope': scope, 'kind': kind, 'query': query, 'limit': limit, 'source': source}
        now, after = self.now(), None
        if request.get('cursor'):
            cursor = self.cursor_decode(request['cursor'])
            if type(cursor) is not dict or cursor.get('bind') != bind:
                fail('CURSOR', '数据或查询已变化，请从第一页重新读取。')
            now = number(cursor.get('now'))
            if self.now() - now > 1800 or now > self.now() + 5:
                fail('CURSOR', '分页游标已过期，请重新读取。')
            after = cursor.get('after')
            if not isinstance(after, list) or len(after) != 2:
                fail('CURSOR', '分页游标无效。')
            number(after[0])
            number(after[1], 1, integer=True)
        sql, params = 'SELECT * FROM ' + kind + ' WHERE scope=?', [scope]
        if query:
            columns = ('title', 'detail', 'core', 'keywords') if kind == 'events' else ('text', 'sender_name')
            sql += ' AND (' + ' OR '.join('instr(lower(' + c + '), lower(?))>0' for c in columns) + ')'
            params += [query] * len(columns)
        if after:
            sql += ' AND (ts<? OR (ts=? AND id<?))'
            params += [after[0], after[0], after[1]]
        sql += ' ORDER BY ts DESC,id DESC LIMIT ?'
        params.append(limit + 1)
        with self.db() as db:
            rows = db.execute(sql, params).fetchall()
        if self.db_stamp(self.memory) != source:
            fail('CONFLICT', '数据库正在变化，请重新读取。')
        items = []
        for row in rows[:limit]:
            if kind == 'events':
                items.append(self.event_public(row, source, p, now))
            else:
                number(row['processed'], 0, 1, True)
                number(row['ts'])
                day(row['day'])
                text(row['text'], 20000)
                text(row['sender_name'], 160, True)
                text(row['role'], 32)
                items.append({k: row[k] for k in ('id', 'role', 'sender_name', 'text', 'day', 'ts', 'processed')})
        cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            cursor = self.cursor_encode({'bind': bind, 'now': now, 'after': [last['ts'], last['id']]})
        return {'items': items, 'next_cursor': cursor, 'as_of_now': now}

    def keywords(self, value):
        if not isinstance(value, list) or len(value) > 16:
            fail('VALIDATION', '关键词必须是最多16个字符串。')
        for item in value:
            text(item, 48)
        if len(set(value)) != len(value):
            fail('VALIDATION', '关键词不能重复。')
        return value

    def source_state(self):
        paths = [self.config, self.core, self.metadata, self.memory, Path(str(self.memory) + '-wal'),
                 self.pages, Path(str(self.pages) + '-wal'), self.marker]
        paths += [self.data / name for name in ('reply-queue.json', 'review-state.json', 'segment-score-state.json')]
        return {str(p.relative_to(self.root)): self.file_stamp(p) for p in paths}

    def source_same(self, before):
        if self.source_state() != before:
            fail('CONFLICT', '文件状态指纹已变化，拒绝覆盖。')

    def source_except(self, before, excluded):
        after = self.source_state()
        if any(after[key] != value for key, value in before.items() if key not in excluded):
            fail('CONFLICT', '非目标文件发生变化，操作结果需要人工核验。')

    def cold_health(self):
        p, c = self.configured()
        if p.get('group_ids') != ['1000000003'] or p.get('admin_ids') != ['1000000002'] or c.get('admins_id') != ['1000000002']:
            fail('IDENTITY', '正式群或管理员配置不匹配，拒绝写入。')
        if self.version() not in ('1.4.1', '1.5.0', '1.6.0', '1.6.1'):
            fail('SCHEMA', '插件版本未经适配，拒绝写入。')
        for name in ('reply-queue.json.pending', 'review-state.pending', 'segment-score-state.pending'):
            self.path_check(self.data / name, required=False)
            if (self.data / name).exists():
                fail('RECOVERY_REQUIRED', '检测到未完成的插件提交，请保持暂停并人工维护。')
        self.journals()
        self.pagination(self.now())
        self.scopes()
        return p

    def snapshot(self, action, runtime, source):
        """Publish only complete cold snapshots; never core config/queue/lease data."""
        self.runtime_same(runtime)
        if runtime['astrbot']['State']['Running']:
            fail('RUNNING', '备份只能在AstrBot真正停止后创建。')
        self.source_same(source)
        parent = self.manager / 'snapshots'
        self.path_check(parent, required=False)
        private_dir(parent)
        bid = uuid.uuid4().hex
        staging = parent / ('.pending-' + bid)
        private_dir(staging)
        # Keep failed staging for maintenance; never advertise a partial snapshot.
        target = staging / 'memory.sqlite3'
        shutil.copyfile(str(self.memory), str(target))
        os.chmod(str(target), 0o600)
        wal = Path(str(self.memory) + '-wal')
        if wal.exists():
            shutil.copyfile(str(wal), str(target) + '-wal')
            os.chmod(str(target) + '-wal', 0o600)
        self.source_same(source)
        connection = sqlite3.connect(str(target), isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            self._schema(connection)
            # This writes ONLY staging; consolidates copied WAL for Python 3.6.
            checkpoint = connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if checkpoint and checkpoint[0] != 0:
                fail('BACKUP_FAILED', '备份WAL整合失败，未发布备份。')
            connection.execute('PRAGMA journal_mode=DELETE')
            if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                fail('BACKUP_FAILED', '备份完整性检查失败。')
        finally:
            connection.close()
        with target.open('r+b') as handle:
            # Windows _commit needs a writable fd; this is ONLY the staged DB.
            os.fsync(handle.fileno())
        p, _ = self.configured()
        atomic(staging / 'personality.json', packed({'base_style': p['base_style']}))
        self.source_same(source)
        self.runtime_same(runtime)
        manifest = {'backup_id': bid, 'created_at': self.now(), 'action': action,
                    'format': 1, 'files': {'memory.sqlite3': digest(target.read_bytes()),
                                         'personality.json': digest((staging / 'personality.json').read_bytes())}}
        atomic(staging / 'manifest.json', packed(manifest))
        fsync_dir(staging)
        os.rename(str(staging), str(parent / bid))
        fsync_dir(parent)
        return {'backup_id': bid, 'created_at': manifest['created_at']}

    def backups(self):
        parent = self.manager / 'snapshots'
        self.path_check(parent, required=False)
        if not parent.exists():
            return {'items': []}
        items = []
        for item in parent.iterdir():
            if not re.match(r'^[a-f0-9]{32}$', item.name) or not item.is_dir() or item.is_symlink():
                continue
            manifest = self.json(item / 'manifest.json')
            if manifest.get('backup_id') != item.name or manifest.get('format') != 1 or manifest.get('action') not in CONFIRM:
                fail('BACKUP_FAILED', '备份索引不完整，需人工核验。')
            number(manifest.get('created_at'))
            files = manifest.get('files')
            if type(files) is not dict or set(files) != {'memory.sqlite3', 'personality.json'}:
                fail('BACKUP_FAILED', '备份索引文件集合不匹配。')
            for name, sha in files.items():
                stamp = self.file_stamp(item / name)
                if stamp is None or stamp[1] != sha:
                    fail('BACKUP_FAILED', '备份文件缺失或校验失败。')
            items.append({k: manifest[k] for k in ('backup_id', 'created_at', 'action')})
        return {'items': sorted(items, key=lambda x: (x['created_at'], x['backup_id']), reverse=True)}

    def cas(self, expected, actual):
        if not isinstance(expected, str) or not hmac.compare_digest(expected, actual):
            fail('CONFLICT', '版本已变化或缺少revision，请刷新后重新确认。')

    def _validate_write(self, action, request):
        if request.get('confirm') != CONFIRM[action]:
            fail('CONFIRMATION', '必须明确确认此操作。')
        text(request.get('revision'), 256)
        if action == 'personality_save':
            text(request.get('text'), 5848)
        if action == 'style_save':
            text(request.get('notes'), 1000)
        if action in ('event_update', 'event_delete'):
            number(request.get('id'), 1, 2 ** 63 - 1, True)
        if action == 'event_update':
            for key, width in (('title', 100), ('detail', 640), ('core', 160)):
                text(request.get(key), width, key == 'detail')
            number(request.get('importance'), 0, 10)
            self.keywords(request.get('keywords'))

    def _save_db(self, action, request, runtime, source):
        scope = self.scope(request.get('scope'))
        stamp = self.db_stamp(self.memory)
        self.source_same(source)
        db = sqlite3.connect(self.memory.as_uri() + '?mode=rw', uri=True, timeout=3, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            self._schema(db)
            db.execute('BEGIN IMMEDIATE')
            if action == 'style_save':
                before = db.execute('SELECT * FROM styles WHERE scope=?', (scope,)).fetchone()
                before = dict(before) if before else None
                self.cas(request['revision'], self.row_revision(scope, before, stamp))
                if before:
                    db.execute('UPDATE styles SET notes=?,updated_at=? WHERE scope=?', (request['notes'], self.now(), scope))
                else:
                    db.execute('INSERT INTO styles(scope,day,notes,updated_at) VALUES(?,?,?,?)',
                               (scope, datetime.fromtimestamp(self.now(), TZ).date().isoformat(), request['notes'], self.now()))
            else:
                before = db.execute('SELECT * FROM events WHERE scope=? AND id=?', (scope, request['id'])).fetchone()
                if before is None:
                    fail('CONFLICT', '这条事件已不存在或不属于所选会话。')
                self.cas(request['revision'], self.row_revision(scope, dict(before), stamp))
                if action == 'event_delete':
                    db.execute('DELETE FROM events WHERE scope=? AND id=?', (scope, request['id']))
                else:
                    db.execute('UPDATE events SET title=?,detail=?,core=?,importance=?,keywords=? WHERE scope=? AND id=?',
                               (request['title'], request['detail'], request['core'], request['importance'],
                                packed(request['keywords']).decode('utf-8'), scope, request['id']))
            self.runtime_same(runtime)
            excluded = {str(self.memory.relative_to(self.root)), str(self.memory.relative_to(self.root)) + '-wal'}
            self.source_except(source, excluded)
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
        self.runtime_same(runtime)
        self.source_except(source, excluded)
        result = {}
        if action in ('event_update', 'event_delete'):
            try:
                self.semantic_module().invalidate_index(self.data, scope, request['id'], action)
            except Exception:
                # The fact transaction already committed. A derived cache failure
                # must not pretend it rolled back or block emergency controls.
                # Every semantic candidate is still checked against current facts.
                result['warning'] = '事件修改已提交；向量索引失效暂未完成，待下次同步。召回会重新核验当前事件，不使用已删或已改的旧正文。'
        if action == 'style_save':
            result['revision'] = self.style_get(scope)['revision']
        if action == 'event_update':
            with self.db() as read:
                row = dict(read.execute('SELECT * FROM events WHERE scope=? AND id=?', (scope, request['id'])).fetchone())
            result['revision'] = self.row_revision(scope, row, self.db_stamp(self.memory))
        return result

    def _service_start(self, runtime, finalize=None):
        cid = runtime['astrbot']['Id']
        original_marker = self.raw(self.marker, 65536) if self.marker.exists() else None
        self.runtime_same(runtime)
        try:
            self.runtime.start(cid)
            after = self.runtime_state()
            if (after['astrbot']['Id'] != cid or not after['astrbot']['State']['Running']
                    or after['napcat'] != runtime['napcat']):
                fail('UNKNOWN_STATE', '启动结果无法核验。')
            # Identity already revalidated; no mark removal until running observed.
            if self.marker.exists():
                self.marker.unlink()
                fsync_dir(self.root)
            # Completion auditing is part of startup, not a fallible step after
            # the compensation scope. An audit failure must not leave QQ live.
            if finalize is not None:
                finalize()
            return {'service': self.public_service(after),
                    'warning': '已启动聊天进程，可能处理旧队列与积压日评。QQ在线与模型可用性尚需分别核验。'}
        except Exception:
            # Best-effort stop ONLY our same verified CID. Never start on failure.
            try:
                after = self._inspect('astrbot')
                if after['Id'] != cid:
                    fail()
                if after['State']['Running']:
                    self.runtime.stop(cid)
                stopped = self._inspect('astrbot')
                if stopped['Id'] != cid or stopped['State']['Running']:
                    fail()
                if original_marker is not None:
                    atomic(self.marker, original_marker)
                else:
                    atomic(self.marker, b'Whale manager: startup failed; service stopped.\n')
            except Exception:
                fail('RESULT_UNKNOWN', '启动失败且无法确认已停止；不要重试启用，请人工核验固定容器。')
            fail('START_FAILED', '启动未成功完成，已核验固定容器停止；没有自动恢复。')

    def emergency_stop(self, request):
        # Caller holds the operation lock. Do not read robot config/database or
        # replace an earlier pending intent: stopping is a safety-only action.
        astrbot = self._inspect('astrbot')
        service = self.public_service({'astrbot': astrbot})
        self.cas(request['revision'], service['id'] + ':' + service['started_at'])
        audit_dir = self.manager / 'stops'
        self.path_check(audit_dir, required=False)
        private_dir(audit_dir)
        audit = audit_dir / (uuid.uuid4().hex + '.json')
        record = {'action': 'service_stop', 'created_at': self.now(), 'status': 'pending', 'cid': service['id']}
        atomic(audit, packed(record))
        if self._inspect('astrbot') != astrbot:
            fail('CONFLICT', 'AstrBot状态指纹已变化；未自动恢复或启动。')
        if service['state'] == 'running':
            self.runtime.stop(service['id'])
        after = self._inspect('astrbot')
        if after['Id'] != service['id'] or after['State']['Running']:
            fail('RESULT_UNKNOWN', '无法确认固定容器已停止；未尝试启动回滚。')
        atomic(self.marker, b'Whale manager: explicitly stopped by user.\n')
        record['status'] = 'complete'
        atomic(audit, packed(record))
        return {'service': self.public_service({'astrbot': after})}

    def write_action(self, action, request):
        self._validate_write(action, request)
        self.path_check(self.manager, required=False)
        with self.lock_factory(self.manager / 'operation.lock'):
            if action == 'service_stop':
                return self.emergency_stop(request)
            if self.intent.exists() and self.json(self.intent).get('status') != 'complete':
                fail('RECOVERY_REQUIRED', '上次操作未确认完成；请保持暂停并人工核验，不能直接重试。')
            runtime = self.runtime_state()
            service = self.public_service(runtime)
            if service['state'] != 'stopped':
                fail('RUNNING', '必须先停止AstrBot，保存不会自动启动。')
            if action in ('backup', 'service_start'):
                self.cas(request['revision'], service['id'] + ':' + service['started_at'])
            self.cold_health()
            source = self.source_state()
            if action == 'personality_save':
                self.cas(request['revision'], self.personality()['revision'])
            if action in ('style_save', 'event_update', 'event_delete'):
                scope = self.scope(request.get('scope'))
                if action == 'style_save':
                    self.cas(request['revision'], self.style_get(scope)['revision'])
                else:
                    with self.db() as db:
                        row = db.execute('SELECT * FROM events WHERE scope=? AND id=?', (scope, request['id'])).fetchone()
                    if row is None:
                        fail('CONFLICT', '事件不存在或不属于所选会话。')
                    self.cas(request['revision'], self.row_revision(scope, dict(row), self.db_stamp(self.memory)))
            intent = {'operation_id': uuid.uuid4().hex, 'action': action, 'created_at': self.now(), 'status': 'pending'}
            atomic(self.intent, packed(intent))
            # A failure from here is deliberately left pending: there is no
            # cross-resource atomic commit. Maintenance compares snapshot/state.
            self.source_same(source)
            self.runtime_same(runtime)
            result = self.snapshot(action, runtime, source)
            self.source_same(source)
            self.runtime_same(runtime)
            if action == 'personality_save':
                old = self.raw(self.config)
                private = self.manager / 'private-config'
                self.path_check(private, required=False)
                private_dir(private)
                atomic(private / (intent['operation_id'] + '.json'), old)
                value = parse_json(old)
                value['base_style'] = request['text']
                self.cas(request['revision'], self.personality()['revision'])
                self.source_same(source)
                self.runtime_same(runtime)
                atomic(self.config, packed(value) + b'\n', stat.S_IMODE(self.config.stat().st_mode))
                self.runtime_same(runtime)
                self.source_except(source, {str(self.config.relative_to(self.root))})
                result['revision'] = self.personality()['revision']
            elif action in ('style_save', 'event_update', 'event_delete'):
                result.update(self._save_db(action, request, runtime, source))
            elif action == 'service_start':
                def finalize_start():
                    intent['status'] = 'complete'
                    atomic(self.intent, packed(intent))
                try:
                    return self._service_start(runtime, finalize_start)
                except Exception:
                    # atomic() may have replaced the record before a failed
                    # directory fsync. Keep that ambiguity visible for review.
                    intent['status'] = 'pending'
                    try:
                        atomic(self.intent, packed(intent))
                    except Exception:
                        pass
                    raise
            if action != 'backup':
                result.pop('created_at', None)
            intent['status'] = 'complete'
            atomic(self.intent, packed(intent))
            return result

    def handle(self, request):
        try:
            if type(request) is not dict:
                fail('VALIDATION', '请求必须是JSON对象。')
            action = request.get('action')
            if not isinstance(action, str) or action not in FIELDS or set(request) - set(FIELDS[action]) - {'action'}:
                fail('VALIDATION', '操作或字段不在允许范围内。')
            if action in CONFIRM:
                result = self.write_action(action, request)
            elif action == 'status':
                result = self.status()
            elif action == 'personality':
                result = self.personality()
            elif action == 'style_get':
                result = self.style_get(request.get('scope'))
            elif action == 'memory_list':
                result = self.memory_list(request)
            else:
                result = self.backups()
            return {'ok': True, 'data': result}
        except SafeError as error:
            return {'ok': False, 'error': {'code': error.code, 'message': error.message}}
        except Exception:
            # Never emit exception text, filenames, SQL, commands, credentials,
            # Docker Env, response bodies or logs to the manager/browser.
            return {'ok': False, 'error': {'code': 'UNAVAILABLE',
                    'message': '读取或操作失败；未自动修复或恢复服务。若写入中断，请保持暂停并人工核验。'}}


def main():
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            fail('TOO_LARGE', '请求超过大小限制。')
        request = parse_json(raw)
        response = Controller().handle(request)
    except SafeError as error:
        response = {'ok': False, 'error': {'code': error.code, 'message': error.message}}
    except Exception:
        response = {'ok': False, 'error': {'code': 'INVALID_REQUEST', 'message': '无法读取请求。'}}
    sys.stdout.buffer.write(packed(response) + b'\n')


if __name__ == '__main__':
    main()
