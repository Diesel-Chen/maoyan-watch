import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime

import watch
import monitor_store

_REAL_POST_DINGTALK = watch.post_dingtalk


def movie(mid='1', name='测试电影', days=('2099-01-02',), hall='IMAX 激光厅', times=('19:40',)):
    return {'id': mid, 'nm': name, 'shows': [{'plist': [
        {'dt': d, 'tm': t, 'th': hall, 'tp': '2D', 'seqNo': 'a'} for d in days for t in times]}]}


def snapshot(*movies):
    return {'showData': {'movies': list(movies)}}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for key, val in {
            'DB_FILE': str(Path(self.tmp.name) / 'monitor.sqlite3'),
            'STATE_FILE': str(Path(self.tmp.name) / 'state.json'),
            'CHANNELS': ('feishu', 'dingtalk'),
            'FEISHU_WEBHOOK': 'https://example.invalid/feishu',
            'DINGTALK_WEBHOOK': 'https://example.invalid/robot?access_token=test',
            'DINGTALK_SECRET': 'test-secret',
            'log': lambda *args: None,
        }.items():
            p = patch.object(watch, key, val)
            p.start()
            self.addCleanup(p.stop)
        self.post = self.patch('_post_one_hook')
        self.ding = self.patch('post_dingtalk')
        self.detail = self.patch('fetch_release', return_value=(None, None, 'unknown'))
        self.db = watch.open_database()
        self.addCleanup(self.db.close)

    def patch(self, name, **kwargs):
        p = patch.object(watch, name, **kwargs)
        result = p.start()
        self.addCleanup(p.stop)
        return result

    def process(self, *movies):
        watch.process_snapshot(snapshot(*movies), self.db)

    def test_baseline_all_films_and_restart_dedupe(self):
        self.process(movie(), movie('2', '另一部电影'))
        self.post.assert_not_called()
        db2 = watch.open_database()
        try:
            watch.process_snapshot(snapshot(movie(), movie('2', '另一部电影')), db2)
        finally:
            db2.close()
        self.post.assert_not_called()
        self.assertEqual(self.db.execute('SELECT count(*) FROM shows').fetchone()[0], 2)

    def test_new_film_id_distinguishes_same_name_and_time(self):
        self.process(movie())
        self.process(movie(), movie('2'))
        self.post.assert_called_once()
        self.assertIn('首次监测到影片', self.post.call_args.args[1])
        self.ding.assert_called_once()

    def test_ordinary_hall_excluded_but_2d_in_imax_included(self):
        self.process()
        self.process(movie(hall='普通厅'), movie('2', hall='IMAX 激光厅'))
        self.post.assert_called_once()
        self.assertEqual(self.db.execute('SELECT count(*) FROM shows').fetchone()[0], 1)

    def test_batch_has_one_link_and_multiple_dates(self):
        self.process(movie())
        self.process(movie(days=('2099-01-03', '2099-01-04'), times=('09:45', '13:00', '19:40')))
        self.post.assert_called_once()
        text = self.post.call_args.args[1]
        self.assertEqual(text.count('https://'), 1)
        self.assertIn('新增 3 场：09:45、13:00、19:40', text)
        self.assertIn('1 月 4 日', text)

    def test_duplicate_entries_and_rotating_seq(self):
        self.process()
        m = movie()
        m['shows'][0]['plist'] *= 2
        self.process(m)
        self.post.assert_called_once()
        m['shows'][0]['plist'][0]['seqNo'] = 'rotated'
        self.process(m)
        self.post.assert_called_once()

    def test_failed_channel_retries_after_restart_even_when_show_disappears(self):
        self.process()
        self.ding.side_effect = OSError('offline')
        with self.assertRaises(OSError):
            self.process(movie())
        self.post.assert_called_once()
        self.ding.side_effect = None
        db2 = watch.open_database()
        try:
            watch.process_snapshot(snapshot(), db2)
        finally:
            db2.close()
        self.post.assert_called_once()
        self.assertEqual(self.ding.call_count, 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox WHERE sent_at IS NULL').fetchone()[0], 0)

    def test_metadata_failure_does_not_block_and_is_cached(self):
        self.detail.side_effect = OSError('unavailable')
        self.process()
        self.process(movie())
        self.assertIn('上映日期暂未查到', self.post.call_args.args[1])
        self.process(movie())
        self.detail.assert_called_once()

    def test_metadata_success_cached(self):
        self.process(movie())
        self.process(movie())
        self.detail.assert_called_once()

    def test_invalid_response_does_not_set_baseline(self):
        with self.assertRaises(ValueError):
            watch.process_snapshot({'error': 'bad'}, self.db)
        self.assertEqual(self.db.execute('SELECT count(*) FROM meta').fetchone()[0], 0)

    def test_migration_preserves_history_and_is_idempotent(self):
        record = {'version': 1, 'scope': [watch.CINEMA_ID, '旧电影', 'IMAX', 123],
                  'seen': {json.dumps(['2099-01-01', '19:40', 'IMAX 激光厅', '2D']):
                           {'first_seen': '2098-12-25T10:00:00+08:00', 'baseline': False}}}
        Path(watch.STATE_FILE).write_text(json.dumps(record))
        for _ in range(2):
            monitor_store.migrate_legacy(self.db, watch.STATE_FILE)
        self.assertEqual(self.db.execute('SELECT count(*) FROM shows').fetchone()[0], 1)
        self.process(movie('123', '旧电影', days=('2099-01-01',)), movie('2', '现有电影'))
        self.post.assert_not_called()
        self.assertEqual(self.db.execute('SELECT first_seen FROM movies WHERE id="123"').fetchone()[0],
                         '2098-12-25T10:00:00+08:00')

    def test_corrupt_legacy_fails_without_marking_imported(self):
        Path(watch.STATE_FILE).write_text('{broken')
        with self.assertRaises(ValueError):
            monitor_store.migrate_legacy(self.db, watch.STATE_FILE)
        self.assertEqual(self.db.execute('SELECT count(*) FROM meta').fetchone()[0], 0)

    def test_no_startup_heartbeat_or_error_notifications(self):
        with patch.object(watch, 'open_database', return_value=self.db), \
             patch.object(watch, 'fetch', side_effect=OSError('offline')), \
             patch.object(watch.time, 'sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                watch.main()
        self.post.assert_not_called()
        self.ding.assert_not_called()

    def test_release_labels_boundaries(self):
        for day, expected in [('2098-12-24', ''), ('2098-12-25', '上映前 7 天'),
                              ('2099-01-01', '首映日'), ('2099-01-07', '上映首周·第 7 天'),
                              ('2099-01-08', '')]:
            self.assertEqual(watch.release_label('2099-01-01', 'release', day), expected)

    def test_premiere_presale_and_rerelease_titles(self):
        fresh = {'2099-01-01': [{'tm': '19:40'}]}
        msg = watch.format_hit('新片', fresh, '2098-12-25T12:00:00+08:00', '2099-01-01', 'release', True)
        self.assertTrue(msg.startswith('🚨 新片首映放票'))
        self.assertIn('上映前 7 天', msg)
        self.assertIn('【首映日】', msg)
        msg = watch.format_hit('老片', fresh, '2098-12-25T12:00:00+08:00', '2099-01-01', 'rerelease', True)
        self.assertTrue(msg.startswith('🎞 重映关注'))
        self.assertNotIn('新片首映', msg)

    def test_rerelease_uses_current_mainland_date(self):
        date, kind, _ = watch.parse_release({'rt': '2019-04-24', 'pubDesc': '2026-09-25中国大陆重映'})
        self.assertEqual((date, kind), ('2026-09-25', 'rerelease'))
        self.assertIsNone(watch.parse_release({'rt': '2099-01-01', 'pubDesc': '2099-01-01美国上映'})[0])

    def test_dingtalk_signature_and_payload(self):
        import base64, hashlib, hmac
        from urllib.parse import parse_qs, urlsplit
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"errcode":0}'
        with patch.object(watch.time, 'time', return_value=1700000000), \
             patch.object(watch.urllib.request, 'urlopen', return_value=response) as send:
            _REAL_POST_DINGTALK('测试')
        req = send.call_args.args[0]
        query = parse_qs(urlsplit(req.full_url).query)
        expected = base64.b64encode(hmac.new(b'test-secret', b'1700000000000\ntest-secret', hashlib.sha256).digest()).decode()
        self.assertEqual(query['sign'], [expected])
        self.assertEqual(json.loads(req.data)['text']['content'], '测试')


if __name__ == '__main__':
    unittest.main()
