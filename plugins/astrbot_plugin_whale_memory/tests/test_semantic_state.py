"""Synthetic facts only; no credentials, production stores or embedding model."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import math
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import semantic_state as state

NOW = datetime(2026, 11, 30, 12, tzinfo=timezone.utc).timestamp()
SOURCE_DDL = '''CREATE TABLE events (
 id INTEGER PRIMARY KEY, scope TEXT NOT NULL, day TEXT NOT NULL,
 title TEXT NOT NULL, detail TEXT NOT NULL, core TEXT NOT NULL,
 importance REAL NOT NULL CHECK(importance>=0 AND importance<=10),
 participants TEXT NOT NULL, keywords TEXT NOT NULL, ts REAL NOT NULL,
 tier TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL
)'''


def event(event_id=1, scope='a', **changes):
    result = {'id': event_id, 'scope': scope, 'day': '2026-11-30',
              'title': '合成考试约定', 'detail': '大家计划一起复习并鼓励彼此。',
              'core': '朋友希望考试前相互鼓励。', 'importance': 8.0,
              'participants': '["101"]', 'keywords': '["考试","鼓励"]',
              'ts': NOW, 'tier': 'active', 'created_at': NOW}
    result.update(changes)
    return result


def unit(axis=0):
    result = [0.0] * state.DIMENSION
    result[axis] = 1.0
    return result


def manifest(path):
    # Exact physical state, including SHM, plus mtime/ctime. Reads ignore atime.
    result = {}
    for suffix in ('', '-wal', '-shm', '-journal'):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            stat = candidate.stat()
            result[suffix] = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                              hashlib.sha256(candidate.read_bytes()).hexdigest())
    return result


class SyntheticFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='whale-test-')
        self.data_dir = Path(self.temp.name)
        self.source = self.data_dir / 'memory.sqlite3'
        self.index_path = self.data_dir / state.INDEX_NAME
        with closing(sqlite3.connect(str(self.source))) as connection:
            connection.execute(SOURCE_DDL)
            connection.execute('PRAGMA user_version=1')
            connection.commit()

    def tearDown(self):
        self.temp.cleanup()

    def put(self, row=None, connection=None):
        row = row or event()
        if connection is not None:
            connection.execute('INSERT OR REPLACE INTO events (' + ','.join(state.EVENT_FIELDS) +
                               ') VALUES (' + ','.join('?' for _ in state.EVENT_FIELDS) + ')',
                               [row[key] for key in state.EVENT_FIELDS])
            connection.commit()
            return row
        with closing(sqlite3.connect(str(self.source))) as connection:
            self.put(row, connection)
        return row

    def change(self, sql, args=()):
        with closing(sqlite3.connect(str(self.source))) as connection:
            connection.execute(sql, args)
            connection.commit()

    def make_index(self, rows, now=NOW):
        eligible = state._eligible(rows, now)
        with closing(sqlite3.connect(str(self.index_path))) as connection:
            connection.execute(state.VECTORS_DDL)
            connection.execute(state.META_DDL)
            connection.execute('PRAGMA application_id={}'.format(state.INDEX_APP_ID))
            connection.execute('PRAGMA user_version={}'.format(state.INDEX_SCHEMA))
            connection.execute('INSERT INTO index_meta VALUES(1,?,?,?,?,?,1,?,?,NULL)',
                               (state.INDEX_SCHEMA, state.MODEL_ID, state.DIMENSION,
                                state.MODEL_REVISION, 'fixture-only', now,
                                state._source_signature(eligible)))
            for row in rows:
                fp = state.event_fingerprint(row, now)
                if fp is not None:
                    text = state.document_text(row, now)
                    connection.execute('INSERT INTO vectors VALUES(?,?,?,?,?)',
                                       (row['scope'], row['id'], fp,
                                        hashlib.sha256(text.encode('utf-8')).hexdigest(),
                                        struct.pack('<512f', *unit())))
            connection.commit()


class ProjectionTests(unittest.TestCase):
    def test_lifecycle_boundaries_and_source_is_not_mutated(self):
        for days, width in ((0, 640), (3 - 1 / state.DAY, 640), (3, 320),
                            (7 - 1 / state.DAY, 320), (7, 160)):
            row = event(ts=NOW - days * state.DAY, detail='详' * 900, core='核' * 300)
            original = dict(row)
            projected = state.project_event(row, NOW)
            self.assertEqual(len(projected['detail']), width)
            self.assertEqual(len(projected['core']), 160)
            self.assertEqual(row, original)
        self.assertIsNone(state.project_event(event(ts=NOW - 14 * state.DAY, importance=7.99), NOW))
        self.assertIsNotNone(state.project_event(event(ts=NOW - 14 * state.DAY + 1, importance=1), NOW))
        row = event(ts=NOW - 14 * state.DAY, detail='淘汰详情', core='重要核心')
        projected = state.project_event(row, NOW)
        self.assertEqual((projected['tier'], projected['detail']), ('archive', '重要核心'))
        self.assertEqual(row['detail'], '淘汰详情')
        self.assertIsNone(state.event_fingerprint(event(ts=NOW - 14 * state.DAY, importance=0), NOW))
        self.assertIsNone(state.document_text(event(ts=NOW - 14 * state.DAY, importance=0), NOW))

    def test_archived_projection_stays_core_only_and_policy_applies(self):
        projected = state.project_event(event(tier='archive', detail='不可回忆', core='只记核心'), NOW)
        self.assertEqual(projected['detail'], '只记核心')
        row = event(ts=NOW - 10 * state.DAY, importance=7)
        self.assertIsNone(state.project_event(row, NOW, active_days=10))
        self.assertEqual(state.project_event(row, NOW, active_days=10, important_threshold=7)['tier'], 'archive')

    def test_fingerprint_full_source_identity_and_projection_not_clock_R(self):
        row = event(ts=NOW - state.DAY, detail='原' * 1200)
        fp = state.event_fingerprint(row, NOW)
        self.assertEqual(fp, state.event_fingerprint(row, NOW + 1))
        changes = {'id': 2, 'scope': 'b', 'day': '2026-11-29', 'title': '新标题',
                   'detail': '原' * 1199 + '改', 'core': '新核', 'importance': 9,
                   'participants': '["202"]', 'keywords': '["不同"]',
                   'ts': row['ts'] + 1, 'tier': 'archive', 'created_at': NOW + 1}
        for key, value in changes.items():
            with self.subTest(field=key):
                changed = dict(row, **{key: value})
                self.assertNotEqual(fp, state.event_fingerprint(changed, NOW))
        self.assertNotEqual(fp, state.event_fingerprint(row, NOW + 2 * state.DAY))
        with mock.patch.object(state, 'MODEL_REVISION', 'new-revision'):
            self.assertNotEqual(fp, state.event_fingerprint(row, NOW))
        with mock.patch.object(state, 'DIMENSION', 513):
            self.assertNotEqual(fp, state.event_fingerprint(row, NOW))

    def test_document_core_first_bounded_and_old_details_absent(self):
        row = event(core='核心', title='标题', detail='详' * 2000)
        text = state.document_text(row, NOW)
        self.assertTrue(text.startswith('核心\n标题\n'))
        self.assertLessEqual(len(text), 1000)
        self.assertEqual(state.document_text(dict(row, ts=NOW - 20 * state.DAY), NOW), '核心\n标题')

    def test_invalid_rows_and_nonfinite_policy_are_rejected(self):
        for changes in ({'importance': math.nan}, {'ts': math.inf}, {'created_at': True},
                        {'keywords': '{}'}, {'participants': '[3]'}, {'tier': 'unknown'}, {'id': True}):
            with self.assertRaises(state.SemanticStateError):
                state.project_event(event(**changes), NOW)
        for kwargs in ({'now': math.nan}, {'now': True}, {'important_threshold': math.inf},
                       {'active_days': True}):
            values = {'now': NOW}
            values.update(kwargs)
            with self.assertRaises(ValueError):
                state.project_event(event(), **values)


class RecallTests(unittest.TestCase):
    def recall(self, rows, query='没有词法交集', **kwargs):
        return state.render_recall(rows, 'a', query, NOW, **kwargs)

    def scores(self, row, similarity=0.9):
        return {row['id']: {'fingerprint': state.event_fingerprint(row, NOW), 'similarity': similarity}}

    def test_semantic_match_requires_current_fp_scope_and_threshold(self):
        row = event(ts=NOW - 20 * state.DAY, title='长期计划', core='要一起准备期末测验', keywords='[]')
        self.assertEqual(self.recall([row]), '')
        self.assertIn('要一起准备期末测验', self.recall([row], semantic_scores=self.scores(row)))
        for similarity in (0.699999, math.nan, math.inf, -1, 1.001, True, '0.9'):
            self.assertEqual(self.recall([row], semantic_scores=self.scores(row, similarity)), '')
        self.assertIn('要一起准备', self.recall([row], semantic_scores=self.scores(row, 0.70)))
        changed = dict(row, created_at=row['created_at'] + 1)
        self.assertEqual(self.recall([changed], semantic_scores=self.scores(row)), '')
        self.assertEqual(self.recall([dict(row, scope='b')], semantic_scores=self.scores(row)), '')
        self.assertEqual(self.recall([], semantic_scores=self.scores(row)), '')
        self.assertEqual(self.recall([dict(row, importance=1)], semantic_scores=self.scores(row)), '')

    def test_old_detail_does_not_leak_through_lexical_or_semantic_recall(self):
        row = event(ts=NOW - 20 * state.DAY, title='旧事件', core='长期承诺',
                    detail='秘密旧详情', keywords='[]')
        self.assertEqual(self.recall([row], query='秘密旧详情'), '')
        text = self.recall([row], semantic_scores=self.scores(row))
        self.assertIn('长期承诺', text)
        self.assertNotIn('秘密旧详情', text)
        low = dict(row, importance=7)
        self.assertEqual(self.recall([low], query='长期承诺', semantic_scores=self.scores(row)), '')

    def test_recent_supplement_is_two_most_recent_independent_of_input_order(self):
        rows = [event(event_id=index, ts=NOW - index, title='近期' + str(index), keywords='[]')
                for index in range(1, 10)]
        text = self.recall(list(reversed(rows)))
        self.assertEqual(len(text.splitlines()), 2)
        self.assertIn('[事件 1｜', text)
        self.assertIn('[事件 2｜', text)
        self.assertEqual(self.recall([event(ts=NOW - 3 * state.DAY, keywords='[]')]), '')

    def test_IR_format_recency_and_exact_global_caps(self):
        rows = [event(event_id=index, ts=NOW - 4 * state.DAY, detail='详' * 100,
                      core='考试鼓励', importance=8) for index in range(1, 20)]
        text = self.recall(rows, query='考试', limit=100, max_chars=10000)
        self.assertEqual(len(text.splitlines()), 8)
        self.assertIn('I=8.0/10｜R=5.00/10', text)
        self.assertLessEqual(len(text), 2200)
        for budget in (0, 1, 30, 100, 250, 2200):
            result = self.recall(rows, query='考试', max_chars=budget)
            self.assertLessEqual(len(result), budget)
            for line in result.splitlines():
                self.assertIn('/10] ', line)
        self.assertEqual(self.recall(rows, query='考试', limit=0), '')

    def test_weighted_topical_selection_and_relevant_archive_only(self):
        strong = event(event_id=1, ts=NOW - 4 * state.DAY, importance=0,
                       title='无交集', core='无交集', detail='无交集', keywords='[]')
        high_i = event(event_id=2, ts=NOW - 4 * state.DAY, importance=10,
                       title='考试', core='考试', detail='考试', keywords='[]')
        text = self.recall([strong, high_i], query='考试', semantic_scores=self.scores(strong, 1.0), limit=1)
        self.assertTrue(text.startswith('[事件 2｜'))
        archive = event(ts=NOW - 30 * state.DAY)
        self.assertEqual(self.recall([archive], query='吃午饭'), '')
        self.assertIn('长期核心', self.recall([archive], query='考试'))


class ReadAndStatusTests(SyntheticFixture):
    def test_physical_read_with_live_wal_does_not_touch_db_wal_shm(self):
        with closing(sqlite3.connect(str(self.source))) as owner:
            owner.execute('PRAGMA journal_mode=WAL')
            self.put(event(), owner)
            self.assertTrue(Path(str(self.source) + '-wal').exists())
            self.assertTrue(Path(str(self.source) + '-shm').exists())
            before = manifest(self.source)
            self.assertEqual(len(state.read_events(self.source)), 1)
            self.assertEqual(state.read_events(self.source, scope='other'), [])
            self.assertEqual(before, manifest(self.source))
            status = state.index_status(self.data_dir, NOW)
            self.assertEqual(status['state'], 'missing')
            self.assertEqual(status['effective_events'], 1)
            self.assertEqual(before, manifest(self.source))

    def test_missing_unknown_and_corrupt_source_never_initialize(self):
        missing = self.data_dir / 'never-created.sqlite3'
        with self.assertRaises(state.SemanticStateError):
            state.read_events(missing)
        self.assertFalse(missing.exists())
        before = manifest(self.source)
        self.change('PRAGMA user_version=2')
        changed = manifest(self.source)
        with self.assertRaises(state.SemanticStateError) as captured:
            state.read_events(self.source)
        self.assertEqual(captured.exception.error_type, 'source_schema_mismatch')
        self.assertEqual(changed, manifest(self.source))
        self.assertNotEqual(before, changed)
        bad = self.data_dir / 'bad.sqlite3'
        bad.write_bytes(b'not sqlite')
        with self.assertRaises(state.SemanticStateError):
            state.read_events(bad)
        self.assertEqual(bad.read_bytes(), b'not sqlite')

    def test_snapshot_change_detected_retried_and_cannot_return_torn_rows(self):
        self.put()
        original = state._manifest
        calls = [0]
        def changing(path):
            result = original(path)
            calls[0] += 1
            if calls[0] % 2 == 0:
                result['-shm'] = ('synthetic-race', 'x')
            return result
        with mock.patch.object(state, '_manifest', side_effect=changing):
            with self.assertRaises(state.SemanticStateError) as captured:
                state.read_events(self.source, attempts=2)
        self.assertEqual(captured.exception.error_type, 'snapshot_changed')
        self.assertEqual(calls[0], 4)
        self.assertEqual(len(state.read_events(self.source)), 1)

    def test_status_readonly_counts_current_identity_not_all_blob_rows(self):
        rows = [self.put(event()), self.put(event(event_id=2, scope='b'))]
        self.make_index(rows)
        before_source, before_index = manifest(self.source), manifest(self.index_path)
        status = state.index_status(self.data_dir, NOW)
        self.assertEqual((status['state'], status['indexed_events'], status['pending_events']), ('ready', 2, 0))
        self.assertEqual(before_source, manifest(self.source))
        self.assertEqual(before_index, manifest(self.index_path))
        self.change('UPDATE events SET created_at=created_at+1 WHERE id=1')
        status = state.index_status(self.data_dir, NOW)
        self.assertEqual((status['state'], status['indexed_events'], status['pending_events']), ('stale', 1, 1))
        self.change('DELETE FROM events WHERE id=2')
        status = state.index_status(self.data_dir, NOW)
        self.assertEqual((status['effective_events'], status['indexed_events'], status['pending_events']), (1, 0, 1))

    def test_status_missing_unknown_nan_model_and_dimension_are_sanitized(self):
        row = self.put(event())
        status = state.index_status(self.data_dir, NOW)
        self.assertEqual((status['state'], status['indexed_events'], status['pending_events']), ('missing', 0, 1))
        self.make_index([row])
        with closing(sqlite3.connect(str(self.index_path))) as connection:
            connection.execute('UPDATE vectors SET embedding=?', (struct.pack('<512f', math.nan, *([0] * 511)),))
            connection.commit()
        status = state.index_status(self.data_dir, NOW)
        self.assertEqual((status['state'], status['indexed_events'], status['error_type']), ('stale', 0, 'vector_invalid'))
        with closing(sqlite3.connect(str(self.index_path))) as connection:
            connection.execute('UPDATE index_meta SET dimension=3')
            connection.commit()
        status = state.index_status(self.data_dir, NOW)
        self.assertEqual((status['state'], status['indexed_events'], status['error_type']),
                         ('unavailable', None, 'index_model_mismatch'))
        self.assertNotIn(str(self.data_dir), repr(status))
        self.change('PRAGMA user_version=2')
        status = state.index_status(self.data_dir, NOW)
        self.assertIsNone(status['effective_events'])
        self.assertIsNone(status['pending_events'])

    def test_status_document_hash_mismatch_is_not_counted_as_indexed(self):
        row = self.put()
        self.make_index([row])
        with closing(sqlite3.connect(str(self.index_path))) as connection:
            connection.execute('UPDATE vectors SET document_hash=?', ('0' * 64,))
            connection.commit()
        status = state.index_status(self.data_dir, NOW)
        self.assertEqual((status['state'], status['indexed_events'], status['pending_events']), ('stale', 0, 1))

    def test_invalidation_rejects_source_alias_before_any_SQLite_open(self):
        row = self.put()
        self.make_index([row])
        before = manifest(self.source)
        with mock.patch.object(state.Path, 'samefile', return_value=True):
            with mock.patch.object(state.sqlite3, 'connect') as connect:
                with self.assertRaises(state.SemanticStateError) as captured:
                    state.invalidate_index(self.data_dir, 'a', 1)
                connect.assert_not_called()
        self.assertEqual(captured.exception.error_type, 'index_path_unsafe')
        self.assertEqual(before, manifest(self.source))

    def test_lifecycle_pending_counts_include_effective_projection_changes(self):
        row = self.put(event(ts=NOW - 3 * state.DAY + 1, detail='详' * 600))
        self.make_index([row])
        self.assertEqual(state.index_status(self.data_dir, NOW)['state'], 'ready')
        status = state.index_status(self.data_dir, NOW + 1)
        self.assertEqual((status['state'], status['indexed_events'], status['pending_events']), ('stale', 0, 1))
        self.change('UPDATE events SET importance=1,ts=?', (NOW - 14 * state.DAY,))
        status = state.index_status(self.data_dir, NOW)
        self.assertEqual((status['effective_events'], status['indexed_events'], status['pending_events']), (0, 0, 0))
        self.assertEqual(status['state'], 'stale')

    def test_invalidate_standard_sqlite_scope_exact_and_source_untouched(self):
        rows = [self.put(event()), self.put(event(event_id=2, scope='b'))]
        self.make_index(rows)
        before = manifest(self.source)
        with mock.patch.dict(sys.modules, {'sqlite_vec': None}):
            self.assertTrue(state.invalidate_index(self.data_dir, 'b', 1))
            with closing(sqlite3.connect(str(self.index_path))) as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM vectors').fetchone()[0], 2)
            self.assertTrue(state.invalidate_index(self.data_dir, 'a', 1))
        self.assertEqual(before, manifest(self.source))
        with closing(sqlite3.connect(str(self.index_path))) as connection:
            self.assertEqual(connection.execute('SELECT event_id FROM vectors').fetchall(), [(2,)])
            meta = connection.execute('SELECT source_signature,error_type,generation FROM index_meta').fetchone()
            self.assertEqual(meta, (None, 'stale', 3))
        self.assertEqual(state.index_status(self.data_dir, NOW)['state'], 'stale')

    def test_invalidate_missing_no_creation_and_bad_schema_no_mutation(self):
        self.assertTrue(state.invalidate_index(self.data_dir, 'a', 1))
        self.assertFalse(self.index_path.exists())
        self.index_path.write_bytes(b'not sqlite')
        before = manifest(self.index_path)
        with self.assertRaises(state.SemanticStateError):
            state.invalidate_index(self.data_dir, 'a', 1)
        self.assertEqual(before, manifest(self.index_path))

    def test_unrecognized_index_triggers_rejected_before_invalidation(self):
        row = self.put()
        self.make_index([row])
        with closing(sqlite3.connect(str(self.index_path))) as connection:
            connection.execute('CREATE TRIGGER unexpected AFTER DELETE ON vectors BEGIN '
                               'UPDATE index_meta SET generation=999; END')
            connection.commit()
        before = manifest(self.index_path)
        with self.assertRaises(state.SemanticStateError) as captured:
            state.invalidate_index(self.data_dir, 'a', 1)
        self.assertEqual(captured.exception.error_type, 'index_schema_mismatch')
        self.assertEqual(before, manifest(self.index_path))


if __name__ == '__main__':
    unittest.main()
