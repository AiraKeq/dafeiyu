"""Read-only fact projections and disposable semantic-index management.

This module deliberately imports only the Python 3.6 standard library. It never
constructs MemoryStore, runs lifecycle writes, loads a model or sqlite-vec, or
opens a live SQLite file with SQLite. DB/WAL are physically copied before reads;
SHM is created only in the private copy; live reader marks are not fact revisions.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import tempfile
import time

MODEL_ID = 'BAAI/bge-small-zh-v1.5'
DIMENSION = 512
MODEL_REVISION = '46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59'
INDEX_NAME = 'semantic-index.sqlite3'
INDEX_SCHEMA = 1
INDEX_APP_ID = 0x57565331
DAY = 86400.0
SHANGHAI = timezone(timedelta(hours=8), name='Asia/Shanghai')
EVENT_FIELDS = ('id', 'scope', 'day', 'title', 'detail', 'core', 'importance',
                'participants', 'keywords', 'ts', 'tier', 'created_at')
EVENT_TYPES = ('INTEGER', 'TEXT', 'TEXT', 'TEXT', 'TEXT', 'TEXT', 'REAL',
               'TEXT', 'TEXT', 'REAL', 'TEXT', 'REAL')

# Ordinary tables, readable/writable by the host's standard SQLite 3.6 binding.
VECTORS_DDL = '''CREATE TABLE vectors (
    scope TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    document_hash TEXT NOT NULL,
    embedding BLOB NOT NULL,
    PRIMARY KEY(scope,event_id)
)'''
META_DDL = '''CREATE TABLE index_meta (
    id INTEGER PRIMARY KEY CHECK(id=1),
    schema_version INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    model_revision TEXT NOT NULL,
    sqlite_vec_version TEXT NOT NULL,
    generation INTEGER NOT NULL,
    last_sync REAL,
    source_signature TEXT,
    error_type TEXT
)'''


class SemanticStateError(RuntimeError):
    """A bounded machine-readable error; never contains facts or file paths."""
    def __init__(self, error_type):
        self.error_type = error_type
        RuntimeError.__init__(self, error_type)


def _epoch(value=None):
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=SHANGHAI)
        value = value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('invalid_timestamp')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('invalid_timestamp')
    return value


def _policy(active_days, important_threshold):
    if isinstance(active_days, bool):
        raise ValueError('invalid_policy')
    days = max(1, int(active_days))
    threshold = float(important_threshold)
    if isinstance(important_threshold, bool) or not math.isfinite(threshold) or not 0 <= threshold <= 10:
        raise ValueError('invalid_policy')
    return days, threshold


def _validated_row(row):
    try:
        result = dict(row)
        if any(key not in result for key in EVENT_FIELDS):
            raise ValueError()
        if type(result['id']) is not int or result['id'] <= 0:
            raise ValueError()
        for key in ('scope', 'day', 'title', 'detail', 'core', 'participants', 'keywords', 'tier'):
            if not isinstance(result[key], str):
                raise ValueError()
        if not result['scope'] or result['tier'] not in ('active', 'archive'):
            raise ValueError()
        for key in ('ts', 'created_at', 'importance'):
            value = result[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError()
        if not 0 <= result['importance'] <= 10:
            raise ValueError()
        for key in ('participants', 'keywords'):
            labels = json.loads(result[key])
            if not isinstance(labels, list) or any(not isinstance(item, str) for item in labels):
                raise ValueError()
        return result
    except (TypeError, ValueError, OverflowError):
        raise SemanticStateError('source_row_invalid')


def project_event(row, now, active_days=14, important_threshold=8):
    """Return the current, non-mutating lifecycle view, or None when forgotten."""
    timestamp = _epoch(now)
    days, threshold = _policy(active_days, important_threshold)
    result = _validated_row(row)
    age = max(0.0, timestamp - result['ts'])
    if age >= days * DAY and result['importance'] < threshold:
        return None
    result['core'] = result['core'][:160]
    if result['tier'] == 'archive' or age >= days * DAY:
        result['tier'] = 'archive'
        result['detail'] = result['core']
    else:
        width = 160 if age >= 7 * DAY else 320 if age >= 3 * DAY else 640
        result['detail'] = result['detail'][:width]
    return result


def _digest(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def event_fingerprint(row, now, active_days=14, important_threshold=8):
    projection = project_event(row, now, active_days, important_threshold)
    if projection is None:
        return None
    source = dict(row)
    # Full source identity includes even text outside the embedding's truncation.
    # Neither wall-clock seconds nor the derived R score belong in this digest.
    return _digest({'source': {key: source[key] for key in EVENT_FIELDS},
                    'projection': {key: projection[key] for key in ('title', 'core', 'detail', 'tier')},
                    'model': MODEL_ID, 'revision': MODEL_REVISION, 'dimension': DIMENSION})


def document_text(row, now, active_days=14, important_threshold=8):
    projection = project_event(row, now, active_days, important_threshold)
    if projection is None:
        return None
    # Core first: FastEmbed's model may truncate at 512 tokens. This is an
    # abstract/effective-detail index, never a full-message or fact backup.
    parts = [projection['core'], projection['title']]
    if projection['detail'] != projection['core']:
        parts.append(projection['detail'])
    return '\n'.join(part for part in parts if part)[:1000]


def _file_signature(path):
    try:
        before = path.stat()
    except FileNotFoundError:
        return None
    if not path.is_file():
        raise SemanticStateError('snapshot_unavailable')
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    after = path.stat()
    def identity(stat):
        return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino, stat.st_dev)
    if identity(before) != identity(after):
        raise SemanticStateError('snapshot_changed')
    return identity(after), digest.hexdigest()


def _manifest(path):
    # SHM contains live reader marks, not committed facts. Other read-only
    # connections may change it while DB/WAL stay stable; using it as a content
    # revision would spuriously reject concurrent prompt reads. Never copy or
    # open the production SHM; DB/WAL hashes and the hot-journal guard suffice.
    return {suffix: _file_signature(Path(str(path) + suffix))
            for suffix in ('', '-wal', '-journal')}


@contextmanager
def _snapshot_connection(path, attempts=2, missing_type='source_missing'):
    """Never hand a live path to SQLite, including immutable=1 or backup()."""
    path = Path(path)
    attempts = min(5, max(1, int(attempts)))
    private = None
    connection = None
    try:
        for attempt in range(attempts):
            try:
                before = _manifest(path)
                if before[''] is None:
                    raise SemanticStateError(missing_type)
                # An outstanding rollback journal may describe a hot transaction.
                if before['-journal'] is not None and before['-journal'][0][0] > 0:
                    raise SemanticStateError('snapshot_changed')
                private = tempfile.TemporaryDirectory(prefix='whale-semantic-')
                copied = Path(private.name) / 'snapshot.sqlite3'
                for suffix in ('', '-wal'):
                    if before[suffix] is not None:
                        target = Path(str(copied) + suffix)
                        shutil.copyfile(str(path) + suffix, str(target))
                        signature = _file_signature(target)
                        if signature is None or signature[1] != before[suffix][1]:
                            raise SemanticStateError('snapshot_changed')
                if before != _manifest(path):
                    raise SemanticStateError('snapshot_changed')
                connection = sqlite3.connect(copied.resolve().as_uri() + '?mode=ro',
                                             uri=True, timeout=1, isolation_level=None)
                connection.row_factory = sqlite3.Row
                connection.execute('PRAGMA query_only=ON')
                connection.execute('BEGIN')
                # Pin/recover only the private copy's WAL snapshot before yielding.
                connection.execute('SELECT count(*) FROM sqlite_master').fetchone()
                break
            except (OSError, sqlite3.Error, SemanticStateError) as exc:
                if connection is not None:
                    connection.close()
                    connection = None
                if private is not None:
                    private.cleanup()
                    private = None
                if isinstance(exc, SemanticStateError) and exc.error_type == missing_type:
                    raise
                if attempt + 1 >= attempts:
                    if isinstance(exc, SemanticStateError):
                        raise
                    raise SemanticStateError('snapshot_unavailable')
        yield connection
    finally:
        if connection is not None:
            connection.close()
        if private is not None:
            private.cleanup()


def _validate_source(connection):
    if connection.execute('PRAGMA user_version').fetchone()[0] != 1:
        raise SemanticStateError('source_schema_mismatch')
    table = connection.execute("SELECT type,sql FROM sqlite_master WHERE name='events'").fetchone()
    columns = connection.execute('PRAGMA table_info(events)').fetchall()
    if (table is None or table['type'] != 'table' or not table['sql'] or
            'virtual' in table['sql'].lower() or
            tuple(item['name'] for item in columns) != EVENT_FIELDS or
            tuple(item['type'].upper() for item in columns) != EVENT_TYPES or
            columns[0]['pk'] != 1):
        raise SemanticStateError('source_schema_mismatch')


def read_events(db_path, scope=None, attempts=2):
    """Read a stable physical DB/WAL copy; failures cannot create a source DB."""
    if scope is not None and not isinstance(scope, str):
        raise ValueError('invalid_scope')
    try:
        with _snapshot_connection(db_path, attempts) as connection:
            _validate_source(connection)
            sql = 'SELECT ' + ','.join(EVENT_FIELDS) + ' FROM events'
            args = ()
            if scope is not None:
                sql += ' WHERE scope=?'
                args = (scope,)
            sql += ' ORDER BY ts DESC,id DESC'
            return [_validated_row(row) for row in connection.execute(sql, args)]
    except sqlite3.Error:
        raise SemanticStateError('source_unavailable')


def _tokens(text):
    text = text.casefold()
    result = set(re.findall(r'[a-z0-9_]+', text))
    for run in re.findall(r'[\u3400-\u4dbf\u4e00-\u9fff]+', text):
        if len(run) == 1:
            result.add(run)
        else:
            result.update(run[index:index + 2] for index in range(len(run) - 1))
    return result


def _relevance(query, query_tokens, event):
    if not query.strip():
        return 0.0
    text = ' '.join((event['title'], event['core'], event['detail']))
    overlap = len(query_tokens & _tokens(text)) / max(1, len(query_tokens))
    hits = [word for word in json.loads(event['keywords'])
            if len(word) >= 2 and word.casefold() in query.casefold()]
    if hits:
        overlap = max(overlap, 0.65)
    return min(1.0, overlap)


def render_recall(rows, scope, query, now, active_days=14, important_threshold=8,
                  half_life_days=4, max_chars=2200, limit=8, semantic_scores=None,
                  min_similarity=0.70):
    """Blend verified semantics with lexical relevance, retaining I/R budgets."""
    timestamp = _epoch(now)
    half_life = float(half_life_days)
    threshold = float(min_similarity)
    if not math.isfinite(half_life) or half_life <= 0:
        raise ValueError('invalid_half_life')
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError('invalid_similarity')
    if not isinstance(scope, str) or not isinstance(query, str):
        raise ValueError('invalid_query')
    max_chars = min(2200, max(0, int(max_chars)))
    limit = min(8, max(0, int(limit)))
    if not max_chars or not limit:
        return ''
    query_tokens = _tokens(query)
    scores = semantic_scores if isinstance(semantic_scores, dict) else {}
    relevant, recent_candidates = [], []
    for source in rows:
        if source['scope'] != scope:
            continue
        event = project_event(source, timestamp, active_days, important_threshold)
        if event is None:
            continue
        relevance = _relevance(query, query_tokens, event)
        candidate = scores.get(event['id'])
        if query.strip() and isinstance(candidate, dict):
            similarity = candidate.get('similarity')
            if (not isinstance(similarity, bool) and isinstance(similarity, (int, float)) and
                    math.isfinite(similarity) and threshold <= similarity <= 1.0 and
                    candidate.get('fingerprint') == event_fingerprint(
                        source, timestamp, active_days, important_threshold)):
                relevance = max(relevance, float(similarity))
        age = max(0.0, timestamp - event['ts'])
        recency = 10.0 * (2.0 ** (-age / DAY / half_life))
        item = (0.5 * relevance + 0.3 * event['importance'] / 10 + 0.2 * recency / 10,
                event['ts'], event['id'], event, recency)
        if relevance > 0:
            relevant.append(item)
        if event['tier'] == 'active' and age < 3 * DAY:
            recent_candidates.append(item)
    relevant.sort(key=lambda item: item[:3], reverse=True)
    recent_candidates.sort(key=lambda item: (item[1], item[2]), reverse=True)
    recent = recent_candidates[:2]
    selected, seen = [], set()
    relevant_count = min(len(relevant), max(1, limit - len(recent)))
    for item in relevant[:relevant_count] + recent + relevant[relevant_count:]:
        if item[2] not in seen:
            selected.append(item)
            seen.add(item[2])
        if len(selected) >= limit:
            break
    lines, used = [], 0
    for _, _, _, event, recency in selected:
        detail = event['core'] if event['tier'] == 'archive' else event['detail']
        if event['core'] not in detail:
            detail = event['core'] + '；' + detail
        detail = re.sub(r'\s+', ' ', detail)
        title = re.sub(r'\s+', ' ', event['title'])
        tier = '长期核心' if event['tier'] == 'archive' else '近期'
        metadata = '[事件 {}｜{}｜{}｜I={:.1f}/10｜R={:.2f}/10] '.format(
            event['id'], event['day'], tier, event['importance'], recency)
        remaining = max_chars - used - (1 if lines else 0)
        # Never cut I/R or metadata in half; tiny budgets have an empty result.
        if remaining < len(metadata):
            break
        line = (metadata + title + '：' + detail)[:remaining]
        used += len(line) + (1 if lines else 0)
        lines.append(line)
    return '\n'.join(lines)


def _normalized_sql(value):
    return re.sub(r'\s+', '', value or '').rstrip(';').lower()


def _is_digest(value):
    return isinstance(value, str) and re.match(r'^[0-9a-f]{64}$', value) is not None


def _validate_index(connection):
    """Exact owned schema/identity: no repair and no extension needed."""
    if (connection.execute('PRAGMA application_id').fetchone()[0] != INDEX_APP_ID or
            connection.execute('PRAGMA user_version').fetchone()[0] != INDEX_SCHEMA):
        raise SemanticStateError('index_schema_mismatch')
    objects = connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    expected = {'vectors': VECTORS_DDL, 'index_meta': META_DDL}
    if (len(objects) != 2 or any(row['type'] != 'table' or row['name'] not in expected or
            _normalized_sql(row['sql']) != _normalized_sql(expected[row['name']]) for row in objects)):
        raise SemanticStateError('index_schema_mismatch')
    rows = connection.execute('SELECT * FROM index_meta').fetchall()
    if len(rows) != 1 or rows[0]['id'] != 1:
        raise SemanticStateError('index_metadata_invalid')
    meta = dict(rows[0])
    if (meta['schema_version'] != INDEX_SCHEMA or meta['model_id'] != MODEL_ID or
            meta['model_revision'] != MODEL_REVISION or meta['dimension'] != DIMENSION):
        raise SemanticStateError('index_model_mismatch')
    if (type(meta['generation']) is not int or meta['generation'] < 0 or
            not isinstance(meta['sqlite_vec_version'], str) or not meta['sqlite_vec_version'] or
            len(meta['sqlite_vec_version']) > 64 or
            (meta['last_sync'] is not None and (not isinstance(meta['last_sync'], (int, float)) or
             not math.isfinite(meta['last_sync']) or meta['last_sync'] < 0)) or
            (meta['source_signature'] is not None and not _is_digest(meta['source_signature'])) or
            meta['error_type'] not in (None, 'stale')):
        raise SemanticStateError('index_metadata_invalid')
    return meta


def _vector_values(value, dimension=DIMENSION):
    """Validate fixed-dimensional unit float32 vectors without any model import."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        if len(value) != dimension * 4:
            raise SemanticStateError('vector_dimension_mismatch')
        values = struct.unpack('<{}f'.format(dimension), value)
    else:
        if not isinstance(value, (list, tuple)) or len(value) != dimension:
            raise SemanticStateError('vector_dimension_mismatch')
        values = value
    try:
        if any(isinstance(number, bool) or not isinstance(number, (int, float)) or
               not math.isfinite(number) for number in values):
            raise SemanticStateError('vector_invalid')
        norm_squared = sum(number * number for number in values)
        if not math.isfinite(norm_squared) or abs(norm_squared - 1.0) > 0.002:
            raise SemanticStateError('vector_not_normalized')
    except (TypeError, ValueError, OverflowError):
        raise SemanticStateError('vector_invalid')
    return values


