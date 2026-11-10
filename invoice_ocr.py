"""Local Apple Vision OCR and conservative table reconstruction from cell positions."""
from i18n import translate as _
from decimal import Decimal
import json
from pathlib import Path
import re
import subprocess
import sys

from money import decimal_value, line_amount, money_input, parse_money


def ocr_money(text):
    """OCR-only grouping normalization; user-entered amounts stay strictly validated."""
    value = re.sub(r'[\s\u00a0\u202f₩]', '', text)
    if re.fullmatch(r'\d{1,3}(?:[,\.]\d{3})+(?:[,.]\d{2})?', value):
        if re.search(r'[,.]\d{2}$', value):
            value = re.sub(r'[,.]', '', value[:-3]) + '.' + value[-2:]
        else:
            value = re.sub(r'[,.]', '', value)
    return money_input(parse_money(value, allow_zero=True))


def parse_observations(observations, korean=None):
    result = _parse_russian(observations)
    if result['items']:
        for row in result['items']:
            row.setdefault('discount', '0.00')
        return result
    if korean:
        return _parse_korean(korean, observations)
    return result


def _parse_korean(observations, russian):
    """Find columns by labels, rows by quantity/price pairs, allowing slanted paper."""
    obs = [dict(o, cx=o['x']+o['w']/2, cy=o['y']+o['h']/2) for o in observations]
    ru = [dict(o, cx=o['x']+o['w']/2, cy=o['y']+o['h']/2) for o in russian]
    result = _parse_russian(russian)
    result.update(items=[], warnings=[], raw_text='\n'.join(o['text'] for o in sorted(obs, key=lambda o:(-o['cy'],o['x']))))
    compact = lambda o: re.sub(r'[\s\[\]():：]', '', o['text'].casefold())
    headers = {}
    for o in obs:
        label = compact(o)
        key = None
        if label in ('품목','붕목','툼목','품명','품목명규격','품목명','품명규격','상품명'): key='name'
        elif label=='수량': key='quantity'
        elif label=='단가': key='price'
        elif label=='공급가액': key='total'
        elif label in ('박스','box'): key='box'
        elif label=='일자': key='date'
        elif label in ('부가세','세액'): key='vat'
        if key:
            # Footer repeats 수량 / 공급가액; the table header is the highest instance.
            if key not in headers or o['cy']>headers[key]['cy']: headers[key]=o
    if not all(k in headers for k in ('name','quantity','price','total')):
        result['warnings'].append('Не удалось определить все столбцы. На фото должны быть видны заголовки и правая часть таблицы.')
        return result

    # Estimate the tilt from header positions. Normalized row height is comparable
    # across the page even when photographed at an angle.
    points=[headers[k] for k in ('name','quantity','price','total')]
    mx=sum(o['cx'] for o in points)/len(points); my=sum(o['cy'] for o in points)/len(points)
    slope=sum((o['cx']-mx)*(o['cy']-my) for o in points)/sum((o['cx']-mx)**2 for o in points)
    slope=max(-.15,min(.15,slope))
    level=lambda o:o['cy']-slope*o['cx']
    head_y=sum(level(o) for o in points)/len(points)
    # Numeric columns are right aligned; their header's right edges are a better
    # boundary than text centers, especially next to a narrow Box column.
    centers={k:(o['x']+o['w'] if k not in ('name','date') else o['cx']) for k,o in headers.items()}
    column=lambda o:min(centers,key=lambda k:abs(o['cx']-centers[k]))
    cells=[o for o in obs if level(o)<head_y-min(o['h']*.45,.008)]
    def number(o):
        try: return ocr_money(o['text'])
        except ValueError: return None
    quantities=[]
    for o in cells:
        if column(o)!='quantity' or not re.fullmatch(r'\d+(?:[.,]\d+)?',o['text'].strip()): continue
        prices=[p for p in cells if column(p)=='price' and number(p) and abs(level(p)-level(o))<max(o['h']*1.2,.006)]
        if prices: quantities.append(o)
    quantities.sort(key=lambda o:-level(o))
    if not quantities:
        result['warnings'].append('Не удалось собрать товарные строки. Проверьте резкость фотографии.')
        return result
    rows_y=[level(o) for o in quantities]
    gaps=[rows_y[i]-rows_y[i+1] for i in range(len(rows_y)-1)]
    typical=sorted(gaps)[len(gaps)//2] if gaps else quantities[0]['h']*2
    def row_index(o):
        y=level(o)
        i=min(range(len(rows_y)),key=lambda i:abs(rows_y[i]-y))
        upper=(rows_y[i-1]+rows_y[i])/2 if i else rows_y[i]+typical*.65
        lower=(rows_y[i]+rows_y[i+1])/2 if i+1<len(rows_y) else rows_y[i]-typical*.55
        return i if lower<y<upper else None

    # Prefer Cyrillic text from the Russian pass, preserving Korean continuations
    # when that pass missed them. Never use a handwritten note as a numeric value.
    names=[o for o in cells if column(o)=='name' and re.search(r'[A-Za-zА-Яа-я가-힣]{2}',o['text'])]
    for r in ru:
        if column(r)!='name' or not re.search(r'[А-Яа-яЁё]',r['text']) or row_index(r) is None: continue
        overlaps=[n for n in names if abs(n['cy']-r['cy'])<max(n['h'],r['h'])*.55
                  and min(n['x']+n['w'],r['x']+r['w'])>max(n['x'],r['x'])]
        if overlaps:
            n=min(overlaps,key=lambda n:abs(n['cy']-r['cy']))
            names.remove(n)
            if '/' in r['text'] and '/' in n['text']:
                r=dict(r,text=r['text'].split('/',1)[0]+'/'+n['text'].split('/',1)[1])
        names.append(r)
    for i,q in enumerate(quantities):
        pieces=sorted([n for n in names if row_index(n)==i],key=lambda o:(-o['cy'],o['x']))
        row=dict(name=' '.join(n['text'] for n in pieces).strip(),quantity=q['text'].replace(',','.'),unit='',
                 unit_price='',discount='0.00',vat_rate='',vat_amount='',line_total='',notes=[])
        for key,target in [('price','unit_price'),('total','line_total'),('vat','vat_amount')]:
            candidates=[c for c in cells if column(c)==key and row_index(c)==i and number(c) is not None]
            if len(candidates)==1: row[target]=number(candidates[0])
            elif len(candidates)>1: row['notes'].append('Несколько значений в столбце — сверьте с фото.')
        units=[c for c in ru if row_index(c)==i and headers['quantity']['cx']<c['cx']<headers['price']['cx']
               and re.fullmatch(r'(?i)шт\.?|кг|уп|л',c['text'].strip())]
        if units: row['unit']=units[0]['text'].lower().strip('.')
        if not row['name']: row['notes'].append('Название не распознано — впишите его по фото.')
        elif any(n.get('confidence',1)<.6 for n in pieces): row['notes'].append('Проверьте название товара по фото.')
        if row['unit_price']:
            base=line_amount(decimal_value(row['quantity']),parse_money(row['unit_price']))
            if not row['line_total']:
                row['line_total']=money_input(base)
                row['notes'].append('Сумма на фото не прочитана; рассчитано количество × цена. Обязательно сверьте эту строку.')
            else:
                actual=parse_money(row['line_total'],allow_zero=True)
                if 0<actual<base:
                    row['discount']=money_input(base-actual)
                    row['notes'].append('Сумма ниже количества × цены. Разница записана как скидка — подтвердите по документу.')
                elif actual!=base:
                    row['notes'].append('Количество × цена отличается от суммы строки — проверьте.')
        result['items'].append(row)

    below=[o for o in obs if level(o)<rows_y[-1]-typical*.55]
    totals=[o for o in below if column(o)=='total' and number(o) is not None]
    if totals: result['printed_total']=number(max(totals,key=level))
    # A footer with VAT explicitly zero is unambiguous; otherwise leave tax mode
    # for review instead of guessing a tax treatment from the supplier's country.
    for o in below:
        label=compact(o)
        # Vision sometimes joins a zero VAT cell with the following 합계 label.
        if 'vat' in headers or any('vat' in compact(c) for c in below):
            zero_tax=re.fullmatch(r'0(?:[.,]00)?\s*합계',o['text'].strip())
            if zero_tax:
                result['vat_total']='0.00'; result['vat_mode']='included'
        if label=='vat':
            candidates=[c for c in below if c['cx']>o['cx'] and abs(level(c)-level(o))<max(o['h'],c['h'])*.7 and number(c) is not None]
            if candidates:
                c=min(candidates,key=lambda c:c['cx'])
                result['vat_total']=number(c)
                result['vat_mode']='included' if result['vat_total']=='0.00' else ''
        if '전미수금' in label or '총미수금' in label:
            candidates=[c for c in below if c['cx']>o['cx'] and abs(level(c)-level(o))<max(o['h'],c['h']) and number(c) is not None]
            if candidates:
                field='previous_balance' if '전미수금' in label else 'balance_with_previous'
                result[field]=number(min(candidates,key=lambda c:c['cx']))
    if result.get('previous_balance') or result.get('balance_with_previous'):
        result['warnings'].append('На фото также указан прежний долг. В сумму новой поставки он не добавляется.')

    suppliers=[o for o in obs if o['text'].strip().startswith('공급자')]
    if suppliers:
        provider=min(suppliers,key=lambda o:o['x'])
        result['supplier_name']=re.split(r'\s*[/|]\s*\d{3}-',re.sub(r'^공급자\s*','',provider['text']))[0].strip()
    for o in obs:
        if compact(o)=='상호':
            candidates=[c for c in obs if c['x']>o['cx'] and abs(c['cy']-o['cy'])<max(c['h'],o['h'])*.7]
            if candidates:
                result['supplier_name']=min(candidates,key=lambda c:c['x'])['text'].strip(' /')
        date_match=re.search(r'\b(20\d{2})[./](\d{2})[./](\d{2})\b',o['text'])
        if date_match:
            result['delivery_date']='-'.join(date_match.groups())
            if re.fullmatch(r'20\d{2}[./]\d{2}[./]\d{2}\s*-\s*\d+',o['text'].strip()):
                result['invoice_number']=re.sub(r'\s+','',o['text'])
    # Quantity sums and carton totals on Korean forms are not product row counts.
    if result['printed_total']:
        total=sum(parse_money(r['line_total'],allow_zero=True) for r in result['items'] if r['line_total'])
        if total!=parse_money(result['printed_total']):
            from money import money_text
            result['warnings'].append(f'По строкам получается {money_text(total)} ₩, напечатанный итог — {money_text(parse_money(result["printed_total"]))} ₩. Проверьте скидки и строки без суммы; сохранение при расхождении заблокировано.')
    else:
        result['warnings'].append('Итоговая сумма не прочитана. Укажите итог именно этой поставки по документу.')
    result['warnings'].append('Сверьте названия и реквизиты: распознавание может ошибаться. На фото должны быть видны все края накладной.')
    return result


def recognize_image(path):
    binary = Path(__file__).parent / 'native' / 'invoice-ocr'
    if sys.platform != 'darwin' or not binary.is_file():
        raise ValueError(_('Распознавание настроено для этого Mac. Не найден модуль распознавания.'))
    def read(languages):
        try:
            completed = subprocess.run([str(binary), str(path), languages], capture_output=True, timeout=150, check=True)
            return json.loads(completed.stdout)
        except subprocess.TimeoutExpired as exc:
            raise ValueError(_('Распознавание заняло слишком много времени. Попробуйте более крупное фото самой накладной.')) from exc
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            raise ValueError(_('Не удалось прочитать изображение. Попробуйте более чёткое фото JPG или PNG.')) from exc
    observations = read('ru-RU,en-US')
    result = parse_observations(observations)
    secondary = []
    if not result['items']:
        secondary = read('ko-KR,en-US')
        result = parse_observations(observations, secondary)
    if not observations and not secondary:
        raise ValueError(_('На фото не найден текст. Снимите накладную крупнее и при хорошем освещении.'))
    # Keep cell positions locally so a failed draft can be diagnosed without another OCR run.
    result['ocr_observations'] = observations
    result['ocr_secondary'] = secondary
    result['ocr_version'] = 2
    return result


def _parse_russian(observations):
    obs = [dict(o, cx=o['x']+o['w']/2, cy=o['y']+o['h']/2) for o in observations]
    ordered = sorted(obs, key=lambda o: (-o['cy'], o['x']))
    raw_text = '\n'.join(o['text'] for o in ordered)
    result = dict(invoice_number='', delivery_date='', supplier_name='', buyer='',
                  printed_total='', vat_total='', vat_mode='', expected_count='',
                  items=[], warnings=[], raw_text=raw_text)

    def beside(label):
        candidates = [o for o in obs if o['cx'] > label['cx'] and abs(o['cy']-label['cy']) < max(label['h'], o['h'])*0.65]
        return ' '.join(o['text'] for o in sorted(candidates, key=lambda o:o['x']))

    months = {'января':1,'февраля':2,'марта':3,'апреля':4,'мая':5,'июня':6,
              'июля':7,'августа':8,'сентября':9,'октября':10,'ноября':11,'декабря':12}
    for o in ordered:
        text = o['text']
        low = text.casefold().strip()
        if low.startswith('поставщик'):
            result['supplier_name'] = text.split(':',1)[1].strip() if ':' in text else ''
            result['supplier_name'] = result['supplier_name'] or beside(o)
        if low.startswith('покупатель'):
            result['buyer'] = text.split(':',1)[1].strip() if ':' in text else ''
            result['buyer'] = result['buyer'] or beside(o)
        number = re.search(r'(?:№|\bN[ºo°]|номер)\s*([\w/-]*\d[\w/-]*)', text, re.I)
        if number and not result['invoice_number']:
            result['invoice_number'] = number.group(1)
        date_match = re.search(r'(\d{1,2})\s+('+'|'.join(months)+r')\s+(20\d{2})', low)
        if date_match:
            result['delivery_date'] = f'{date_match[3]}-{months[date_match[2]]:02d}-{int(date_match[1]):02d}'
        numeric_date = re.search(r'\b(\d{2})[./](\d{2})[./](20\d{2})\b', low)
        if numeric_date and not result['delivery_date']:
            result['delivery_date'] = f'{numeric_date[3]}-{numeric_date[2]}-{numeric_date[1]}'
        count = re.search(r'всего\s+(?:наименований|позиций)\s*[: ]?\s*(\d+)', low)
        if count:
            result['expected_count'] = count[1]
        if low.startswith('итого') or low.startswith('в том числе ндс'):
            if low.startswith('в том числе ндс'):
                result['vat_mode']='included'
            content = re.sub(r'^[^:]*:\s*', '', text) if ':' in text else ''
            content = content or beside(o)
            value = re.search(r'\d[\d\s\u00a0]*[,.]\d{2}', content)
            if value:
                field = 'vat_total' if 'ндс' in low else 'printed_total'
                try:
                    result[field] = money_input(parse_money(value[0], allow_zero=True))
                except ValueError:
                    pass

    headers = {}
    for o in obs:
        low = o['text'].casefold().strip()
        if low in ('товар','наименование','наименование товара'):
            headers['name'] = o
        elif 'колич' in low:
            headers['quantity'] = o
        elif low == 'цена':
            headers['price'] = o
        elif 'ставка' in low and 'ндс' in low and 'сумма' in low:
            headers['vat_rate'] = dict(o, cx=o['x']+o['w']*.25)
            headers['vat_amount'] = dict(o, cx=o['x']+o['w']*.75)
        elif 'ставка' in low and 'ндс' in low:
            headers['vat_rate'] = o
        elif 'сумма' in low and 'ндс' in low and not low.startswith('в том'):
            headers['vat_amount'] = o
        elif low in ('сумма','стоимость'):
            headers['total'] = o
    required = ('name','quantity','price','total')
    if not all(k in headers for k in required):
        result['warnings'].append('Не удалось определить столбцы таблицы. Добавьте строки вручную и сверьте их с фото.')
        return result
    keys = sorted(headers, key=lambda k: headers[k]['cx'])
    centers = [headers[k]['cx'] for k in keys]

    def column(o):
        return min(keys, key=lambda k:abs(o['cx']-headers[k]['cx']))

    header_y = headers['name']['cy']
    total_labels = [o for o in obs if o['text'].casefold().startswith('итого') and o['cy']<header_y]
    bottom = max((o['cy'] for o in total_labels), default=0)
    cells = [o for o in obs if bottom+0.005 < o['cy'] < header_y-max(headers['name']['h']*.65,0.005)]
    names = sorted([o for o in cells if column(o)=='name' and re.search(r'[A-Za-zА-Яа-яЁё]{2}',o['text'])], key=lambda o:-o['cy'])
    if not names:
        result['warnings'].append('Товарные строки не найдены. Проверьте фото и заполните таблицу.')
        return result
    for index, name in enumerate(names):
        row = dict(name=re.sub(r'^\s*\d+\s+', '', name['text']).strip(), quantity='', unit='',
                   unit_price='', vat_rate='', vat_amount='', line_total='', notes=[])
        assigned = {k:[] for k in keys if k!='name'}
        for cell in cells:
            key = column(cell)
            if key=='name':
                continue
            nearest = min(range(len(names)), key=lambda i:abs(cell['cy']-names[i]['cy']))
            if nearest == index and abs(cell['cy']-name['cy']) < max(name['h']*1.2,0.012):
                assigned[key].append(cell)
        for key, values in assigned.items():
            text = ' '.join(o['text'] for o in sorted(values,key=lambda o:o['x']))
            target = {'price':'unit_price','total':'line_total','vat_amount':'vat_amount','vat_rate':'vat_rate','quantity':'quantity'}[key]
            if key=='quantity':
                match = re.search(r'\d+(?:[,.]\d+)?',text)
                if match:
                    row['quantity']=match[0].replace(',','.')
                row['unit']=re.sub(r'[\d.,\s]+','',text).lower()
                known_unit=re.search(r'(шт|кг|уп|кор|ящ|л)\.?$',row['unit'])
                if known_unit:
                    row['unit']=known_unit[1]
            elif key=='vat_rate':
                match=re.search(r'\d+(?:[,.]\d+)?',text)
                row[target]=match[0].replace(',','.') if match else ''
            else:
                try:
                    row[target]=money_input(parse_money(text,allow_zero=key=='vat_amount'))
                except ValueError:
                    row['notes'].append('Проверьте поле «'+{'price':'Цена','total':'Сумма','vat_amount':'НДС'}[key]+'».')
        if not row['quantity'] and row['unit_price'] and row['line_total']:
            q=decimal_value(row['line_total'])/decimal_value(row['unit_price'])
            if q>0 and q==q.quantize(Decimal('.001')):
                row['quantity']=format(q.normalize(),'f')
                row['notes'].append('Количество восстановлено по сумме и цене — проверьте.')
        if not row['line_total'] and row['quantity'] and row['unit_price']:
            row['line_total']=money_input(line_amount(decimal_value(row['quantity']),parse_money(row['unit_price'])))
            row['notes'].append('Сумма строки рассчитана по количеству и цене — проверьте.')
        if name.get('confidence',1)<.6:
            row['notes'].append('Проверьте название товара по фото.')
        result['items'].append(row)
    if result['expected_count'] and int(result['expected_count'])!=len(result['items']):
        result['warnings'].append(f"На фото указано строк: {result['expected_count']}, распознано: {len(result['items'])}.")
    if not result['printed_total']:
        result['warnings'].append('Итоговая сумма не распознана. Укажите её с бумажной накладной.')
    return result
