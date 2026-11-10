"""Schema creation and an atomic, repeatable migration of the existing ledger."""
from contextlib import closing
import sqlite3

from money import MAX_MONEY


def initialize_database(path):
    with closing(sqlite3.connect(path, timeout=30)) as db:
        db.execute('PRAGMA foreign_keys = ON')
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute('CREATE TABLE IF NOT EXISTS suppliers (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)')
            for table, date_column in [('deliveries', 'delivery_date'), ('payments', 'payment_date')]:
                db.execute(f'''CREATE TABLE IF NOT EXISTS {table} (
                    id INTEGER PRIMARY KEY,
                    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
                    {date_column} TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL CHECK(typeof(amount_minor)='integer' AND amount_minor>0),
                    note TEXT NOT NULL DEFAULT '')''')
            db.execute('''CREATE TABLE IF NOT EXISTS payment_allocations (
                id INTEGER PRIMARY KEY,
                payment_id INTEGER NOT NULL REFERENCES payments(id),
                delivery_id INTEGER NOT NULL REFERENCES deliveries(id),
                amount_minor INTEGER NOT NULL CHECK(typeof(amount_minor)='integer' AND amount_minor>0),
                UNIQUE(payment_id, delivery_id))''')
            for table in ('deliveries', 'payments', 'payment_allocations'):
                columns = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
                if 'amount_krw' in columns:
                    if 'amount_minor' in columns:
                        raise RuntimeError('Неоднозначный формат денежных сумм.')
                    if db.execute(f'SELECT 1 FROM {table} WHERE amount_krw>? LIMIT 1', (MAX_MONEY // 100,)).fetchone():
                        raise RuntimeError('Слишком большая сумма для перевода формата.')
                    db.execute(f'ALTER TABLE {table} RENAME COLUMN amount_krw TO amount_minor')
                    db.execute(f'UPDATE {table} SET amount_minor=amount_minor*100')
            db.execute('''CREATE TABLE IF NOT EXISTS invoice_documents (
                delivery_id INTEGER PRIMARY KEY REFERENCES deliveries(id),
                invoice_number TEXT NOT NULL,
                buyer TEXT NOT NULL DEFAULT '',
                draft_token TEXT NOT NULL UNIQUE,
                image_extension TEXT NOT NULL,
                source_sha256 TEXT NOT NULL UNIQUE,
                printed_total_minor INTEGER NOT NULL CHECK(printed_total_minor>0),
                vat_total_minor INTEGER CHECK(vat_total_minor>=0),
                vat_mode TEXT NOT NULL CHECK(vat_mode IN ('included','excluded')),
                created_at TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS invoice_items (
                id INTEGER PRIMARY KEY,
                delivery_id INTEGER NOT NULL REFERENCES deliveries(id),
                position INTEGER NOT NULL,
                name TEXT NOT NULL,
                quantity TEXT NOT NULL,
                unit TEXT NOT NULL DEFAULT '',
                unit_price_minor INTEGER NOT NULL CHECK(unit_price_minor>0),
                vat_rate TEXT NOT NULL DEFAULT '',
                vat_amount_minor INTEGER CHECK(vat_amount_minor>=0),
                line_total_minor INTEGER NOT NULL CHECK(line_total_minor>0),
                UNIQUE(delivery_id, position))''')
            item_columns = {r[1] for r in db.execute('PRAGMA table_info(invoice_items)')}
            if 'discount_minor' not in item_columns:
                db.execute("ALTER TABLE invoice_items ADD COLUMN discount_minor INTEGER NOT NULL DEFAULT 0 CHECK(typeof(discount_minor)='integer' AND discount_minor>=0)")
            payment_columns = {r[1] for r in db.execute('PRAGMA table_info(payments)')}
            if 'cancelled_at' not in payment_columns:
                db.execute('ALTER TABLE payments ADD COLUMN cancelled_at TEXT')
            if 'cancellation_reason' not in payment_columns:
                db.execute("ALTER TABLE payments ADD COLUMN cancellation_reason TEXT NOT NULL DEFAULT ''")
            db.execute('''CREATE VIEW IF NOT EXISTS active_payments AS
                SELECT * FROM payments WHERE cancelled_at IS NULL''')
            db.execute('''CREATE VIEW IF NOT EXISTS active_payment_allocations AS
                SELECT a.* FROM payment_allocations a JOIN payments p ON p.id=a.payment_id
                WHERE p.cancelled_at IS NULL''')
            db.execute('''CREATE TABLE IF NOT EXISTS invoice_revisions (
                id INTEGER PRIMARY KEY,
                delivery_id INTEGER NOT NULL REFERENCES deliveries(id),
                changed_at TEXT NOT NULL,
                reason TEXT NOT NULL,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL)''')
            db.commit()
        except Exception:
            db.rollback()
            raise
