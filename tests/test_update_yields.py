import contextlib
import importlib.util
import io
import json
import re
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('updater', Path(__file__).resolve().parents[1] / 'scripts/update_yields.py')
u = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(u)
PAGE = Path(u.ROOT, 'd1/index.html').read_text()
ROWS = {s['code']: s for s in [json.loads(re.sub(r'([{,])\s*([A-Za-z]\w*)\s*:', r'\1"\2":', r.strip().rstrip(','))) for r in re.findall(r'^  \{code:"\d{4}"[^\n]*\},$', PAGE, re.M)]}


def previous(code='8725'):
    s = deepcopy(ROWS[code])
    s['quoteAt'] = (datetime.now(u.JST) - timedelta(days=1)).isoformat()
    return s


def quote(s):
    r = u.DIVIDEND_REVIEWS[s['code']]
    return {'price': s['price'] + 10, 'shares': s['shares'], 'quote_at': datetime.now(u.JST),
            'div': r['reported_div'], 'div_period': r['period']}


class UpdaterTests(unittest.TestCase):
    def test_quotes_do_not_depend_on_irbank_and_keep_history_and_adjustment(self):
        s = previous()
        with patch.object(u, 'fetch', return_value='quote') as fetch, patch.object(u, 'parse_yahoo', return_value=quote(s)):
            data, cache = u.get_stock(s['code'], s)
        self.assertTrue(all('irbank' not in c.args[0] for c in fetch.call_args_list))
        self.assertEqual(data['div'], 140)
        self.assertEqual(data['hist'], s['hist'])
        self.assertEqual(data['avg10y'], s['avg10y'])
        self.assertEqual(data['cy'], u.rounded(u.Decimal('140') / u.Decimal(str(s['price'] + 10)) * 100))
        self.assertEqual(cache['price'], s['price'] + 10)

    def test_japan_quote_failure_uses_chart_timestamp_and_labels_cached_shares(self):
        s = previous()
        meta = {'symbol': '8725.T', 'currency': 'JPY', 'regularMarketPrice': 4900, 'regularMarketTime': int(datetime.now(u.JST).timestamp())}
        chart = json.dumps({'chart': {'error': None, 'result': [{'meta': meta}]}})
        with patch.object(u, 'fetch', side_effect=[OSError('Japan quote HTTP 403'), chart]):
            data, _ = u.get_stock(s['code'], s)
        self.assertEqual(data['price'], 4900)
        self.assertEqual(data['div'], 140)
        self.assertEqual(data['quoteSource'], 'Yahoo Finance chart API')
        self.assertEqual(data['sharesAt'], s['quoteAt'])
        self.assertIn('前回確認値', data['updateWarning'])

    def test_revised_forecast_keeps_reviewed_dividend_and_refreshes_price(self):
        s = previous()
        q = quote(s)
        q['div'] = 190
        with patch.object(u, 'fetch', return_value='quote'), patch.object(u, 'parse_yahoo', return_value=q):
            data, _ = u.get_stock(s['code'], s)
        self.assertEqual(data['div'], 140)
        self.assertEqual(data['price'], q['price'])
        self.assertTrue(data['divNeedsReview'])
        self.assertEqual(data['observedDiv'], 190)

    def test_chart_split_rejects_stale_per_share_basis(self):
        s = previous()
        result = {'meta': {'symbol': '8725.T', 'currency': 'JPY'}, 'events': {'splits': {'new': {'date': int(datetime.now(u.JST).timestamp())}}}}
        with self.assertRaisesRegex(ValueError, 'stock split'):
            u.parse_chart(json.dumps({'chart': {'result': [result]}}), '8725', s)

    def test_invalid_or_older_quote_is_rejected(self):
        s = previous()
        for bad in [{'price': float('nan')}, {'quote_at': datetime.fromisoformat(s['quoteAt']) - timedelta(days=1)}, {'shares': s['shares'] * 4}]:
            q = quote(s)
            q.update(bad)
            with patch.object(u, 'fetch', return_value='quote'), patch.object(u, 'parse_yahoo', return_value=q), self.assertRaises(ValueError):
                u.get_stock(s['code'], s)

    def run_main(self, all_fail=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root/'d1').mkdir()
        (root/'data').mkdir()
        rows = [previous('8725'), previous('4674')]
        original = '<div id="updateStatus"></div>\nデータ取得日: 2026年10月8日 12:50\n' + '\n'.join('  {' + ', '.join(k+':'+json.dumps(v,ensure_ascii=False) for k,v in row.items())+'},' for row in rows)
        (root/'d1/index.html').write_text(original)
        (root/'data/dividends.json').write_text('{}')
        def result(code, row):
            if all_fail or code == '4674':
                raise OSError('Both quote sources failed')
            row = row.copy()
            row.update({'price': 4900, 'divNeedsReview': False})
            return row, {'price': 4900}
        with patch.object(u, 'ROOT', str(root)), patch.object(u, 'get_stock', side_effect=result), contextlib.redirect_stdout(io.StringIO()):
            if all_fail:
                with self.assertRaisesRegex(SystemExit, 'No quotes updated'):
                    u.main()
            else:
                u.main()
        return root, rows, original

    def test_partial_failure_publishes_good_stock_and_preserves_failed_timestamp(self):
        root, rows, _ = self.run_main()
        page = (root/'d1/index.html').read_text()
        summary = json.loads((root/'data/last_update.json').read_text())
        self.assertEqual(summary['updated'], ['8725'])
        self.assertEqual(summary['failed'], ['4674'])
        self.assertIn('株価取得 1/2銘柄', page)
        self.assertIn(rows[1]['quoteAt'], page)
        self.assertIn('updateStatus:"failed"', page)
        self.assertIn('price:4900', page)

    def test_all_sources_failed_does_not_claim_fresh_data(self):
        root, _, original = self.run_main(all_fail=True)
        self.assertEqual((root/'d1/index.html').read_text(), original)
        self.assertEqual(json.loads((root/'data/last_update.json').read_text())['updated'], [])

    def test_all_37_reviewed_dividends_and_changed_period_guard(self):
        self.assertEqual(len(ROWS), 37)
        for code, review in u.DIVIDEND_REVIEWS.items():
            self.assertEqual(u.calculation_dividend(code, review['reported_div'], review['period'])[0], review['calculation_div'])
            with self.assertRaises(ValueError):
                u.calculation_dividend(code, review['reported_div'], '2099/03')


if __name__ == '__main__':
    unittest.main()
