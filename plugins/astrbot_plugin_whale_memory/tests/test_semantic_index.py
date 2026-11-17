"""Fake-embedder sync tests plus optional real sqlite-vec native-kernel tests.

The fake vectors are mathematical fixtures, not evidence of model quality.
Missing sqlite-vec skips native tests and never installs/downloads anything.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import importlib.util
import math
import sqlite3
import struct
import sys
import threading
import unittest
from unittest import mock

from test_semantic_state import NOW, SyntheticFixture, event, manifest, unit
import semantic_index as index
import semantic_state as state

NATIVE_AVAILABLE = importlib.util.find_spec('sqlite_vec') is not None


class FakeEmbedder:
    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    def __call__(self, texts):
        self.calls.append(list(texts))
        if self.callback is not None:
            self.callback(texts)
        return [unit(1 if '轴二' in text else 0) for text in texts]


class ReconcileTests(SyntheticFixture):
    def setUp(self):
        SyntheticFixture.setUp(self)
        # Only sync semantics are faked. No Python distance replacement exists.
        self.kernel_patch = mock.patch.object(index, '_load_vec', return_value='fixture-only')
        self.kernel_patch.start()
        self.addCleanup(self.kernel_patch.stop)
        self.embedder = FakeEmbedder()
        self.subject = index.SemanticIndex(self.data_dir, self.embedder)

    def test_constructor_no_files_model_or_extension_and_only_explicit_reconcile_builds(self):
        self.put()
        before = manifest(self.source)
        self.assertFalse(self.index_path.exists())
        self.assertEqual(self.embedder.calls, [])
        self.assertEqual(state.index_status(self.data_dir, NOW)['state'], 'missing')
        self.assertFalse(self.index_path.exists())
        status = self.subject.reconcile(NOW)
        self.assertEqual((status['state'], status['indexed_events'], status['pending_events']), ('ready', 1, 0))
        self.assertEqual(before, manifest(self.source))
        with closing(sqlite3.connect(str(self.index_path))) as connection:
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            self.assertEqual(set(name for name, in tables), {'vectors', 'index_meta'})
            row = connection.execute('SELECT embedding FROM vectors').fetchone()
            self.assertEqual(len(row[0]), 512 * 4)
            self.assertNotIn('合成'.encode('utf-8'), row[0])

    def test_incremental_doc_hash_reuse_identity_relabel_and_changed_text_embed(self):
        row = self.put()
        self.subject.reconcile(NOW)
        self.assertEqual(len(self.embedder.calls), 1)
        self.subject.reconcile(NOW + 1)
        self.assertEqual(len(self.embedder.calls), 1)
        self.change('UPDATE events SET importance=9,created_at=created_at+1 WHERE id=1')
        status = self.subject.reconcile(NOW)
        self.assertEqual(status['state'], 'ready')
        self.assertEqual(len(self.embedder.calls), 1)
        current = state.read_events(self.source)[0]
        with closing(sqlite3.connect(str(self.index_path))) as connection:
            self.assertEqual(connection.execute('SELECT fingerprint FROM vectors').fetchone()[0],
                             state.event_fingerprint(current, NOW))
        self.change('UPDATE events SET core=?,detail=? WHERE id=1', ('轴二新核', '全新详情'))
        self.subject.reconcile(NOW)
        self.assertEqual(len(self.embedder.calls), 2)
        self.assertNotEqual(state.event_fingerprint(row, NOW), state.event_fingerprint(current, NOW))

    def test_identical_documents_deduplicate_embedding_and_are_scope_isolated(self):
        self.put(event())
        self.put(event(event_id=2, scope='b'))
        status = self.subject.reconcile(NOW)
        self.assertEqual(status['indexed_events'], 2)
        self.assertEqual(len(self.embedder.calls), 1)
        self.assertEqual(len(self.embedder.calls[0]), 1)
        with closing(sqlite3.connect(str(self.index_path))) as connection:
            rows = connection.execute('SELECT scope,event_id,fingerprint FROM vectors ORDER BY scope').fetchall()
            self.assertEqual([row[:2] for row in rows], [('a', 1), ('b', 2)])
            self.assertNotEqual(rows[0][2], rows[1][2])

    def test_deleted_expired_and_orphan_vectors_removed_with_no_source_writes(self):
        self.put(event())
        self.put(event(event_id=2, importance=1, ts=NOW - 14 * state.DAY + 1))
        self.subject.reconcile(NOW)
        self.change('DELETE FROM events WHERE id=1')
        before = manifest(self.source)
        status = self.subject.reconcile(NOW + 1)
        self.assertEqual((status['state'], status['effective_events'], status['indexed_events']), ('ready', 0, 0))
        self.assertEqual(before, manifest(self.source))
        with closing(sqlite3.connect(str(self.index_path))) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM vectors').fetchone()[0], 0)

    def test_lifecycle_projection_change_embeddings_core_only_and_fingerprint(self):
        self.put(event(ts=NOW - 3 * state.DAY + 1, detail='详' * 600))
        self.subject.reconcile(NOW)
        self.subject.reconcile(NOW + 1)
        self.assertEqual(len(self.embedder.calls), 2)
        self.assertLess(len(self.embedder.calls[1][0]), len(self.embedder.calls[0][0]))
        self.change('UPDATE events SET ts=?', (NOW - 14 * state.DAY,))
        status = self.subject.reconcile(NOW)
        self.assertEqual(status['state'], 'ready')
        self.assertEqual(len(self.embedder.calls), 3)
        self.assertNotIn('详', self.embedder.calls[-1][0])
        self.assertIn('朋友希望', self.embedder.calls[-1][0])

    def test_source_changed_while_embedding_is_pending_not_stale_publication(self):
        self.put()
        self.embedder.callback = lambda texts: self.change('UPDATE events SET created_at=created_at+1')
        status = self.subject.reconcile(NOW)
        self.assertEqual((status['state'], status['effective_events'], status['indexed_events'],
                          status['pending_events']), ('stale', 1, 0, 1))
        self.embedder.callback = None
        status = self.subject.reconcile(NOW)
        self.assertEqual(status['state'], 'ready')

    def test_source_deleted_or_added_during_inference_rechecked(self):
        self.put()
        self.embedder.callback = lambda texts: self.change('DELETE FROM events')
        status = self.subject.reconcile(NOW)
        self.assertEqual((status['state'], status['indexed_events']), ('ready', 0))
        self.put()
        self.embedder.callback = lambda texts: self.put(event(event_id=2, scope='b', core='轴二'))
        status = self.subject.reconcile(NOW)
        self.assertEqual((status['effective_events'], status['indexed_events'], status['pending_events']), (2, 1, 1))

    def test_model_failure_and_bad_vectors_never_create_partial_index(self):
        self.put()
        for vector in ([0.0] * 512, [math.nan] + [0.0] * 511,
                       [math.inf] + [0.0] * 511, [1.0], [True] + [0.0] * 511,
                       [10 ** 10000] + [0.0] * 511):
            subject = index.SemanticIndex(self.data_dir, lambda texts: [vector])
            with self.assertRaises(state.SemanticStateError):
                subject.reconcile(NOW)
            self.assertFalse(self.index_path.exists())
        subject = index.SemanticIndex(self.data_dir, lambda texts: [])
        with self.assertRaises(index.SemanticIndexError) as captured:
            subject.reconcile(NOW)
        self.assertEqual(captured.exception.error_type, 'embedding_count_mismatch')
        def broken(texts):
            raise RuntimeError('must not leak synthetic facts or paths')
        subject = index.SemanticIndex(self.data_dir, broken)
        with self.assertRaises(index.SemanticIndexError) as captured:
            subject.reconcile(NOW)
        self.assertEqual(str(captured.exception), 'embedding_failed')
        self.assertFalse(self.index_path.exists())

    def test_unknown_schema_model_revision_and_corrupt_blob_not_silently_rebuilt(self):
        self.put()
        self.subject.reconcile(NOW)
        with closing(sqlite3.connect(str(self.index_path))) as connection:
            connection.execute('UPDATE index_meta SET model_revision=?', ('wrong',))
            connection.commit()
        before = manifest(self.index_path)
        with self.assertRaises(state.SemanticStateError) as captured:
            self.subject.reconcile(NOW)
        self.assertEqual(captured.exception.error_type, 'index_model_mismatch')
        self.assertEqual(before, manifest(self.index_path))
        with closing(sqlite3.connect(str(self.index_path))) as connection:
            connection.execute('UPDATE index_meta SET model_revision=?', (state.MODEL_REVISION,))
            connection.execute('UPDATE vectors SET embedding=?', (b'bad',))
            connection.commit()
        before = manifest(self.index_path)
        with self.assertRaises(state.SemanticStateError):
            self.subject.reconcile(NOW)
        self.assertEqual(before, manifest(self.index_path))

    def test_post_commit_invalidator_generation_cannot_be_overwritten(self):
        self.put()
        self.subject.reconcile(NOW)
        self.change('UPDATE events SET detail=?', ('需要生成的新详情',))
        self.embedder.callback = lambda texts: state.invalidate_index(self.data_dir, 'a', 1)
        with self.assertRaises(index.SemanticIndexError) as captured:
            self.subject.reconcile(NOW)
        self.assertEqual(captured.exception.error_type, 'index_concurrent_update')
        with closing(sqlite3.connect(str(self.index_path))) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM vectors').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT error_type FROM index_meta').fetchone()[0], 'stale')
        self.embedder.callback = None
        self.assertEqual(self.subject.reconcile(NOW)['state'], 'ready')

    def test_two_workers_generation_CAS_only_one_first_publication(self):
        self.put()
        barrier = threading.Barrier(2)
        def run_worker():
            embedder = FakeEmbedder(callback=lambda texts: barrier.wait(timeout=5))
            subject = index.SemanticIndex(self.data_dir, embedder)
            try:
                return subject.reconcile(NOW)['state']
            except state.SemanticStateError as exc:
                return exc.error_type
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda unused: run_worker(), range(2)))
        self.assertEqual(results.count('ready'), 1, results)
        self.assertTrue(any(result in ('index_concurrent_update', 'index_write_failed') for result in results), results)
        self.assertEqual(state.index_status(self.data_dir, NOW)['indexed_events'], 1)

    def test_live_source_WAL_and_SHM_unchanged_during_reconcile(self):
        with closing(sqlite3.connect(str(self.source))) as owner:
            owner.execute('PRAGMA journal_mode=WAL')
            self.put(event(), owner)
            before = manifest(self.source)
            status = self.subject.reconcile(NOW)
            self.assertEqual(status['state'], 'ready')
            self.assertEqual(before, manifest(self.source))


class DependencyTests(SyntheticFixture):
    def test_missing_lazy_dependency_raises_safely_without_creation(self):
        self.put()
        embedder = FakeEmbedder()
        with mock.patch.dict(sys.modules, {'sqlite_vec': None}):
            subject = index.SemanticIndex(self.data_dir, embedder)
            self.assertEqual(embedder.calls, [])
            self.assertFalse(self.index_path.exists())
            with self.assertRaises(index.SemanticIndexError) as captured:
                subject.reconcile(NOW)
        self.assertEqual(captured.exception.error_type, 'sqlite_vec_missing')
        self.assertFalse(self.index_path.exists())
        self.assertEqual(embedder.calls, [])

    def test_fixed_dimension_and_invalid_query_vectors_fail_before_native_load(self):
        for dimension in (3, 513, True):
            with self.assertRaises(index.SemanticIndexError):
                index.SemanticIndex(self.data_dir, FakeEmbedder(), dimension=dimension)
        subject = index.SemanticIndex(self.data_dir, FakeEmbedder())
        with mock.patch.object(index, '_load_vec') as kernel:
            for vector in ([1.0], [math.nan] + [0.0] * 511, [0.0] * 512):
                with self.assertRaises(state.SemanticStateError):
                    subject.search('a', vector, now=NOW)
            kernel.assert_not_called()
        self.assertFalse(self.index_path.exists())


@unittest.skipUnless(NATIVE_AVAILABLE, 'sqlite_vec is not installed; no network/install in core tests')
class NativeKernelTests(SyntheticFixture):
    def setUp(self):
        SyntheticFixture.setUp(self)
        self.embedder = FakeEmbedder()
        self.subject = index.SemanticIndex(self.data_dir, self.embedder)

    def test_real_kernel_cosine_scope_and_readonly_candidates_no_text(self):
        self.put(event())
        self.put(event(event_id=2, core='轴二'))
        self.put(event(event_id=3, scope='b'))
        self.subject.reconcile(NOW)
        before_source, before_index = manifest(self.source), manifest(self.index_path)
        found = self.subject.search('a', unit(), now=NOW)
        self.assertEqual([row['event_id'] for row in found], [1, 2])
        self.assertAlmostEqual(found[0]['similarity'], 1.0, places=6)
        self.assertAlmostEqual(found[1]['similarity'], 0.0, places=6)
        self.assertEqual(set(found[0]), {'event_id', 'scope', 'fingerprint', 'similarity'})
        self.assertEqual(before_source, manifest(self.source))
        self.assertEqual(before_index, manifest(self.index_path))
        self.assertEqual(self.subject.search("a' OR 1=1 --", unit(), now=NOW), [])
        self.assertEqual(len(self.subject.search('a', unit(), limit=1, now=NOW)), 1)

    def test_changed_deleted_reused_ID_and_lifecycle_old_vectors_rejected_before_reconcile(self):
        row = self.put(event(ts=NOW - 4 * state.DAY, keywords='[]'))
        self.subject.reconcile(NOW)
        candidates = self.subject.search('a', unit(), now=NOW)
        self.assertEqual(len(candidates), 1)
        scores = {row['id']: {'fingerprint': candidates[0]['fingerprint'], 'similarity': 1.0}}
        self.change('UPDATE events SET created_at=created_at+1')
        self.assertEqual(self.subject.search('a', unit(), now=NOW), [])
        current = state.read_events(self.source)
        self.assertEqual(state.render_recall(current, 'a', '陌生措辞', NOW, semantic_scores=scores), '')
        self.change('DELETE FROM events')
        self.assertEqual(self.subject.search('a', unit(), now=NOW), [])
        self.put(dict(row, scope='b', created_at=NOW + 2))
        self.assertEqual(self.subject.search('a', unit(), now=NOW), [])
        self.assertEqual(self.subject.search('b', unit(), now=NOW), [])
        self.put(dict(row, created_at=NOW + 3))
        self.assertEqual(self.subject.search('a', unit(), now=NOW), [])
        self.subject.reconcile(NOW)
        self.change('UPDATE events SET importance=1,ts=?', (NOW - 14 * state.DAY,))
        self.assertEqual(self.subject.search('a', unit(), now=NOW), [])

    def test_model_dimension_and_corrupt_native_input_rejected(self):
        self.put()
        self.subject.reconcile(NOW)
        with closing(sqlite3.connect(str(self.index_path))) as connection:
            connection.execute('UPDATE vectors SET embedding=?', (struct.pack('<512f', math.nan, *([0] * 511)),))
            connection.commit()
        with self.assertRaises(state.SemanticStateError) as captured:
            self.subject.search('a', unit(), now=NOW)
        self.assertEqual(captured.exception.error_type, 'vector_invalid')
        with closing(sqlite3.connect(str(self.index_path))) as connection:
            connection.execute('UPDATE index_meta SET dimension=3')
            connection.commit()
        with self.assertRaises(state.SemanticStateError) as captured:
            self.subject.search('a', unit(), now=NOW)
        self.assertEqual(captured.exception.error_type, 'index_model_mismatch')

    def test_native_search_without_index_does_not_create(self):
        self.put()
        with self.assertRaises(state.SemanticStateError) as captured:
            self.subject.search('a', unit(), now=NOW)
        self.assertEqual(captured.exception.error_type, 'index_missing')
        self.assertFalse(self.index_path.exists())

    def test_negative_cosine_supported_but_never_becomes_semantic_recall(self):
        row = self.put(event(ts=NOW - 20 * state.DAY, core='长期核', title='历史', keywords='[]'))
        self.subject.reconcile(NOW)
        negative = [-number for number in unit()]
        found = self.subject.search('a', negative, now=NOW)
        self.assertAlmostEqual(found[0]['similarity'], -1.0, places=6)
        scores = {row['id']: {'fingerprint': found[0]['fingerprint'], 'similarity': found[0]['similarity']}}
        self.assertEqual(state.render_recall([row], 'a', '陌生措辞', NOW, semantic_scores=scores), '')


if __name__ == '__main__':
    unittest.main()
