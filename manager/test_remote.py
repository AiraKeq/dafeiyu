"""Isolated standard-library tests; never Docker, SSH, network, or plugin stores.
Run: python -B -m unittest discover -s manager -p test_remote.py -v
All fixtures are synthetic and live under TemporaryDirectory.
"""
import ast
import copy
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import remote

MAIN = remote.MAIN_SCOPE
OTHER = 'aiocqhttp:GroupMessage:1000000004'
PRIVATE = 'aiocqhttp:FriendMessage:1000000002'
NOW = 1800000000.0
SECRET = 'synthetic-api-secret-DO-NOT-EXPOSE'
TOKEN = 'synthetic-napcat-token-DO-NOT-EXPOSE'
CREDENTIAL = 'synthetic-Credential-DO-NOT-EXPOSE'


def inspect_fixture(role, running):
    mounts = ([{'Type': 'bind', 'Source': remote.ROOT + '/data/astrbot', 'Destination': '/AstrBot/data', 'RW': True},
               {'Type': 'bind', 'Source': '/etc/localtime', 'Destination': '/etc/localtime', 'RW': False}]
              if role == 'astrbot' else [
                  {'Type': 'bind', 'Source': remote.ROOT + '/data/napcat/' + source, 'Destination': destination, 'RW': True}
                  for source, destination in [('qq', '/app/.config/QQ'), ('config', '/app/napcat/config'), ('plugins', '/app/napcat/plugins')]])
    return {'Id': ('a' if role == 'astrbot' else 'b') * 64, 'Name': '/qq-deepseek-' + role + '-1',
            'ConfigImage': remote.ASTR_IMAGE if role == 'astrbot' else remote.NAP_IMAGE,
            'Image': 'sha256:' + 'c' * 64 if role == 'astrbot' else remote.NAP_IMAGE_ID,
            'Labels': {'com.docker.compose.project': 'qq-deepseek', 'com.docker.compose.service': role,
                       'com.docker.compose.project.working_dir': remote.ROOT,
                       'com.docker.compose.project.config_files': remote.ROOT + '/compose.yaml',
                       'com.docker.compose.oneoff': 'False'},
            'Mounts': mounts, 'RestartCount': 0,
            'State': {'Running': running, 'Status': 'running' if running else 'exited', 'Paused': False,
                      'Restarting': False, 'Dead': False, 'Pid': 123 if running else 0,
                      'StartedAt': '2026-11-15T08:53:28.114995179Z',
                      'FinishedAt': '0001-01-01T00:00:00Z' if running else '2026-11-15T15:23:35.000000000Z',
                      'Error': SECRET}, 'Env': ['API_KEY=' + SECRET]}


class FakeRuntime:
    def __init__(self):
        self.values = {'astrbot': inspect_fixture('astrbot', False), 'napcat': inspect_fixture('napcat', True)}
        self.calls, self.requests = [], []
        self.inspect_count = 0
        self.inspect_hook = None
        self.start_error = None
        self.stop_error = None
        self.auth_code, self.status_code, self.info_code = 0, 0, 0
        self.qq_account = remote.ACCOUNT
        self.online = True

    def inspect(self, role):
        self.inspect_count += 1
        if self.inspect_hook:
            self.inspect_hook(role)
        return copy.deepcopy(self.values[role])

    def start(self, cid):
        self.calls.append(('start', cid))
        state = self.values['astrbot']['State']
        state.update(Running=True, Status='running', Pid=222,
                     StartedAt='2027-01-15T01:01:01.000000000Z', FinishedAt='0001-01-01T00:00:00Z')
        if self.start_error:
            raise self.start_error

    def stop(self, cid):
        self.calls.append(('stop', cid))
        if self.stop_error:
            raise self.stop_error
        self.values['astrbot']['State'].update(Running=False, Status='exited', Pid=0,
                                               FinishedAt='2027-01-15T01:02:01.000000000Z')

    def post(self, path, body, credential=None):
        self.requests.append(path)
        if path == '/api/auth/login':
            return {'code': self.auth_code, 'data': {'Credential': CREDENTIAL}, 'message': SECRET}
        if path.endswith('CheckLoginStatus'):
            return {'code': self.status_code, 'data': {'isLogin': self.online, 'coreReady': self.online,
                    'isOffline': not self.online, 'qrcodeurl': SECRET, 'loginError': SECRET}}
        return {'code': self.info_code, 'data': {'uin': self.qq_account, 'online': self.online, 'nickname': SECRET}}


