"""Disposable semantic vectors; only the independent worker uses this module.

No third-party import, filesystem write, or model invocation at import or object
construction. sqlite-vec is lazy-loaded for its mature native cosine-distance
kernel. The facts database is never opened by SQLite or modified here.
"""
import hashlib
from pathlib import Path
import sqlite3
import struct

try:
    from . import semantic_state as state
except ImportError:
    import semantic_state as state


class SemanticIndexError(state.SemanticStateError):
    pass


def _load_vec(connection):
    try:
        import sqlite_vec
    except ImportError:
        raise SemanticIndexError('sqlite_vec_missing')
    except Exception:
        raise SemanticIndexError('sqlite_vec_unavailable')
    try:
        connection.enable_load_extension(True)
        try:
            sqlite_vec.load(connection)
        finally:
            connection.enable_load_extension(False)
        version = connection.execute('SELECT vec_version()').fetchone()[0]
        if not isinstance(version, str) or not version or len(version) > 64:
            raise SemanticIndexError('sqlite_vec_unavailable')
        return version
    except Exception:
        raise SemanticIndexError('sqlite_vec_unavailable')


def _document_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _blob(vector):
    values = state._vector_values(vector)
    try:
        blob = struct.pack('<{}f'.format(state.DIMENSION), *values)
    except (struct.error, OverflowError):
        raise SemanticIndexError('vector_invalid')
    state._vector_values(blob)
    return blob