def _valid_vector_row(row):
    if (not isinstance(row['scope'], str) or not row['scope'] or
            type(row['event_id']) is not int or row['event_id'] <= 0 or
            not _is_digest(row['fingerprint']) or not _is_digest(row['document_hash']) or
            not isinstance(row['embedding'], bytes)):
        raise SemanticStateError('index_row_invalid')
    _vector_values(row['embedding'])


def _eligible(rows, now, active_days=14, important_threshold=8):
    result = {}
    for row in rows:
        fingerprint = event_fingerprint(row, now, active_days, important_threshold)
        if fingerprint is not None:
            result[(row['scope'], row['id'])] = fingerprint
    return result


def _source_signature(eligible):
    return _digest([[scope, event_id, eligible[(scope, event_id)]]
                    for scope, event_id in sorted(eligible)])


def index_status(data_dir, now=None, active_days=14, important_threshold=8):
    result = {'state': 'unavailable', 'engine': 'sqlite-vec', 'model': MODEL_ID,
              'dimensions': DIMENSION, 'indexed_events': None, 'effective_events': None,
              'pending_events': None, 'last_sync': None, 'error_type': None}
    data_dir = Path(data_dir)
    try:
        timestamp = _epoch(now)
        rows = read_events(data_dir / 'memory.sqlite3')
        eligible = _eligible(rows, timestamp, active_days, important_threshold)
        document_hashes = {
            (row['scope'], row['id']): hashlib.sha256(document_text(
                row, timestamp, active_days, important_threshold).encode('utf-8')).hexdigest()
            for row in rows if (row['scope'], row['id']) in eligible}
        result['effective_events'] = len(eligible)
    except (SemanticStateError, ValueError, OSError, sqlite3.Error) as exc:
        eligible = None
        result['error_type'] = getattr(exc, 'error_type', 'source_unavailable')
    if not (data_dir / INDEX_NAME).is_file():
        result['state'] = 'missing'
        if eligible is not None:
            result['indexed_events'] = 0
            result['pending_events'] = len(eligible)
        return result
    try:
        with _snapshot_connection(data_dir / INDEX_NAME, missing_type='index_missing') as connection:
            meta = _validate_index(connection)
            result['last_sync'] = meta['last_sync']
            vectors = connection.execute('SELECT * FROM vectors').fetchall()
            if eligible is None:
                return result
            matched = 0
            invalid = False
            for row in vectors:
                try:
                    _valid_vector_row(row)
                except SemanticStateError:
                    invalid = True
                    continue
                key = (row['scope'], row['event_id'])
                if (eligible.get(key) == row['fingerprint'] and
                        document_hashes.get(key) == row['document_hash']):
                    matched += 1
            result['indexed_events'] = matched
            result['pending_events'] = len(eligible) - matched
            ready = (not invalid and matched == len(eligible) == len(vectors) and
                     meta['source_signature'] == _source_signature(eligible) and
                     meta['error_type'] is None and meta['last_sync'] is not None)
            result['state'] = 'ready' if ready else 'stale'
            result['error_type'] = 'vector_invalid' if invalid else None
    except (SemanticStateError, OSError, sqlite3.Error) as exc:
        result['state'] = 'unavailable'
        result['error_type'] = getattr(exc, 'error_type', 'index_unavailable')
    return result