class Fixture(unittest.TestCase):
    def setUp(self):
        # Patches make any accidental real network/process access an immediate failure.
        self.no_docker = mock.patch.object(remote.DockerRuntime, '_docker', side_effect=AssertionError('REAL DOCKER FORBIDDEN'))
        self.no_network = mock.patch.object(remote.DockerRuntime, 'post', side_effect=AssertionError('REAL NETWORK FORBIDDEN'))
        self.no_docker.start()
        self.no_network.start()
        self.addCleanup(self.no_docker.stop)
        self.addCleanup(self.no_network.stop)
        self.temp = tempfile.TemporaryDirectory(prefix='whale-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = FakeRuntime()
        self.controller = remote.Controller(root=self.root, runtime=self.runtime, now=lambda: NOW)
        c = self.controller
        c.data.mkdir(parents=True)
        c.config.parent.mkdir(parents=True)
        c.metadata.parent.mkdir(parents=True)
        self.plugin = {'base_style': '根据真实交流慢慢成长。', 'group_ids': ['1000000003'], 'admin_ids': ['1000000002'],
                       'provider_id': 'deepseek/deepseek-flash', 'active_days': 14, 'important_threshold': 8,
                       'half_life_days': 4, 'raw_retention_days': 14, 'enabled': True, 'proactive_enabled': True,
                       'names': ['大肥鱼'], 'reply_start_hour': 8, 'reply_end_hour': 24,
                       'unrecognized_private_key': SECRET}
        self.put(c.config, self.plugin)
        self.put(c.core, {'admins_id': ['1000000002'], 'provider': [
            {'id': 'deepseek/deepseek-flash', 'model': 'deepseek-flash', 'provider_source_id': 'deepseek', 'enable': True}],
            'provider_sources': [{'id': 'deepseek', 'key': [SECRET]}], 'password': SECRET})
        c.metadata.write_text('name: whale_memory\nversion: 1.4.1\n', encoding='utf-8')
        c.marker.write_bytes(b'previous pause\n')
        webui = self.root / 'data/napcat/config/webui.json'
        webui.parent.mkdir(parents=True)
        self.put(webui, {'token': TOKEN})
        db = sqlite3.connect(str(c.memory))
        for sql in remote.MEMORY_SQL.values():
            db.execute(sql)
        db.execute('PRAGMA user_version=1')
        for index, scope in enumerate((MAIN, OTHER, PRIVATE), 1):
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (index, scope, 'message-' + str(index), 'user', '123456', '合成昵称',
                        '正文' + scope, NOW - 86400, '2027-01-14', 0))
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (index, scope, '2027-01-14', '标题' + str(index), '详情' + scope, '核心',
                        8.0, '["123456"]', '["关键词"]', NOW - 4 * 86400, 'active', NOW - 1))
            db.execute('INSERT INTO styles VALUES(?,?,?,?)', (scope, '2027-01-14', '成长' + scope, NOW - 1))
        db.commit()
        db.close()
        self.put(c.data / 'reply-queue.json', {'version': 1, 'scopes': {MAIN: {
            'seq': 1, 'covered': 0, 'last_reply': NOW - 1, 'retry_at': NOW, 'failures': 2,
            'pending': [{'message_id': 'queued', 'text': 'queue-body-hidden', 'ts': NOW, 'seq': 1}], 'seen': {'queued': 1}}}})
        self.put(c.data / 'review-state.json', {'completed_day': '2027-01-14', 'next_attempt': NOW,
            'last_result': {'processed_batches': 2, 'failed_batches': 1, 'events': 3, 'style_updated': 2,
                            'errors': [{'message': SECRET}]}})
        self.put(c.data / 'segment-score-state.json', {'version': 1, 'scopes': {MAIN: {'counts': {'1': 2}}}})
        pages = sqlite3.connect(str(c.pages))
        for sql in remote.PAGE_SQL.values():
            pages.execute(sql)
        pages.execute('PRAGMA user_version=1')
        pages.execute('PRAGMA application_id=1464878897')
        for i, expiry in enumerate((NOW - 1, NOW + 600)):
            pages.execute('INSERT INTO page_responses VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                          (MAIN, str(i), 'generation-' + str(i), 'source', 'PAGE BODY HIDDEN', 0, 1,
                           NOW - 100, expiry, 'LEASE TOKEN HIDDEN', 0, NOW - 2 if i == 0 else NOW + 10))
        pages.commit()
        pages.close()

    def put(self, path, value):
        path.write_bytes(remote.packed(value))

    def action(self, action, **values):
        return self.controller.handle(dict(action=action, **values))

    def ok(self, action, **values):
        result = self.action(action, **values)
        self.assertTrue(result['ok'], result)
        return result['data']

    def error(self, action, code=None, **values):
        result = self.action(action, **values)
        self.assertFalse(result['ok'], result)
        if code:
            self.assertEqual(code, result['error']['code'])
        for secret in (SECRET, TOKEN, CREDENTIAL):
            self.assertNotIn(secret, json.dumps(result))
        return result

    def tree(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob('*') if p.is_file()}

    def service_revision(self):
        value = self.ok('status')['service']
        return value['id'] + ':' + value['started_at']

    def event(self, scope=MAIN):
        return self.ok('memory_list', scope=scope, kind='events')['items'][0]

    def event_args(self, **changes):
        e = self.event()
        value = {k: e[k] for k in ('id', 'revision', 'title', 'detail', 'core', 'importance', 'keywords')}
        value.update(scope=MAIN, confirm='保存记忆')
        value.update(changes)
        return value

    def query(self, sql, params=()):
        db = sqlite3.connect(str(self.controller.memory))
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def mutate(self, sql, params=()):
        db = sqlite3.connect(str(self.controller.memory))
        db.execute(sql, params)
        db.commit()
        db.close()


