"""Language isolation, navigation, exact amounts and bilingual invoice workflows."""
import ast
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unittest

import main
from dashboard import count_label, overview
from i18n import ENGLISH, get_language, localize_message, translate
from money import money_text, parse_money
import test_ledger_corrections as ledger_fixture


class TextParser(HTMLParser):
    def __init__(self, html):
        super().__init__(); self.skip = 0; self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'): self.skip += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style'): self.skip -= 1

    def handle_data(self, text):
        if not self.skip: self.text.append(text)


class LanguageTest(unittest.TestCase):
    def setUp(self):
        self.fixture = ledger_fixture.CorrectionsTest()
        self.fixture.setUp()
        self.client = self.fixture.client
        draft = dict(token='c'*32, extension='.png', invoice_number='TEST-2',
                     delivery_date='2026-11-02', supplier_name='Роял', buyer='Магазин',
                     printed_total='20.00', vat_total='0.00', vat_mode='included',
                     expected_count='1', source_sha256='c'*64, items=[dict(name='Товар В', quantity='2', unit='шт',
                     unit_price='10.00', line_total='20.00', discount='0.00', vat_rate='0', vat_amount='0.00',
                     notes=['Количество восстановлено по сумме и цене — проверьте.'])],
                     warnings=['Не удалось собрать товарные строки. Проверьте резкость фотографии.',
                               'На фото указано строк: 21, распознано: 20.'])
        (self.fixture.photos/(draft['token']+'.json')).write_text(json.dumps(draft))

    def tearDown(self):
        self.fixture.tearDown()

    def switch(self, language, path='/'):
        return self.client.post('/language', data=dict(language=language, next=path))

    def test_default_persistent_language_and_same_filtered_page(self):
        before = self.fixture.dump()
        self.assertIn('<html lang="ru">', self.client.get('/').text)
        response = self.switch('en', '/payments?supplier_id=1')
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, '/payments?supplier_id=1')
        self.assertIn('Max-Age=31536000', response.headers['Set-Cookie'])
        self.assertIn('SameSite=Lax', response.headers['Set-Cookie'])
        response = self.client.get(response.location)
        self.assertIn('<html lang="en">', response.text)
        self.assertIn('open invoices', response.text)
        self.assertIn('Роял', response.text)
        self.assertEqual(response.headers['Content-Language'], 'en')
        self.assertIn('Cookie', response.headers['Vary'])
        # Separate clients do not share language; a new client with the same
        # persistent cookie does, without server session state.
        other = main.app.test_client()
        self.assertIn('<html lang="ru">', other.get('/').text)
        other.set_cookie('ui_language', 'en')
        self.assertIn('<html lang="en">', other.get('/').text)
        self.switch('ru')
        self.assertIn('<html lang="ru">', self.client.get('/').text)
        self.assertEqual(self.fixture.dump(), before)

    def test_all_english_pages_and_saved_user_data_unchanged(self):
        self.switch('en')
        routes = ['/', '/suppliers', '/deliveries', '/payments', '/payments?supplier_id=1',
                  '/paid-invoices', '/invoices/import', '/invoices/1', '/invoices/1/edit',
                  '/invoices/2/edit', '/suppliers/1/edit', '/suppliers/1/delete',
                  '/payments/1/cancel', '/payments/3/allocate', '/invoices/drafts/'+'c'*32]
        allowed = ['Роял', 'С авансом', 'ТЕСТ-1', 'Накладная №', 'Без товарных строк',
                   'Магазин', 'Товар А', 'Товар Б', 'Товар В', 'За накладную', 'Часть оплаты', 'Аванс', 'шт']
        for route in routes:
            with self.subTest(route=route):
                response=self.client.get(route)
                self.assertEqual(response.status_code, 200)
                visible=' '.join(TextParser(response.text).text)
                for value in allowed: visible=visible.replace(value,'')
                visible=visible.replace(' Р ', ' ').replace(' С ', ' ')
                self.assertNotRegex(visible, '[А-Яа-яЁё]')
                self.assertIn('<html lang="en">',response.text)
        detail=self.client.get('/invoices/1').text
        self.assertIn('Товар А',detail)
        self.assertIn('Edit supplier “Роял”',self.client.get('/suppliers').text)

    def test_invalid_language_and_external_return_paths(self):
        self.switch('en')
        response=self.switch('ko')
        self.assertEqual(response.status_code,400)
        self.assertIn('Choose Russian or English.',response.text)
        self.assertNotIn('Set-Cookie',response.headers)
        for path in ['https://example.com', '//example.com', '/\\example.com',
                     '/%2fexample.com', '/%255cexample.com', '/%0d%0aLocation:bad']:
            with self.subTest(path=path): self.assertEqual(self.switch('en',path).location,'/')
        self.client.set_cookie('ui_language','unsupported')
        self.assertIn('<html lang="ru">',self.client.get('/').text)

    def test_errors_validation_and_ocr_warnings_in_english(self):
        self.switch('en')
        self.assertIn('Enter a supplier name.',self.client.post('/suppliers',data=dict(name=' ')).text)
        self.assertIn('Choose a supplier',self.client.post('/deliveries',data={}).text)
        self.assertIn('Choose an invoice photo.',self.client.post('/invoices/import',data={}).text)
        response=self.client.post('/invoices/1/edit',data=self.fixture.edit_form(item_quantity=['2','2']))
        self.assertEqual(response.status_code,400)
        self.assertIn('Row 1:',response.text)
        self.assertIn('Quantity × price',response.text)
        response=self.client.post('/payments/1/cancel',data=dict(form_token=self.fixture.form_token('/payments/1/cancel')))
        self.assertIn('Enter a reason',response.text)
        draft=self.client.get('/invoices/drafts/'+'c'*32).text
        self.assertIn('Rows on the photo: 21; recognized: 20.',draft)
        self.assertIn('quantity was calculated',draft)
        self.assertNotIn('Количество восстановлено',draft)

    def test_money_and_months_are_per_request_and_arithmetic_stays_exact(self):
        self.assertEqual(money_text(123456), '1 234,56')
        for language, expected, caption in [('en','1,234.56','November 2026'),('ru','1 234,56','Ноябрь 2026')]:
            with main.app.test_request_context('/',headers={'Cookie':f'ui_language={language}'}):
                self.assertEqual(money_text(123456), expected)
                self.assertEqual(parse_money('1234.56'),123456)
                self.assertEqual(parse_money('1234,56'),123456)
                self.assertEqual(overview(self.fixture.path,'2026-11')['month_label'],caption)
                if language=='en':
                    self.assertEqual(count_label(21,('invoice','invoices','invoices')),'21 invoices')
                    self.assertEqual(count_label(1,('invoice','invoices','invoices')),'1 invoice')

    def test_english_edit_and_cancel_preserve_audit_and_precision(self):
        self.switch('en')
        data=self.fixture.edit_form(item_quantity=['2','2'],item_line_total=['100.00','50.00'],printed_total='150.00')
        self.assertEqual(self.client.post('/invoices/1/edit',data=data).status_code,302)
        detail=self.client.get('/invoices/1').text
        self.assertIn('Before correction',detail)
        self.assertIn('After correction',detail)
        self.assertEqual(self.fixture.scalar('SELECT amount_minor FROM deliveries WHERE id=1'),15000)
        self.assertEqual(self.client.post('/payments/1/cancel',data=self.fixture.cancel_form()).status_code,302)
        self.assertIn('Payment entry cancelled',self.client.get('/payments/1/cancel').text)
        self.assertEqual(self.fixture.scalar('SELECT COUNT(*) FROM payments'),3)

    def test_catalog_covers_template_calls_and_placeholder_names(self):
        from string import Formatter
        root=Path(__file__).parent
        for p in root.glob('templates/*.html'):
            for match in re.finditer(r"_\(('(?:[^'\\]|\\.)*')",p.read_text()):
                key=ast.literal_eval(match[1])
                self.assertIn(key,ENGLISH,(str(p),key))
        for source,target in ENGLISH.items():
            fields=lambda text:{field for _,field,_,_ in Formatter().parse(text) if field is not None}
            self.assertEqual(fields(source),fields(target),source)


if __name__=='__main__': unittest.main()
