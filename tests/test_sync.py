import importlib
import os
import unittest
from datetime import date
from unittest.mock import patch, Mock

with patch.dict(os.environ, {k: 'fake' for k in ('FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'SUPABASE_URL', 'SUPABASE_SERVICE_KEY')}):
    sync = importlib.import_module('sync')


class SyncTests(unittest.TestCase):
    def test_database_failure_is_failure(self):
        response = Mock(status_code=500, text='conflict')
        with patch.object(sync.requests, 'post', return_value=response):
            with self.assertRaises(RuntimeError):
                sync.upsert_bloggers([{'link': '', 'record_date': '2026-10-17'}])

    def test_blank_links_use_actual_conflict_key_and_date_argument(self):
        records = [{'link': '', 'name': 'A', 'record_date': '2026-10-17'}, {'link': '', 'name': 'B', 'record_date': '2026-10-17'}]
        with patch.object(sync.sys, 'argv', ['sync.py', '2026-10-17']), patch.object(sync, 'get_app_token', return_value='fake'), patch.object(sync, 'get_spreadsheet_token', return_value='fake'), patch.object(sync, 'get_all_sheets', return_value=[{'title': '10.17', 'sheet_id': 's'}]), patch.object(sync, 'read_sheet_data', return_value=[]), patch.object(sync, 'parse_rows', return_value=(records, {})), patch.object(sync, 'upsert_bloggers', return_value=1) as upsert, patch.object(sync, 'check_revisit', return_value=([], [])), patch.object(sync, 'calc_stats'), patch.object(sync, 'calc_kpi'), patch.object(sync, 'ai_summarize_types'), patch.object(sync, 'build_report', return_value='report'), patch.object(sync, 'save_daily_report') as save, patch.object(sync, 'push_wecom'), patch.object(sync, 'push_serverchan'):
            for record in records:
                record['status'] = ''
            sync.main()
            self.assertEqual(len(upsert.call_args.args[0]), 1)
            self.assertEqual(save.call_args.args[0], date(2026, 10, 17))
