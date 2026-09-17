import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import watch

_REAL_POST_DINGTALK = watch.post_dingtalk


def snapshot(*shows):
    return {'showData': {'movies': [{'nm': watch.MOVIE_NAME, 'shows': [
        {'plist': [{'dt': day, 'tm': hour, 'th': 'IMAX 激光厅',
                    'tp': 'IMAX2D', 'seqNo': seq} for day, hour, seq in shows]}]}]}}


class ContinuousMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in [('CHANNELS', ('feishu', 'dingtalk')),
                            ('FEISHU_WEBHOOK', 'https://example.invalid/feishu'),
                            ('DINGTALK_WEBHOOK', 'https://example.invalid/robot?access_token=test'),
                            ('DINGTALK_SECRET', 'test-secret'),
                            ('STATE_FILE', str(Path(self.temp.name) / 'state.json')),
                            ('log', lambda *args: None)]:
            p = patch.object(watch, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(watch, '_post_one_hook')
        self.post = p.start()
        self.addCleanup(p.stop)
        p = patch.object(watch, 'post_dingtalk')
        self.ding = p.start()
        self.addCleanup(p.stop)
        self.initial = snapshot(('2099-01-01', '19:40', 'a'))

    def test_baseline_restart_and_rotating_seq_do_not_notify(self):
        state = watch.process_snapshot(self.initial, None)
        self.assertEqual(len(state), 1)
        state = watch.load_state()
        watch.process_snapshot(snapshot(('2099-01-01', '19:40', 'changed')), state)
        self.post.assert_not_called()

    def test_new_day_and_added_show_notify_only_once(self):
        state = watch.process_snapshot(self.initial, None)
        data = snapshot(('2099-01-01', '19:40', 'a'),
                        ('2099-01-01', '22:00', 'b'),
                        ('2099-01-02', '19:40', 'c'))
        watch.process_snapshot(data, state)
        self.assertEqual(self.post.call_count, 1)
        for call in self.post.call_args_list:
            self.assertEqual(call.args[0], watch.FEISHU_WEBHOOK)
            self.assertIn('发现时间（北京时间）', call.args[1])
        watch.process_snapshot(data, watch.load_state())
        self.assertEqual(self.post.call_count, 1)

    def test_five_simultaneous_shows_are_one_message(self):
        state = watch.process_snapshot(self.initial, None)
        shows = [('2099-01-02', hour, str(i)) for i, hour in
                 enumerate(['09:45', '13:00', '16:20', '19:40', '22:55'])]
        data = snapshot(*(shows + [shows[0]]))
        watch.process_snapshot(data, state)
        self.post.assert_called_once()
        self.assertIn('新增 5 场', self.post.call_args.args[1])
        watch.process_snapshot(data, watch.load_state())
        self.post.assert_called_once()

    def test_idle_loop_sends_no_status_notifications(self):
        with patch.object(watch, 'fetch', return_value=self.initial), \
             patch.object(watch.time, 'sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                watch.main()
        self.post.assert_not_called()

    def test_errors_only_logged(self):
        for error in [watch.Blocked('403'), OSError('offline')]:
            with patch.object(watch, 'fetch', side_effect=error), \
                 patch.object(watch.time, 'sleep', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    watch.main()
        self.post.assert_not_called()

    def test_failed_delivery_is_retried(self):
        state = watch.process_snapshot(self.initial, None)
        data = snapshot(('2099-01-02', '19:40', 'b'))
        self.post.side_effect = OSError('offline')
        with self.assertRaises(OSError):
            watch.process_snapshot(data, state)
        self.assertEqual(len(watch.load_state()), 2)
        self.ding.assert_called_once()
        self.post.side_effect = None
        watch.process_snapshot(data, state)
        self.assertEqual(len(watch.load_state()), 2)

    def test_dingtalk_failure_does_not_repeat_feishu_after_restart(self):
        state = watch.process_snapshot(self.initial, None)
        data = snapshot(('2099-01-02', '19:40', 'b'))
        self.ding.side_effect = OSError('offline')
        with self.assertRaises(OSError):
            watch.process_snapshot(data, state)
        self.post.assert_called_once()
        self.ding.side_effect = None
        watch.process_snapshot(data, watch.load_state())
        self.post.assert_called_once()
        self.assertEqual(self.ding.call_count, 2)

    def test_old_records_are_not_backfilled_to_dingtalk(self):
        state = watch.process_snapshot(self.initial, None)
        watch.process_snapshot(self.initial, state)
        self.ding.assert_not_called()

    def test_dingtalk_request_signature_and_payload(self):
        import base64, hashlib, hmac
        from urllib.parse import parse_qs, urlsplit
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"errcode":0}'
        with patch.object(watch, 'DINGTALK_SECRET', 'test-secret'), \
             patch.object(watch.time, 'time', return_value=1700000000), \
             patch.object(watch.urllib.request, 'urlopen', return_value=response) as send:
            _REAL_POST_DINGTALK('测试')
        req = send.call_args.args[0]
        query = parse_qs(urlsplit(req.full_url).query)
        expected = base64.b64encode(hmac.new(b'test-secret', b'1700000000000\ntest-secret', hashlib.sha256).digest()).decode()
        self.assertEqual(query['sign'], [expected])
        self.assertEqual(query['timestamp'], ['1700000000000'])
        self.assertEqual(json.loads(req.data)['text']['content'], '测试')

    def test_disappearance_and_reappearance_do_not_notify(self):
        state = watch.process_snapshot(self.initial, None)
        watch.process_snapshot(snapshot(), state)
        watch.process_snapshot(self.initial, state)
        self.post.assert_not_called()

    def test_invalid_response_does_not_create_baseline(self):
        with self.assertRaises(ValueError):
            watch.process_snapshot({'error': 'unavailable'}, None)
        self.assertFalse(Path(watch.STATE_FILE).exists())

    def test_changed_filter_creates_new_baseline(self):
        watch.process_snapshot(self.initial, None)
        with patch.object(watch, 'MOVIE_NAME', '另一部电影'):
            self.assertIsNone(watch.load_state())

    def test_corrupt_state_is_not_silently_reset(self):
        Path(watch.STATE_FILE).write_text('{broken')
        with self.assertRaises(ValueError):
            watch.load_state()


if __name__ == '__main__':
    unittest.main()
