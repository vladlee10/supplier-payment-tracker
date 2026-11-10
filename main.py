from i18n import translate as _, register_i18n
from flask import Flask, render_template, request, redirect, url_for
import sqlite3
from pathlib import Path
from datetime import date, datetime
from contextlib import closing
from zoneinfo import ZoneInfo
from invoice_storage import initialize_database
from invoices import register_invoice_routes
from money import parse_money, money_text, money_input
from dashboard import overview
from supplier_management import register_supplier_management
from ledger_corrections import register_ledger_corrections
from supplier_reports import register_supplier_reports

app = Flask(__name__)
register_i18n(app)
app.jinja_env.filters["money"] = money_text
app.jinja_env.filters["money_input"] = money_input

DB_PATH = Path(__file__).parent / "supplier_payments.db"


def init_db():
    initialize_database(DB_PATH)


init_db()


@app.route("/", methods=["GET", "POST"])
def index():
    # Keep an already-open supplier form usable after the home page changes.
    if request.method == "POST":
        return suppliers()
    error = None
    try:
        data = overview(DB_PATH, request.args.get("month"))
    except ValueError as exc:
        data = overview(DB_PATH)
        error = str(exc)
    return render_template("dashboard.html", error=error, **data)


@app.route("/suppliers", methods=["GET", "POST"])
def suppliers():
    error = None

    if request.method == "POST":
        name = request.form.get("name", "").strip()

        if not name:
            error = _('Введите название поставщика.')
        elif len(name) > 200:
            error = _('Название должно быть не длиннее 200 символов.')
        else:
            try:
                with sqlite3.connect(DB_PATH) as connection:
                    connection.execute(
                        "INSERT INTO suppliers (name) VALUES (?)",
                        (name,),
                    )
            except sqlite3.IntegrityError:
                error = _('Такой поставщик уже существует.')
            else:
                return redirect(url_for("suppliers"))

    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        suppliers = connection.execute(
            "SELECT id, name FROM suppliers ORDER BY name"
        ).fetchall()

    return render_template(
        "index.html",
        suppliers=suppliers,
        error=error,
        message={"updated": _('Название поставщика сохранено.'), "deleted": _('Поставщик удалён.')}.get(request.args.get("result")),
    )

@app.route("/deliveries", methods=["GET", "POST"])
def deliveries():
    error = None

    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        suppliers = connection.execute(
            "SELECT id, name FROM suppliers ORDER BY name"
        ).fetchall()

        if request.method == "POST":
            try:
                supplier_id = int(request.form.get("supplier_id", ""))
                amount_minor = parse_money(request.form.get("amount_krw", ""))
                delivery_date = date.fromisoformat(
                    request.form.get("delivery_date", "")
                ).isoformat()
            except ValueError:
                error = _('Выберите поставщика, укажите дату и сумму в вонах.')
            else:
                if not any(s["id"] == supplier_id for s in suppliers):
                    error = _('Выберите существующего поставщика.')
                elif not 1 <= amount_minor <= 2**63 - 1:
                    error = _('Укажите корректную положительную сумму в вонах.')
                else:
                    with connection:
                        connection.execute(
                            """
                            INSERT INTO deliveries
                                (supplier_id, delivery_date, amount_minor)
                            VALUES (?, ?, ?)
                            """,
                            (supplier_id, delivery_date, amount_minor),
                        )

                    return redirect(url_for("deliveries"))

        delivery_rows = connection.execute(
            """
            SELECT deliveries.id,
                   (SELECT invoice_number FROM invoice_documents WHERE delivery_id=deliveries.id) AS invoice_number,
                   deliveries.delivery_date,
                   deliveries.amount_minor,
                   suppliers.name AS supplier_name
            FROM deliveries
            JOIN suppliers ON suppliers.id = deliveries.supplier_id
            ORDER BY deliveries.delivery_date DESC, deliveries.id DESC
            """
        ).fetchall()

    return render_template(
        "deliveries.html",
        suppliers=suppliers,
        deliveries=delivery_rows,
        error=error,
        today=date.today().isoformat(),
    )

