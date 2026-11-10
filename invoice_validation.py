"""Shared money and row validation for importing and editing invoices."""
from i18n import translate as _
from datetime import date
from decimal import Decimal
from money import MAX_MONEY, decimal_value, line_amount, money_text, parse_money


def apply_invoice_form(draft, form):
    draft.update({key: form.get(key, '').strip() for key in
                  ('invoice_number','delivery_date','supplier_name','buyer','printed_total','vat_total','vat_mode','expected_count')})
    fields = ('name','quantity','unit','unit_price','vat_rate','vat_amount','line_total','discount')
    columns = [form.getlist('item_'+field) for field in fields]
    if not columns[-1]:
        columns[-1] = ['0'] * len(columns[0])
    if len({len(c) for c in columns}) != 1 or len(columns[0]) > 250:
        raise ValueError(_('Не удалось прочитать строки. Допускается до 250 товаров.'))
    draft['items'] = [dict(zip(fields, row), notes=[]) for row in zip(*columns)]


def validate_invoice(draft, *, reviewed, discounts_reviewed):
    if reviewed != 'yes':
        raise ValueError(_('Проверьте таблицу по фото и отметьте подтверждение.'))
    if len(draft['invoice_number'])>100:
        raise ValueError(_('Номер накладной должен быть не длиннее 100 символов.'))
    delivery_date = date.fromisoformat(draft['delivery_date']).isoformat()
    if draft['vat_mode'] not in ('included', 'excluded'):
        raise ValueError(_('Укажите, как НДС учтён в накладной.'))
    printed_total = parse_money(draft['printed_total'])
    vat_total = parse_money(draft['vat_total'],allow_zero=True) if draft['vat_total'] else None
    if not 1<=len(draft['items'])<=250:
        raise ValueError(_('Добавьте товары из накладной.'))
    if draft['expected_count'] and int(draft['expected_count']) != len(draft['items']):
        raise ValueError(_('Количество строк отличается от указанного в накладной. Проверьте таблицу.'))
    items = []
    for index, item in enumerate(draft['items'], 1):
        try:
            name=item['name'].strip()
            if not name or len(name)>500 or len(item['unit'])>30:
                raise ValueError(_('Проверьте название товара и единицу измерения.'))
            quantity=decimal_value(item['quantity'])
            if not 0<quantity<=Decimal('1000000000') or quantity.as_tuple().exponent < -3:
                raise ValueError(_('Количество должно быть положительным, до трёх знаков после запятой.'))
            price=parse_money(item['unit_price'])
            total=parse_money(item['line_total'])
            discount=parse_money(item['discount'] or '0',allow_zero=True)
            base=line_amount(quantity, price)
            if base>MAX_MONEY or discount>base or base-discount!=total:
                raise ValueError(_('Количество × цена − скидка не совпадает с суммой строки.'))
            if discount and discounts_reviewed!='yes':
                raise ValueError(_('Подтвердите скидки по оригиналу или исправьте количество, цену и сумму строки.'))
            rate=decimal_value(item['vat_rate']) if item['vat_rate'] else None
            if rate is not None and not 0<=rate<=100:
                raise ValueError(_('Проверьте ставку НДС.'))
            vat=parse_money(item['vat_amount'],allow_zero=True) if item['vat_amount'] else None
            if vat is not None and vat>total:
                raise ValueError(_('Сумма НДС превышает сумму строки.'))
            items.append((index,name,format(quantity,'f'),item['unit'].strip(),price,format(rate,'f') if rate is not None else '',discount,vat,total))
        except ValueError as exc:
            raise ValueError(_('Строка {value1}: {value2}', value1=index, value2=exc)) from exc
    subtotal=sum(item[-1] for item in items)
    known_tax=[item[-2] for item in items]
    if vat_total is not None and all(v is not None for v in known_tax) and sum(known_tax)!=vat_total:
        raise ValueError(_('Сумма НДС по строкам отличается от НДС внизу накладной.'))
    if draft['vat_mode']=='excluded' and vat_total is None:
        raise ValueError(_('Укажите НДС, который добавляется к сумме товаров.'))
    total=subtotal+(vat_total or 0) if draft['vat_mode']=='excluded' else subtotal
    if total!=printed_total or total>MAX_MONEY:
        raise ValueError(_('По товарам получается {value1} ₩, на документе указано {value2} ₩. Проверьте строки и итог.', value1=money_text(total), value2=money_text(printed_total)))
    if len(draft['buyer'])>500:
        raise ValueError(_('Проверьте поле покупателя.'))
    return dict(delivery_date=delivery_date, printed_total=printed_total, vat_total=vat_total, total=total, items=items)
