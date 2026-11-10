"""Overview checks with isolated ledgers; run with python -m unittest test_dashboard."""
from datetime import date
from pathlib import Path
import sqlite3
import tempfile
import unittest

import main
from dashboard import month_shift, overview
from invoice_storage import initialize_database


class OverviewTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='overview-test-')
        self.path = Path(self.directory.name) / 'ledger.sqlite'
        initialize_database(self.path)
        with sqlite3.connect(self.path) as db:
            db.executemany('INSERT INTO suppliers(id,name) VALUES(?,?)', [
                (1,'Империя'),(2,'Роял'),(3,'С авансом'),(4,'Новый'),(5,'Старые оплаты'),
                (6,'роял'),(7,'Тест <script>alert(1)</script>')])
            db.executemany('INSERT INTO deliveries(id,supplier_id,delivery_date,amount_minor) VALUES(?,?,?,?)',[
                (1,1,'2026-10-25',10000000),(2,1,'2026-11-02',1005000),
                (3,2,'2026-11-01',10000),(4,5,'2026-11-03',250000),(5,6,'2026-11-02',70000)])
            db.executemany('INSERT INTO payments(id,supplier_id,payment_date,amount_minor) VALUES(?,?,?,?)',[
                (1,1,'2026-11-02',1500000),(2,1,'2026-11-03',1500000),(3,2,'2026-11-01',10000),
                (4,3,'2026-10-30',50000000),(5,5,'2026-11-03',250000)])
            db.executemany('INSERT INTO payment_allocations(payment_id,delivery_id,amount_minor) VALUES(?,?,?)',[
                (1,1,1000000),(2,1,1000000),(3,3,10000)])
        self.old_path = main.DB_PATH
        main.DB_PATH = self.path
        main.app.config['TESTING'] = True
        self.client = main.app.test_client()

    def tearDown(self):
        main.DB_PATH = self.old_path
        self.directory.cleanup()

    def test_monthly_totals_and_current_debt_are_independent(self):
        november = overview(self.path,'2026-11',today=date(2026,11,4))
        october = overview(self.path,'2026-10',today=date(2026,11,4))
        self.assertEqual(november['monthly_deliveries'],1005000+10000+250000+70000)
        self.assertEqual(november['monthly_payments'],3000000+10000+250000)
        self.assertEqual(october['monthly_deliveries'],10000000)
        self.assertEqual(october['monthly_payments'],50000000)
        # The large credit at a different supplier must not offset this debt.
        self.assertEqual(november['debt'],8005000+70000)
        self.assertEqual(october['debt'],november['debt'])
        self.assertEqual(november['pending'],4)
        self.assertEqual(november['partial'],1)
        self.assertEqual(november['debt_suppliers'],2)
        self.assertEqual(november['suppliers'][0]['id'],1)
        by_id = {s['id']:s for s in november['suppliers']}
        self.assertEqual(by_id[3]['credit'],50000000)
        self.assertFalse(by_id[3]['needs_allocation'])
        self.assertEqual(by_id[1]['available'],1000000)
        self.assertTrue(by_id[5]['needs_allocation'])
        self.assertNotEqual(by_id[5]['caption'],'Расчёты закрыты')
        self.assertEqual({s['id'] for s in november['allocation_suppliers']},{1,5})

    def test_activity_and_month_navigation(self):
        result = overview(self.path,'2026-11',today=date(2026,11,4))
        self.assertEqual(len(result['activity']),6)
        self.assertTrue(all(event['event_date'].startswith('2026-11') for event in result['activity']))
        self.assertEqual(result['previous_month'],'2026-10')
        self.assertEqual(result['next_month'],'2026-12')
        self.assertEqual(overview(self.path,'2026-12')['next_month'],'2027-01')
        self.assertEqual(month_shift(date(2026,1,1),-1),'2025-12')
        self.assertIsNone(month_shift(date(1,1,1),-1))
        self.assertIsNone(month_shift(date(9999,12,1),1))
        for bad in ('2026-00','2026-13','2026-1','0000-01',"2026-11' OR 1=1 --"):
            with self.assertRaises(ValueError):
                overview(self.path,bad)

    def test_home_supplier_routes_and_existing_pages(self):
        response = self.client.get('/?month=2026-11')
        self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        self.assertIn('Обзор магазина',html)
        self.assertIn('80 750',html)
        self.assertNotIn('<script>alert(1)</script>',html)
        self.assertIn('&lt;script&gt;',html)
        self.assertIn('Ноябрь 2026'.lower(),html)
        self.assertIn('href="/suppliers"',html)
        self.assertIn('name="month"',html)
        self.assertIn('aria-current="page"',html)
        for url in ('/suppliers','/deliveries','/payments','/paid-invoices','/invoices/import','/invoices/1'):
            self.assertEqual(self.client.get(url).status_code,200,url)
        created=self.client.post('/suppliers',data={'name':'Новый партнёр'})
        self.assertEqual(created.status_code,302)
        self.assertTrue(created.location.endswith('/suppliers'))
        old_form=self.client.post('/',data={'name':'Из старой вкладки'})
        self.assertEqual(old_form.status_code,302)
        self.assertTrue(old_form.location.endswith('/suppliers'))
        duplicate=self.client.post('/suppliers',data={'name':'Империя'})
        self.assertIn('уже существует',duplicate.get_data(as_text=True))
        invalid=self.client.get('/?month=broken')
        self.assertEqual(invalid.status_code,200)
        self.assertIn('Выберите месяц',invalid.get_data(as_text=True))

    def test_read_only_empty_period_and_precision(self):
        with sqlite3.connect(self.path) as db:
            before='\n'.join(db.iterdump())
        future=overview(self.path,'2030-01')
        self.assertEqual(future['monthly_payments'],0)
        self.assertEqual(future['activity'],[])
        self.assertGreater(future['debt'],0)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(before,'\n'.join(db.iterdump()))
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO deliveries(supplier_id,delivery_date,amount_minor) VALUES(4,'2026-11-04',101)")
        precise=overview(self.path,'2026-11')
        self.assertEqual(precise['debt'],8005000+70000+101)
        self.assertIn('1,01',self.client.get('/?month=2026-11').get_data(as_text=True))
        empty=Path(self.directory.name)/'empty.sqlite'
        initialize_database(empty)
        main.DB_PATH=empty
        html=self.client.get('/?month=2026-11').get_data(as_text=True)
        self.assertIn('Добавьте первого поставщика',html)
        self.assertIn('За этот месяц записей нет',html)


if __name__=='__main__':
    unittest.main()
