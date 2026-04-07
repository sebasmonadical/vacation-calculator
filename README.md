# Vacation & Final Payment Calculator

A deterministic, rule-based payroll calculator that reads a vacation report PDF and computes an employee's final payment — including worked days, pay in lieu of notice, and vacation payout.

**No AI is used for calculations.** All logic is explicit, traceable, and auditable.

---

## What it does

1. Upload a vacation report PDF → fields are extracted automatically
2. Enter the remaining payroll values (salary, worked days, etc.)
3. Click **Calculate** → get a full breakdown with a formula audit trail

**Fields extracted from PDF:**
- Employee name · Start date · End date · Renewal date
- Allocated vacation days · Roll over · Days used · Days remaining

**Fields entered manually:**
- Monthly salary (CAD)
- Last day of work
- Worked days in final period
- Working days in the month
- Pay in lieu of notice (yes/no)

---

## Requirements

- Python 3.9 or higher
- pip

---

## Quick start

### 1 — Clone the repository

```bash
git clone https://github.com/sebasmonadical/vacation-calculator.git
cd vacation-calculator
```

### 2 — Install dependencies

```bash
pip3 install -r requirements.txt
```

### 3 — Start the server

```bash
python3 server.py
```

You should see:

```
============================================================
  Vacation Calculator — PDF Extraction Server
  http://localhost:5050
  Press Ctrl+C to stop
============================================================
```

### 4 — Open the app

Open `index.html` directly in your browser (double-click the file, or drag it into a browser window).

> Keep the terminal running while you use the app. The browser talks to the local server for PDF extraction.

---

## How to use

1. **Upload PDF** — drag and drop or click the upload area
2. **Review extracted data** — check all auto-filled fields; use **Override** to correct anything
3. **Enter manual inputs** — salary, worked days, working days in month, pay in lieu
4. **Click Calculate** — the results table shows every component with its source (PDF / Manual / Derived) and a full formula audit trail
5. **Print / Save** — use the Print button to save as PDF

---

## Project structure

```
vacation-calculator/
├── index.html        # Frontend — all UI and calculation engine
├── server.py         # Backend — Python/Flask PDF extraction server (pdfplumber)
├── requirements.txt  # Python dependencies
└── README.md
```

### How it works

| Layer | File | Responsibility |
|-------|------|---------------|
| Frontend | `index.html` | UI, form handling, deterministic calculation engine, results rendering |
| Backend | `server.py` | PDF text + table extraction using pdfplumber; returns structured JSON |

The frontend tries the local backend first (`http://localhost:5050`). If the server is not running, it falls back to browser-side PDF.js extraction automatically.

---

## Calculation formulas

```
daily_rate        = salary / working_days_in_month
worked_days_value = worked_days × daily_rate
pay_in_lieu_value = 10 × daily_rate   (if applicable, else 0)
vacation_payout   = vacation_days_remaining × daily_rate
final_payment     = worked_days_value + pay_in_lieu_value + vacation_payout
```

### How to modify a formula

| What to change | Where |
|---|---|
| Daily rate basis (e.g. annual ÷ 260) | `calculateDailyRate()` in `index.html` |
| Notice period (default: 10 days) | `NOTICE_WORKING_DAYS` constant in `index.html` |
| Vacation payout method | `calculateVacationPayout()` in `index.html` |
| Add a new payment component | `calculateFinalPayment()` in `index.html` |
| PDF field extraction patterns | `extract_from_text()` / `extract_employee_row()` in `server.py` |

---

## Troubleshooting

**Fields not extracted from my PDF**
- Click "Show extracted raw text" after uploading — this shows exactly what was read
- Use the **Override** button on any field to enter the value manually
- If no text is extracted at all, the PDF is likely scanned (image-based); enter all values manually

**Server not starting**
```bash
# Check if port 5050 is already in use
lsof -i :5050
# Kill whatever is using it, then retry
```

**Dependencies won't install**
```bash
# Make sure you have Python 3.9+
python3 --version

# Try upgrading pip first
pip3 install --upgrade pip
pip3 install -r requirements.txt
```

---

## Disclaimer

This calculator provides an estimate only. Final payments should be reviewed according to applicable employment laws and company policy.
