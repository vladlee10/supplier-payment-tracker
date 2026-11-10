"""Accounting corrections, audit history, stale forms and atomic rollback."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from zoneinfo import ZoneInfo

from werkzeug.datastructures import MultiDict
import main
from dashboard import overview
from invoice_storage import initialize_database
from test_supplier_management import FormParser


class CorrectionsTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ledger-corrections-test-')
        self.path=Path(self.temp.name)/'ledger.db'
        initialize_database(self.path)
        self.photos=Path(self.temp.name)/'photos'; self.photos.mkdir()
        self.token='a'*32
        (self.photos/(self.token+'.json')).write_text(json.dumps(dict(extension='.png')))
        (self.photos/(self.token+'.png')).write_bytes(b'original-photo-fixture')
        with sqlite3.connect(self.path) as db:
            db.executemany('INSERT INTO suppliers(id,name) VALUES(?,?)',[(1,'Роял'),(2,'С авансом')])
            db.executemany('INSERT INTO deliveries(id,supplier_id,delivery_date,amount_minor,note) VALUES(?,?,?,?,?)',[
                (1,1,'2026-11-01',10000,'Накладная № ТЕСТ-1'),(2,1,'2026-11-02',20000,'Без товарных строк')])
            db.execute('''INSERT INTO invoice_documents(delivery_id,invoice_number,buyer,draft_token,image_extension,
                source_sha256,printed_total_minor,vat_total_minor,vat_mode,created_at) VALUES(1,'ТЕСТ-1','Магазин',?,'.png',?,10000,0,'included','2026-11-01T12:00:00+09:00')''',(self.token,'b'*64))
            db.executemany('''INSERT INTO invoice_items(delivery_id,position,name,quantity,unit,unit_price_minor,vat_rate,vat_amount_minor,line_total_minor)
                VALUES(1,?,?,?,'шт',?,'0',0,5000)''',[(1,'Товар А','1',5000),(2,'Товар Б','2',2500)])
            db.executemany('INSERT INTO payments(id,supplier_id,payment_date,amount_minor,note) VALUES(?,?,?,?,?)',[
                (1,1,'2026-11-01',10000,'За накладную'),(2,1,'2026-11-02',5000,'Часть оплаты'),(3,2,'2026-11-03',2500,'Аванс')])
            db.executemany('INSERT INTO payment_allocations(payment_id,delivery_id,amount_minor) VALUES(?,?,?)',[(1,1,10000),(2,2,5000)])
        self.old_path=main.DB_PATH;main.DB_PATH=self.path
        self.old_storage=main.app.config['INVOICE_STORAGE'];main.app.config['INVOICE_STORAGE']=str(self.photos)
        main.app.config['TESTING']=True;self.client=main.app.test_client()

    def tearDown(self):
        main.DB_PATH=self.old_path;main.app.config['INVOICE_STORAGE']=self.old_storage
        self.temp.cleanup()

    def scalar(self,sql,args=()):
        with sqlite3.connect(self.path) as db:return db.execute(sql,args).fetchone()[0]

    def dump(self):
        with sqlite3.connect(self.path) as db:return '\n'.join(db.iterdump())

    def form_token(self,path):
        response=self.client.get(path)
        self.assertEqual(response.status_code,200)
        return FormParser(response.get_data(as_text=True)).values['form_token']

    def cancel_form(self,payment_id=1):
        return dict(form_token=self.form_token(f'/payments/{payment_id}/cancel'),reason='Ошибочно записали оплату')

    def edit_form(self,**updates):
        data=MultiDict(dict(form_token=self.form_token('/invoices/1/edit'),reason='Исправили по оригиналу',
            invoice_number='ТЕСТ-1',delivery_date='2026-11-01',buyer='Магазин',printed_total='100.00',
            vat_total='0.00',vat_mode='included',expected_count='',reviewed='yes',discounts_reviewed='yes'))
        for row in [('Товар А','1','50.00','50.00'),('Товар Б','2','25.00','50.00')]:
            for key,value in zip(('name','quantity','unit_price','line_total'),row):data.add('item_'+key,value)
            for key,value in [('unit','шт'),('vat_rate','0'),('vat_amount','0.00'),('discount','0.00')]:data.add('item_'+key,value)
        for key,value in updates.items():data.setlist(key,value if isinstance(value,list) else [value])
        return data

    def test_cancel_full_payment_reopens_invoice_and_updates_every_balance(self):
        before=overview(self.path,'2026-11')
        self.assertEqual(before['debt'],15000)
        self.assertEqual(before['pending'],1)
        self.assertIn('ТЕСТ-1',self.client.get('/paid-invoices').get_data(as_text=True))
        form=self.cancel_form()
        response=self.client.post('/payments/1/cancel',data=form)
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM payments'),3)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM payment_allocations'),2)
        self.assertEqual(self.scalar('SELECT SUM(amount_minor) FROM active_payment_allocations WHERE delivery_id=1') or 0,0)
        after=overview(self.path,'2026-11')
        self.assertEqual(after['debt'],25000)
        self.assertEqual(after['pending'],2)
        self.assertEqual(after['monthly_payments'],7500)
        self.assertNotIn('ТЕСТ-1',self.client.get('/paid-invoices').get_data(as_text=True))
        history=self.client.get('/payments?supplier_id=1').get_data(as_text=True)
        self.assertIn('Отменена',history);self.assertIn(form['reason'],history)
        self.assertIn('ТЕСТ-1',history)
        cancelled=self.client.get('/payments/1/cancel').get_data(as_text=True)
        self.assertIn('Прежние привязки',cancelled)
        self.assertNotIn('class="ledger-danger"',cancelled)
        when=self.scalar('SELECT cancelled_at FROM payments WHERE id=1')
        self.assertEqual(datetime.fromisoformat(when).date(),datetime.now(ZoneInfo('Asia/Seoul')).date())
        before_repeat=self.dump()
        self.assertEqual(self.client.post('/payments/1/cancel',data=form).status_code,302)
        self.assertEqual(self.dump(),before_repeat)
        self.assertEqual(self.client.post('/payments/1/allocate',data=dict(delivery_id=2,amount_krw='10',expected_available=1000,expected_remaining=15000)).status_code,302)
        self.assertEqual(self.dump(),before_repeat)

    def test_cancel_partial_unallocated_and_multi_invoice_payments(self):
        response=self.client.post('/payments/2/cancel',data=self.cancel_form(2))
        self.assertEqual(response.status_code,302)
        self.assertEqual(overview(self.path,'2026-11')['debt'],20000)
        self.assertEqual(self.scalar('SELECT SUM(amount_minor) FROM active_payment_allocations WHERE delivery_id=2') or 0,0)
        self.assertEqual(self.client.post('/payments/3/cancel',data=self.cancel_form(3)).status_code,302)
        by_id={s['id']:s for s in overview(self.path,'2026-11')['suppliers']}
        self.assertEqual(by_id[2]['credit'],0)
        # Supplier history is kept even for a cancelled advance.
        self.assertIn('удаление недоступно',self.client.get('/suppliers/2/delete').get_data(as_text=True))
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE payments SET amount_minor=5000 WHERE id=1')
            db.execute('UPDATE payment_allocations SET amount_minor=5000 WHERE payment_id=1')
            db.execute("INSERT INTO payments(id,supplier_id,payment_date,amount_minor) VALUES(4,1,'2026-11-03',10000)")
            db.executemany('INSERT INTO payment_allocations(payment_id,delivery_id,amount_minor) VALUES(4,?,?)',[(1,5000),(2,5000)])
        self.assertEqual(self.client.post('/payments/4/cancel',data=self.cancel_form(4)).status_code,302)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM payment_allocations WHERE payment_id=4'),2)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM active_payment_allocations WHERE payment_id=4'),0)
        self.assertEqual(self.scalar('SELECT SUM(amount_minor) FROM active_payment_allocations WHERE delivery_id=1'),5000)

    def test_correct_payment_can_be_recorded_again_after_cancellation(self):
        self.assertEqual(self.client.post('/payments/1/cancel',data=self.cancel_form()).status_code,302)
        response=self.client.post('/payments',data=dict(supplier_id='1',delivery_id='1',expected_remaining='10000'))
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM payments'),4)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM active_payments'),3)
        self.assertEqual(self.scalar('SELECT SUM(amount_minor) FROM active_payment_allocations WHERE delivery_id=1'),10000)
        self.assertEqual(overview(self.path,'2026-11')['debt'],15000)
        self.assertIn('ТЕСТ-1',self.client.get('/paid-invoices').get_data(as_text=True))

    def test_cancellation_requires_reason_current_token_and_allocations(self):
        before=self.dump()
        for form in ({'reason':'test'},dict(self.cancel_form(),reason=''),dict(self.cancel_form(),reason='x'*1001)):
            self.assertEqual(self.client.post('/payments/1/cancel',data=form).status_code,400)
            self.assertEqual(self.dump(),before)
        self.assertEqual(self.client.post('/payments/2/cancel',data=self.cancel_form(1)).status_code,400)
        with sqlite3.connect(self.path) as db:db.execute('UPDATE payment_allocations SET amount_minor=2500 WHERE payment_id=2')
        form=self.cancel_form(2)
        with sqlite3.connect(self.path) as db:db.execute('UPDATE payment_allocations SET amount_minor=5000 WHERE payment_id=2')
        response=self.client.post('/payments/2/cancel',data=form)
        self.assertEqual(response.status_code,400)
        self.assertIn('изменились в другой вкладке',response.get_data(as_text=True))
        self.assertIsNone(self.scalar('SELECT cancelled_at FROM payments WHERE id=2'))

    def test_edit_full_invoice_opens_remainder_and_keeps_complete_audit_and_photo(self):
        photo=self.photos/(self.token+'.png');photo_hash=hashlib.sha256(photo.read_bytes()).digest()
        data=self.edit_form(delivery_date='2026-10-30',invoice_number='ТЕСТ-ИСПРАВЛЕН',buyer='Другой магазин',
            printed_total='125.00',item_quantity=['1.5','2'],item_line_total=['75.00','50.00'],item_name=['Товар А новый','Товар Б'])
        response=self.client.post('/invoices/1/edit',data=data)
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.scalar('SELECT amount_minor FROM deliveries WHERE id=1'),12500)
        self.assertEqual(self.scalar('SELECT supplier_id FROM deliveries WHERE id=1'),1)
        self.assertEqual(self.scalar('SELECT printed_total_minor FROM invoice_documents WHERE delivery_id=1'),12500)
        self.assertEqual(self.scalar('SELECT SUM(line_total_minor) FROM invoice_items WHERE delivery_id=1'),12500)
        self.assertEqual(self.scalar('SELECT amount_minor FROM payments WHERE id=1'),10000)
        self.assertEqual(self.scalar('SELECT amount_minor FROM payment_allocations WHERE payment_id=1'),10000)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM invoice_revisions'),1)
        with sqlite3.connect(self.path) as db:
            before,after=db.execute('SELECT before_json,after_json FROM invoice_revisions').fetchone()
        before,after=json.loads(before),json.loads(after)
        self.assertEqual(before['items'][0]['quantity'],'1')
        self.assertEqual(after['items'][0]['quantity'],'1.5')
        self.assertEqual(after['items'][0]['name'],'Товар А новый')
        self.assertEqual(before['document']['invoice_number'],'ТЕСТ-1')
        self.assertEqual(after['document']['invoice_number'],'ТЕСТ-ИСПРАВЛЕН')
        self.assertEqual(hashlib.sha256(photo.read_bytes()).digest(),photo_hash)
        self.assertEqual(overview(self.path,'2026-11')['monthly_deliveries'],20000)
        self.assertEqual(overview(self.path,'2026-10')['monthly_deliveries'],12500)
        self.assertEqual(overview(self.path,'2026-11')['debt'],17500)
        self.assertNotIn('ТЕСТ-ИСПРАВЛЕН',self.client.get('/paid-invoices').get_data(as_text=True))
        detail=self.client.get(response.location).get_data(as_text=True)
        for text in ['История изменений','До исправления','После исправления','Товар А новый','Другой магазин','Исправили по оригиналу']:
            self.assertIn(text,detail)
        again=self.client.post('/invoices/1/edit',data=data)
        self.assertEqual(again.status_code,400)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM invoice_revisions'),1)

    def test_paid_floor_and_cancelled_allocations_are_distinguished(self):
        data=self.edit_form(printed_total='90.00',item_quantity=['0.8','2'],item_line_total=['40.00','50.00'])
        before=self.dump()
        response=self.client.post('/invoices/1/edit',data=data)
        self.assertEqual(response.status_code,400)
        self.assertIn('уже привязано 100',response.get_data(as_text=True))
        self.assertEqual(self.dump(),before)
        self.client.post('/payments/1/cancel',data=self.cancel_form())
        # A form opened before cancellation is stale.
        self.assertEqual(self.client.post('/invoices/1/edit',data=data).status_code,400)
        data=self.edit_form(printed_total='90.00',item_quantity=['0.8','2'],item_line_total=['40.00','50.00'])
        self.assertEqual(self.client.post('/invoices/1/edit',data=data).status_code,302)
        self.assertEqual(self.scalar('SELECT amount_minor FROM deliveries WHERE id=1'),9000)
        self.assertEqual(self.scalar('SELECT SUM(amount_minor) FROM payment_allocations WHERE delivery_id=1'),10000)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM active_payment_allocations WHERE delivery_id=1'),0)

    def test_legacy_summary_edits_and_noop_do_not_fabricate_document_or_history(self):
        before=self.dump()
        self.assertEqual(self.client.post('/invoices/1/edit',data=self.edit_form()).status_code,302)
        self.assertEqual(self.dump(),before)
        form=dict(form_token=self.form_token('/invoices/2/edit'),delivery_date='2026-10-29',printed_total='175.25',note='Исправлена сумма',reason='Сверили накладную')
        self.assertEqual(self.client.post('/invoices/2/edit',data=form).status_code,302)
        self.assertEqual(self.scalar('SELECT amount_minor FROM deliveries WHERE id=2'),17525)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM invoice_documents WHERE delivery_id=2'),0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM invoice_revisions WHERE delivery_id=2'),1)
        self.assertIn('Сверили накладную',self.client.get('/invoices/2').get_data(as_text=True))
        data=self.edit_form(item_name=['<script>alert(1)</script>','Товар Б'])
        self.assertEqual(self.client.post('/invoices/1/edit',data=data).status_code,302)
        html=self.client.get('/invoices/1').get_data(as_text=True)
        self.assertNotIn('<script>alert(1)</script>',html)
        self.assertIn('&lt;script&gt;',html)

    def test_invalid_rows_total_reason_duplicates_and_token_leave_everything_unchanged(self):
        before=self.dump()
        for updates in [dict(printed_total='99'),dict(item_quantity=['0','2']),dict(item_unit_price=['NaN','25']),
                        dict(reason=''),dict(reviewed=''),dict(form_token='forged'),dict(item_discount=['1','0'])]:
            response=self.client.post('/invoices/1/edit',data=self.edit_form(**updates))
            self.assertEqual(response.status_code,400,updates)
            self.assertEqual(self.dump(),before)
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO invoice_documents(delivery_id,invoice_number,draft_token,image_extension,source_sha256,printed_total_minor,vat_mode,created_at) VALUES(2,'ДРУГОЙ',?,'.png',?,20000,'included','2026-11-01T00:00:00+09:00')",('c'*32,'d'*64))
        before=self.dump()
        data=self.edit_form(delivery_date='2026-11-02',invoice_number='ДРУГОЙ')
        response=self.client.post('/invoices/1/edit',data=data)
        self.assertEqual(response.status_code,400)
        self.assertIn('уже есть другая накладная',response.get_data(as_text=True))
        self.assertEqual(self.dump(),before)

    def test_removing_number_preserves_paid_invoice_and_history(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE deliveries SET delivery_date='2026-11-01' WHERE id=2")
            db.execute("INSERT INTO invoice_documents(delivery_id,invoice_number,draft_token,image_extension,source_sha256,printed_total_minor,vat_mode,created_at) VALUES(2,'',?,'.png',?,20000,'included','2026-11-01T00:00:00+09:00')",('c'*32,'d'*64))
        response=self.client.post('/invoices/1/edit',data=self.edit_form(invoice_number=''))
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.scalar('SELECT invoice_number FROM invoice_documents WHERE delivery_id=1'),'')
        self.assertEqual(self.scalar('SELECT note FROM deliveries WHERE id=1'),'')
        self.assertEqual(self.scalar('SELECT amount_minor FROM active_payment_allocations WHERE delivery_id=1'),10000)
        self.assertIn('Накладная — запись #1',self.client.get(response.location).text)
        self.assertIn('ТЕСТ-1',self.client.get(response.location).text)
        self.assertIn('>Не указан</dd>',self.client.get(response.location).text)
        self.assertIn('href="/invoices/1">#1</a>',self.client.get('/paid-invoices').text)

    def test_failure_in_items_or_history_rolls_back_the_entire_edit(self):
        for target in ('invoice_items','invoice_revisions'):
            with sqlite3.connect(self.path) as db:
                db.execute(f"CREATE TRIGGER fail_save BEFORE INSERT ON {target} BEGIN SELECT RAISE(ABORT,'test rollback'); END")
            before=self.dump()
            response=self.client.post('/invoices/1/edit',data=self.edit_form(item_name=['Changed','Товар Б']))
            self.assertEqual(response.status_code,400)
            self.assertEqual(self.dump(),before)
            with sqlite3.connect(self.path) as db:db.execute('DROP TRIGGER fail_save')

    def test_gets_missing_ids_and_repeatable_migration(self):
        before=self.dump()
        for url in ['/invoices/1/edit','/invoices/2/edit','/payments/1/cancel','/invoices/1']:
            self.assertEqual(self.client.get(url).status_code,200)
        self.assertEqual(self.dump(),before)
        for record_id in (999,2**70):
            for url in (f'/payments/{record_id}/cancel',f'/invoices/{record_id}/edit'):
                self.assertEqual(self.client.get(url).status_code,404)
                self.assertEqual(self.client.post(url).status_code,404)
        initialize_database(self.path)
        self.assertEqual(self.dump(),before)
        with sqlite3.connect(self.path) as db:
            db.execute('DROP VIEW active_payment_allocations');db.execute('DROP VIEW active_payments')
            db.execute('ALTER TABLE payments DROP COLUMN cancelled_at');db.execute('ALTER TABLE payments DROP COLUMN cancellation_reason')
            old=db.execute('SELECT id,supplier_id,payment_date,amount_minor,note FROM payments ORDER BY id').fetchall()
        initialize_database(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT id,supplier_id,payment_date,amount_minor,note FROM payments ORDER BY id').fetchall(),old)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM active_payments').fetchone()[0],3)


if __name__=='__main__': unittest.main()