def _check_index_path(path, source_path):
    # A side-index symlink/hardlink must never turn an invalidation into a live
    # SQLite open on the facts file (which could write its WAL shared memory).
    try:
        if path.is_symlink() or (path.exists() and source_path.exists() and path.samefile(source_path)):
            raise SemanticStateError('index_path_unsafe')
    except OSError:
        raise SemanticStateError('index_unavailable')


def invalidate_index(data_dir, scope, event_id, reason='source_change'):
    """Standard-SQLite, post-fact-commit invalidation; never creates an index."""
    if not isinstance(scope, str) or not scope or type(event_id) is not int or event_id <= 0:
        raise ValueError('invalid_event_identity')
    path = Path(data_dir) / INDEX_NAME
    _check_index_path(path, Path(data_dir) / 'memory.sqlite3')
    if not path.exists():
        return True
    connection = None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True,
                                     timeout=0.25, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=250')
        connection.execute('BEGIN IMMEDIATE')
        _validate_index(connection)
        connection.execute('DELETE FROM vectors WHERE scope=? AND event_id=?', (scope, event_id))
        connection.execute("UPDATE index_meta SET error_type='stale',source_signature=NULL,"
                           'generation=generation+1 WHERE id=1')
        connection.commit()
        return True
    except (sqlite3.Error, OSError) as exc:
        raise SemanticStateError('index_invalidation_failed')
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