class ReadTests(Fixture):
    def test_read_actions_never_write_any_robot_file(self):
        before = self.tree()
        self.ok('status')
        self.ok('personality')
        self.ok('style_get', scope=MAIN)
        self.ok('memory_list', scope=MAIN, kind='events')
        self.ok('memory_list', scope=MAIN, kind='messages')
        self.ok('backups')
        self.assertEqual(before, self.tree())
        self.assertEqual([], self.runtime.calls)
        self.assertFalse(self.controller.manager.exists())

    def test_status_whitelist_and_real_business_codes(self):
        result = self.ok('status')
        encoded = json.dumps(result)
        for forbidden in (SECRET, TOKEN, CREDENTIAL, 'PAGE BODY', 'LEASE TOKEN', 'queue-body', '详情', '正文'):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual('online', result['qq']['state'])
        self.assertEqual(remote.ACCOUNT, result['qq']['account'])
        self.assertEqual({'pending': 1, 'failures': 2}, result['queue'])
        self.assertEqual({'active': 1, 'expired': 1}, result['pagination'])
        self.assertEqual(True, result['model']['key_configured'])
        self.assertEqual('unknown', result['model']['availability'])
        self.assertNotIn('unrecognized_private_key', result['rules'])
        for field in ('auth_code', 'status_code', 'info_code'):
            setattr(self.runtime, field, -1)
            self.assertEqual('unknown', self.ok('status')['qq']['state'])
            setattr(self.runtime, field, 0)
        self.runtime.auth_code = False  # bool is not a business integer code.
        self.assertEqual('unknown', self.ok('status')['qq']['state'])

    def test_qq_account_must_match_and_no_restart(self):
        self.runtime.qq_account = '123456789'
        self.assertEqual('unknown', self.ok('status')['qq']['state'])
        self.runtime.qq_account = remote.ACCOUNT
        self.runtime.online = False
        self.assertEqual('offline', self.ok('status')['qq']['state'])
        self.assertEqual([], self.runtime.calls)

    def test_unreadable_qq_credential_has_safe_unknown_status(self):
        (self.root / 'data/napcat/config/webui.json').write_bytes(b'BROKEN ' + TOKEN.encode())
        self.assertEqual('unknown', self.ok('status')['qq']['state'])

    def test_scope_isolation_and_search_injection(self):
        for kind in ('events', 'messages'):
            data = self.ok('memory_list', scope=MAIN, kind=kind)
            self.assertEqual([1], [x['id'] for x in data['items']])
            for query in ("' OR 1=1 --", '%', '_', "x'); DROP TABLE events;--"):
                self.assertEqual([], self.ok('memory_list', scope=MAIN, kind=kind, query=query)['items'])
        self.assertEqual([2], [x['id'] for x in self.ok('memory_list', scope=OTHER, kind='events')['items']])
        self.error('memory_list', scope='../../elsewhere', kind='messages')
        self.error('memory_list', scope=MAIN, kind='events;DELETE FROM events')
        self.assertEqual(3, len(self.query('SELECT id FROM events')))

    def test_pagination_tied_timestamps_and_binding(self):
        for i in range(4, 10):
            self.mutate('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (i, MAIN, str(i), 'user', '123', 'nick', 'same', NOW - 86400, '2027-01-14', 0))
        first = self.ok('memory_list', scope=MAIN, kind='messages', limit=2)
        ids, cursor = [x['id'] for x in first['items']], first['next_cursor']
        self.error('memory_list', 'CURSOR', scope=OTHER, kind='messages', limit=2, cursor=cursor)
        self.error('memory_list', 'CURSOR', scope=MAIN, kind='events', limit=2, cursor=cursor)
        self.error('memory_list', 'CURSOR', scope=MAIN, kind='messages', query='changed', limit=2, cursor=cursor)
        self.error('memory_list', 'CURSOR', scope=MAIN, kind='messages', limit=3, cursor=cursor)
        self.error('memory_list', 'CURSOR', scope=MAIN, kind='messages', limit=2, cursor=cursor + 'x')
        while cursor:
            page = self.ok('memory_list', scope=MAIN, kind='messages', limit=2, cursor=cursor)
            self.assertEqual(first['as_of_now'], page['as_of_now'])
            ids += [x['id'] for x in page['items']]
            cursor = page['next_cursor']
        self.assertEqual([9, 8, 7, 6, 5, 4, 1], ids)

    def test_cursor_rejects_database_mutation(self):
        self.mutate("UPDATE messages SET scope=?", (MAIN,))
        page = self.ok('memory_list', scope=MAIN, kind='messages', limit=1)
        self.mutate('UPDATE messages SET text=? WHERE id=1', ('changed',))
        self.error('memory_list', 'CURSOR', scope=MAIN, kind='messages', limit=1, cursor=page['next_cursor'])

    def test_recency_and_lifecycle_exact_boundaries_no_mutation(self):
        cases = [(0, 7.9, 'keep', 10), (4, 7.9, 'keep', 5),
                 (14, 7.9, 'delete', 10 * 2 ** -3.5), (14, 8, 'archive', 10 * 2 ** -3.5)]
        for age, importance, action, recency in cases:
            self.mutate('UPDATE events SET ts=?,importance=? WHERE id=1', (NOW - age * 86400, importance))
            before = self.tree()
            event = self.event()
            self.assertEqual(importance, event['importance'])
            self.assertAlmostEqual(recency, event['recency'])
            self.assertEqual(action, event['predicted_action'])
            self.assertEqual(before, self.tree())
        self.mutate('UPDATE events SET ts=?,detail=? WHERE id=1', (NOW - 3 * 86400, '长' * 321))
        self.assertEqual('compress', self.event()['predicted_action'])

    def test_bad_file_does_not_repair_or_create_data(self):
        (self.controller.data / 'review-state.json').write_bytes(b'{"bad":')
        before = self.tree()
        status = self.ok('status')
        self.assertEqual('stopped', status['service']['state'])
        self.assertIsNone(status['queue']['pending'])
        self.assertTrue(any('不可读' in x for x in status['warnings']))
        self.assertEqual(before, self.tree())
        self.controller.memory.write_bytes(b'not sqlite')
        before = self.tree()
        self.error('memory_list', scope=MAIN, kind='messages')
        self.assertEqual(before, self.tree())

    def test_missing_database_not_created(self):
        self.controller.memory.unlink()
        self.error('memory_list', scope=MAIN, kind='messages')
        self.assertFalse(self.controller.memory.exists())

    def test_query_only_prevents_accidental_write(self):
        before = self.tree()
        with self.controller.db() as db:
            self.assertEqual(1, db.execute('PRAGMA query_only').fetchone()[0])
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM messages')
        self.assertEqual(before, self.tree())

    def test_wal_read_does_not_write_source_or_shm(self):
        db = sqlite3.connect(str(self.controller.memory))
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("UPDATE styles SET notes='WAL成长' WHERE scope=?", (MAIN,))
        db.commit()
        before = self.tree()
        self.assertEqual('WAL成长', self.ok('style_get', scope=MAIN)['notes'])
        self.ok('status')
        self.assertEqual(before, self.tree())

    def test_personality_budget_is_exact_unicode_character_count(self):
        p = self.ok('personality')
        self.assertEqual(len(self.plugin['base_style']), p['characters'])
        self.assertEqual(len(remote.IDENTITY + self.plugin['base_style']), p['combined_characters'])
        self.assertEqual(2000, p['polish_budget'])


class WriteTests(Fixture):
    def test_personality_cas_preserves_other_config_and_never_starts(self):
        old = self.controller.config.read_bytes()
        p = self.ok('personality')
        result = self.ok('personality_save', text='新基调，保留成长。', revision=p['revision'], confirm='保存性格')
        value = json.loads(self.controller.config.read_text(encoding='utf-8'))
        expect = dict(self.plugin, base_style='新基调，保留成长。')
        self.assertEqual(expect, value)
        self.assertNotEqual(p['revision'], result['revision'])
        self.assertEqual(old, next((self.controller.manager / 'private-config').iterdir()).read_bytes())
        self.assertTrue(self.controller.marker.exists())
        self.assertEqual([], self.runtime.calls)
        self.error('personality_save', 'CONFLICT', text='stale', revision=p['revision'], confirm='保存性格')
        self.assertEqual(1, len(self.ok('backups')['items']))

    def test_style_cas_day_preservation_and_other_scope_untouched(self):
        old = self.ok('style_get', scope=MAIN)
        other = self.query('SELECT * FROM styles WHERE scope=?', (OTHER,))
        self.ok('style_save', scope=MAIN, notes='新成长便笺', revision=old['revision'], confirm='保存成长便笺')
        new = self.ok('style_get', scope=MAIN)
        self.assertEqual(old['day'], new['day'])
        self.assertEqual('新成长便笺', new['notes'])
        self.assertEqual(other, self.query('SELECT * FROM styles WHERE scope=?', (OTHER,)))
        self.error('style_save', 'CONFLICT', scope=MAIN, notes='stale', revision=old['revision'], confirm='保存成长便笺')
        self.assertEqual([], self.runtime.calls)

    def test_event_update_only_allowed_fields_and_full_row_cas(self):
        before = self.query('SELECT scope,day,ts,tier,created_at,participants FROM events WHERE id=1')
        messages = self.query('SELECT * FROM messages')
        styles = self.query('SELECT * FROM styles')
        args = self.event_args(title='修正', importance=6.2, keywords=['a', '真实'])
        result = self.ok('event_update', **args)
        self.assertNotEqual(args['revision'], result['revision'])
        self.assertEqual(before, self.query('SELECT scope,day,ts,tier,created_at,participants FROM events WHERE id=1'))
        self.assertEqual(messages, self.query('SELECT * FROM messages'))
        self.assertEqual(styles, self.query('SELECT * FROM styles'))
        self.error('event_update', 'CONFLICT', **args)

    def test_event_delete_retains_raw_growth_and_before_image(self):
        item = self.event()
        raw = self.query('SELECT * FROM messages')
        growth = self.query('SELECT * FROM styles')
        result = self.ok('event_delete', scope=MAIN, id=item['id'], revision=item['revision'], confirm='删除这条记忆')
        self.assertEqual([], self.query('SELECT * FROM events WHERE id=1'))
        self.assertEqual(raw, self.query('SELECT * FROM messages'))
        self.assertEqual(growth, self.query('SELECT * FROM styles'))
        backup = self.controller.manager / 'snapshots' / result['backup_id'] / 'memory.sqlite3'
        db = sqlite3.connect(str(backup))
        try:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM events WHERE id=1').fetchone()[0])
        finally:
            db.close()

    def test_wrong_scope_id_is_rejected(self):
        e = self.event(OTHER)
        self.error('event_delete', 'CONFLICT', scope=MAIN, id=e['id'], revision=e['revision'], confirm='删除这条记忆')
        self.assertEqual(3, len(self.query('SELECT * FROM events')))

    def test_id_reuse_cannot_pass_stale_revision(self):
        e = self.event(PRIVATE)
        self.mutate('DELETE FROM events WHERE id=3')
        self.mutate('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (3, PRIVATE, '2027-01-15', 'new', 'detail', 'core', 7, '[]', '[]', NOW, 'active', NOW))
        self.error('event_delete', 'CONFLICT', scope=PRIVATE, id=3, revision=e['revision'], confirm='删除这条记忆')
        self.assertEqual('new', self.query('SELECT title FROM events WHERE id=3')[0][0])

    def test_confirmation_and_ranges_and_unknown_fields(self):
        args = self.event_args()
        for importance in (True, -0.1, 10.01, float('nan'), float('inf'), '8'):
            self.error('event_update', **dict(args, importance=importance))
        for keywords in ('abc', ['x'] * 17, ['x', 'x'], ['x' * 49], [None]):
            self.error('event_update', **dict(args, keywords=keywords))
        self.error('event_update', 'CONFIRMATION', **dict(args, confirm='yes'))
        self.error('event_update', 'VALIDATION', **dict(args, title='t' * 101))
        self.error('event_update', 'VALIDATION', **dict(args, scope_override=OTHER))
        self.error('event_update', **dict(args, id=True))
        self.error('style_save', scope=MAIN, notes='', revision='stale', confirm='保存成长便笺')
        self.assertEqual([], self.runtime.calls)
        for limit in (0, 101, True, '10'):
            self.error('memory_list', scope=MAIN, kind='messages', limit=limit)

    def test_all_write_actions_require_confirmation_and_revision(self):
        before = self.tree()
        for action in remote.CONFIRM:
            self.error(action, 'CONFIRMATION')
            self.error(action, confirm=remote.CONFIRM[action])
        self.assertEqual(before, self.tree())

    def test_running_service_refuses_every_non_stop_write(self):
        self.runtime.values['astrbot'] = inspect_fixture('astrbot', True)
        p = self.ok('personality')
        self.error('personality_save', 'RUNNING', text='new', revision=p['revision'], confirm='保存性格')
        self.error('backup', 'RUNNING', revision=self.service_revision(), confirm='备份记忆')
        self.error('service_start', 'RUNNING', revision=self.service_revision(), confirm='启用大肥鱼')
        self.assertEqual([], self.runtime.calls)

    def test_unknown_and_mismatched_identity_fail_closed(self):
        original = copy.deepcopy(self.runtime.values['astrbot'])
        transforms = [lambda x: x['Labels'].update({'com.docker.compose.project': 'evil'}),
                      lambda x: x['Labels'].update({'com.docker.compose.project.working_dir': '/other'}),
                      lambda x: x['Labels'].update({'com.docker.compose.project.config_files': '/tmp/evil.yaml'}),
                      lambda x: x['Mounts'][0].update(Source='/wrong'),
                      lambda x: x['Mounts'][0].update(RW=False),
                      lambda x: x.update(ConfigImage='soulter/astrbot:latest'),
                      lambda x: x['State'].update(Status='restarting', Restarting=True),
                      lambda x: x['State'].update(Status='created'),
                      lambda x: x['State'].update(Running='false'),
                      lambda x: x.update(Id='short')]
        revision = self.ok('personality')['revision']
        for transform in transforms:
            self.runtime.values['astrbot'] = copy.deepcopy(original)
            transform(self.runtime.values['astrbot'])
            self.assertEqual('unknown', self.ok('status')['service']['state'])
            self.error('personality_save', text='new', revision=revision, confirm='保存性格')
        self.assertEqual([], self.runtime.calls)

    def test_bad_json_or_schema_refuses_save(self):
        revision = self.ok('personality')['revision']
        path = self.controller.data / 'reply-queue.json'
        path.write_bytes(b'broken')
        old = self.controller.config.read_bytes()
        self.error('personality_save', text='new', revision=revision, confirm='保存性格')
        self.assertEqual(old, self.controller.config.read_bytes())
        self.put(path, {'version': 1, 'scopes': {}})
        self.mutate('PRAGMA user_version=99')
        self.error('personality_save', text='new', revision=revision, confirm='保存性格')
        self.assertEqual(old, self.controller.config.read_bytes())

    def test_unknown_trigger_rejects_all_db_writes(self):
        revision = self.ok('personality')['revision']
        self.mutate("CREATE TRIGGER evil AFTER DELETE ON events BEGIN DELETE FROM messages; END")
        self.error('personality_save', 'SCHEMA', text='new', revision=revision, confirm='保存性格')
        self.assertEqual(3, len(self.query('SELECT id FROM messages')))

    def test_pending_plugin_commit_refuses_write(self):
        revision = self.ok('personality')['revision']
        (self.controller.data / 'review-state.pending').write_bytes(b'{}')
        self.error('personality_save', 'RECOVERY_REQUIRED', text='new', revision=revision, confirm='保存性格')


class BackupAndFailureTests(Fixture):
    def test_backup_contains_growth_all_scopes_personality_no_config_or_secrets(self):
        before = self.tree()
        result = self.ok('backup', revision=self.service_revision(), confirm='备份记忆')
        folder = self.controller.manager / 'snapshots' / result['backup_id']
        self.assertEqual({'memory.sqlite3', 'personality.json', 'manifest.json'}, {p.name for p in folder.iterdir()})
        for p in folder.iterdir():
            self.assertNotIn(SECRET.encode(), p.read_bytes())
            self.assertNotIn(TOKEN.encode(), p.read_bytes())
        db = sqlite3.connect(str(folder / 'memory.sqlite3'))
        try:
            self.assertEqual(self.query('SELECT * FROM styles'), db.execute('SELECT * FROM styles').fetchall())
            self.assertEqual(3, db.execute('SELECT COUNT(*) FROM messages').fetchone()[0])
            self.assertEqual('ok', db.execute('PRAGMA integrity_check').fetchone()[0])
        finally:
            db.close()
        after = self.tree()
        for name, value in before.items():
            self.assertEqual(value, after[name])
        self.assertEqual([result['backup_id']], [x['backup_id'] for x in self.ok('backups')['items']])
        self.assertEqual([], self.runtime.calls)

    def test_backup_includes_committed_wal_without_modifying_source(self):
        db = sqlite3.connect(str(self.controller.memory))
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("UPDATE styles SET notes='WAL latest growth' WHERE scope=?", (MAIN,))
        db.commit()
        before = self.tree()
        result = self.ok('backup', revision=self.service_revision(), confirm='备份记忆')
        after = self.tree()
        for name, value in before.items():
            self.assertEqual(value, after[name])
        backup = sqlite3.connect(str(self.controller.manager / 'snapshots' / result['backup_id'] / 'memory.sqlite3'))
        try:
            self.assertEqual('WAL latest growth', backup.execute('SELECT notes FROM styles WHERE scope=?', (MAIN,)).fetchone()[0])
            self.assertEqual('delete', backup.execute('PRAGMA journal_mode').fetchone()[0])
        finally:
            backup.close()

    def test_partial_snapshot_never_indexed_and_write_never_applied(self):
        old = self.controller.config.read_bytes()
        revision = self.ok('personality')['revision']
        with mock.patch.object(remote.shutil, 'copyfile', side_effect=OSError(SECRET)):
            self.error('personality_save', text='new', revision=revision, confirm='保存性格')
        self.assertEqual(old, self.controller.config.read_bytes())
        self.assertEqual([], self.ok('backups')['items'])
        self.error('personality_save', 'RECOVERY_REQUIRED', text='new', revision=revision, confirm='保存性格')
        self.assertEqual([], self.runtime.calls)

    def test_audit_failure_refuses_any_data_write(self):
        old = self.controller.config.read_bytes()
        revision = self.ok('personality')['revision']
        original = remote.atomic
        def fail_intent(path, raw, mode=0o600):
            if path == self.controller.intent:
                raise OSError(SECRET)
            return original(path, raw, mode)
        with mock.patch.object(remote, 'atomic', side_effect=fail_intent):
            self.error('personality_save', text='new', revision=revision, confirm='保存性格')
        self.assertEqual(old, self.controller.config.read_bytes())
        self.assertEqual([], self.runtime.calls)

    def test_failure_after_config_commit_marks_recovery_and_no_auto_restore(self):
        revision = self.ok('personality')['revision']
        original = remote.atomic
        def fail_finish(path, raw, mode=0o600):
            if path == self.controller.intent and b'"complete"' in raw:
                raise OSError(SECRET)
            return original(path, raw, mode)
        with mock.patch.object(remote, 'atomic', side_effect=fail_finish):
            self.error('personality_save', text='committed', revision=revision, confirm='保存性格')
        self.assertEqual('committed', self.ok('personality')['text'])
        self.error('backup', 'RECOVERY_REQUIRED', revision=self.service_revision(), confirm='备份记忆')
        self.assertTrue(any('上次操作' in x for x in self.ok('status')['warnings']))
        self.assertEqual([], self.runtime.calls)

    def test_transaction_failure_rolls_back_event(self):
        args = self.event_args(title='never committed')
        before = self.query('SELECT * FROM events')
        original = self.controller.runtime_same
        def fail_in_transaction(state):
            # The RW connection created a rollback journal only after DML.
            if Path(str(self.controller.memory) + '-journal').exists():
                raise OSError(SECRET)
            return original(state)
        with mock.patch.object(self.controller, 'runtime_same', side_effect=fail_in_transaction):
            self.error('event_update', **args)
        self.assertEqual(before, self.query('SELECT * FROM events'))

    def test_runtime_fingerprint_change_during_backup_refuses_publish(self):
        revision = self.ok('personality')['revision']
        old = self.controller.config.read_bytes()
        original = remote.shutil.copyfile
        def interference(source, target):
            result = original(source, target)
            self.runtime.values['astrbot']['State']['StartedAt'] = '2027-01-15T01:00:00Z'
            return result
        with mock.patch.object(remote.shutil, 'copyfile', side_effect=interference):
            self.error('personality_save', 'CONFLICT', text='new', revision=revision, confirm='保存性格')
        self.assertEqual(old, self.controller.config.read_bytes())
        self.assertEqual([], self.ok('backups')['items'])

    def test_source_mutation_during_backup_refuses_publish(self):
        old = self.controller.config.read_bytes()
        revision = self.ok('personality')['revision']
        original = remote.shutil.copyfile
        def interference(source, target):
            result = original(source, target)
            self.put(self.controller.data / 'review-state.json', {'completed_day': '2027-01-13'})
            return result
        with mock.patch.object(remote.shutil, 'copyfile', side_effect=interference):
            self.error('personality_save', text='new', revision=revision, confirm='保存性格')
        self.assertEqual(old, self.controller.config.read_bytes())
        self.assertEqual([], self.ok('backups')['items'])

    def test_backup_index_checks_missing_or_corrupt_file(self):
        result = self.ok('backup', revision=self.service_revision(), confirm='备份记忆')
        folder = self.controller.manager / 'snapshots' / result['backup_id']
        (folder / 'personality.json').write_bytes(b'corrupted')
        self.error('backups', 'BACKUP_FAILED')

    def test_lock_contention_refuses_operation(self):
        revision = self.ok('personality')['revision']
        @contextmanager
        def busy(_):
            remote.fail('BUSY', 'busy')
            yield
        self.controller.lock_factory = busy
        self.error('personality_save', 'BUSY', text='new', revision=revision, confirm='保存性格')
        self.assertEqual([], self.runtime.calls)


class ServiceTests(Fixture):
    def test_start_only_verified_fixed_cid_and_never_napcat(self):
        result = self.ok('service_start', revision=self.service_revision(), confirm='启用大肥鱼')
        self.assertEqual([('start', 'a' * 64)], self.runtime.calls)
        self.assertEqual('running', result['service']['state'])
        self.assertFalse(self.controller.marker.exists())
        self.assertIn('旧队列', result['warning'])
        self.assertIn('尚需', result['warning'])

    def test_stop_failure_never_starts_rollback(self):
        self.runtime.values['astrbot'] = inspect_fixture('astrbot', True)
        self.runtime.stop_error = OSError(SECRET)
        self.error('service_stop', revision=self.service_revision(), confirm='停止大肥鱼')
        self.assertEqual([('stop', 'a' * 64)], self.runtime.calls)
        self.assertFalse(any(x[0] == 'start' for x in self.runtime.calls))

    def test_stop_success_and_stale_cas(self):
        self.runtime.values['astrbot'] = inspect_fixture('astrbot', True)
        self.error('service_stop', 'CONFLICT', revision='stale', confirm='停止大肥鱼')
        result = self.ok('service_stop', revision=self.service_revision(), confirm='停止大肥鱼')
        self.assertEqual('stopped', result['service']['state'])
        self.assertTrue(self.controller.marker.exists())
        self.assertEqual([('stop', 'a' * 64)], self.runtime.calls)

    def test_stop_can_make_bad_data_safe(self):
        self.runtime.values['astrbot'] = inspect_fixture('astrbot', True)
        revision = self.service_revision()
        self.controller.config.write_bytes(b'broken')
        result = self.ok('service_stop', revision=revision, confirm='停止大肥鱼')
        self.assertEqual('stopped', result['service']['state'])

    def test_pending_operation_can_still_stop_and_pending_is_preserved(self):
        self.runtime.values['astrbot'] = inspect_fixture('astrbot', True)
        revision = self.service_revision()
        self.controller.manager.mkdir(parents=True)
        self.controller.intent.write_bytes(b'{"status":"pending","action":"service_start"}')
        original = self.controller.intent.read_bytes()
        result = self.ok('service_stop', revision=revision, confirm='停止大肥鱼')
        self.assertEqual('stopped', result['service']['state'])
        self.assertEqual(original, self.controller.intent.read_bytes())
        self.assertEqual([('stop', 'a' * 64)], self.runtime.calls)
        self.error('service_start', 'RECOVERY_REQUIRED', revision=self.service_revision(), confirm='启用大肥鱼')

    def test_unreadable_intent_or_unrelated_path_cannot_block_stop(self):
        self.runtime.values['astrbot'] = inspect_fixture('astrbot', True)
        revision = self.service_revision()
        self.controller.manager.mkdir(parents=True)
        self.controller.intent.write_bytes(b'CORRUPT')
        self.controller.config.unlink()
        self.controller.config.mkdir()  # unrelated invalid path; do not inspect it for stop
        result = self.ok('service_stop', revision=revision, confirm='停止大肥鱼')
        self.assertEqual('stopped', result['service']['state'])
        self.assertEqual(b'CORRUPT', self.controller.intent.read_bytes())
        self.assertEqual([('stop', 'a' * 64)], self.runtime.calls)

    def test_status_partial_failure_keeps_stop_revision(self):
        self.runtime.values['astrbot'] = inspect_fixture('astrbot', True)
        self.controller.config.write_bytes(b'CORRUPT')
        self.controller.memory.write_bytes(b'CORRUPT')
        value = self.ok('status')
        self.assertEqual('running', value['service']['state'])
        self.assertEqual('a' * 64, value['service']['id'])
        self.assertEqual([], value['scopes'])
        self.assertTrue(any('不可读' in item for item in value['warnings']))

    def test_start_partial_failure_stops_owned_cid_restores_marker(self):
        old_marker = self.controller.marker.read_bytes()
        self.runtime.start_error = OSError(SECRET)
        self.error('service_start', 'START_FAILED', revision=self.service_revision(), confirm='启用大肥鱼')
        self.assertEqual([('start', 'a' * 64), ('stop', 'a' * 64)], self.runtime.calls)
        self.assertEqual(old_marker, self.controller.marker.read_bytes())
        self.assertFalse(self.runtime.values['astrbot']['State']['Running'])

    def test_start_failure_stop_uncertain_is_not_reported_stopped(self):
        self.runtime.start_error = OSError(SECRET)
        self.runtime.stop_error = OSError(SECRET)
        self.error('service_start', 'RESULT_UNKNOWN', revision=self.service_revision(), confirm='启用大肥鱼')
        self.assertEqual([('start', 'a' * 64), ('stop', 'a' * 64)], self.runtime.calls)

    def test_start_failure_never_stops_replacement_cid(self):
        original = self.runtime.start
        def replaced(cid):
            original(cid)
            self.runtime.values['astrbot']['Id'] = 'd' * 64
            raise OSError(SECRET)
        self.runtime.start = replaced
        self.error('service_start', 'RESULT_UNKNOWN', revision=self.service_revision(), confirm='启用大肥鱼')
        self.assertEqual([('start', 'a' * 64)], self.runtime.calls)

    def test_start_completion_audit_failure_stops_owned_container(self):
        original = remote.atomic
        marker = self.controller.marker.read_bytes()
        def fail_completion(path, raw, mode=0o600):
            if path == self.controller.intent and json.loads(raw).get('status') == 'complete':
                raise OSError(SECRET)
            return original(path, raw, mode)
        with mock.patch.object(remote, 'atomic', side_effect=fail_completion):
            self.error('service_start', 'START_FAILED', revision=self.service_revision(), confirm='启用大肥鱼')
        self.assertEqual([('start', 'a' * 64), ('stop', 'a' * 64)], self.runtime.calls)
        self.assertFalse(self.runtime.values['astrbot']['State']['Running'])
        self.assertEqual(marker, self.controller.marker.read_bytes())
        self.assertEqual('pending', json.loads(self.controller.intent.read_bytes())['status'])

    def test_start_completion_fsync_failure_keeps_pending_and_stops(self):
        original = remote.atomic
        marker = self.controller.marker.read_bytes()
        def fail_after_replace(path, raw, mode=0o600):
            result = original(path, raw, mode)
            if path == self.controller.intent and json.loads(raw).get('status') == 'complete':
                raise OSError(SECRET)
            return result
        with mock.patch.object(remote, 'atomic', side_effect=fail_after_replace):
            self.error('service_start', 'START_FAILED', revision=self.service_revision(), confirm='启用大肥鱼')
        self.assertEqual([('start', 'a' * 64), ('stop', 'a' * 64)], self.runtime.calls)
        self.assertFalse(self.runtime.values['astrbot']['State']['Running'])
        self.assertEqual(marker, self.controller.marker.read_bytes())
        self.assertEqual('pending', json.loads(self.controller.intent.read_bytes())['status'])

    def test_napcat_failure_cannot_hide_or_block_emergency_stop(self):
        self.runtime.values['astrbot']['State'].update(Running=True, Status='running', Pid=321)
        def missing_napcat(role):
            if role == 'napcat':
                raise OSError(SECRET)
        self.runtime.inspect_hook = missing_napcat
        status = self.ok('status')
        self.assertEqual('running', status['service']['state'])
        self.assertEqual('unknown', status['qq']['state'])
        result = self.ok('service_stop', revision=self.service_revision(), confirm='停止大肥鱼')
        self.assertEqual('stopped', result['service']['state'])
        self.assertEqual([('stop', 'a' * 64)], self.runtime.calls)

    def test_napcat_failure_after_start_does_not_block_compensation(self):
        def lost_napcat(role):
            if role == 'napcat' and self.runtime.values['astrbot']['State']['Running']:
                raise OSError(SECRET)
        self.runtime.inspect_hook = lost_napcat
        self.error('service_start', 'START_FAILED', revision=self.service_revision(), confirm='启用大肥鱼')
        self.assertEqual([('start', 'a' * 64), ('stop', 'a' * 64)], self.runtime.calls)
        self.assertFalse(self.runtime.values['astrbot']['State']['Running'])


class ProtocolTests(unittest.TestCase):
    def test_cli_sanitized_single_envelope_and_root_rejection(self):
        for raw in (b'{"action":"status","root":"/tmp"}', b'not-json',
                    b'{"action":"status","action":"service_start"}', b'{"x":NaN}', b'[]', b'0' * 65537):
            with mock.patch.object(remote.sys, 'stdin', mock.Mock(buffer=io.BytesIO(raw))), \
                    mock.patch.object(remote.sys, 'stdout', mock.Mock(buffer=io.BytesIO())) as output:
                remote.main()
                data = output.buffer.getvalue()
                self.assertEqual(1, len(data.splitlines()))
                self.assertFalse(json.loads(data)['ok'])
                self.assertNotIn(b'Traceback', data)

    def test_ddl_and_identity_match_existing_plugin_without_importing_it(self):
        plugin = Path(remote.__file__).parent.parent / 'plugins/astrbot_plugin_whale_memory'
        if not plugin.exists():
            plugin = Path(remote.__file__).parent.parent / 'data/astrbot/plugins/astrbot_plugin_whale_memory'
        if not plugin.exists():
            self.skipTest('read-only plugin checkout unavailable in this isolated deployment')
        memory_tree = ast.parse((plugin / 'memory_store.py').read_text(encoding='utf-8'))
        ddl = next(ast.literal_eval(node.args[0]) for node in ast.walk(memory_tree)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr == 'executescript')
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        try:
            db.executescript(ddl)
            controller = remote.Controller(root='.', runtime=FakeRuntime())
            controller._schema(db)
        finally:
            db.close()
        page_tree = ast.parse((plugin / 'page_store.py').read_text(encoding='utf-8'))
        page_sql = next(ast.literal_eval(node.value) for node in page_tree.body
                        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_SCHEMA_SQL' for t in node.targets))
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        try:
            for sql in page_sql.values():
                db.execute(sql)
            db.execute('PRAGMA user_version=1')
            db.execute('PRAGMA application_id=1464878897')
            controller._schema(db, page=True)
        finally:
            db.close()
        main_tree = ast.parse((plugin / 'main.py').read_text(encoding='utf-8'))
        identity = next(ast.literal_eval(node.value) for node in main_tree.body
                        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'FIXED_IDENTITY' for t in node.targets))
        self.assertEqual(identity + '\n表达基调（固定身份不变）：\n', remote.IDENTITY)
        self.assertEqual(152, len(remote.IDENTITY))

    def test_python36_syntax_and_no_plugin_store_imports(self):
        source = Path(remote.__file__).read_text(encoding='utf-8')
        # feature_version is test-only (Python 3.8+); production has no modern APIs.
        if hasattr(ast, 'parse') and __import__('sys').version_info >= (3, 8):
            ast.parse(source, feature_version=(3, 6))
        for forbidden in ('from __future__ import annotations', 'import MemoryStore', 'import PageStore',
                          'capture_output=', 'missing_ok=', 'datetime.fromisoformat', 'subprocess.PIPE'):
            self.assertNotIn(forbidden, source)


if __name__ == '__main__':
    unittest.main(verbosity=2)
