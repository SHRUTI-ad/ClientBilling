"""
Client Billing Manager
Track clients, payments, balances, and generate monthly PDF invoices.
"""

from __future__ import annotations

import os
from datetime import date, datetime
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
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import func

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
        return Decimal(str(self.opening_balance or 0)) + Decimal(str(total))

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
        return self.total_charged - self.total_paid


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
    method = db.Column(db.String(50), default="Bank Transfer")
    notes = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


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


def ensure_monthly_charge(client: Client, billing_month: str) -> Charge | None:
    """Add monthly fee charge for the month if not already present."""
    existing = Charge.query.filter_by(
        client_id=client.id, billing_month=billing_month
    ).first()
    if existing:
        return existing
    if money(client.monthly_fee) <= 0:
        return None
    charge = Charge(
        client_id=client.id,
        amount=money(client.monthly_fee),
        charge_date=date.today(),
        billing_month=billing_month,
        description=f"Monthly fee — {month_label(billing_month)}",
    )
    db.session.add(charge)
    db.session.flush()
    return charge


def build_invoice_pdf(invoice: Invoice, settings: Settings) -> BytesIO:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleCenter",
        parent=styles["Heading1"],
        alignment=TA_CENTER,
        fontSize=20,
        textColor=colors.HexColor("#0f766e"),
        spaceAfter=6,
    )
    subtitle = ParagraphStyle(
        "Sub",
        parent=styles["Normal"],
        alignment=TA_CENTER,
        fontSize=11,
        textColor=colors.HexColor("#475569"),
        spaceAfter=16,
    )
    left = ParagraphStyle("Left", parent=styles["Normal"], alignment=TA_LEFT, fontSize=10, leading=14)
    right = ParagraphStyle("Right", parent=styles["Normal"], alignment=TA_RIGHT, fontSize=10, leading=14)
    bold = ParagraphStyle(
        "Bold",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        textColor=colors.HexColor("#0f172a"),
    )

    client = invoice.client
    symbol = settings.currency_symbol or "₹"
    story = []

    story.append(Paragraph(settings.business_name or "Invoice", title_style))
    biz_lines = []
    if settings.business_address:
        biz_lines.append(settings.business_address.replace("\n", "<br/>"))
    contact_bits = [x for x in [settings.business_email, settings.business_phone] if x]
    if contact_bits:
        biz_lines.append(" | ".join(contact_bits))
    if biz_lines:
        story.append(Paragraph("<br/>".join(biz_lines), subtitle))
    else:
        story.append(Spacer(1, 8))

    story.append(Paragraph("MONTHLY INVOICE", bold))
    story.append(Spacer(1, 8))

    header_data = [
        [
            Paragraph(
                f"<b>Bill To</b><br/>{client.name}<br/>"
                f"{(client.address or '').replace(chr(10), '<br/>')}<br/>"
                f"{client.email}<br/>{client.phone}",
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
    header_table = Table(header_data, colWidths=[3.4 * inch, 3.4 * inch])
    header_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    story.append(header_table)
    story.append(Spacer(1, 18))

    rows = [
        ["Description", "Amount"],
        ["Previous outstanding balance", fmt_money(invoice.previous_balance, symbol)],
        [f"Monthly fee — {month_label(invoice.billing_month)}", fmt_money(invoice.monthly_fee, symbol)],
        ["Total charged to date", fmt_money(invoice.total_charged, symbol)],
        ["Total paid to date", fmt_money(invoice.total_paid, symbol)],
        ["Balance due", fmt_money(invoice.balance_due, symbol)],
    ]
    table = Table(rows, colWidths=[4.6 * inch, 2.2 * inch])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f766e")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#ecfdf5")),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 20))

    if invoice.notes:
        story.append(Paragraph(f"<b>Notes:</b> {invoice.notes}", left))
        story.append(Spacer(1, 10))

    story.append(
        Paragraph(
            "Please settle the pending balance at your earliest convenience. "
            "Thank you for your business.",
            left,
        )
    )
    story.append(Spacer(1, 24))
    story.append(
        Paragraph(
            f"Generated on {datetime.now().strftime('%d %b %Y %H:%M')}",
            ParagraphStyle("Foot", parent=styles["Normal"], fontSize=8, textColor=colors.grey, alignment=TA_CENTER),
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
        "current_month": date.today().strftime("%Y-%m"),
    }


@app.route("/")
def dashboard():
    clients = Client.query.filter_by(is_active=True).order_by(Client.name).all()
    all_clients = Client.query.order_by(Client.name).all()
    total_budget = sum((c.total_charged for c in clients), Decimal("0"))
    total_paid = sum((c.total_paid for c in clients), Decimal("0"))
    total_pending = sum((c.pending for c in clients), Decimal("0"))
    recent_payments = Payment.query.order_by(Payment.payment_date.desc(), Payment.id.desc()).limit(8).all()
    recent_invoices = Invoice.query.order_by(Invoice.issue_date.desc(), Invoice.id.desc()).limit(8).all()
    return render_template(
        "dashboard.html",
        clients=clients,
        all_clients=all_clients,
        total_budget=total_budget,
        total_paid=total_paid,
        total_pending=total_pending,
        recent_payments=recent_payments,
        recent_invoices=recent_invoices,
    )


@app.route("/clients")
def clients_list():
    show = request.args.get("show", "active")
    q = Client.query.order_by(Client.name)
    if show == "active":
        q = q.filter_by(is_active=True)
    elif show == "inactive":
        q = q.filter_by(is_active=False)
    clients = q.all()
    return render_template("clients.html", clients=clients, show=show)


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
    return render_template("client_detail.html", client=client)


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
            payment_date=datetime.strptime(request.form["payment_date"], "%Y-%m-%d").date(),
            method=request.form.get("method", "Bank Transfer").strip(),
            notes=request.form.get("notes", "").strip(),
        )
        db.session.add(payment)
        db.session.commit()
        flash("Payment recorded. Balance updated.", "success")
        return redirect(url_for("payments"))

    payments_list = Payment.query.order_by(Payment.payment_date.desc(), Payment.id.desc()).all()
    clients = Client.query.filter_by(is_active=True).order_by(Client.name).all()
    return render_template("payments.html", payments=payments_list, clients=clients)


@app.route("/payments/<int:payment_id>/delete", methods=["POST"])
def payment_delete(payment_id):
    payment = Payment.query.get_or_404(payment_id)
    db.session.delete(payment)
    db.session.commit()
    flash("Payment removed. Balance recalculated.", "warning")
    return redirect(request.referrer or url_for("payments"))


@app.route("/charges", methods=["POST"])
def charge_add():
    client_id = int(request.form["client_id"])
    amount = money(request.form.get("amount") or 0)
    billing_month = request.form.get("billing_month") or date.today().strftime("%Y-%m")
    if amount <= 0:
        flash("Charge amount must be greater than zero.", "danger")
        return redirect(url_for("client_detail", client_id=client_id))
    charge = Charge(
        client_id=client_id,
        amount=amount,
        charge_date=datetime.strptime(request.form.get("charge_date") or date.today().isoformat(), "%Y-%m-%d").date(),
        billing_month=billing_month,
        description=request.form.get("description", "Extra charge").strip() or "Extra charge",
    )
    db.session.add(charge)
    db.session.commit()
    flash("Charge added. Balance updated.", "success")
    return redirect(url_for("client_detail", client_id=client_id))


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
                monthly_fee=money(client.monthly_fee),
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
    return render_template("invoice_detail.html", invoice=invoice)


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

with app.app_context():
    db.create_all()
    get_settings()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
