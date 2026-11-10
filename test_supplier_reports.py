"""Monthly period boundaries, supplier isolation and exact Excel reconciliation."""
from io import BytesIO
from pathlib import Path
import sqlite3
import tempfile
import unittest
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import main
from invoice_storage import initialize_database
from report_workbook import export_statement, MAX_EXCEL_MINOR, NS
from supplier_reports import supplier_statement, month_bounds


class SupplierReportTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='supplier-report-test-')
        self.path=Path(self.temp.name)/'ledger.db'
        initialize_database(self.path)
        with sqlite3.connect(self.path) as db:
            db.executemany('INSERT INTO suppliers(id,name) VALUES(?,?)',[(1,'Роял'),(2,'роял'),(3,'Без записей')])
            db.executemany('INSERT INTO deliveries(id,supplier_id,delivery_date,amount_minor,note) VALUES(?,?,?,?,?)',[
                (1,1,'2026-10-15',100000,'Октябрь'),(2,1,'2026-11-01',200000,'Начало месяца'),
                (3,1,'2026-11-30',50125,'Конец месяца'),(4,1,'2026-12-01',999999,'Будущая поставка'),
                (5,2,'2026-11-01',900000,'Другой поставщик')])
            db.executemany('INSERT INTO payments(id,supplier_id,payment_date,amount_minor,note) VALUES(?,?,?,?,?)',[
                (1,1,'2026-10-20',25000,'Ранее'),(2,1,'2026-11-05',120000,'За две накладные'),
                (3,1,'2026-11-30',10125,'Часть оплаты'),(4,1,'2026-11-15',50000,'Отменена'),
                (5,1,'2026-12-01',50000,'Будущая оплата'),(6,2,'2026-11-02',100000,'Другой поставщик')])
            db.execute("UPDATE payments SET cancelled_at='2026-12-05T12:00:00+09:00',cancellation_reason='Ошибка' WHERE id=4")
            db.executemany('INSERT INTO payment_allocations(payment_id,delivery_id,amount_minor) VALUES(?,?,?)',[
                (1,1,25000),(2,1,75000),(2,2,45000),(3,3,10125),(4,2,50000)])
        self.old_path=main.DB_PATH;main.DB_PATH=self.path
        main.app.config['TESTING']=True;self.client=main.app.test_client()

    def tearDown(self):
        main.DB_PATH=self.old_path;self.temp.cleanup()

    def dump(self):
        with sqlite3.connect(self.path) as db:return '\n'.join(db.iterdump())

    def test_previous_balance_month_boundaries_and_no_double_counting(self):
        before=self.dump()
        report=supplier_statement(self.path,1,'2026-11')
        self.assertEqual((report['opening'],report['supplied'],report['paid'],report['closing']),(75000,250125,130125,195000))
        self.assertEqual([(e['kind'],e['id']) for e in report['events']],[('delivery',2),('payment',2),('delivery',3),('payment',3)])
        self.assertEqual([e['balance'] for e in report['events']],[275000,155000,205125,195000])
        self.assertEqual(len(report['events'][1]['allocations']),2)
        self.assertEqual(self.dump(),before)

    def test_advances_and_empty_month_keep_signed_balance(self):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO payments(supplier_id,payment_date,amount_minor) VALUES(3,'2026-10-31',25050)")
        report=supplier_statement(self.path,3,'2026-11')
        self.assertEqual((report['opening'],report['supplied'],report['paid'],report['closing']),(-25050,0,0,-25050))
        self.assertEqual(report['events'],[])
        zero=supplier_statement(self.path,3,'2026-09')
        self.assertEqual(zero['closing'],0)

    def test_month_validations_leap_year_and_december(self):
        self.assertEqual(month_bounds('2024-02')[1].isoformat(),'2024-02-29')
        self.assertEqual(month_bounds('2026-02')[1].isoformat(),'2026-02-28')
        self.assertEqual(month_bounds('9999-12')[1].isoformat(),'9999-12-31')
        for month in ['2026-13','2026-00','0000-01','2026-1','2026-11-01',"2026-11' OR 1=1--"]:
            with self.assertRaises(ValueError):month_bounds(month)
        for value in [0,-1,999,2**63]:
            with self.assertRaises(ValueError):supplier_statement(self.path,value,'2026-11')

    def test_bilingual_preview_and_download_do_not_change_records(self):
        before=self.dump()
        self.assertEqual(self.client.get('/reports').status_code,200)
        for language,title in [('ru','Отчёт по поставщику'),('en','Supplier statement')]:
            self.client.set_cookie('ui_language',language)
            page=self.client.get('/reports?supplier_id=1&month=2026-11')
            self.assertEqual(page.status_code,200);self.assertIn(title,page.text)
            self.assertIn('Роял',page.text);self.assertNotIn('Другой поставщик',page.text)
            result=self.client.get('/reports/supplier.xlsx?supplier_id=1&month=2026-11')
            self.assertEqual(result.status_code,200)
            self.assertIn('supplier-1-2026-11.xlsx',result.headers['Content-Disposition'])
            self.assertIn('no-store',result.headers['Cache-Control'])
            self.assertTrue(result.data.startswith(b'PK'))
            self.assertNotIn('Будущая поставка',page.text)
        self.assertEqual(self.dump(),before)
        for query in ['', '?supplier_id=999&month=2026-11','?supplier_id=1&month=bad','?supplier_id=99999999999999999999&month=2026-11']:
            self.assertEqual(self.client.get('/reports/supplier.xlsx'+query).status_code,400)

    @staticmethod
    def cells(content):
        with ZipFile(BytesIO(content)) as book:
            for name in book.namelist():
                if name.endswith('.xml'):ET.fromstring(book.read(name))
            root=ET.fromstring(book.read('xl/worksheets/sheet1.xml'))
            return {c.attrib['r']:c for c in root.iter('{'+NS+'}c')},root,book.namelist()

    def test_xlsx_typed_dates_formulas_cached_totals_and_styles(self):
        report=supplier_statement(self.path,1,'2026-11')
        cells,root,names=self.cells(export_statement(report))
        value=lambda name:cells[name].find('{'+NS+'}v').text
        formula=lambda name:cells[name].find('{'+NS+'}f').text
        self.assertEqual([value(c) for c in ['C7','C8','C9','C10']],['750.00','2501.25','1301.25','1950.00'])
        self.assertEqual(formula('E16'),'ROUND(E15+C16-D16,2)')
        self.assertEqual(formula('C20'),'SUM(C16:C19)')
        self.assertEqual(formula('D20'),'SUM(D16:D19)')
        self.assertEqual(formula('E20'),'E19')
        self.assertEqual(value('E19'),'1950.00')
        self.assertIsNone(cells['A16'].get('t'))
        self.assertEqual(cells['B16'].get('t'),'inlineStr')
        self.assertIn('xl/styles.xml',names)
        self.assertEqual(root.find('{'+NS+'}autoFilter').get('ref'),'A14:G19')
        self.assertEqual(root.find('.//{'+NS+'}pane').get('topLeftCell'),'A16')

    def test_empty_xlsx_has_no_circular_sum_and_carries_advance(self):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO payments(supplier_id,payment_date,amount_minor) VALUES(3,'2026-10-01',10125)")
        cells,root,_=self.cells(export_statement(supplier_statement(self.path,3,'2026-11')))
        self.assertEqual(cells['C16'].find('{'+NS+'}f').text,'0')
        self.assertEqual(cells['E16'].find('{'+NS+'}f').text,'E15')
        self.assertEqual(cells['E16'].find('{'+NS+'}v').text,'-101.25')

    def test_excel_literal_user_text_and_unicode(self):
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE suppliers SET name=? WHERE id=1',('=HYPERLINK("http://example.com") 러시아',))
            db.execute('UPDATE deliveries SET note=? WHERE id=2',('+cmd|=1 & <test>\x01',))
        content=export_statement(supplier_statement(self.path,1,'2026-11'))
        cells,_,_=self.cells(content)
        for name in ['A3','G16']:
            self.assertEqual(cells[name].get('t'),'inlineStr')
            self.assertIsNone(cells[name].find('{'+NS+'}f'))
        self.assertIn('러시아',ET.tostring(cells['A3'],encoding='unicode'))
        self.assertIn('&amp;',ET.tostring(cells['G16'],encoding='unicode'))

    def test_extreme_money_is_exact_in_preview_and_not_silently_rounded_in_excel(self):
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE deliveries SET amount_minor=? WHERE id=2',(MAX_EXCEL_MINOR+1,))
        report=supplier_statement(self.path,1,'2026-11')
        self.assertEqual(report['events'][0]['amount_minor'],MAX_EXCEL_MINOR+1)
        with self.assertRaises(ValueError):export_statement(report)
        self.assertEqual(self.client.get('/reports?supplier_id=1&month=2026-11').status_code,200)
        self.assertEqual(self.client.get('/reports/supplier.xlsx?supplier_id=1&month=2026-11').status_code,400)


if __name__=='__main__':unittest.main()
