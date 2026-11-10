"""Read-only monthly supplier statements, reconciled in exact minor units."""
from calendar import monthrange
from contextlib import closing
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

from flask import Blueprint, render_template, request, send_file

from dashboard import MONTHS
from i18n import translate as _
from report_workbook import export_statement


def month_bounds(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', value):
        raise ValueError(_('Выберите корректный месяц отчёта.'))
    try:
        first = date.fromisoformat(value + '-01')
    except ValueError as exc:
        raise ValueError(_('Выберите корректный месяц отчёта.')) from exc
    last = first.replace(day=monthrange(first.year, first.month)[1])
    return first, last


def supplier_statement(path, supplier_id, month):
    first, last = month_bounds(month)
    if not 1 <= supplier_id <= 2**63 - 1:
        raise ValueError(_('Выберите существующего поставщика.'))
    # One consistent, read-only snapshot. Allocations never create extra payments.
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        supplier = db.execute('SELECT id,name FROM suppliers WHERE id=?', (supplier_id,)).fetchone()
        if supplier is None:
            raise ValueError(_('Выберите существующего поставщика.'))
        deliveries = [dict(r) for r in db.execute('''
            SELECT d.id,d.delivery_date AS event_date,d.amount_minor,d.note,doc.invoice_number
            FROM deliveries d LEFT JOIN invoice_documents doc ON doc.delivery_id=d.id
            WHERE d.supplier_id=? AND d.delivery_date<=? ORDER BY d.delivery_date,d.id
        ''', (supplier_id, last.isoformat()))]
        payments = [dict(r) for r in db.execute('''
            SELECT p.id,p.payment_date AS event_date,p.amount_minor,p.note
            FROM active_payments p WHERE p.supplier_id=? AND p.payment_date<=?
            ORDER BY p.payment_date,p.id
        ''', (supplier_id, last.isoformat()))]
        allocation_rows = db.execute('''
            SELECT p.id AS payment_id,d.id AS delivery_id,doc.invoice_number,a.amount_minor
            FROM active_payments p JOIN active_payment_allocations a ON a.payment_id=p.id
            JOIN deliveries d ON d.id=a.delivery_id
            LEFT JOIN invoice_documents doc ON doc.delivery_id=d.id
            WHERE p.supplier_id=? AND p.payment_date>=? AND p.payment_date<=?
            ORDER BY p.id,d.id
        ''', (supplier_id, first.isoformat(), last.isoformat())).fetchall()
    links = {}
    for row in allocation_rows:
        links.setdefault(row['payment_id'], []).append(dict(row))
    opening = sum(r['amount_minor'] for r in deliveries if r['event_date'] < first.isoformat())
    opening -= sum(r['amount_minor'] for r in payments if r['event_date'] < first.isoformat())
    events = []
    for kind, rows in [('delivery', deliveries), ('payment', payments)]:
        for row in rows:
            if row['event_date'] < first.isoformat():
                continue
            events.append(dict(row, kind=kind, allocations=links.get(row['id'], []) if kind == 'payment' else []))
    events.sort(key=lambda r: (r['event_date'], 0 if r['kind'] == 'delivery' else 1, r['id']))
    balance = opening
    for event in events:
        balance += event['amount_minor'] if event['kind'] == 'delivery' else -event['amount_minor']
        event['balance'] = balance
    supplied = sum(e['amount_minor'] for e in events if e['kind'] == 'delivery')
    paid = sum(e['amount_minor'] for e in events if e['kind'] == 'payment')
    assert balance == opening + supplied - paid
    return dict(supplier=dict(supplier), month=month, month_label=f'{_(MONTHS[first.month-1])} {first.year}',
                first=first, last=last, opening=opening, supplied=supplied, paid=paid, closing=balance,
                events=events, generated_at=datetime.now(ZoneInfo('Asia/Seoul')))


def register_supplier_reports(app, database_path):
    bp = Blueprint('supplier_reports', __name__)

    def page_data():
        selected_id = request.args.get('supplier_id', '')
        month = request.args.get('month', datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y-%m'))
        with closing(sqlite3.connect(Path(database_path()).resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            suppliers = db.execute('SELECT id,name FROM suppliers ORDER BY name').fetchall()
        report = error = None
        try:
            month_bounds(month)
            if selected_id:
                if not re.fullmatch(r'[0-9]{1,19}', selected_id):
                    raise ValueError(_('Выберите существующего поставщика.'))
                report = supplier_statement(database_path(), int(selected_id), month)
        except ValueError as exc:
            error = str(exc)
        return dict(suppliers=suppliers, selected_id=selected_id, month=month, report=report, error=error)

    @bp.get('/reports')
    def index():
        data = page_data()
        return render_template('supplier_report.html', **data), 400 if data['error'] else 200

    @bp.get('/reports/supplier.xlsx')
    def download():
        data = page_data()
        if data['report'] is None:
            data['error'] = data['error'] or _('Выберите поставщика и месяц отчёта.')
            return render_template('supplier_report.html', **data), 400
        try:
            workbook = export_statement(data['report'])
        except ValueError as exc:
            data['error'] = str(exc)
            return render_template('supplier_report.html', **data), 400
        response = send_file(BytesIO(workbook), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                             as_attachment=True, download_name=f"supplier-{data['report']['supplier']['id']}-{data['month']}.xlsx",
                             max_age=0)
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Language'] = request.cookies.get('ui_language', 'ru') if request.cookies.get('ui_language') in ('ru', 'en') else 'ru'
        return response

    app.register_blueprint(bp)
