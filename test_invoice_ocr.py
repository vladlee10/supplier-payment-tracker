"""OCR layout and invoice saving regression checks, using an isolated ledger."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from werkzeug.datastructures import MultiDict

from invoice_ocr import ocr_money, parse_observations
from invoice_storage import initialize_database
from money import money_input, parse_money


def cell(text, x, y, w=.05, h=.018, tilt=.04):
    return dict(text=text, x=x-w/2, y=y+tilt*x-h/2, w=w, h=h, confidence=.95)


def korean_table(count=11, discounted=False):
    cells=[cell(t,x,.9,w) for t,x,w in [('품목명[규격]',.30,.13),('수량',.52,.05),
        ('Box',.59,.03),('단가',.67,.04),('공급가액',.78,.08)]]
    for i in range(count):
        y=.85-i*.045
        cells.extend([cell('PRODUCT '+chr(65+i),.26,y,.20),cell('2',.55,y,.025),
                      cell('1',.62,y,.012),cell('1,000',.71,y),
                      cell('1,600' if discounted else '2,000',.81,y,.06)])
    # Totals and carton/quantity counts are not additional product rows.
    footer=.85-count*.045
    cells.extend([cell(str(count*2),.55,footer),cell(str(count),.62,footer),
        cell('총합계',.25,footer),cell('공급가액',.28,footer-.04),
        cell('VAT',.52,footer-.04),cell('0 합계',.71,footer-.04),
        cell(f'{count*(1600 if discounted else 2000):,}',.85,footer-.04,.09),
        cell('2026/04/25 -3',.77,.96,.14)])
    return cells


class LayoutTest(unittest.TestCase):
    def test_korean_columns_slant_cartons_and_whole_won(self):
        draft=parse_observations([],korean_table())
        self.assertEqual(len(draft['items']),11)
        self.assertEqual(draft['printed_total'],'22000.00')
        self.assertEqual(draft['vat_total'],'0.00')
        self.assertEqual(draft['delivery_date'],'2026-04-25')
        self.assertEqual(draft['invoice_number'],'2026/04/25-3')
        self.assertEqual([r['quantity'] for r in draft['items']],['2']*11)
        self.assertTrue(all(r['line_total']=='2000.00' for r in draft['items']))

    def test_discount_and_previous_debt_stay_separate(self):
        obs=korean_table(8,True)
        footer=.85-8*.045-.04
        obs.extend([cell('전미수금:',.10,footer-.07,.10),cell('5,000',.35,footer-.07),
            cell('총미수금',.54,footer-.07,.10),cell('17,800',.71,footer-.07)])
        draft=parse_observations([],obs)
        self.assertEqual(draft['printed_total'],'12800.00')
        self.assertEqual(draft['previous_balance'],'5000.00')
        self.assertEqual(draft['balance_with_previous'],'17800.00')
        self.assertTrue(all(r['discount']=='400.00' for r in draft['items']))
        self.assertTrue(all(r['unit_price']=='1000.00' for r in draft['items']))

    def test_missing_amount_is_flagged_without_changing_printed_total(self):
        obs=korean_table(2,True)
        obs=[o for o in obs if not (o['text']=='1,600' and o['y']<.83)]
        draft=parse_observations([],obs)
        self.assertEqual(draft['printed_total'],'3200.00')
        self.assertEqual(draft['items'][-1]['line_total'],'2000.00')
        self.assertTrue(any('рассчитано' in n for n in draft['items'][-1]['notes']))
        self.assertTrue(any('сохранение' in n for n in draft['warnings']))

    def test_grouping_and_number_marker(self):
        for text,expected in [('1,712,200','1712200.00'),('1.712,200','1712200.00'),
                              ('1 601 135,50','1601135.50'),('0','0.00')]:
            self.assertEqual(ocr_money(text),expected)
        result=parse_observations([cell('Nº',.05,.8),cell('Накладная Nº АБ-123 от 01.10.2024',.5,.1,.6)])
        self.assertEqual(result['invoice_number'],'АБ-123')
        with self.assertRaises(ValueError): ocr_money('12 OOO')


class SaveTest(unittest.TestCase):
    def setUp(self):
        import main
        self.main=main
        self.temp=tempfile.TemporaryDirectory(prefix='invoice-ocr-test-')
        self.path=Path(self.temp.name)/'ledger.db'
        self.files=Path(self.temp.name)/'photos'; self.files.mkdir()
        initialize_database(self.path)
        self.old_path=main.DB_PATH; main.DB_PATH=self.path
        self.old_storage=main.app.config['INVOICE_STORAGE']; main.app.config['INVOICE_STORAGE']=str(self.files)
        main.app.config['TESTING']=True
        self.client=main.app.test_client()
        self.token='a'*32
        self.draft=parse_observations([],korean_table(2,True))
        self.draft.update(token=self.token,source_sha256='b'*64,extension='.png',supplier_name='Test supplier')
        (self.files/(self.token+'.json')).write_text(json.dumps(self.draft))

    def tearDown(self):
        self.main.DB_PATH=self.old_path
        self.main.app.config['INVOICE_STORAGE']=self.old_storage
        self.temp.cleanup()

    def form(self, **updates):
        data=MultiDict((k,self.draft[k]) for k in ('invoice_number','delivery_date','supplier_name',
            'buyer','printed_total','vat_total','vat_mode','expected_count'))
        data.update(dict(supplier_id='new',reviewed='yes',discounts_reviewed='yes'))
        for row in self.draft['items']:
            for k in ('name','quantity','unit','unit_price','vat_rate','vat_amount','line_total','discount'):
                data.add('item_'+k,row[k])
        for key,value in updates.items(): data.setlist(key,[value])
        return data

    def count(self):
        with sqlite3.connect(self.path) as db: return db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0]

    def test_discount_confirmation_math_and_document_total_are_required(self):
        url='/invoices/drafts/'+self.token
        response=self.client.post(url,data=self.form(discounts_reviewed=''))
        self.assertIn('Подтвердите скидки',response.get_data(as_text=True)); self.assertEqual(self.count(),0)
        response=self.client.post(url,data=self.form(printed_total='9999'))
        self.assertIn('По товарам получается',response.get_data(as_text=True)); self.assertEqual(self.count(),0)
        data=self.form(); data.setlist('item_discount',['0','400'])
        response=self.client.post(url,data=data)
        self.assertIn('цена − скидка',response.get_data(as_text=True)); self.assertEqual(self.count(),0)
        response=self.client.post(url,data=self.form())
        self.assertEqual(response.status_code,302); self.assertEqual(self.count(),1)
        self.assertEqual(self.client.post(url,data=self.form()).status_code,302); self.assertEqual(self.count(),1)
        with sqlite3.connect(self.path) as db:
            rows=db.execute('SELECT unit_price_minor,discount_minor,line_total_minor FROM invoice_items').fetchall()
            self.assertEqual(rows,[(100000,40000,160000)]*2)
            before=list(db.iterdump())
        initialize_database(self.path)
        with sqlite3.connect(self.path) as db:self.assertEqual(list(db.iterdump()),before)
        self.assertIn('Скидка',self.client.get(response.location).get_data(as_text=True))

    def test_unnumbered_invoices_on_same_date_stay_distinct_but_identical_photo_is_reused(self):
        data=self.form()
        data.pop('invoice_number')
        first=self.client.post('/invoices/drafts/'+self.token,data=data)
        self.assertEqual(first.status_code,302)
        self.assertIn('Накладная — запись #1',self.client.get(first.location).text)

        token='c'*32
        (self.files/(token+'.json')).write_text(json.dumps(dict(self.draft,token=token,source_sha256='d'*64)))
        second=self.client.post('/invoices/drafts/'+token,data=self.form(invoice_number='   '))
        self.assertEqual(second.status_code,302)
        self.assertNotEqual(first.location,second.location)
        self.assertEqual(self.count(),2)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT invoice_number FROM invoice_documents ORDER BY delivery_id').fetchall(),[('',),('',)])
            self.assertEqual(db.execute('SELECT note FROM deliveries ORDER BY id').fetchall(),[('',),('',)])

        repeated='e'*32
        (self.files/(repeated+'.json')).write_text(json.dumps(dict(self.draft,token=repeated)))
        self.assertEqual(self.client.post('/invoices/drafts/'+repeated,data=data).location,first.location)
        self.assertEqual(self.count(),2)

    def test_additive_migration_preserves_old_rows(self):
        with sqlite3.connect(self.path) as db:
            db.execute('ALTER TABLE invoice_items DROP COLUMN discount_minor')
            db.execute("INSERT INTO suppliers(id,name) VALUES(1,'Old supplier')")
            db.execute("INSERT INTO deliveries(id,supplier_id,delivery_date,amount_minor) VALUES(1,1,'2026-01-01',100)")
            db.execute("INSERT INTO invoice_items(delivery_id,position,name,quantity,unit_price_minor,line_total_minor) VALUES(1,1,'Old product','1',100,100)")
        initialize_database(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT unit_price_minor,discount_minor,line_total_minor FROM invoice_items').fetchone(),(100,0,100))
            self.assertEqual(db.execute('SELECT amount_minor FROM deliveries').fetchone()[0],100)


if __name__=='__main__': unittest.main()
