"""Fill the styled Excel template using only the app's standard-library runtime.

The template is authored with Artifact Tool. Values are written as typed OOXML
cells with cached formula results so Excel opens a complete, recalculable report.
"""
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import re
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

from i18n import translate as _
from money import money_text

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
ET.register_namespace('x', NS)
TEMPLATE = Path(__file__).parent / 'report_assets' / 'statement-template.xlsx'
MAX_EXCEL_MINOR = 999_999_999_999_999


def tag(name):
    return '{' + NS + '}' + name


def export_statement(report):
    values = [report[k] for k in ('opening', 'supplied', 'paid', 'closing')]
    values += [v for e in report['events'] for v in (e['amount_minor'], e['balance'])]
    if any(abs(v) > MAX_EXCEL_MINOR for v in values):
        raise ValueError(_('Суммы слишком велики для точной выгрузки в Excel. Отчёт на сайте сохраняет точные значения.'))
    with ZipFile(TEMPLATE) as template:
        root = ET.fromstring(template.read('xl/worksheets/sheet1.xml'))
        data = root.find(tag('sheetData'))
        styles = {c.attrib['r']: c.attrib.get('s', '0') for c in data.iter(tag('c'))}
        for row in list(data):
            data.remove(row)

        def row(number, height=24):
            return ET.SubElement(data, tag('row'), r=str(number), ht=str(height), customHeight='1')

        def cell(parent, column, number, value=None, *, source=None, formula=None, minor=False):
            address = f'{column}{number}'
            node = ET.SubElement(parent, tag('c'), r=address, s=styles.get(source or address, styles['A16']))
            if formula is not None:
                ET.SubElement(node, tag('f')).text = formula
            if value is None:
                return
            if isinstance(value, date):
                if value.year < 1900:
                    value = value.isoformat()  # Excel cannot display pre-1900 dates.
                else:
                    baseline = date(1899, 12, 30) if value >= date(1900, 3, 1) else date(1899, 12, 31)
                    value = (value - baseline).days
            if isinstance(value, int):
                ET.SubElement(node, tag('v')).text = format(Decimal(value) / 100, '.2f') if minor else str(value)
            else:
                text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value))
                if len(text) > 32767:
                    raise ValueError(_('Текст одной записи слишком длинный для Excel. Сократите примечание и повторите выгрузку.'))
                # Literal strings, including =/+/-/@, can never become formulas.
                node.set('t', 'inlineStr')
                ET.SubElement(ET.SubElement(node, tag('is')), tag('t'), {'{http://www.w3.org/XML/1998/namespace}space':'preserve'}).text = text

        count = len(report['events'])
        last_event = 15 + count
        closing_row = last_event + 1
        if closing_row > 1048576:
            raise ValueError(_('Слишком много записей для одного листа Excel.'))
        metadata = {
            2: _('Сверка с поставщиком'),
            3: report['supplier']['name'],
            4: f"{report['month_label']} ({report['first'].isoformat()} — {report['last'].isoformat()})",
            5: _('Источник: учёт магазина. Сформировано: {time}, время Кореи.', time=report['generated_at'].strftime('%Y-%m-%d %H:%M')),
            12: _('Положительный остаток — долг магазина поставщику. Отрицательный — аванс.'),
            13: _('Отчёт составлен по текущим записям: исправления учтены, отменённые оплаты исключены.'),
        }
        for number in range(1, 15):
            current = row(number, 30 if number in (2,14) else 23)
            if number in metadata:
                cell(current, 'A', number, metadata[number])
            if 7 <= number <= 10:
                key = ('opening','supplied','paid','closing')[number-7]
                label = ('Остаток на начало месяца','Поставки за месяц','Оплаты за месяц','Остаток на конец месяца')[number-7]
                formula = {'opening':'E15', 'supplied':f'C{closing_row}', 'paid':f'D{closing_row}', 'closing':f'E{closing_row}'}[key]
                cell(current, 'A', number, _(label))
                cell(current, 'B', number)
                cell(current, 'C', number, report[key], minor=True, formula=formula)
            if number == 14:
                for column,label in zip('ABCDEFG', ('Дата','Документ','Поставки, ₩','Оплаты, ₩','Остаток, ₩','Привязанные накладные','Примечание')):
                    cell(current,column,number,_(label))

        first = row(15, 32)
        for column,value in zip('ABCDEFG', (report['first'],_('Остаток на начало месяца'),None,None,report['opening'],None,None)):
            cell(first,column,15,value,minor=column=='E')
        for index,event in enumerate(report['events'],16):
            if event['kind']=='delivery':
                document = _('Накладная №') + ' ' + event['invoice_number'] if event.get('invoice_number') else _('Поставка #') + str(event['id'])
            else:
                document = _('Оплата №') + ' ' + str(event['id'])
            allocations = '; '.join(str(a.get('invoice_number') or '#'+str(a['delivery_id'])) + ' (' + money_text(a['amount_minor']) + ' ₩)' for a in event['allocations'])
            # Unallocated money is still a real payment/advance in the monthly totals.
            if event['kind']=='payment':
                unallocated=event['amount_minor']-sum(a['amount_minor'] for a in event['allocations'])
                if unallocated:
                    allocations += ('; ' if allocations else '') + _('Без привязки: {amount} ₩',amount=money_text(unallocated))
            note = event['note']
            height = min(240, max(30, 16 * max(1, (len(note)+39)//40, (len(allocations)+31)//32, (len(document)+29)//30)))
            current = row(index,height)
            content = (date.fromisoformat(event['event_date']), document,
                       event['amount_minor'] if event['kind']=='delivery' else 0,
                       event['amount_minor'] if event['kind']=='payment' else 0,
                       event['balance'], allocations, note)
            for column,value in zip('ABCDEFG',content):
                cell(current,column,index,value,source=f'{column}16',minor=column in 'CDE',
                     formula=f'ROUND(E{index-1}+C{index}-D{index},2)' if column=='E' else None)
        ending = row(closing_row, 32)
        for column,value in zip('ABCDEFG',(report['last'],_('Остаток на конец месяца'),report['supplied'],report['paid'],report['closing'],None,None)):
            formula = None
            if column in 'CD':
                formula = f'SUM({column}16:{column}{last_event})' if count else '0'
            if column=='E':
                formula=f'E{last_event}'
            cell(ending,column,closing_row,value,source=f'{column}17',minor=column in 'CDE',formula=formula)
        # Keep worksheet elements in OOXML schema order, before margins/setup.
        dimension=root.find(tag('dimension'))
        if dimension is not None:root.remove(dimension)
        root.insert(1,ET.Element(tag('dimension'),ref=f'A1:G{closing_row}'))
        for name in ('autoFilter','pageMargins','pageSetup','printOptions'):
            existing=root.find(tag(name))
            if existing is not None:root.remove(existing)
        ET.SubElement(root,tag('autoFilter'),ref=f'A14:G{last_event}')
        ET.SubElement(root,tag('printOptions'),horizontalCentered='1')
        ET.SubElement(root,tag('pageMargins'),left='0.3',right='0.3',top='0.5',bottom='0.5',header='0.2',footer='0.2')
        ET.SubElement(root,tag('pageSetup'),orientation='landscape',paperSize='9',fitToWidth='1',fitToHeight='0')
        sheet_pr=root.find(tag('sheetPr'))
        ET.SubElement(sheet_pr,tag('pageSetUpPr'),fitToPage='1')
        book=ET.fromstring(template.read('xl/workbook.xml'))
        book.find(tag('sheets')).find(tag('sheet')).set('name',_('Сверка'))
        # The workbook opens with valid cached results and recalculates on edits.
        ET.SubElement(book,tag('calcPr'),calcMode='auto',fullCalcOnLoad='1')
        result=BytesIO()
        with ZipFile(result,'w',ZIP_DEFLATED) as out:
            for name in template.namelist():
                if name=='xl/worksheets/sheet1.xml':content=ET.tostring(root,encoding='utf-8',xml_declaration=True)
                elif name=='xl/workbook.xml':content=ET.tostring(book,encoding='utf-8',xml_declaration=True)
                else:content=template.read(name)
                out.writestr(name,content)
        return result.getvalue()
