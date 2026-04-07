"""
vacation_calculator/server.py
─────────────────────────────
Python / Flask backend for the Vacation & Final Payment Calculator.

Uses pdfplumber for PDF text extraction — far more reliable than
browser-side PDF.js because pdfplumber:
  • Preserves table structure (label + value in adjacent cells)
  • Gives word-level bounding-box data for positional matching
  • Handles multi-column layouts correctly

All field extraction is deterministic rule-based logic.
No AI / LLM is used at any point.

Run:
    python3 server.py
    → http://localhost:5050

API:
    POST /api/parse-pdf
        multipart body: pdf=<file>
    → { employee_name, start_date, end_date,
        vacation_days_accrued, roll_over_days,
        vacation_days_used, vacation_days_remaining,
        renewal_date, raw_text, raw_tables, debug }
"""

import re
import io
import os
import logging
from datetime import datetime

import pdfplumber
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)
logging.basicConfig(level=logging.INFO)

# ─────────────────────────────────────────────────────────────────────────────
# DATE PARSING
# ─────────────────────────────────────────────────────────────────────────────

MONTH_NAMES = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?"
    r"|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
)

DATE_PATTERNS = [
    # ISO: 2024-01-15
    re.compile(r"\b(\d{4}[-/]\d{1,2}[-/]\d{1,2})\b"),
    # Month DD, YYYY  or  Month DD YYYY
    re.compile(rf"\b({MONTH_NAMES})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.IGNORECASE),
    # DD Month YYYY
    re.compile(rf"\b(\d{{1,2}})\s+({MONTH_NAMES})\s+(\d{{4}})\b", re.IGNORECASE),
    # MM/DD/YYYY
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"),
    # DD-MM-YYYY
    re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b"),
]


def parse_date(s: str) -> str | None:
    """Convert any common date string to ISO YYYY-MM-DD, or return None."""
    if not s:
        return None
    s = s.strip()

    # Already ISO
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    if re.match(r"^\d{4}/\d{2}/\d{2}$", s):
        return s.replace("/", "-")

    # Try Python's datetime parser for natural language dates
    for fmt in ["%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
                "%m/%d/%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"]:
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Last resort: let dateutil try
    try:
        from dateutil import parser as dp
        return dp.parse(s, dayfirst=False).strftime("%Y-%m-%d")
    except Exception:
        pass

    return None


def find_first_date(text: str) -> str | None:
    """Return the first parseable date found anywhere in `text`."""
    # Quick ISO scan
    m = re.search(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b", text)
    if m:
        return parse_date(m.group())

    # Month-name scan
    m = re.search(
        rf"(?:{MONTH_NAMES})\s+\d{{1,2}},?\s+\d{{4}}", text, re.IGNORECASE
    )
    if m:
        return parse_date(m.group())

    # Slash-style scan
    m = re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b", text)
    if m:
        return parse_date(m.group())

    return None


# ─────────────────────────────────────────────────────────────────────────────
# NUMBER PARSING
# ─────────────────────────────────────────────────────────────────────────────

def find_first_number(text: str) -> float | None:
    """Return the first integer or decimal found in `text`."""
    m = re.search(r"\b(\d+(?:\.\d+)?)\b", text.strip())
    return float(m.group(1)) if m else None


# ─────────────────────────────────────────────────────────────────────────────
# LABEL MATCHING HELPERS
# ─────────────────────────────────────────────────────────────────────────────

# Words that must NOT appear in an extracted employee name
NAME_BLOCKLIST = re.compile(
    r"\b(?:current|previous|next|year|period|quarter|month|annual|monthly|"
    r"weekly|report|vacation|summary|statement|payroll|company|inc|ltd|"
    r"corp|page|date|total|balance|remaining|used|allocated|entitlement|"
    r"accrued|rollover|roll|over|renewal|anniversary|department|division|"
    r"manager|supervisor|hr|human|resources)\b",
    re.IGNORECASE,
)


def is_valid_name(s: str) -> bool:
    if not s:
        return False
    s = s.strip()
    if re.search(r"\d", s):                    # no digits
        return False
    words = s.split()
    if len(words) < 2:                         # need at least 2 words
        return False
    if NAME_BLOCKLIST.search(s):               # no report/period words
        return False
    # Each word should start with a letter (not all-caps abbreviation)
    if not all(re.match(r"[A-Za-z]", w) for w in words):
        return False
    return True


def label_matches(cell_text: str, labels: list[str]) -> bool:
    """True if cell_text matches any of the label strings (case-insensitive)."""
    ct = cell_text.strip().lower()
    for lbl in labels:
        if re.search(lbl, ct, re.IGNORECASE):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# TABLE-BASED EXTRACTION
# Scans every table extracted by pdfplumber looking for label→value pairs.
# ─────────────────────────────────────────────────────────────────────────────

FIELD_LABELS = {
    "employee_name": [
        r"employee\s*name", r"^employee$", r"^name$", r"full\s*name",
    ],
    "start_date": [
        r"start\s*date", r"hire\s*date", r"date\s*of\s*hire",
        r"employment\s*(?:start|date|commencement)", r"commencement\s*date",
        r"joining\s*date", r"seniority\s*date",
    ],
    "end_date": [
        r"end\s*date", r"termination\s*date",
        r"last\s*(?:day|date)(?:\s*(?:of\s*(?:employment|work)|worked))?",
        r"separation\s*date", r"departure\s*date",
    ],
    "vacation_days_accrued": [
        r"allocated", r"accrued", r"entitlement",
        r"vacation\s*days?\s*(?:allocated|accrued|entitlement)",
        r"annual\s*(?:vacation\s*)?entitlement", r"days?\s*allocated",
        r"total\s*(?:vacation\s*)?days?",
    ],
    "roll_over_days": [
        r"roll\s*[-_]?\s*over", r"rollover",
        r"carr(?:y|ied)\s*(?:over|forward)", r"carry\s*forward",
        r"previous\s*balance",
    ],
    "vacation_days_used": [
        r"days?\s*used", r"vacation\s*used", r"used\s*days?",
        r"days?\s*taken", r"^taken$", r"consumed",
    ],
    "vacation_days_remaining": [
        r"days?\s*remaining", r"vacation\s*remaining", r"remaining\s*days?",
        r"current\s*balance", r"outstanding\s*balance",
        r"available\s*days?", r"closing\s*balance", r"^balance$",
    ],
    "renewal_date": [
        r"renewal\s*date", r"anniversary\s*date",
        r"next\s*renewal", r"renew(?:s|al)?",
    ],
}


def extract_from_tables(tables: list[list[list[str | None]]]) -> dict:
    """
    Scan every table cell. When a cell contains a known label, look for
    the corresponding value in:
      • the cell immediately to the right (same row, next column)
      • the cell directly below (next row, same column)
    Returns a dict of field → raw string value (unconverted).
    """
    result = {}

    for table in tables:
        if not table:
            continue
        rows = [[str(c).strip() if c else "" for c in row] for row in table]
        num_rows = len(rows)

        for r_idx, row in enumerate(rows):
            for c_idx, cell in enumerate(row):
                if not cell:
                    continue
                for field, labels in FIELD_LABELS.items():
                    if field in result:
                        continue
                    if not label_matches(cell, labels):
                        continue

                    # Value to the right on the same row
                    for offset in range(1, len(row) - c_idx):
                        candidate = rows[r_idx][c_idx + offset]
                        if candidate:
                            result[field] = candidate
                            break

                    # Value in the row below (same column)
                    if field not in result and r_idx + 1 < num_rows:
                        candidate = rows[r_idx + 1][c_idx] if c_idx < len(rows[r_idx + 1]) else ""
                        if candidate:
                            result[field] = candidate

    return result


# ─────────────────────────────────────────────────────────────────────────────
# TEXT-LINE BASED EXTRACTION
# Fallback when table extraction misses a field.
# ─────────────────────────────────────────────────────────────────────────────

def extract_employee_row(lines: list[str], date_inline_rx: re.Pattern) -> dict:
    """
    Handles the common HR report layout where employee data sits in a
    column-header row followed immediately by a value row, e.g.:

        Employee             Start Date          End Date
        John Doe             May 16, 2025        May 16, 2026

    Strategy:
      1. Find a line that contains "Employee" AND "Start Date" / "End Date"
         (i.e. a header row with multiple column labels).
      2. On the next non-empty data line, find all dates left-to-right.
         • First date  → start_date
         • Second date → end_date
      3. Everything before the first date on the data line → employee name.

    Returns a dict with any of {employee_name, start_date, end_date} found.
    """
    result = {}
    header_rx = re.compile(
        r"employee.*(?:start\s*date|end\s*date|hire\s*date)", re.IGNORECASE
    )

    for i, line in enumerate(lines):
        if not header_rx.search(line):
            continue

        # Look at the next 1-3 lines for the data row
        for j in range(i + 1, min(i + 4, len(lines))):
            data_line = lines[j].strip()
            if not data_line:
                continue

            # Find all dates on the data line (in left-to-right order)
            all_dates = list(date_inline_rx.finditer(data_line))
            if not all_dates:
                continue

            # Employee name: text that precedes the first date
            first_date_start = all_dates[0].start()
            name_candidate = data_line[:first_date_start].strip()
            # Clean trailing punctuation / whitespace
            name_candidate = re.sub(r"[\s,;:|]+$", "", name_candidate).strip()
            if name_candidate and is_valid_name(name_candidate):
                result["employee_name"] = name_candidate

            if len(all_dates) >= 1:
                result["start_date"] = all_dates[0].group()
            if len(all_dates) >= 2:
                result["end_date"] = all_dates[-1].group()   # last date = end date

            break  # found the data row, stop searching

        if result:
            break

    return result


def extract_from_text(full_text: str) -> dict:
    """
    Line-by-line extraction.  Runs in this priority order:

      1. extract_employee_row()  — handles "Employee / Start / End" column header
         pattern (e.g. John Doe  May 16, 2025  May 16, 2026).
         This is the most reliable strategy for standard HR reports.

      2. Same-line scan  ("Label: Value" on one line)
      3. Next-line scan  (label on its own line, value on the following line)
      4. Date-range scan ("from date to date" in notes / summary text)

    All numeric vacation fields use the same same-line / next-line approach.
    """
    lines = [l.strip() for l in full_text.splitlines() if l.strip()]
    result = {}

    DATE_INLINE = (
        rf"(?:{MONTH_NAMES})\s+\d{{1,2}},?\s+\d{{4}}"
        r"|\d{4}[-/]\d{1,2}[-/]\d{1,2}"
        r"|\d{1,2}[-/]\d{1,2}[-/]\d{4}"
    )
    date_rx = re.compile(DATE_INLINE, re.IGNORECASE)
    NUM_INLINE = r"\d+(?:\.\d+)?"

    def scan_same_line(label_rx: str, value_rx: str) -> str | None:
        """Match 'label <optional separator> value' on the same line."""
        # Allow extra words between label and value (for "Allocated vacation days: 33")
        pattern = re.compile(
            rf"(?:{label_rx})(?:\s+\w+){{0,4}}\s*[:\-\t]?\s*({value_rx})",
            re.IGNORECASE,
        )
        m = pattern.search(full_text)
        return m.group(1).strip() if m else None

    def scan_next_line(label_tests: list[str], value_rx: str, lookahead: int = 3) -> str | None:
        """Find a label line, then look for a value on the next few lines."""
        v_pattern = re.compile(rf"^[:\-]?\s*({value_rx})\s*$", re.IGNORECASE)
        for i, line in enumerate(lines):
            for lt in label_tests:
                if re.search(lt, line, re.IGNORECASE):
                    for j in range(i + 1, min(i + 1 + lookahead, len(lines))):
                        m = v_pattern.match(lines[j])
                        if m:
                            return m.group(1).strip()
                        m2 = re.search(rf"({value_rx})", lines[j], re.IGNORECASE)
                        if m2:
                            return m2.group(1).strip()
        return None

    # ── STRATEGY 1: column-header row  (highest priority) ────────────────────
    # Handles: "Employee   Start Date   End Date" / "John Doe   date   date"
    row_data = extract_employee_row(lines, date_rx)
    result.update(row_data)

    # ── STRATEGY 2: employee name — inline or stacked labels ─────────────────
    if "employee_name" not in result:
        name_labels = [
            r"employee\s*name", r"^employee$", r"^name$", r"full\s*name",
            r"prepared\s*for", r"report\s*for",
        ]
        name_value_rx = r"[A-Za-z][A-Za-z '.,-]{2,50}"

        for label in name_labels:
            val = scan_same_line(label, name_value_rx)
            if val and is_valid_name(val):
                result["employee_name"] = val
                break

    if "employee_name" not in result:
        val = scan_next_line(
            [r"employee\s*name", r"^employee$", r"^name$"],
            r"[A-Za-z][A-Za-z '.,-]{2,50}"
        )
        if val and is_valid_name(val):
            result["employee_name"] = val

    # ── STRATEGY 3: start date — inline or stacked ───────────────────────────
    if "start_date" not in result:
        start_labels = [
            r"start\s*date", r"hire\s*date", r"date\s*of\s*hire",
            r"employment\s*(?:start|date|commencement)",
            r"commencement\s*date", r"joining\s*date", r"seniority\s*date",
        ]
        for label in start_labels:
            val = scan_same_line(label, DATE_INLINE)
            if val:
                result["start_date"] = val
                break
        if "start_date" not in result:
            val = scan_next_line(start_labels, DATE_INLINE)
            if val:
                result["start_date"] = val

    # ── STRATEGY 4: end date — inline or stacked ─────────────────────────────
    if "end_date" not in result:
        end_labels = [
            r"end\s*date", r"termination\s*date",
            r"last\s*(?:day|date)(?:\s*(?:of\s*(?:employment|work)|worked))?",
            r"separation\s*date", r"departure\s*date", r"cessation\s*date",
        ]
        for label in end_labels:
            val = scan_same_line(label, DATE_INLINE)
            if val:
                result["end_date"] = val
                break
        if "end_date" not in result:
            val = scan_next_line(end_labels, DATE_INLINE)
            if val:
                result["end_date"] = val

    # ── STRATEGY 5: date-range expression  "from date to date" ───────────────
    # Also fixes the case where start_date == end_date (both landed on same date)
    start_eq_end = (
        result.get("start_date") and
        result.get("start_date") == result.get("end_date")
    )
    if "start_date" not in result or "end_date" not in result or start_eq_end:
        range_rx = re.compile(
            rf"({DATE_INLINE})\s*(?:to|–|—|-)\s*({DATE_INLINE})", re.IGNORECASE
        )
        m = range_rx.search(full_text)
        if m and m.group(1) != m.group(2):    # only use if dates are different
            if "start_date" not in result or start_eq_end:
                result["start_date"] = m.group(1)
            if "end_date" not in result or start_eq_end:
                result["end_date"] = m.group(2)

    # ── Allocated vacation days ───────────────────────────────────────────────
    # FIX: "Allocated vacation days: 33" has extra words between label and number.
    # scan_same_line now allows up to 4 words between label and value, but we
    # also add the explicit full-phrase patterns as the highest-priority matches.
    NUMERIC_FIELDS = {
        "vacation_days_accrued": [
            # Specific multi-word patterns first (most precise)
            r"allocated\s+vacation\s+days?",
            r"allocated\s+days?",
            r"vacation\s+days?\s+allocated",
            r"accrued\s+vacation\s+days?",
            # Generic fallbacks
            r"accrued", r"entitlement",
            r"vacation\s*days?\s*(?:allocated|accrued|entitlement)",
            r"annual\s*(?:vacation\s*)?entitlement",
            r"total\s*(?:vacation\s*)?days?",
        ],
        "roll_over_days": [
            r"roll\s*[-_]?\s*over", r"rollover",
            r"carr(?:y|ied)\s*(?:over|forward)", r"carry\s*forward",
            r"previous\s*balance",
        ],
        "vacation_days_used": [
            r"days?\s*used", r"vacation\s*used", r"used\s*days?",
            r"days?\s*taken", r"consumed",
        ],
        "vacation_days_remaining": [
            r"days?\s*remaining", r"vacation\s*remaining",
            r"remaining\s*days?", r"current\s*balance",
            r"outstanding\s*balance", r"available\s*days?",
            r"closing\s*balance",
        ],
    }

    for field, labels in NUMERIC_FIELDS.items():
        if field in result:
            continue
        for label in labels:
            val = scan_same_line(label, NUM_INLINE)
            if val:
                result[field] = val
                break
        if field not in result:
            for label in labels:
                val = scan_next_line([label], NUM_INLINE)
                if val:
                    result[field] = val
                    break

    # ── Renewal date ──────────────────────────────────────────────────────────
    renewal_labels = [
        r"renewal\s*date", r"anniversary\s*date",
        r"next\s*renewal", r"renew(?:s|al)?",
    ]
    if "renewal_date" not in result:
        for label in renewal_labels:
            val = scan_same_line(label, DATE_INLINE)
            if val:
                result["renewal_date"] = val
                break
    if "renewal_date" not in result:
        val = scan_next_line(renewal_labels, DATE_INLINE)
        if val:
            result["renewal_date"] = val

    return result


# ─────────────────────────────────────────────────────────────────────────────
# NORMALIZATION
# ─────────────────────────────────────────────────────────────────────────────

def normalize(raw: dict) -> dict:
    """Convert raw string values to typed Python values."""
    out = {}

    # String
    out["employee_name"] = (
        re.sub(r"\s+", " ", raw["employee_name"]).strip()
        if raw.get("employee_name") else None
    )

    # Dates
    for f in ("start_date", "end_date", "renewal_date"):
        out[f] = parse_date(raw.get(f)) if raw.get(f) else None

    # NOTE: end_date and renewal_date can legitimately be the same
    # (employment ends on the vacation anniversary date) — no guard needed.

    # Numbers
    for f in ("vacation_days_accrued", "roll_over_days",
              "vacation_days_used", "vacation_days_remaining"):
        raw_val = raw.get(f)
        if raw_val is not None:
            n = find_first_number(str(raw_val))
            out[f] = n
        else:
            out[f] = None

    return out


# ─────────────────────────────────────────────────────────────────────────────
# MAIN EXTRACTION PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def extract_pdf(file_bytes: bytes) -> dict:
    """
    Full extraction pipeline:
      1. pdfplumber → extract tables + text from every page
      2. Table-based extraction (most reliable for structured HR reports)
      3. Text-line fallback for any missing fields
      4. Normalize types
    Returns extracted fields + debug info.
    """
    all_text_lines = []
    all_tables     = []
    raw_tables_str = []

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            # Extract tables
            for table in page.extract_tables():
                if table:
                    all_tables.append(table)
                    raw_tables_str.append(
                        "\n".join(
                            "  |  ".join(str(c) if c else "" for c in row)
                            for row in table
                        )
                    )

            # Extract text (layout mode preserves column alignment)
            text = page.extract_text(layout=True) or page.extract_text() or ""
            all_text_lines.append(text)

    full_text = "\n".join(all_text_lines)

    # Step 1: try tables
    raw = extract_from_tables(all_tables)

    # Step 2: fill any gaps from free text
    text_raw = extract_from_text(full_text)
    for field, val in text_raw.items():
        if field not in raw or not raw[field]:
            raw[field] = val

    # Step 3: normalize
    normalized = normalize(raw)

    return {
        **normalized,
        "raw_text":   full_text,
        "raw_tables": "\n\n--- TABLE ---\n".join(raw_tables_str),
        "debug":      {"table_hit": raw, "text_raw": text_raw},
    }


# ─────────────────────────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the frontend — works both locally and on Render."""
    return send_file(os.path.join(os.path.dirname(__file__), "index.html"))


@app.route("/api/parse-pdf", methods=["POST"])
def parse_pdf():
    if "pdf" not in request.files:
        return jsonify({"success": False, "error": "No PDF file in request"}), 400

    file = request.files["pdf"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "File must be a PDF"}), 400

    try:
        result = extract_pdf(file.read())
        return jsonify({"success": True, **result})
    except Exception as e:
        logging.exception("PDF extraction failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": "1.0"})


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  Vacation Calculator — running at http://localhost:5050")
    print("  Press Ctrl+C to stop")
    print("=" * 60)
    app.run(host="127.0.0.1", port=5050, debug=False)
