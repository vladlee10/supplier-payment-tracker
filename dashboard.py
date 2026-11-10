"""Read-only overview of the ledger; all money remains integer minor units."""
from i18n import translate as _, get_language
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo


MONTHS = ('Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
          'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь')
MONTHS_GENITIVE = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
                  'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря')


def month_shift(first, delta):
    index = (first.year - 1) * 12 + first.month - 1 + delta
    if not 0 <= index < 9999 * 12:
        return None
    return f'{index // 12 + 1:04d}-{index % 12 + 1:02d}'


def count_label(count, forms):
    if get_language() == 'en':
        return f'{count} {forms[0] if count == 1 else forms[2]}'
    if 11 <= count % 100 <= 14:
        word = forms[2]
    elif count % 10 == 1:
        word = forms[0]
    elif 2 <= count % 10 <= 4:
        word = forms[1]
    else:
        word = forms[2]
    return f'{count} {word}'


def overview(database_path, selected_month=None, *, today=None):
    today = today or datetime.now(ZoneInfo('Asia/Seoul')).date()
    current_month = today.isoformat()[:7]
    selected_month = selected_month or current_month
    if not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])', selected_month):
        raise ValueError(_('Выберите месяц для сводки.'))
    try:
        first = date.fromisoformat(selected_month + '-01')
    except ValueError as exc:
        raise ValueError(_('Выберите корректный месяц для сводки.')) from exc

    with closing(sqlite3.connect(Path(database_path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        monthly_deliveries = db.execute('''
            SELECT COALESCE(SUM(amount_minor),0) AS amount, COUNT(*) AS count
            FROM deliveries WHERE substr(delivery_date,1,7)=?
        ''', (selected_month,)).fetchone()
        monthly_payments = db.execute('''
            SELECT COALESCE(SUM(amount_minor),0) AS amount, COUNT(*) AS count
            FROM active_payments WHERE substr(payment_date,1,7)=?
        ''', (selected_month,)).fetchone()
        suppliers = [dict(row) for row in db.execute('''
            WITH delivered AS (
                SELECT supplier_id, SUM(amount_minor) AS supplied
                FROM deliveries GROUP BY supplier_id
            ), paid AS (
                SELECT supplier_id, SUM(amount_minor) AS paid
                FROM active_payments GROUP BY supplier_id
            ), allocated_invoice AS (
                SELECT delivery_id, SUM(amount_minor) AS amount
                FROM active_payment_allocations GROUP BY delivery_id
            ), open_invoices AS (
                SELECT d.supplier_id, COUNT(*) AS pending,
                       SUM(CASE WHEN COALESCE(a.amount,0)>0 THEN 1 ELSE 0 END) AS partial
                FROM deliveries d LEFT JOIN allocated_invoice a ON a.delivery_id=d.id
                WHERE d.amount_minor>COALESCE(a.amount,0)
                GROUP BY d.supplier_id
            ), allocated_payment AS (
                SELECT payment_id, SUM(amount_minor) AS amount
                FROM active_payment_allocations GROUP BY payment_id
            ), available AS (
                SELECT p.supplier_id, SUM(MAX(p.amount_minor-COALESCE(a.amount,0),0)) AS available
                FROM active_payments p LEFT JOIN allocated_payment a ON a.payment_id=p.id
                GROUP BY p.supplier_id
            )
            SELECT s.id, s.name, COALESCE(d.supplied,0) AS supplied,
                   COALESCE(p.paid,0) AS paid, COALESCE(o.pending,0) AS pending,
                   COALESCE(o.partial,0) AS partial, COALESCE(a.available,0) AS available
            FROM suppliers s
            LEFT JOIN delivered d ON d.supplier_id=s.id
            LEFT JOIN paid p ON p.supplier_id=s.id
            LEFT JOIN open_invoices o ON o.supplier_id=s.id
            LEFT JOIN available a ON a.supplier_id=s.id
        ''')]
        activity = [dict(row) for row in db.execute('''
            SELECT * FROM (
                SELECT 'delivery' AS kind, d.id, d.supplier_id, s.name AS supplier_name,
                       d.delivery_date AS event_date, d.amount_minor,
                       doc.invoice_number, 0 AS sort_kind
                FROM deliveries d JOIN suppliers s ON s.id=d.supplier_id
                LEFT JOIN invoice_documents doc ON doc.delivery_id=d.id
                WHERE substr(d.delivery_date,1,7)=?
                UNION ALL
                SELECT 'payment' AS kind, p.id, p.supplier_id, s.name AS supplier_name,
                       p.payment_date AS event_date, p.amount_minor,
                       NULL AS invoice_number, 1 AS sort_kind
                FROM active_payments p JOIN suppliers s ON s.id=p.supplier_id
                WHERE substr(p.payment_date,1,7)=?
            ) ORDER BY event_date DESC, sort_kind DESC, id DESC LIMIT 6
        ''', (selected_month, selected_month))]

    for supplier in suppliers:
        balance = supplier['supplied'] - supplier['paid']
        supplier['debt'] = max(balance, 0)
        supplier['credit'] = max(-balance, 0)
        supplier['needs_allocation'] = supplier['available'] > 0 and supplier['pending'] > 0
        if supplier['needs_allocation']:
            supplier['caption'] = _('Есть нераспределённые оплаты')
        elif supplier['debt']:
            supplier['caption'] = _('Есть накладные к оплате')
        elif supplier['credit']:
            supplier['caption'] = _('Есть аванс у поставщика')
        elif supplier['supplied'] or supplier['paid']:
            supplier['caption'] = _('Расчёты закрыты')
        else:
            supplier['caption'] = _('Пока нет поставок')
    suppliers.sort(key=lambda s: (-s['debt'], s['name'].casefold(), s['id']))
    for event in activity:
        event_date = date.fromisoformat(event['event_date'])
        event['date_label'] = f'{event_date.day} {_(MONTHS_GENITIVE[event_date.month-1])}'

    debt_suppliers = sum(s['debt'] > 0 for s in suppliers)
    partial = sum(s['partial'] for s in suppliers)
    allocation_suppliers = [s for s in suppliers if s['needs_allocation']]
    return {
        'selected_month': selected_month,
        'month_label': f'{_(MONTHS[first.month-1])} {first.year}',
        'current_month': current_month,
        'previous_month': month_shift(first, -1),
        'next_month': month_shift(first, 1),
        'today_label': f'{today.day} {_(MONTHS_GENITIVE[today.month-1])} {today.year}',
        'monthly_deliveries': monthly_deliveries['amount'],
        'monthly_delivery_count': count_label(monthly_deliveries['count'], (_('накладная'), _('накладные'), _('накладных'))),
        'monthly_payments': monthly_payments['amount'],
        'monthly_payment_count': count_label(monthly_payments['count'], (_('оплата'), _('оплаты'), _('оплат'))),
        'debt': sum(s['debt'] for s in suppliers),
        'debt_suppliers': debt_suppliers,
        'debt_suppliers_label': count_label(debt_suppliers, (_('поставщик'), _('поставщика'), _('поставщиков'))),
        'pending': sum(s['pending'] for s in suppliers),
        'partial_label': count_label(partial, (_('частично оплаченная'), _('частично оплаченные'), _('частично оплаченных'))),
        'partial': partial,
        'suppliers': suppliers,
        'supplier_count_label': count_label(len(suppliers), (_('поставщик'), _('поставщика'), _('поставщиков'))),
        'activity': activity,
        'allocation_suppliers': allocation_suppliers,
    }
