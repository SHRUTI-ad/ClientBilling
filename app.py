"""
Client Billing Manager
Track clients, payments, balances, and generate monthly PDF invoices.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch, mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import func, inspect, text

START_MONTH = "2026-10"

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PDF_DIR = os.path.join(BASE_DIR, "invoices_pdf")
os.makedirs(PDF_DIR, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "client-billing-dev-key-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(BASE_DIR, "billing.db"),
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), default="")
    phone = db.Column(db.String(40), default="")
    address = db.Column(db.Text, default="")
    monthly_fee = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    opening_balance = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    opening_date = db.Column(db.Date, nullable=True)
    notes = db.Column(db.Text, default="")
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    payments = db.relationship(
        "Payment",
        backref="client",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="desc(Payment.payment_date)",
    )
    charges = db.relationship(
        "Charge",
        backref="client",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="desc(Charge.charge_date)",
    )
    invoices = db.relationship(
        "Invoice",
        backref="client",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="desc(Invoice.issue_date)",
    )

    @property
    def total_charged(self) -> Decimal:
        total = (
            db.session.query(func.coalesce(func.sum(Charge.amount), 0))
            .filter(Charge.client_id == self.id)
            .scalar()
        )
        return Decimal(str(total))

    @property
    def total_paid(self) -> Decimal:
        total = (
            db.session.query(func.coalesce(func.sum(Payment.amount), 0))
            .filter(Payment.client_id == self.id)
            .scalar()
        )
        return Decimal(str(total))

    @property
    def pending(self) -> Decimal:
        return self.total_charged - self.total_paid - money(self.opening_balance)

    def package_for_month(self, billing_month: str) -> Decimal:
        return standard_package(billing_month)


class Charge(db.Model):
    __tablename__ = "charges"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    charge_date = db.Column(db.Date, nullable=False, default=date.today)
    billing_month = db.Column(db.String(7), nullable=False)  # YYYY-MM
    description = db.Column(db.String(255), default="Monthly fee")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    payment_date = db.Column(db.Date, nullable=False, default=date.today)
    method = db.Column(db.String(50), default="")
    place = db.Column(db.String(120), default="")
    received_from = db.Column(db.String(120), default="")
    notes = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def place_label(self) -> str:
        return self.place or self.method or ""


class MonthlyPackage(db.Model):
    __tablename__ = "monthly_packages"
    __table_args__ = (db.UniqueConstraint("client_id", "billing_month"),)

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    billing_month = db.Column(db.String(7), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)


class Invoice(db.Model):
    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    invoice_number = db.Column(db.String(40), unique=True, nullable=False)
    billing_month = db.Column(db.String(7), nullable=False)
    issue_date = db.Column(db.Date, nullable=False, default=date.today)
    monthly_fee = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    previous_balance = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_charged = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total_paid = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    balance_due = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Settings(db.Model):
    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    business_name = db.Column(db.String(120), default="My Business")
    business_email = db.Column(db.String(120), default="")
    business_phone = db.Column(db.String(40), default="")
    business_address = db.Column(db.Text, default="")
    currency_symbol = db.Column(db.String(8), default="₹")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def parse_date(value: str | None, fallback: date | None = None) -> date:
    if value:
        return datetime.strptime(value, "%Y-%m-%d").date()
    return fallback or date.today()


def get_settings() -> Settings:
    settings = Settings.query.first()
    if not settings:
        settings = Settings()
        db.session.add(settings)
        db.session.commit()
    return settings


def fmt_money(value, symbol="₹") -> str:
    return f"{symbol}{money(value):,.2f}"


def next_invoice_number(billing_month: str) -> str:
    ym = billing_month.replace("-", "")
    prefix = f"INV-{ym}-"
    last = (
        Invoice.query.filter(Invoice.invoice_number.like(f"{prefix}%"))
        .order_by(Invoice.invoice_number.desc())
        .first()
    )
    seq = 1
    if last:
        try:
            seq = int(last.invoice_number.split("-")[-1]) + 1
        except ValueError:
            seq = Invoice.query.count() + 1
    return f"{prefix}{seq:03d}"


def month_label(billing_month: str) -> str:
    try:
        return datetime.strptime(billing_month, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return billing_month


def add_months(billing_month: str, count: int) -> str:
    year, month = (int(part) for part in billing_month.split("-"))
    index = (year * 12 + month - 1) + count
    return f"{index // 12}-{index % 12 + 1:02d}"


def current_billing_month() -> str:
    this_month = date.today().strftime("%Y-%m")
    return max(this_month, START_MONTH)


def billing_months(extra_ahead: int = 2) -> list[str]:
    last = add_months(current_billing_month(), extra_ahead)
    months = []
    cursor = START_MONTH
    while cursor <= last:
        months.append(cursor)
        cursor = add_months(cursor, 1)
    return months


def months_between(start_month: str, end_month: str) -> list[str]:
    if end_month < start_month:
        start_month, end_month = end_month, start_month
    start_month = max(start_month, START_MONTH)
    months = []
    cursor = start_month
    while cursor <= end_month:
        months.append(cursor)
        cursor = add_months(cursor, 1)
    return months


def accounting_year_bounds(on_date: date | None = None) -> tuple[date, date]:
    on_date = on_date or date.today()
    first = date(2026, 10, 1)
    if on_date < first:
        return first, date(2027, 9, 30)
    if on_date.month >= 10:
        return date(on_date.year, 10, 1), date(on_date.year + 1, 9, 30)
    return date(on_date.year - 1, 10, 1), date(on_date.year, 9, 30)


def standard_package(billing_month: str) -> Decimal:
    """October and the other months are 20,000. November, December, January and February are 35,000."""
    month = int(billing_month.split("-")[1])
    if month in (11, 12, 1, 2):
        return money(35000)
    return money(20000)


def package_charge_for(client_id: int, billing_month: str) -> Charge | None:
    return (
        Charge.query.filter(
            Charge.client_id == client_id,
            Charge.billing_month == billing_month,
            Charge.description.like("Monthly package%"),
        )
        .order_by(Charge.id.desc())
        .first()
    )


def upsert_package_charge(client: Client, billing_month: str, amount: Decimal, charge_date: date) -> Charge:
    description = f"Monthly package — {month_label(billing_month)}"
    charge = package_charge_for(client.id, billing_month)
    if charge:
        charge.amount = amount
        charge.charge_date = charge_date
        charge.description = description
    else:
        charge = Charge(
            client_id=client.id,
            amount=amount,
            charge_date=charge_date,
            billing_month=billing_month,
            description=description,
        )
        db.session.add(charge)
    db.session.flush()
    return charge


def ensure_monthly_charge(client: Client, billing_month: str) -> Charge | None:
    """Add the saved monthly package for that month if it is not already posted."""
    amount = client.package_for_month(billing_month)
    if amount <= 0:
        return package_charge_for(client.id, billing_month)
    year, month = (int(part) for part in billing_month.split("-"))
    return upsert_package_charge(client, billing_month, amount, date(year, month, 1))


def build_ledger(client: Client) -> list[dict]:
    events = []
    opening_on = client.opening_date or (
        client.created_at.date() if client.created_at else date.today()
    )
    events.append(
        {
            "date": opening_on,
            "sort": 0,
            "kind": "opening",
            "particulars": "Advance balance",
            "added": Decimal("0"),
            "deducted": money(client.opening_balance),
            "ref_id": None,
        }
    )
    charges = Charge.query.filter_by(client_id=client.id).order_by(Charge.charge_date, Charge.id)
    for charge in charges:
        events.append(
            {
                "date": charge.charge_date,
                "sort": 1,
                "kind": "charge",
                "particulars": charge.description,
                "added": money(charge.amount),
                "deducted": Decimal("0"),
                "ref_id": charge.id,
            }
        )
    payments = Payment.query.filter_by(client_id=client.id).order_by(Payment.payment_date, Payment.id)
    for payment in payments:
        who = payment.received_from or "client"
        particulars = f"Payment received from {who}"
        if payment.place:
            particulars += f" · {payment.place}"
        events.append(
            {
                "date": payment.payment_date,
                "sort": 2,
                "kind": "payment",
                "particulars": particulars,
                "added": Decimal("0"),
                "deducted": money(payment.amount),
                "ref_id": payment.id,
            }
        )
    events.sort(key=lambda item: (item["date"], item["sort"], item["ref_id"] or 0))
    balance = Decimal("0")
    for item in events:
        balance += item["added"] - item["deducted"]
        item["balance"] = balance
    return events


_letterhead_reader = None


def letterhead_reader():
    """A smaller copy of the letterhead, so each invoice PDF stays light."""
    global _letterhead_reader
    if _letterhead_reader is not None:
        return _letterhead_reader
    background = os.path.join(BASE_DIR, "BILL_GROWW.png")
    if not os.path.exists(background):
        return None
    from PIL import Image as PILImage
    from reportlab.lib.utils import ImageReader

    image = PILImage.open(background).convert("RGB")
    image.thumbnail((1240, 1754), PILImage.Resampling.LANCZOS)
    encoded = BytesIO()
    image.save(encoded, format="JPEG", quality=85)
    encoded.seek(0)
    _letterhead_reader = ImageReader(encoded)
    return _letterhead_reader


def draw_invoice_background(canvas, doc) -> None:
    reader = letterhead_reader()
    if reader is None:
        return
    canvas.saveState()
    canvas.drawImage(reader, 0, 0, width=A4[0], height=A4[1], preserveAspectRatio=False)
    canvas.restoreState()


def build_invoice_pdf(invoice: Invoice, settings: Settings) -> BytesIO:
    buffer = BytesIO()
    page_width, page_height = A4
    content_width = page_width - 32 * mm
    # Keep text in the empty cream area: below the logo and above the bank details.
    frame = Frame(
        16 * mm,
        page_height * 0.45,
        content_width,
        page_height * 0.39,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
        id="invoice-body",
        showBoundary=0,
    )
    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        title=invoice.invoice_number,
    )
    doc.addPageTemplates([PageTemplate(id="letterhead", frames=[frame], onPage=draw_invoice_background)])
    styles = getSampleStyleSheet()
    left = ParagraphStyle("Left", parent=styles["Normal"], alignment=TA_LEFT, fontSize=9, leading=12, textColor=colors.HexColor("#1a1a1a"))
    right = ParagraphStyle("Right", parent=styles["Normal"], alignment=TA_RIGHT, fontSize=9, leading=12, textColor=colors.HexColor("#1a1a1a"))
    bold = ParagraphStyle(
        "Bold",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        textColor=colors.HexColor("#0f172a"),
    )

    client = invoice.client
    # Helvetica does not contain the ₹ glyph and renders it as a black box.
    symbol = "Rs. "
    story = []

    story.append(Paragraph("MONTHLY INVOICE", bold))
    story.append(Spacer(1, 6))

    header_data = [
        [
            Paragraph(
                f"<b>Bill To</b><br/>{client.name}<br/>"
                f"{(client.address or '').replace(chr(10), '<br/>')}<br/>"
                f"{client.phone}",
                left,
            ),
            Paragraph(
                f"<b>Invoice #:</b> {invoice.invoice_number}<br/>"
                f"<b>Issue Date:</b> {invoice.issue_date.strftime('%d %b %Y')}<br/>"
                f"<b>Billing Period:</b> {month_label(invoice.billing_month)}<br/>"
                f"<b>Status:</b> {'Paid' if money(invoice.balance_due) <= 0 else 'Balance Due'}",
                right,
            ),
        ]
    ]
    header_table = Table(header_data, colWidths=[content_width * 0.5, content_width * 0.5])
    header_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#1a1a1a")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    story.append(header_table)
    story.append(Spacer(1, 10))

    ledger = [row for row in build_ledger(client) if row["date"] <= invoice.issue_date]
    rows = [["Date", "Particulars", "Added", "Received", "Balance"]]
    for row in ledger:
        rows.append(
            [
                row["date"].strftime("%d %b %Y"),
                Paragraph(row["particulars"], left),
                fmt_money(row["added"], symbol) if row["added"] else "—",
                fmt_money(row["deducted"], symbol) if row["deducted"] else "—",
                fmt_money(row["balance"], symbol),
            ]
        )
    rows.append(["", "Balance due", "", "", fmt_money(invoice.balance_due, symbol)])
    table = Table(
        rows,
        colWidths=[
            content_width * 0.14,
            content_width * 0.36,
            content_width * 0.16,
            content_width * 0.16,
            content_width * 0.18,
        ],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E21873")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#1a1a1a")),
                ("BACKGROUND", (0, 1), (-1, -2), colors.white),
                ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#e7c3d2")),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#FDE7F1")),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 8))

    if invoice.notes:
        story.append(Paragraph(f"<b>Notes:</b> {invoice.notes}", left))
        story.append(Spacer(1, 6))

    story.append(
        Paragraph(
            f"Generated on {datetime.now().strftime('%d %b %Y %H:%M')}",
            ParagraphStyle("Foot", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#334155"), alignment=TA_CENTER),
        )
    )

    doc.build(story)
    buffer.seek(0)
    return buffer


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.context_processor
def inject_globals():
    settings = get_settings()
    return {
        "settings": settings,
        "fmt_money": lambda v: fmt_money(v, settings.currency_symbol),
        "month_label": month_label,
        "today": date.today().isoformat(),
        "current_month": current_billing_month(),
    }


@app.route("/")
def dashboard():
    selected_month = request.args.get("month") or current_billing_month()
    if selected_month < START_MONTH:
        selected_month = START_MONTH
    clients = Client.query.order_by(Client.name).all()

    rows = []
    for client in clients:
        received = (
            db.session.query(func.coalesce(func.sum(Payment.amount), 0))
            .filter(
                Payment.client_id == client.id,
                func.strftime("%Y-%m", Payment.payment_date) == selected_month,
            )
            .scalar()
        )
        rows.append(
            {
                "client": client,
                "package": client.package_for_month(selected_month),
                "received": money(received),
                "pending": client.pending,
            }
        )

    return render_template(
        "dashboard.html",
        rows=rows,
        selected_month=selected_month,
        months=billing_months(),
        month_package=standard_package(selected_month),
        month_received=sum((row["received"] for row in rows), Decimal("0")),
        total_pending=sum((row["pending"] for row in rows), Decimal("0")),
    )


def report_data(from_date: date, to_date: date) -> dict:
    if from_date == to_date:
        totals = dict(
            db.session.query(
                func.strftime("%Y-%m-%d", Payment.payment_date),
                func.coalesce(func.sum(Payment.amount), 0),
            )
            .filter(Payment.payment_date == from_date)
            .group_by(func.strftime("%Y-%m-%d", Payment.payment_date))
            .all()
        )
        chart = [{"label": from_date.strftime("%d %b %Y"), "received": float(money(totals.get(from_date.isoformat(), 0)))}]
    else:
        totals = dict(
            db.session.query(
                func.strftime("%Y-%m", Payment.payment_date),
                func.coalesce(func.sum(Payment.amount), 0),
            )
            .filter(Payment.payment_date >= from_date, Payment.payment_date <= to_date)
            .group_by(func.strftime("%Y-%m", Payment.payment_date))
            .all()
        )
        chart = [
            {"label": month_label(billing_month), "received": float(money(totals.get(billing_month, 0)))}
            for billing_month in months_between(from_date.strftime("%Y-%m"), to_date.strftime("%Y-%m"))
        ]

    received = money(
        db.session.query(func.coalesce(func.sum(Payment.amount), 0))
        .filter(Payment.payment_date >= from_date, Payment.payment_date <= to_date)
        .scalar()
    )
    clients = Client.query.order_by(Client.name).all()
    months = months_between(from_date.strftime("%Y-%m"), to_date.strftime("%Y-%m"))
    package_total = sum((standard_package(billing_month) for billing_month in months), Decimal("0")) * len(clients)
    pending_rows = []
    previous_rows = []
    for client in clients:
        if client.pending > 0:
            pending_rows.append({"client": client, "amount": client.pending})
        if money(client.opening_balance) > 0:
            previous_rows.append({"client": client, "amount": money(client.opening_balance)})
    return {
        "chart": chart,
        "received": received,
        "package_total": package_total,
        "pending_rows": pending_rows,
        "previous_rows": previous_rows,
        "pending_total": sum((row["amount"] for row in pending_rows), Decimal("0")),
        "previous_total": sum((row["amount"] for row in previous_rows), Decimal("0")),
    }


def build_report_pdf(data: dict, settings: Settings, from_date: date, to_date: date) -> BytesIO:
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("ReportTitle", parent=styles["Heading1"], alignment=TA_CENTER, textColor=colors.HexColor("#E21873"), fontSize=18)
    normal = ParagraphStyle("ReportNormal", parent=styles["Normal"], fontSize=10, leading=14)
    story = [
        Paragraph(settings.business_name or "Report", title),
        Spacer(1, 6),
        Paragraph(f"Client balances<br/>{from_date.strftime('%d %b %Y')} to {to_date.strftime('%d %b %Y')}", ParagraphStyle("Sub", parent=normal, alignment=TA_CENTER, textColor=colors.HexColor("#8a726c"))),
        Spacer(1, 14),
    ]
    symbol = settings.currency_symbol or "₹"
    summary = [
        ["Current package total", fmt_money(data["package_total"], symbol)],
        ["Received", fmt_money(data["received"], symbol)],
        ["Pending to receive", fmt_money(data["pending_total"], symbol)],
        ["Previous balance with me", fmt_money(data["previous_total"], symbol)],
    ]
    summary_table = Table(summary, colWidths=[4.2 * inch, 2.2 * inch])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#15803d")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#FDEAEA")),
        ("BACKGROUND", (0, 3), (-1, 3), colors.HexColor("#E8F6EC")),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#f0d5e0")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.extend([summary_table, Spacer(1, 16), Paragraph("Pending amount to receive", styles["Heading3"])])
    pending = [["Client", "Phone", "Pending"]] + [
        [row["client"].name, row["client"].phone or "", fmt_money(row["amount"], symbol)] for row in data["pending_rows"]
    ]
    if len(pending) == 1:
        pending.append(["No client has a pending amount", "", ""])
    story.append(_report_client_table(pending))
    story.extend([Spacer(1, 14), Paragraph("Previous balance with me", styles["Heading3"])])
    previous = [["Client", "Phone", "Previous balance"]] + [
        [row["client"].name, row["client"].phone or "", fmt_money(row["amount"], symbol)] for row in data["previous_rows"]
    ]
    if len(previous) == 1:
        previous.append(["No client has a previous balance", "", ""])
    story.append(_report_client_table(previous))
    doc.build(story)
    buffer.seek(0)
    return buffer


def _report_client_table(rows: list) -> Table:
    table = Table(rows, colWidths=[3.1 * inch, 1.6 * inch, 1.7 * inch])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E21873")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (2, 1), (2, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#f0d5e0")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


@app.route("/reports")
def reports():
    year_start, year_end = accounting_year_bounds()
    from_date = parse_date(request.args.get("from"), year_start)
    to_date = parse_date(request.args.get("to"), year_end)
    if from_date > to_date:
        from_date, to_date = to_date, from_date
    today = date.today()
    data = report_data(from_date, to_date)
    return render_template(
        "reports.html",
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
        today=today.isoformat(),
        month_start=today.replace(day=1).isoformat(),
        year_start=year_start.isoformat(),
        year_end=year_end.isoformat(),
        **data,
    )


@app.route("/reports/pdf")
def reports_pdf():
    year_start, year_end = accounting_year_bounds()
    from_date = parse_date(request.args.get("from"), year_start)
    to_date = parse_date(request.args.get("to"), year_end)
    if from_date > to_date:
        from_date, to_date = to_date, from_date
    pdf = build_report_pdf(report_data(from_date, to_date), get_settings(), from_date, to_date)
    return send_file(
        pdf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"report-{from_date.isoformat()}-to-{to_date.isoformat()}.pdf",
    )


@app.route("/clients")
def clients_list():
    clients = Client.query.order_by(Client.name).all()
    return render_template("clients.html", clients=clients)


@app.route("/clients/new", methods=["GET", "POST"])
def client_new():
    if request.method == "POST":
        client = Client(
            name=request.form["name"].strip(),
            email=request.form.get("email", "").strip(),
            phone=request.form.get("phone", "").strip(),
            address=request.form.get("address", "").strip(),
            monthly_fee=money(request.form.get("monthly_fee") or 0),
            opening_balance=money(request.form.get("opening_balance") or 0),
            opening_date=parse_date(request.form.get("opening_date")),
            notes=request.form.get("notes", "").strip(),
            is_active=True,
        )
        if not client.name:
            flash("Client name is required.", "danger")
            return render_template("client_form.html", client=client, title="Add Client")
        db.session.add(client)
        db.session.commit()
        flash(f"Client '{client.name}' added.", "success")
        return redirect(url_for("client_detail", client_id=client.id))
    return render_template("client_form.html", client=None, title="Add Client")


@app.route("/clients/<int:client_id>")
def client_detail(client_id):
    client = Client.query.get_or_404(client_id)
    return render_template(
        "client_detail.html",
        client=client,
        ledger=build_ledger(client),
    )


@app.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
def client_edit(client_id):
    client = Client.query.get_or_404(client_id)
    if request.method == "POST":
        client.name = request.form["name"].strip()
        client.email = request.form.get("email", "").strip()
        client.phone = request.form.get("phone", "").strip()
        client.address = request.form.get("address", "").strip()
        client.monthly_fee = money(request.form.get("monthly_fee") or 0)
        client.opening_balance = money(request.form.get("opening_balance") or 0)
        client.opening_date = parse_date(request.form.get("opening_date"), client.opening_date)
        client.notes = request.form.get("notes", "").strip()
        client.is_active = request.form.get("is_active") == "on"
        if not client.name:
            flash("Client name is required.", "danger")
            return render_template("client_form.html", client=client, title="Edit Client")
        db.session.commit()
        flash("Client updated.", "success")
        return redirect(url_for("client_detail", client_id=client.id))
    return render_template("client_form.html", client=client, title="Edit Client")


@app.route("/clients/<int:client_id>/delete", methods=["POST"])
def client_delete(client_id):
    client = Client.query.get_or_404(client_id)
    name = client.name
    db.session.delete(client)
    db.session.commit()
    flash(f"Client '{name}' and related records deleted.", "warning")
    return redirect(url_for("clients_list"))


@app.route("/payments", methods=["GET", "POST"])
def payments():
    if request.method == "POST":
        client_id = int(request.form["client_id"])
        amount = money(request.form.get("amount") or 0)
        if amount <= 0:
            flash("Payment amount must be greater than zero.", "danger")
            return redirect(url_for("payments"))
        payment = Payment(
            client_id=client_id,
            amount=amount,
            payment_date=parse_date(request.form.get("payment_date")),
            method="",
            place=request.form.get("place", "").strip(),
            received_from=request.form.get("received_from", "").strip(),
            notes=request.form.get("notes", "").strip(),
        )
        db.session.add(payment)
        db.session.commit()
        flash("Payment recorded. Balance updated.", "success")
        return redirect(request.referrer or url_for("payments"))

    payments_list = Payment.query.order_by(Payment.payment_date.desc(), Payment.id.desc()).all()
    clients = Client.query.filter_by(is_active=True).order_by(Client.name).all()
    return render_template("payments.html", payments=payments_list, clients=clients)


@app.route("/payments/<int:payment_id>/edit", methods=["GET", "POST"])
def payment_edit(payment_id):
    payment = Payment.query.get_or_404(payment_id)
    if request.method == "POST":
        amount = money(request.form.get("amount") or 0)
        if amount <= 0:
            flash("Payment amount must be greater than zero.", "danger")
            return redirect(url_for("payment_edit", payment_id=payment.id))
        payment.amount = amount
        payment.payment_date = parse_date(request.form.get("payment_date"), payment.payment_date)
        payment.place = request.form.get("place", "").strip()
        payment.received_from = request.form.get("received_from", "").strip()
        payment.notes = request.form.get("notes", "").strip()
        db.session.commit()
        flash("Payment updated. Balance recalculated.", "success")
        return redirect(url_for("client_detail", client_id=payment.client_id))
    clients = Client.query.filter_by(is_active=True).order_by(Client.name).all()
    return render_template("payment_form.html", payment=payment, clients=clients)


@app.route("/payments/<int:payment_id>/delete", methods=["POST"])
def payment_delete(payment_id):
    payment = Payment.query.get_or_404(payment_id)
    db.session.delete(payment)
    db.session.commit()
    flash("Payment removed. Balance recalculated.", "warning")
    return redirect(request.referrer or url_for("payments"))


@app.route("/clients/<int:client_id>/months", methods=["POST"])
def save_month(client_id):
    client = Client.query.get_or_404(client_id)
    billing_month = request.form.get("billing_month")
    if not billing_month:
        flash("Choose a month first.", "danger")
        return redirect(url_for("client_detail", client_id=client.id))
    amount = money(request.form.get("amount") or 0)
    if amount <= 0:
        flash("Enter a package amount greater than zero.", "danger")
        return redirect(url_for("client_detail", client_id=client.id))
    package = MonthlyPackage.query.filter_by(
        client_id=client.id, billing_month=billing_month
    ).first()
    if not package:
        package = MonthlyPackage(client_id=client.id, billing_month=billing_month)
        db.session.add(package)
    package.amount = amount
    year, month = (int(part) for part in billing_month.split("-"))
    upsert_package_charge(client, billing_month, amount, date(year, month, 1))
    db.session.commit()
    flash(f"{month_label(billing_month)} saved. Balance updated.", "success")
    return redirect(url_for("client_detail", client_id=client.id))


@app.route("/charges/<int:charge_id>/edit", methods=["GET", "POST"])
def charge_edit(charge_id):
    charge = Charge.query.get_or_404(charge_id)
    if request.method == "POST":
        amount = money(request.form.get("amount") or 0)
        if amount <= 0:
            flash("Amount must be greater than zero.", "danger")
            return redirect(url_for("charge_edit", charge_id=charge.id))
        charge.amount = amount
        charge.charge_date = parse_date(request.form.get("charge_date"), charge.charge_date)
        charge.billing_month = request.form.get("billing_month") or charge.billing_month
        charge.description = request.form.get("description", "").strip() or charge.description
        if charge.description.startswith("Monthly package"):
            package = MonthlyPackage.query.filter_by(
                client_id=charge.client_id, billing_month=charge.billing_month
            ).first()
            if not package:
                package = MonthlyPackage(client_id=charge.client_id, billing_month=charge.billing_month)
                db.session.add(package)
            package.amount = amount
        db.session.commit()
        flash("Entry updated. Balance recalculated.", "success")
        return redirect(url_for("client_detail", client_id=charge.client_id))
    return render_template("charge_form.html", charge=charge)


@app.route("/charges/<int:charge_id>/delete", methods=["POST"])
def charge_delete(charge_id):
    charge = Charge.query.get_or_404(charge_id)
    client_id = charge.client_id
    db.session.delete(charge)
    db.session.commit()
    flash("Charge removed. Balance recalculated.", "warning")
    return redirect(url_for("client_detail", client_id=client_id))


@app.route("/invoices", methods=["GET", "POST"])
def invoices():
    if request.method == "POST":
        billing_month = request.form.get("billing_month") or date.today().strftime("%Y-%m")
        client_ids = request.form.getlist("client_ids")
        if not client_ids:
            flash("Select at least one client.", "danger")
            return redirect(url_for("invoices"))

        created = 0
        skipped = 0
        for cid in client_ids:
            client = Client.query.get(int(cid))
            if not client:
                continue
            existing = Invoice.query.filter_by(
                client_id=client.id, billing_month=billing_month
            ).first()
            if existing:
                skipped += 1
                continue

            # Snapshot previous balance before adding this month's fee
            previous_balance = client.pending
            ensure_monthly_charge(client, billing_month)
            db.session.flush()

            invoice = Invoice(
                client_id=client.id,
                invoice_number=next_invoice_number(billing_month),
                billing_month=billing_month,
                issue_date=date.today(),
                monthly_fee=client.package_for_month(billing_month),
                previous_balance=previous_balance,
                total_charged=client.total_charged,
                total_paid=client.total_paid,
                balance_due=client.pending,
                notes=request.form.get("notes", "").strip(),
            )
            db.session.add(invoice)
            created += 1

        db.session.commit()
        msg = f"Created {created} invoice(s) for {month_label(billing_month)}."
        if skipped:
            msg += f" Skipped {skipped} (already invoiced)."
        flash(msg, "success")
        return redirect(url_for("invoices"))

    invoices_list = Invoice.query.order_by(Invoice.issue_date.desc(), Invoice.id.desc()).all()
    clients = Client.query.filter_by(is_active=True).order_by(Client.name).all()
    return render_template("invoices.html", invoices=invoices_list, clients=clients)


@app.route("/invoices/<int:invoice_id>")
def invoice_detail(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    ledger = [row for row in build_ledger(invoice.client) if row["date"] <= invoice.issue_date]
    return render_template("invoice_detail.html", invoice=invoice, ledger=ledger)


@app.route("/invoices/<int:invoice_id>/pdf")
def invoice_pdf(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    settings = get_settings()
    pdf = build_invoice_pdf(invoice, settings)
    filename = f"{invoice.invoice_number}.pdf"
    return send_file(
        pdf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/invoices/<int:invoice_id>/delete", methods=["POST"])
def invoice_delete(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    db.session.delete(invoice)
    db.session.commit()
    flash("Invoice deleted.", "warning")
    return redirect(url_for("invoices"))


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    settings = get_settings()
    if request.method == "POST":
        settings.business_name = request.form.get("business_name", "").strip() or "My Business"
        settings.business_email = request.form.get("business_email", "").strip()
        settings.business_phone = request.form.get("business_phone", "").strip()
        settings.business_address = request.form.get("business_address", "").strip()
        settings.currency_symbol = request.form.get("currency_symbol", "₹").strip() or "₹"
        db.session.commit()
        flash("Business settings saved.", "success")
        return redirect(url_for("settings_page"))
    return render_template("settings.html", biz=settings)


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

def upgrade_schema():
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    if "payments" in tables:
        columns = {column["name"] for column in inspector.get_columns("payments")}
        if "place" not in columns:
            db.session.execute(text("ALTER TABLE payments ADD COLUMN place VARCHAR(120) DEFAULT ''"))
        if "received_from" not in columns:
            db.session.execute(text("ALTER TABLE payments ADD COLUMN received_from VARCHAR(120) DEFAULT ''"))
    if "clients" in tables:
        columns = {column["name"] for column in inspector.get_columns("clients")}
        if "opening_date" not in columns:
            db.session.execute(text("ALTER TABLE clients ADD COLUMN opening_date DATE"))
    db.session.commit()


with app.app_context():
    db.create_all()
    upgrade_schema()
    get_settings()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
