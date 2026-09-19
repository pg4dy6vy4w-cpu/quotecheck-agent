"""Offline regression tests. Only external Apify/PDF imports are isolated.

Run with: python -m unittest discover -s tests -v
PDF tests exercise extracted text; no network, billing, or live storage is used.
"""
import ast
import asyncio
import json
import pathlib
import unittest

source = pathlib.Path(__file__).resolve().parents[1] / 'src/main.py'
tree = ast.parse(source.read_text())
tree.body = [node for node in tree.body if not (
    isinstance(node, ast.ImportFrom) and node.module in ('apify', 'pypdf'))]
module = {'__name__': 'quotecheck_test'}
exec(compile(tree, str(source), 'exec'), module)


class ActorDouble:
    class Log:
        def info(self, *args):
            pass
    log = Log()
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def get_input(self):
        return {'quotes': self.quotes}
    async def push_data(self, result):
        self.result = result
    async def set_value(self, key, value, **kwargs):
        json.loads(value, parse_constant=lambda value: self.reject(value))
    def reject(self, value):
        raise AssertionError('Invalid JSON constant: ' + value)


def item(name='Office chair', price=10, quantity=1, **extra):
    return dict(description=name, unit_price=price, quantity=quantity, **extra)


def quote(supplier, items=None, **extra):
    return dict(supplier=supplier, items=items if items is not None else [item()], **extra)


class RegressionTests(unittest.TestCase):
    def run_quotes(self, quotes):
        actor = ActorDouble()
        actor.quotes = quotes
        module['Actor'] = actor
        asyncio.run(module['main']())
        json.dumps(actor.result, allow_nan=False)
        return actor.result

    def assert_review(self, quotes):
        result = self.run_quotes(quotes)
        self.assertTrue(result['review_required'])
        self.assertFalse(result['totals_comparable'])
        self.assertIsNone(result['lowest_total_supplier'])
        self.assertNotIn('lowest_normalized_total_supplier', result)
        return result

    def test_baseline(self):
        result = self.run_quotes([quote('A'), quote('B', [item(price=12)])])
        self.assertFalse(result['review_required'])
        self.assertEqual(result['lowest_total_supplier'], 'A')

    def test_mixed_currencies(self):
        self.assert_review([quote('A', currency='GBP'), quote('B', currency='USD')])

    def test_partial_currency(self):
        self.assert_review([quote('A', currency='GBP'), quote('B')])

    def test_symbol_currencies(self):
        self.assert_review([quote('A', [item(price='£10')]), quote('B', [item(price='$9')])])

    def test_quote_total(self):
        self.assert_review([quote('A', total=1), quote('B')])

    def test_line_total(self):
        self.assert_review([quote('A', [item(total=1)]), quote('B')])

    def test_missing_price_with_total(self):
        self.assert_review([quote('A', [item(price=None)], total=1), quote('B')])

    def test_invalid_numbers(self):
        for value in ['NaN', 'Infinity', '-Infinity', -1, True]:
            with self.subTest(value=value):
                self.assert_review([quote('A', [item(price=value)]), quote('B')])

    def test_incomplete_baskets(self):
        self.assert_review([quote('A'), quote('B', [item(), item('Printer')]), quote('C', [item('Printer')])])

    def test_four_suppliers(self):
        result = self.run_quotes([quote(supplier) for supplier in 'ABCD'])
        self.assertFalse(result['review_required'])
        self.assertEqual(len(result['matched_items']), 1)
        self.assertEqual(result['matched_items'][0]['missing_suppliers'], [])

    def test_duplicate_suppliers(self):
        self.assert_review([quote('A'), quote('A')])

    def test_decimal_comma_pdf_text(self):
        quotes = [module['parse_pdf_quote']('Supplier: '+supplier+'\nOffice chair 2 €100,50 €201,00', supplier+'.pdf', index)
                  for index, supplier in enumerate('AB')]
        result = self.run_quotes(quotes)
        self.assertEqual(result['supplier_totals'], {'A': 201.0, 'B': 201.0})
        self.assertFalse(result['review_required'])

    def test_money_formats(self):
        for value, expected in [('1,234.56',1234.56),('1.234,56',1234.56),('100,50',100.5),('1,234',1234)]:
            self.assertEqual(module['money'](value), expected)

    def test_quantity_normalization(self):
        result = self.run_quotes([quote('A', [item(quantity=2)]), quote('B', [item(price=12)])])
        self.assertTrue(result['review_required'])
        self.assertEqual(result['normalized_supplier_totals'], {'A':20.0,'B':24.0})

    def test_model_mismatch(self):
        result = self.run_quotes([quote('A', [item('Latitude 7450')]),quote('B', [item('Latitude 7440')])])
        self.assertTrue(result['review_required'])
        self.assertIsNone(result['lowest_total_supplier'])

    def test_explicit_dollar_currency(self):
        result = self.run_quotes([quote('A', [item(price='$10')], currency='USD'), quote('B', currency='USD')])
        self.assertFalse(result['review_required'])

    def test_pdf_currency_mismatch(self):
        quotes = [module['parse_pdf_quote']('Supplier: '+supplier+'\nOffice chair 2 '+symbol+'100.00 '+symbol+'200.00', supplier+'.pdf', index)
                  for index, (supplier, symbol) in enumerate([('A','£'), ('B','$')])]
        self.assert_review(quotes)
