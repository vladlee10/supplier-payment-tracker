"""Supplier changes preserve ledger links and require a current confirmation form."""
from html.parser import HTMLParser
from pathlib import Path
import sqlite3
import tempfile
import unittest

import main
from invoice_storage import initialize_database


class FormParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and 'name' in attrs:
            self.values[attrs['name']] = attrs.get('value', '')


class SupplierManagementTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='supplier-management-test-')
        self.path = Path(self.directory.name) / 'ledger.db'
        initialize_database(self.path)
        with sqlite3.connect(self.path) as db:
            db.executemany('INSERT INTO suppliers(id,name) VALUES (?,?)', [
                (1, 'Поставщик с историей'), (2, 'Пустой'), (3, 'С авансом'), (4, 'Другой')])
            db.execute("INSERT INTO deliveries(id,supplier_id,delivery_date,amount_minor) VALUES(1,1,'2026-11-01',100000)")
            db.executemany('INSERT INTO payments(id,supplier_id,payment_date,amount_minor) VALUES(?,?,?,?)', [
                (1,1,'2026-11-02',100000), (2,3,'2026-11-03',250000)])
            db.execute('INSERT INTO payment_allocations(payment_id,delivery_id,amount_minor) VALUES(1,1,100000)')
        self.old_path = main.DB_PATH
        main.DB_PATH = self.path
        main.app.config['TESTING'] = True
        self.client = main.app.test_client()

    def tearDown(self):
        main.DB_PATH = self.old_path
        self.directory.cleanup()

    def record(self, supplier_id):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT id,name FROM suppliers WHERE id=?', (supplier_id,)).fetchone()

    def ledger(self):
        with sqlite3.connect(self.path) as db:
            return {t:db.execute('SELECT * FROM '+t+' ORDER BY id').fetchall()
                    for t in ('deliveries','payments','payment_allocations','invoice_items')}

    def form(self, supplier_id, action):
        response = self.client.get(f'/suppliers/{supplier_id}/{action}')
        self.assertEqual(response.status_code, 200)
        return FormParser(response.get_data(as_text=True)).values

    def test_list_and_get_pages_do_not_change_records(self):
        before = self.ledger()
        response = self.client.get('/suppliers')
        html = response.get_data(as_text=True)
        self.assertIn('/suppliers/1/edit', html)
        self.assertIn('/suppliers/1/delete', html)
        self.form(2, 'delete')
        edit = self.client.get('/suppliers/1/edit').get_data(as_text=True)
        self.assertIn('aria-current="page"', edit)
        self.assertIn('href="/suppliers" class="active"', edit)
        self.assertEqual(self.record(2), (2, 'Пустой'))
        self.assertEqual(self.ledger(), before)

    def test_rename_keeps_id_paid_invoice_and_all_ledger_rows(self):
        before = self.ledger()
        form = self.form(1, 'edit')
        form['name'] = '  Новое название  '
        response = self.client.post('/suppliers/1/edit', data=form)
        self.assertEqual(response.status_code, 302)
        self.assertIn('result=updated', response.location)
        self.assertEqual(self.record(1), (1, 'Новое название'))
        self.assertEqual(self.ledger(), before)
        for url in ('/deliveries', '/payments?supplier_id=1', '/paid-invoices', '/invoices/1'):
            html = self.client.get(url).get_data(as_text=True)
            self.assertIn('Новое название', html, url)
        self.assertIn('Название поставщика сохранено', self.client.get(response.location).get_data(as_text=True))

    def test_duplicate_blank_long_names_and_html_are_handled(self):
        for name, message in [('Другой','уже существует'), ('  ','Введите название'), ('x'*201,'не длиннее 200')]:
            form = self.form(2, 'edit'); form['name'] = name
            response = self.client.post('/suppliers/2/edit', data=form)
            self.assertEqual(response.status_code, 400)
            self.assertIn(message, response.get_data(as_text=True))
            self.assertEqual(self.record(2), (2, 'Пустой'))
        form = self.form(2, 'edit'); form['name'] = '<script>alert(1)</script>'
        self.assertEqual(self.client.post('/suppliers/2/edit', data=form).status_code, 302)
        html = self.client.get('/suppliers').get_data(as_text=True)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_delete_empty_card_requires_confirmation_and_preserves_history(self):
        before = self.ledger()
        response = self.client.post('/suppliers/2/delete', data={})
        self.assertEqual(response.status_code, 400)
        self.assertIsNotNone(self.record(2))
        form = self.form(2, 'delete')
        response = self.client.post('/suppliers/2/delete', data=form)
        self.assertEqual(response.status_code, 302)
        self.assertIn('result=deleted', response.location)
        self.assertIsNone(self.record(2))
        self.assertEqual(self.ledger(), before)
        self.assertEqual(self.client.post('/suppliers/2/delete', data=form).status_code, 404)

    def test_paid_history_and_advance_block_deletion(self):
        before = self.ledger()
        for supplier_id in (1,3):
            response = self.client.get(f'/suppliers/{supplier_id}/delete')
            html = response.get_data(as_text=True)
            self.assertIn('удаление недоступно', html)
            self.assertNotIn('class="supplier-danger-button"', html)
            self.assertEqual(self.client.post(f'/suppliers/{supplier_id}/delete').status_code, 409)
            self.assertIsNotNone(self.record(supplier_id))
        self.assertEqual(self.ledger(), before)

    def test_new_history_after_confirmation_is_checked_on_post(self):
        form = self.form(2, 'delete')
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO deliveries(supplier_id,delivery_date,amount_minor) VALUES(2,'2026-11-05',25000)")
        before = self.ledger()
        response = self.client.post('/suppliers/2/delete', data=form)
        self.assertEqual(response.status_code, 409)
        self.assertIn('Удалить поставщика нельзя', response.get_data(as_text=True))
        self.assertIsNotNone(self.record(2))
        self.assertEqual(self.ledger(), before)

    def test_stale_forged_and_cross_action_forms_are_rejected(self):
        stale_edit = self.form(2, 'edit')
        stale_delete = self.form(2, 'delete')
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE suppliers SET name='Уже изменён' WHERE id=2")
        stale_edit['name'] = 'Перезапись'
        self.assertEqual(self.client.post('/suppliers/2/edit', data=stale_edit).status_code, 400)
        self.assertEqual(self.client.post('/suppliers/2/delete', data=stale_delete).status_code, 400)
        self.assertEqual(self.record(2), (2, 'Уже изменён'))
        delete_form = self.form(2, 'delete'); delete_form['name'] = 'Wrong action'
        self.assertEqual(self.client.post('/suppliers/2/edit', data=delete_form).status_code, 400)
        edit_form = self.form(2, 'edit'); edit_form['form_token'] += 'tampered'
        self.assertEqual(self.client.post('/suppliers/2/edit', data=edit_form).status_code, 400)
        form = self.form(2, 'delete')
        self.assertEqual(self.client.post('/suppliers/4/delete', data=form).status_code, 400)
        self.assertIsNotNone(self.record(4))

    def test_missing_and_out_of_range_ids(self):
        for supplier_id in (999,2**70):
            for action in ('edit','delete'):
                self.assertEqual(self.client.get(f'/suppliers/{supplier_id}/{action}').status_code, 404)
                self.assertEqual(self.client.post(f'/suppliers/{supplier_id}/{action}', data={}).status_code, 404)


if __name__ == '__main__':
    unittest.main()