@app.route("/payments", methods=["GET", "POST"])
def payments():
    error = None
    selected_id = (
        request.form.get("supplier_id", "")
        if request.method == "POST"
        else request.args.get("supplier_id", "")
    )

    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        if request.method == "POST":
            connection.execute("BEGIN IMMEDIATE")

        suppliers = connection.execute(
            "SELECT id, name FROM suppliers ORDER BY name"
        ).fetchall()

        selected_supplier = next(
            (s for s in suppliers if str(s["id"]) == selected_id),
            None,
        )

        invoices = []
        unallocated = []

        if selected_supplier is not None:
            invoices = connection.execute(
                """
                SELECT d.id, d.delivery_date, d.amount_minor,
                       (SELECT invoice_number FROM invoice_documents WHERE delivery_id=d.id) AS invoice_number,
                       d.amount_minor - COALESCE(SUM(a.amount_minor), 0) AS remaining
                FROM deliveries AS d
                LEFT JOIN active_payment_allocations AS a ON a.delivery_id = d.id
                WHERE d.supplier_id = ?
                GROUP BY d.id
                HAVING remaining > 0
                ORDER BY d.delivery_date, d.id
                """,
                (selected_supplier["id"],),
            ).fetchall()

            unallocated = connection.execute(
                """
                SELECT p.id, p.payment_date,
                       p.amount_minor - COALESCE(SUM(a.amount_minor), 0) AS available
                FROM active_payments AS p
                LEFT JOIN active_payment_allocations AS a ON a.payment_id = p.id
                WHERE p.supplier_id = ?
                GROUP BY p.id
                HAVING available > 0
                ORDER BY p.payment_date, p.id
                """,
                (selected_supplier["id"],),
            ).fetchall()

        if request.method == "POST":
            try:
                if selected_supplier is None:
                    raise ValueError(_('Выберите поставщика.'))

                if unallocated:
                    raise ValueError(
                        _('Сначала привяжите ранее записанные оплаты к накладным.')
                    )

                try:
                    delivery_id = int(request.form.get("delivery_id", ""))
                    expected = int(request.form.get("expected_remaining", ""))
                except ValueError:
                    raise ValueError(_('Выберите накладную из списка.'))

                invoice = next(
                    (row for row in invoices if row["id"] == delivery_id),
                    None,
                )

                if invoice is None or invoice["remaining"] != expected:
                    raise ValueError(
                        _('Остаток накладной изменился или она уже оплачена. Обновите страницу.')
                    )

                amount_minor = invoice["remaining"]
                payment_date = datetime.now(
                    ZoneInfo("Asia/Seoul")
                ).date().isoformat()

                with connection:
                    cursor = connection.execute(
                        """
                        INSERT INTO payments
                            (supplier_id, payment_date, amount_minor, note)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            selected_supplier["id"],
                            payment_date,
                            amount_minor,
                            f'Накладная: запись #{delivery_id}',
                        ),
                    )

                    connection.execute(
                        """
                        INSERT INTO payment_allocations
                            (payment_id, delivery_id, amount_minor)
                        VALUES (?, ?, ?)
                        """,
                        (cursor.lastrowid, delivery_id, amount_minor),
                    )

                return redirect(
                    url_for("payments", supplier_id=selected_supplier["id"])
                )

            except ValueError as exc:
                connection.rollback()
                error = str(exc)

        history_supplier_id = (
            selected_supplier["id"] if selected_supplier is not None else None
        )

        payment_rows = connection.execute(
            """
            SELECT p.*, s.name AS supplier_name
            FROM payments AS p
            JOIN suppliers AS s ON s.id = p.supplier_id
            WHERE (? IS NULL OR p.supplier_id = ?)
            ORDER BY p.payment_date DESC, p.id DESC
            """,
            (history_supplier_id, history_supplier_id),
        ).fetchall()

    balance = (
        sum(row["remaining"] for row in invoices)
        - sum(row["available"] for row in unallocated)
    )

    return render_template(
        "payments.html",
        suppliers=suppliers,
        selected_supplier=selected_supplier,
        invoices=invoices,
        unallocated=unallocated,
        debt=max(balance, 0),
        credit=max(-balance, 0),
        payments=payment_rows,
        error=error,
    )

@app.route("/payments/<int:payment_id>/allocate", methods=["GET", "POST"])
def allocate_payment(payment_id):
    error = None

    if payment_id > 2**63 - 1:
        return _('Оплата не найдена'), 404

    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        if request.method == "POST":
            connection.execute("BEGIN IMMEDIATE")

        payment = connection.execute(
            """
            SELECT p.*,
                   s.name AS supplier_name,
                   p.amount_minor - COALESCE((
                       SELECT SUM(a.amount_minor)
                       FROM active_payment_allocations AS a
                       WHERE a.payment_id = p.id
                   ), 0) AS available
            FROM active_payments AS p
            JOIN suppliers AS s ON s.id = p.supplier_id
            WHERE p.id = ?
            """,
            (payment_id,),
        ).fetchone()

        if payment is None:
            cancelled = connection.execute("SELECT 1 FROM payments WHERE id=? AND cancelled_at IS NOT NULL", (payment_id,)).fetchone()
            if cancelled:
                return redirect(url_for("ledger_corrections.cancel_payment", payment_id=payment_id))
            return _('Оплата не найдена'), 404

        invoices = connection.execute(
            """
            SELECT d.id,
                   (SELECT invoice_number FROM invoice_documents WHERE delivery_id=d.id) AS invoice_number,
                   d.delivery_date,
                   d.amount_minor,
                   d.amount_minor - COALESCE(SUM(a.amount_minor), 0) AS remaining
            FROM deliveries AS d
            LEFT JOIN active_payment_allocations AS a ON a.delivery_id = d.id
            WHERE d.supplier_id = ?
            GROUP BY d.id
            HAVING remaining > 0
            ORDER BY d.delivery_date, d.id
            """,
            (payment["supplier_id"],),
        ).fetchall()

        if request.method == "POST":
            try:
                delivery_id = int(request.form.get("delivery_id", ""))
                amount_minor = parse_money(request.form.get("amount_krw", ""))
                expected_available = int(request.form.get("expected_available", ""))
                expected_remaining = int(request.form.get("expected_remaining", ""))

                invoice = next(
                    (row for row in invoices if row["id"] == delivery_id),
                    None,
                )

                if invoice is None:
                    raise ValueError

                if expected_available != payment["available"]:
                    raise ValueError

                if expected_remaining != invoice["remaining"]:
                    raise ValueError

                if not 1 <= amount_minor <= min(
                    payment["available"], invoice["remaining"]
                ):
                    raise ValueError

                with connection:
                    connection.execute(
                        """
                        INSERT INTO payment_allocations
                            (payment_id, delivery_id, amount_minor)
                        VALUES (?, ?, ?)
                        ON CONFLICT (payment_id, delivery_id)
                        DO UPDATE SET amount_minor =
                            payment_allocations.amount_minor + excluded.amount_minor
                        """,
                        (payment_id, delivery_id, amount_minor),
                    )

                return redirect(
                    url_for("allocate_payment", payment_id=payment_id)
                )

            except ValueError:
                connection.rollback()
                error = (
                    _('Проверьте накладную и сумму. Если остатки изменились, обновите страницу.')
                )

        return render_template(
            "allocate_payment.html",
            payment=payment,
            invoices=invoices,
            error=error,
        )

@app.route("/paid-invoices")
def paid_invoices():
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row

        invoices = connection.execute(
            """
            SELECT d.id,
                   (SELECT invoice_number FROM invoice_documents WHERE delivery_id=d.id) AS invoice_number,
                   d.supplier_id,
                   s.name AS supplier_name,
                   d.delivery_date,
                   d.amount_minor,
                   SUM(a.amount_minor) AS paid_amount,
                   MAX(p.payment_date) AS last_payment_date
            FROM deliveries AS d
            JOIN suppliers AS s ON s.id = d.supplier_id
            JOIN active_payment_allocations AS a ON a.delivery_id = d.id
            JOIN active_payments AS p
                ON p.id = a.payment_id
                AND p.supplier_id = d.supplier_id
            GROUP BY d.id
            HAVING paid_amount >= d.amount_minor
            ORDER BY s.name, s.id, last_payment_date DESC, d.id DESC
            """
        ).fetchall()

    groups = {}

    for invoice in invoices:
        supplier_id = invoice["supplier_id"]

        if supplier_id not in groups:
            groups[supplier_id] = {
                "name": invoice["supplier_name"],
                "invoices": [],
            }

        groups[supplier_id]["invoices"].append(invoice)

    return render_template(
        "paid_invoices.html",
        supplier_groups=list(groups.values()),
    )

register_invoice_routes(app, lambda: DB_PATH)
register_supplier_management(app, lambda: DB_PATH)
register_ledger_corrections(app, lambda: DB_PATH)
register_supplier_reports(app, lambda: DB_PATH)


if __name__ == "__main__":
    app.run(debug=True, port=5002)
