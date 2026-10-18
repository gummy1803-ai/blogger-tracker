import os
import unittest
from datetime import date
from unittest.mock import patch, Mock
import dup_check as d


def row(link='', account='', mark=''):
    return [1, link, '', '博主', '', account, '张三', mark]


class DedupTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'TARGET_DATE': '2026-10-18'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def scan(self, data):
        sheets = [{'title': title, 'sheet_id': sid} for sid, (title, _) in data.items()]
        with patch.object(d, 'read_sheet_data', side_effect=lambda t, s, sid: data[sid][1]), patch.object(d, 'write_dup_marks', return_value=0) as write:
            result = d.run_dup_check('fake', 'fake', sheets)
            return result, write.call_args_list

    def test_cross_sheet_and_current_sheet_only(self):
        (n, details), writes = self.scan({'old': ('10.17', [row(account='abc'), row(account='abc')]), 'new': ('10.18', [row(account='abc')])})
        self.assertEqual(n, 0)
        self.assertEqual(len(details), 1)
        self.assertEqual(writes[0].args[2:], ('new', [3]))
        self.assertEqual(details[0]['first_sheet'], '10.17')

    def test_alias_chain_and_existing_mark(self):
        (_, details), writes = self.scan({'s': ('10.18', [row('https://douyin.com/user/A'), row('https://douyin.com/user/A', 'account', '已重复'), row(account='account')])})
        self.assertEqual(writes[0].args[-1], [5])
        self.assertEqual(len(details), 1)

    def test_blank_and_different_platforms(self):
        (_, details), _ = self.scan({'s': ('10.18', [row(), row(), row('https://douyin.com/user/abcdefghi'), row('https://xiaohongshu.com/user/abcdefghi')])})
        self.assertFalse(details)

    def test_rich_link(self):
        self.assertEqual(d.get_cell([[{'link': 'https://douyin.com/user/A'}]], 0), 'https://douyin.com/user/A')

    def test_query_ids_remain_distinct(self):
        self.assertFalse(d.normalize_key('https://example.com/user?id=1', '') & d.normalize_key('https://example.com/user?id=2', ''))

    def test_read_failure_prevents_all_writes(self):
        with patch.object(d, 'read_sheet_data', side_effect=[ [row(account='a'), row(account='a')], RuntimeError('read failed')]), patch.object(d, 'write_dup_marks') as write:
            with self.assertRaises(RuntimeError):
                d.run_dup_check('', '', [{'title': '10.18', 'sheet_id': 'a'}, {'title': '10.17', 'sheet_id': 'b'}])
            write.assert_not_called()

    def test_dry_run_no_write_or_lock(self):
        with patch.object(d, 'DRY_RUN', True), patch.object(d.requests, 'post') as post:
            self.assertEqual(d.write_dup_marks('', '', 's', [3]), 0)
            self.assertFalse(d.lock_sheet('', '', 's', 100))
            post.assert_not_called()

    def test_h_column_payload(self):
        r = Mock(); r.json.return_value = {'code': 0}
        with patch.object(d, 'DRY_RUN', False), patch.object(d.requests, 'post', return_value=r) as post:
            self.assertEqual(d.write_dup_marks('fake', 'fake', 's', [3, 5]), 2)
            self.assertEqual(post.call_args.kwargs['json']['valueRanges'][1], {'range': 's!H5:H5', 'values': [['已重复']]})

    def test_write_failure_raises(self):
        r = Mock(); r.json.return_value = {'code': 999}
        with patch.object(d, 'DRY_RUN', False), patch.object(d.requests, 'post', return_value=r):
            with self.assertRaises(RuntimeError):
                d.write_dup_marks('', '', 's', [3])

    def test_midnight_targets_previous_business_day(self):
        with patch.dict(os.environ, {'TARGET_DATE': ''}), patch.object(d, 'IS_FINAL', True), patch.object(d, 'business_today', return_value=date(2026, 10, 19)):
            self.assertEqual(d.target_date(), date(2026, 10, 18))

    def test_lock_requires_all_gates(self):
        with patch.object(d, 'DRY_RUN', False), patch.object(d, 'ALLOW_LOCK', False), patch.object(d, 'IS_FINAL', True), patch.object(d.requests, 'post') as post:
            self.assertFalse(d.lock_sheet('', '', 's', 100))
            post.assert_not_called()

    def test_lock_failure_not_success(self):
        r = Mock(); r.json.return_value = {'code': 999}
        with patch.object(d, 'DRY_RUN', False), patch.object(d, 'ALLOW_LOCK', True), patch.object(d, 'IS_FINAL', True), patch.object(d.requests, 'post', return_value=r):
            with self.assertRaises(RuntimeError):
                d.lock_sheet('', '', 's', 100)

    def test_report_does_not_claim_lock(self):
        self.assertNotIn('已锁定', d.build_dup_report(0, [], False))


if __name__ == '__main__':
    unittest.main()
