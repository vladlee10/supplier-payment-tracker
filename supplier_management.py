"""Rename a supplier or remove an unused card without changing ledger history."""
from i18n import translate as _
from contextlib import closing
import secrets
import sqlite3

from flask import Blueprint, abort, redirect, render_template, request, url_for
from itsdangerous import BadData, URLSafeTimedSerializer


def register_supplier_management(app, database_path):
    bp = Blueprint('supplier_management', __name__)
    # Bound to the supplier, action and current name. Old forms after a restart
    # are rejected with a fresh form, rather than accepting a blind POST.
    forms = URLSafeTimedSerializer(secrets.token_urlsafe(32), salt='supplier-management')

    def connect():
        db = sqlite3.connect(database_path(), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def load_supplier(db, supplier_id):
        if supplier_id > 2**63 - 1:
            abort(404)
        supplier = db.execute('''SELECT s.*,
            (SELECT COUNT(*) FROM deliveries d WHERE d.supplier_id=s.id) AS delivery_count,
            (SELECT COUNT(*) FROM payments p WHERE p.supplier_id=s.id) AS payment_count
            FROM suppliers s WHERE s.id=?''', (supplier_id,)).fetchone()
        if supplier is None:
            abort(404)
        return supplier

    def form_token(supplier, action):
        return forms.dumps(dict(id=supplier['id'], name=supplier['name'], action=action))

    def check_form(supplier, action):
        try:
            data = forms.loads(request.form.get('form_token', ''), max_age=3600)
            if not isinstance(data, dict) or data.get('id') != supplier['id'] or data.get('action') != action:
                raise BadData('Wrong form')
        except BadData as exc:
            raise ValueError(_('Страница устарела. Проверьте данные и повторите действие.')) from exc
        if data.get('name') != supplier['name']:
            raise ValueError(_('Название уже изменилось в другой вкладке. Проверьте текущее название и повторите действие.'))

    @bp.route('/suppliers/<int:supplier_id>/edit', methods=['GET', 'POST'])
    def edit(supplier_id):
        error = None
        status = 200
        with closing(connect()) as db:
            with db:
                if request.method == 'POST':
                    db.execute('BEGIN IMMEDIATE')
                supplier = load_supplier(db, supplier_id)
                name = supplier['name']
                if request.method == 'POST':
                    name = request.form.get('name', '').strip()
                    try:
                        check_form(supplier, 'edit')
                        if not name:
                            raise ValueError(_('Введите название поставщика.'))
                        if len(name) > 200:
                            raise ValueError(_('Название должно быть не длиннее 200 символов.'))
                        db.execute('UPDATE suppliers SET name=? WHERE id=?', (name, supplier_id))
                    except ValueError as exc:
                        error = str(exc)
                        status = 400
                    except sqlite3.IntegrityError:
                        error = _('Такой поставщик уже существует. Выберите другое название.')
                        status = 400
                    else:
                        return redirect(url_for('suppliers', result='updated'))
        return render_template('supplier_edit.html', supplier=supplier, name=name, error=error,
                               form_token=form_token(supplier, 'edit')), status

    @bp.route('/suppliers/<int:supplier_id>/delete', methods=['GET', 'POST'])
    def delete(supplier_id):
        error = None
        status = 200
        with closing(connect()) as db:
            with db:
                if request.method == 'POST':
                    db.execute('BEGIN IMMEDIATE')
                supplier = load_supplier(db, supplier_id)
                has_history = bool(supplier['delivery_count'] or supplier['payment_count'])
                if request.method == 'POST':
                    try:
                        check_form(supplier, 'delete')
                        # Recheck under the write lock: a delivery/payment may have
                        # been added since the confirmation page was opened.
                        if has_history:
                            raise ValueError(_('Удалить поставщика нельзя: у него есть поставки или оплаты. История учёта сохранена.'))
                        db.execute('DELETE FROM suppliers WHERE id=?', (supplier_id,))
                    except ValueError as exc:
                        error = str(exc)
                        status = 409 if has_history else 400
                    except sqlite3.IntegrityError:
                        error = _('Поставщик связан с записями учёта. Удаление отменено.')
                        status = 409
                    else:
                        return redirect(url_for('suppliers', result='deleted'))
        return render_template('supplier_delete.html', supplier=supplier, has_history=has_history,
                               error=error, form_token=form_token(supplier, 'delete')), status

    app.register_blueprint(bp)
