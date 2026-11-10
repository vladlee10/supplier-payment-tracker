from i18n import translate as _
"""Photo upload -> editable draft -> validated invoice and its line items."""
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, redirect, render_template, request, send_file, url_for
from werkzeug.exceptions import RequestEntityTooLarge

from invoice_ocr import recognize_image
from invoice_validation import apply_invoice_form, validate_invoice
from ledger_corrections import revision_history


def register_invoice_routes(app, database_path):
    bp = Blueprint('invoices', __name__)
    app.config.setdefault('INVOICE_STORAGE', str(Path(app.root_path) / 'invoice_files'))
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

    def connect():
        db = sqlite3.connect(database_path(), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def storage():
        path = Path(current_app.config['INVOICE_STORAGE'])
        path.mkdir(parents=True, exist_ok=True)
        return path

    def read_draft(token):
        if not re.fullmatch(r'[a-f0-9]{32}', token):
            abort(404)
        path = storage() / (token + '.json')
        if not path.is_file():
            abort(404)
        return json.loads(path.read_text())

    @bp.route('/invoices/import', methods=['GET', 'POST'])
    def upload():
        error = None
        if request.method == 'POST':
            photo_path = None
            try:
                upload = request.files.get('photo')
                if upload is None or not upload.filename:
                    raise ValueError(_('Выберите фотографию накладной.'))
                data = upload.read(15 * 1024 * 1024 + 1)
                if len(data) > 15 * 1024 * 1024:
                    raise ValueError(_('Фото должно быть не больше 15 МБ.'))
                if data.startswith(b'\x89PNG\r\n\x1a\n'):
                    extension = '.png'
                elif data.startswith(b'\xff\xd8\xff'):
                    extension = '.jpg'
                else:
                    raise ValueError(_('Выберите изображение JPG или PNG.'))
                digest = hashlib.sha256(data).hexdigest()
                with closing(connect()) as db:
                    existing = db.execute('SELECT delivery_id FROM invoice_documents WHERE source_sha256=?', (digest,)).fetchone()
                if existing:
                    return redirect(url_for('invoices.detail', delivery_id=existing['delivery_id']))
                token = uuid4().hex
                photo_path = storage() / (token + extension)
                photo_path.write_bytes(data)
                draft = recognize_image(photo_path)
                draft.update(token=token, extension=extension, source_sha256=digest)
                (storage() / (token + '.json')).write_text(json.dumps(draft, ensure_ascii=False))
                return redirect(url_for('invoices.review', token=token))
            except ValueError as exc:
                if photo_path and photo_path.exists():
                    photo_path.unlink()
                error = str(exc)
        return render_template('invoice_upload.html', error=error)

    @bp.route('/invoices/drafts/<token>', methods=['GET', 'POST'])
    def review(token):
        draft = read_draft(token)
        with closing(connect()) as db:
            existing = db.execute('SELECT delivery_id FROM invoice_documents WHERE draft_token=? OR source_sha256=?', (token, draft['source_sha256'])).fetchone()
            if existing:
                return redirect(url_for('invoices.detail', delivery_id=existing['delivery_id']))
            suppliers = db.execute('SELECT id,name FROM suppliers ORDER BY name').fetchall()
        match = next((str(s['id']) for s in suppliers if s['name'].casefold() in draft['supplier_name'].casefold()), 'new')
        selected = request.form.get('supplier_id', match)
        error = None
        if request.method == 'POST':
            try:
                apply_invoice_form(draft, request.form)
                values=validate_invoice(draft, reviewed=request.form.get('reviewed'), discounts_reviewed=request.form.get('discounts_reviewed'))
                delivery_date, printed_total, vat_total, total, items = (values[k] for k in ('delivery_date','printed_total','vat_total','total','items'))
                with closing(connect()) as db:
                    db.execute('BEGIN IMMEDIATE')
                    with db:
                        already=db.execute('SELECT delivery_id FROM invoice_documents WHERE draft_token=? OR source_sha256=?',(token,draft['source_sha256'])).fetchone()
                        if already:
                            return redirect(url_for('invoices.detail',delivery_id=already['delivery_id']))
                        if selected=='new':
                            supplier_name=draft['supplier_name'].strip()
                            if not supplier_name or len(supplier_name)>200:
                                raise ValueError(_('Укажите название поставщика.'))
                            db.execute('INSERT INTO suppliers(name) VALUES (?) ON CONFLICT(name) DO NOTHING',(supplier_name,))
                            supplier_id=db.execute('SELECT id FROM suppliers WHERE name=?',(supplier_name,)).fetchone()['id']
                        else:
                            supplier_id=int(selected)
                            if not any(s['id']==supplier_id for s in suppliers):
                                raise ValueError(_('Выберите существующего поставщика.'))
                        invoice_number=draft['invoice_number']
                        if invoice_number:
                            duplicate=db.execute('''SELECT d.id FROM deliveries d JOIN invoice_documents doc ON doc.delivery_id=d.id
                                WHERE d.supplier_id=? AND d.delivery_date=? AND doc.invoice_number=?''',(supplier_id,delivery_date,invoice_number)).fetchone()
                            if duplicate:
                                raise ValueError(_('У этого поставщика уже есть накладная с таким номером и датой.'))
                        cursor=db.execute('INSERT INTO deliveries(supplier_id,delivery_date,amount_minor,note) VALUES (?,?,?,?)',
                            (supplier_id,delivery_date,total,'Накладная № '+invoice_number if invoice_number else ''))
                        delivery_id=cursor.lastrowid
                        db.execute('''INSERT INTO invoice_documents(delivery_id,invoice_number,buyer,draft_token,image_extension,
                            source_sha256,printed_total_minor,vat_total_minor,vat_mode,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                            (delivery_id,invoice_number,draft['buyer'],token,draft['extension'],draft['source_sha256'],printed_total,
                             vat_total,draft['vat_mode'],datetime.now(ZoneInfo('Asia/Seoul')).isoformat()))
                        db.executemany('''INSERT INTO invoice_items(delivery_id,position,name,quantity,unit,unit_price_minor,vat_rate,
                            discount_minor,vat_amount_minor,line_total_minor) VALUES (?,?,?,?,?,?,?,?,?,?)''',[(delivery_id,*item) for item in items])
                return redirect(url_for('invoices.detail',delivery_id=delivery_id))
            except (ValueError, OverflowError) as exc:
                error=str(exc) or _('Проверьте поля накладной.')
        return render_template('invoice_review.html',draft=draft,suppliers=suppliers,selected=selected,error=error)

    @bp.get('/invoices/images/<token>')
    def image(token):
        draft=read_draft(token)
        if draft['extension'] not in ('.jpg','.png'):
            abort(404)
        response=send_file(storage()/(token+draft['extension']),mimetype='image/png' if draft['extension']=='.png' else 'image/jpeg')
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Cache-Control']='private, no-store'
        return response

    @bp.get('/invoices/<int:delivery_id>')
    def detail(delivery_id):
        if delivery_id>2**63-1:
            abort(404)
        with closing(connect()) as db:
            delivery=db.execute('''SELECT d.*,s.name AS supplier_name,
                COALESCE((SELECT SUM(a.amount_minor) FROM active_payment_allocations a WHERE a.delivery_id=d.id),0) AS paid_minor
                FROM deliveries d JOIN suppliers s ON s.id=d.supplier_id WHERE d.id=?''',(delivery_id,)).fetchone()
            if delivery is None:
                abort(404)
            document=db.execute('SELECT * FROM invoice_documents WHERE delivery_id=?',(delivery_id,)).fetchone()
            items=db.execute('SELECT * FROM invoice_items WHERE delivery_id=? ORDER BY position',(delivery_id,)).fetchall()
            revisions=revision_history(db,delivery_id)
        return render_template('invoice_detail.html',delivery=delivery,document=document,items=items,revisions=revisions)

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(error):
        return render_template('invoice_upload.html',error=_('Фото должно быть не больше 15 МБ.')),413

    app.register_blueprint(bp)
