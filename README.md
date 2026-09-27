# Client Billing Manager

Web app to manage ~10 clients, track monthly fees / payments / pending balances, and generate monthly PDF invoices.

## Features

- Dashboard with total charged, paid, and pending amounts
- Add / edit / remove clients
- Monthly fee + opening balance per client
- Record payments (UPI, bank, cash, etc.)
- Add extra charges when needed
- Auto-calculate pending balance: **charged − paid**
- Generate monthly invoices for selected clients
- Download PDF invoice with balance due
- Business details on invoices (Settings page)

## Project location

`C:\Users\shrutia\Downloads\ClientBilling`

This folder is separate from Validation_GUI.

## Run locally

```powershell
cd C:\Users\shrutia\Downloads\ClientBilling
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open: [http://127.0.0.1:5000](http://127.0.0.1:5000)

## Typical workflow

1. Open **Settings** and enter your business name / contact (shown on PDFs).
2. Add clients with their **monthly fee** (and any past dues as opening balance).
3. Each month, open **Invoices**, pick the month, select clients, generate invoices.
4. Download PDF and send to the client.
5. When money comes in, record it under **Payments** — pending balance updates automatically.

## Host online later

This app is ready to host on services such as:

- Render
- Railway
- PythonAnywhere
- Fly.io

Use `gunicorn app:app` as the start command. For production, set a strong `SECRET_KEY` environment variable. SQLite works for a small number of clients; you can switch to PostgreSQL later if needed.

## Tech

- Python + Flask
- SQLite database (`billing.db`)
- ReportLab for PDF invoices
- Bootstrap 5 UI
