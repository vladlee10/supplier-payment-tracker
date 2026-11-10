"""Audited invoice edits and reversible accounting effects of cancelled payments."""
from i18n import translate as _, get_language
from contextlib import closing
from datetime import date, datetime
import hashlib
import json
import secrets
import sqlite3
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, redirect, render_template, request, url_for
from itsdangerous import BadData, URLSafeTimedSerializer

from invoice_validation import apply_invoice_form, validate_invoice
from money import money_input, money_text, parse_money


def json_state(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def invoice_state(db, delivery_id):
    if delivery_id > 2**63-1:
        abort(404)
    delivery = db.execute('SELECT d.*,s.name AS supplier_name FROM deliveries d JOIN suppliers s ON s.id=d.supplier_id WHERE d.id=?', (delivery_id,)).fetchone()
    if delivery is None:
        abort(404)
    document = db.execute('SELECT * FROM invoice_documents WHERE delivery_id=?', (delivery_id,)).fetchone()
    return dict(delivery=dict(delivery), document=dict(document) if document else None,
                items=[dict(r) for r in db.execute('SELECT * FROM invoice_items WHERE delivery_id=? ORDER BY position', (delivery_id,))],
                allocations=[dict(r) for r in db.execute('SELECT * FROM active_payment_allocations WHERE delivery_id=? ORDER BY id', (delivery_id,))])


def invoice_draft(state):
    delivery, doc = state['delivery'], state['document']
    draft=dict(invoice_number=doc['invoice_number'] if doc else '', delivery_date=delivery['delivery_date'],
               supplier_name=delivery['supplier_name'], buyer=doc['buyer'] if doc else '',
               printed_total=money_input(delivery['amount_minor']),
               vat_total=money_input(doc['vat_total_minor']) if doc and doc['vat_total_minor'] is not None else '',
               vat_mode=doc['vat_mode'] if doc else '', expected_count='', items=[], warnings=[],
               token=doc['draft_token'] if doc else '', note=delivery['note'])
    for row in state['items']:
        draft['items'].append(dict(name=row['name'], quantity=row['quantity'], unit=row['unit'],
            unit_price=money_input(row['unit_price_minor']), discount=money_input(row['discount_minor']),
            vat_rate=row['vat_rate'], vat_amount=money_input(row['vat_amount_minor']) if row['vat_amount_minor'] is not None else '',
            line_total=money_input(row['line_total_minor']), notes=[]))
    return draft


def revision_history(db, delivery_id):
    return [dict(id=r['id'], changed_at=r['changed_at'], reason=r['reason'],
                 before=json.loads(r['before_json']), after=json.loads(r['after_json']))
            for r in db.execute('SELECT * FROM invoice_revisions WHERE delivery_id=? ORDER BY id DESC', (delivery_id,))]


def register_ledger_corrections(app, database_path):
    bp=Blueprint('ledger_corrections', __name__)
    forms=URLSafeTimedSerializer(secrets.token_urlsafe(32), salt='ledger-corrections')
    app.jinja_env.filters['local_datetime']=lambda value: datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d %H:%M' if get_language() == 'en' else '%d.%m.%Y %H:%M')

    def connect():
        db=sqlite3.connect(database_path(), timeout=30)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def token(action, record_id, state):
        version=hashlib.sha256(json_state(state).encode()).hexdigest()
        return forms.dumps(dict(action=action, id=record_id, version=version))

    def verify(action, record_id, state):
        try:
            value=forms.loads(request.form.get('form_token',''), max_age=3600)
            if not isinstance(value,dict) or value.get('id')!=record_id or value.get('action')!=action:
                raise BadData('Wrong form')
        except BadData as exc:
            raise ValueError(_('Страница устарела. Проверьте данные в обновлённой форме и повторите действие.')) from exc
        version=hashlib.sha256(json_state(state).encode()).hexdigest()
        if value.get('version')!=version:
            raise ValueError(_('Накладная или связанные оплаты изменились в другой вкладке. Проверьте текущие данные перед сохранением.'))

    def reason():
        text=request.form.get('reason','').strip()
        if not text or len(text)>1000:
            raise ValueError(_('Укажите причину изменения — от 1 до 1000 символов.'))
        return text

    @bp.route('/payments/<int:payment_id>/cancel', methods=['GET','POST'])
    def cancel_payment(payment_id):
        if payment_id>2**63-1:
            abort(404)
        error=None
        status=200
        with closing(connect()) as db:
            if request.method=='POST':
                db.execute('BEGIN IMMEDIATE')
            payment=db.execute('SELECT p.*,s.name AS supplier_name FROM payments p JOIN suppliers s ON s.id=p.supplier_id WHERE p.id=?',(payment_id,)).fetchone()
            if payment is None:
                abort(404)
            payment=dict(payment)
            allocations=[dict(r) for r in db.execute('''SELECT a.*,d.delivery_date,d.amount_minor AS invoice_amount,
                doc.invoice_number, COALESCE((SELECT SUM(x.amount_minor) FROM active_payment_allocations x WHERE x.delivery_id=d.id),0) AS paid_minor
                FROM payment_allocations a JOIN deliveries d ON d.id=a.delivery_id
                LEFT JOIN invoice_documents doc ON doc.delivery_id=d.id WHERE a.payment_id=? ORDER BY a.id''',(payment_id,))]
            state=dict(payment=payment,allocations=allocations)
            if request.method=='POST':
                if payment['cancelled_at']:
                    db.rollback()
                    return redirect(url_for('ledger_corrections.cancel_payment', payment_id=payment_id))
                try:
                    verify('cancel',payment_id,state)
                    text=reason()
                    db.execute('UPDATE payments SET cancelled_at=?,cancellation_reason=? WHERE id=? AND cancelled_at IS NULL',
                               (datetime.now(ZoneInfo('Asia/Seoul')).isoformat(),text,payment_id))
                    db.commit()
                    return redirect(url_for('payments',supplier_id=payment['supplier_id'],result='cancelled'))
                except ValueError as exc:
                    db.rollback(); error=str(exc); status=400
            form_token=token('cancel',payment_id,state)
        for row in allocations:
            row['remaining_after']=max(row['invoice_amount']-row['paid_minor']+(row['amount_minor'] if not payment['cancelled_at'] else 0),0)
        return render_template('payment_cancel.html',payment=payment,allocations=allocations,
                               error=error,form_token=form_token),status

    @bp.route('/invoices/<int:delivery_id>/edit', methods=['GET','POST'])
    def edit_invoice(delivery_id):
        error=None
        status=200
        with closing(connect()) as db:
            if request.method=='POST':
                db.execute('BEGIN IMMEDIATE')
            state=invoice_state(db,delivery_id)
            draft=invoice_draft(state)
            detailed=state['document'] is not None
            paid=sum(r['amount_minor'] for r in state['allocations'])
            if request.method=='POST':
                try:
                    if detailed:
                        apply_invoice_form(draft,request.form)
                    else:
                        draft.update(delivery_date=request.form.get('delivery_date',''),printed_total=request.form.get('printed_total',''),note=request.form.get('note','').strip())
                    verify('edit',delivery_id,state)
                    text=reason()
                    if detailed:
                        values=validate_invoice(draft,reviewed=request.form.get('reviewed'),discounts_reviewed=request.form.get('discounts_reviewed'))
                        new_date,new_total=values['delivery_date'],values['total']
                        invoice_number=draft['invoice_number']
                        if invoice_number:
                            duplicate=db.execute('''SELECT 1 FROM deliveries d JOIN invoice_documents doc ON doc.delivery_id=d.id
                                WHERE d.supplier_id=? AND d.delivery_date=? AND doc.invoice_number=? AND d.id<>?''',
                                (state['delivery']['supplier_id'],new_date,invoice_number,delivery_id)).fetchone()
                            if duplicate:
                                raise ValueError(_('У этого поставщика уже есть другая накладная с таким номером и датой.'))
                        note=state['delivery']['note']
                        old_number=state['document']['invoice_number']
                        if invoice_number!=old_number and note==('Накладная № '+old_number if old_number else ''):
                            note='Накладная № '+invoice_number if invoice_number else ''
                    else:
                        new_date=date.fromisoformat(draft['delivery_date']).isoformat()
                        new_total=parse_money(draft['printed_total'])
                        if len(draft['note'])>1000:
                            raise ValueError(_('Примечание должно быть не длиннее 1000 символов.'))
                        note=draft['note']
                    if new_total<paid:
                        raise ValueError(_('К накладной уже привязано {value1} ₩ оплат. Новая сумма не может быть меньше. Проверьте записи оплат и их распределение.', value1=money_text(paid)))
                    db.execute('UPDATE deliveries SET delivery_date=?,amount_minor=?,note=? WHERE id=?',
                               (new_date,new_total,note,delivery_id))
                    if detailed:
                        db.execute('''UPDATE invoice_documents SET invoice_number=?,buyer=?,printed_total_minor=?,vat_total_minor=?,vat_mode=? WHERE delivery_id=?''',
                            (draft['invoice_number'],draft['buyer'],values['printed_total'],values['vat_total'],draft['vat_mode'],delivery_id))
                        db.execute('DELETE FROM invoice_items WHERE delivery_id=?',(delivery_id,))
                        db.executemany('''INSERT INTO invoice_items(delivery_id,position,name,quantity,unit,unit_price_minor,vat_rate,
                            discount_minor,vat_amount_minor,line_total_minor) VALUES (?,?,?,?,?,?,?,?,?,?)''',[(delivery_id,*row) for row in values['items']])
                    after=invoice_state(db,delivery_id)
                    def content(value):
                        return dict(value,items=[{k:v for k,v in row.items() if k!='id'} for row in value['items']])
                    if content(state)==content(after):
                        db.rollback()
                    else:
                        db.execute('INSERT INTO invoice_revisions(delivery_id,changed_at,reason,before_json,after_json) VALUES(?,?,?,?,?)',
                            (delivery_id,datetime.now(ZoneInfo('Asia/Seoul')).isoformat(),text,json_state(state),json_state(after)))
                        db.commit()
                    return redirect(url_for('invoices.detail',delivery_id=delivery_id,result='updated'))
                except (ValueError,OverflowError) as exc:
                    db.rollback(); error=str(exc) or _('Проверьте поля накладной.');status=400
                except sqlite3.IntegrityError:
                    db.rollback();error=_('Не удалось сохранить все строки. Изменения отменены; проверьте накладную.');status=400
            return render_template('invoice_edit.html',draft=draft,state=state,detailed=detailed,paid=paid,
                                   error=error,form_token=token('edit',delivery_id,state)),status

    app.register_blueprint(bp)