class SemanticIndex:
    def __init__(self, data_dir, embedder, active_days=14, important_threshold=8,
                 dimension=512):
        if type(dimension) is not int or dimension != state.DIMENSION:
            raise SemanticIndexError('vector_dimension_mismatch')
        if not callable(embedder):
            raise ValueError('invalid_embedder')
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / state.INDEX_NAME
        self.source_path = self.data_dir / 'memory.sqlite3'
        self.embedder = embedder
        self.active_days, self.important_threshold = state._policy(active_days, important_threshold)
        self.dimension = dimension

    def _documents(self, rows, now):
        result = {}
        for row in rows:
            fingerprint = state.event_fingerprint(row, now, self.active_days, self.important_threshold)
            if fingerprint is not None:
                text = state.document_text(row, now, self.active_days, self.important_threshold)
                result[(row['scope'], row['id'])] = {
                    'fingerprint': fingerprint, 'document_hash': _document_hash(text), 'text': text}
        return result

    def _existing(self):
        state._check_index_path(self.path, self.source_path)
        if not self.path.exists():
            return None, {}
        try:
            with state._snapshot_connection(self.path, missing_type='index_missing') as connection:
                meta = state._validate_index(connection)
                rows = {}
                for item in connection.execute('SELECT * FROM vectors'):
                    state._valid_vector_row(item)
                    row = dict(item)
                    rows[(row['scope'], row['event_id'])] = row
                return meta, rows
        except sqlite3.Error:
            raise SemanticIndexError('index_unavailable')

    def reconcile(self, now=None):
        """Incremental embedding, source recheck, then a short atomic publication.

        A source change during embedding is left pending, not relabeled with the
        new identity. Generation CAS prevents a second worker or invalidator
        from overwriting a newer publication. There is no claim of a globally
        atomic cross-database transaction: recall always rechecks source fp.
        """
        initial_time = state._epoch(now)
        initial = self._documents(state.read_events(self.source_path), initial_time)
        old_meta, old = self._existing()
        # Missing native dependency cannot leave behind an empty side database.
        kernel = sqlite3.connect(':memory:')
        try:
            version = _load_vec(kernel)
        finally:
            kernel.close()
        cache = {row['document_hash']: row['embedding'] for row in old.values()}
        pending_hashes = []
        pending_texts = []
        for key in sorted(initial):
            document = initial[key]
            doc_hash = document['document_hash']
            if doc_hash not in cache and doc_hash not in pending_hashes:
                pending_hashes.append(doc_hash)
                pending_texts.append(document['text'])
        # Keep inference batches small on the low-memory CPU worker; no source
        # or side-index transaction is held while the injected model runs.
        for offset in range(0, len(pending_texts), 16):
            texts = pending_texts[offset:offset + 16]
            try:
                vectors = self.embedder(texts)
            except Exception:
                raise SemanticIndexError('embedding_failed')
            if not isinstance(vectors, (list, tuple)) or len(vectors) != len(texts):
                raise SemanticIndexError('embedding_count_mismatch')
            for doc_hash, vector in zip(pending_hashes[offset:offset + 16], vectors):
                cache[doc_hash] = _blob(vector)
        final_time = state._epoch(now)
        current = self._documents(state.read_events(self.source_path), final_time)
        published = {}
        for key, document in current.items():
            initial_document = initial.get(key)
            if initial_document is not None and initial_document['fingerprint'] == document['fingerprint']:
                blob = cache.get(document['document_hash'])
            else:
                # A previously indexed still-current entry can remain even when
                # another source row was added/changed during model inference.
                previous = old.get(key)
                blob = (previous['embedding'] if previous is not None and
                        previous['fingerprint'] == document['fingerprint'] and
                        previous['document_hash'] == document['document_hash'] else None)
            if blob is not None:
                published[key] = (document, blob)
        eligible = {key: document['fingerprint'] for key, document in current.items()}
        complete = len(published) == len(current)
        connection = None
        try:
            # This is the only code path that may create the derived index.
            # The data directory already exists because the source was read.
            state._check_index_path(self.path, self.source_path)
            if self.path.exists():
                connection = sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw',
                                             uri=True, timeout=0.25, isolation_level=None)
            else:
                connection = sqlite3.connect(str(self.path), timeout=0.25, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA busy_timeout=250')
            connection.execute('BEGIN IMMEDIATE')
            objects = connection.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if objects:
                live_meta = state._validate_index(connection)
                if old_meta is None or live_meta['generation'] != old_meta['generation']:
                    raise SemanticIndexError('index_concurrent_update')
            else:
                if old_meta is not None:
                    raise SemanticIndexError('index_concurrent_update')
                # No IF NOT EXISTS or silent rebuild of an unfamiliar database.
                if (connection.execute('PRAGMA application_id').fetchone()[0] != 0 or
                        connection.execute('PRAGMA user_version').fetchone()[0] != 0):
                    raise SemanticIndexError('index_schema_mismatch')
                connection.execute(state.VECTORS_DDL)
                connection.execute(state.META_DDL)
                connection.execute('PRAGMA application_id={}'.format(state.INDEX_APP_ID))
                connection.execute('PRAGMA user_version={}'.format(state.INDEX_SCHEMA))
            # Diff publication also removes expired, changed and orphan vectors.
            for item in connection.execute('SELECT scope,event_id,fingerprint,document_hash FROM vectors').fetchall():
                key = (item['scope'], item['event_id'])
                replacement = published.get(key)
                if (replacement is None or item['fingerprint'] != replacement[0]['fingerprint'] or
                        item['document_hash'] != replacement[0]['document_hash']):
                    connection.execute('DELETE FROM vectors WHERE scope=? AND event_id=?', key)
            for (scope, event_id), (document, blob) in published.items():
                connection.execute(
                    'INSERT OR REPLACE INTO vectors(scope,event_id,fingerprint,document_hash,embedding) '
                    'VALUES(?,?,?,?,?)',
                    (scope, event_id, document['fingerprint'], document['document_hash'], sqlite3.Binary(blob)))
            generation = 1 if old_meta is None else old_meta['generation'] + 1
            connection.execute(
                'INSERT OR REPLACE INTO index_meta(id,schema_version,model_id,dimension,model_revision,'
                'sqlite_vec_version,generation,last_sync,source_signature,error_type) '
                'VALUES(1,?,?,?,?,?,?,?,?,?)',
                (state.INDEX_SCHEMA, state.MODEL_ID, self.dimension, state.MODEL_REVISION,
                 version, generation, final_time,
                 state._source_signature(eligible) if complete else None,
                 None if complete else 'stale'))
            connection.commit()
        except sqlite3.Error:
            raise SemanticIndexError('index_write_failed')
        except OSError:
            raise SemanticIndexError('index_unavailable')
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
        # State counters reflect a new read, not just this generation's old view.
        return state.index_status(self.data_dir, now=final_time,
                                  active_days=self.active_days, important_threshold=self.important_threshold)

    def search(self, scope, query_vector, limit=12, now=None):
        if not isinstance(scope, str) or not scope:
            raise ValueError('invalid_scope')
        query_blob = _blob(query_vector)
        limit = min(100, max(0, int(limit)))
        if not limit:
            return []
        timestamp = state._epoch(now)
        documents = self._documents(state.read_events(self.source_path, scope=scope), timestamp)
        try:
            with state._snapshot_connection(self.path, missing_type='index_missing') as connection:
                state._validate_index(connection)
                # Validate before native code: malformed BLOB/NaN must not reach
                # the distance function, even when a bad row is not the nearest.
                for item in connection.execute('SELECT * FROM vectors WHERE scope=?', (scope,)):
                    state._valid_vector_row(item)
                _load_vec(connection)
                rows = connection.execute(
                    'SELECT event_id,scope,fingerprint,document_hash,'
                    'vec_distance_cosine(embedding,?) AS distance FROM vectors '
                    'WHERE scope=? ORDER BY distance ASC,event_id ASC LIMIT ?',
                    (sqlite3.Binary(query_blob), scope, limit)).fetchall()
                candidates = []
                for row in rows:
                    document = documents.get((row['scope'], row['event_id']))
                    if (document is None or document['fingerprint'] != row['fingerprint'] or
                            document['document_hash'] != row['document_hash']):
                        continue
                    distance = row['distance']
                    if (not isinstance(distance, (int, float)) or
                            not state.math.isfinite(distance) or not -0.00001 <= distance <= 2.00001):
                        raise SemanticIndexError('distance_invalid')
                    candidates.append({'event_id': row['event_id'], 'scope': row['scope'],
                                       'fingerprint': row['fingerprint'],
                                       'similarity': max(-1.0, min(1.0, 1.0 - distance))})
                return candidates
        except sqlite3.Error:
            raise SemanticIndexError('index_search_failed')
