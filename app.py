"""
G-Status Student Portal
=======================

A read-only Flask application that turns a Google Form's linked
"Form Responses" spreadsheet into a per-student status portal.

Data flow:
    Google Form -> Google Sheets -> G-Status backend -> /G001 page

The app never writes to the sheet, never submits forms and never asks for
Google account passwords. It reads a publicly accessible / published sheet
over HTTPS (CSV export endpoints) and caches the result in memory.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

import supabase_store
from supabase_store import SupabaseError

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_ROOT, "config.json")
PUBLIC_DIR = os.path.join(APP_ROOT, "public")

# Vercel runs the app as a read-only serverless function: config.json cannot be
# rewritten there, so settings come from environment variables instead.
IS_VERCEL = bool(os.environ.get("VERCEL"))

DEFAULT_CONFIG = {
    "sheet_url": "",
    "sheet_name": "",
    "master_url": "",
    "master_tab": "",
    "good_min": 0,
    "average_min": -10,
    "refresh_seconds": 20,
    "portal_title": "G-Status",
    "portal_subtitle": "Student Status Portal",
}

# How long the server keeps a fetched copy of the sheet before re-fetching.
CACHE_TTL_SECONDS = 20
HTTP_TIMEOUT_SECONDS = 15

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
# Room for three rank photos (10 MB each) in one upload.
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_config_lock = threading.Lock()


def load_config() -> dict:
    """Read config.json, falling back to defaults and environment variables."""
    cfg = dict(DEFAULT_CONFIG)
    stored_thresholds: dict = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            stored_thresholds = {k: stored[k] for k in ("good_min", "average_min") if k in stored}
            cfg.update({k: v for k, v in stored.items() if v not in (None, "")})
    except (OSError, ValueError):
        # Missing or malformed config is not fatal - the admin page can fix it.
        pass

    # Environment variables win (useful on Render, where the disk may be
    # ephemeral and secrets belong in the dashboard).
    env_url = os.environ.get("GOOGLE_SHEET_URL", "").strip()
    if env_url:
        cfg["sheet_url"] = env_url
    env_tab = os.environ.get("GOOGLE_SHEET_NAME", "").strip()
    if env_tab:
        cfg["sheet_name"] = env_tab
    env_master = os.environ.get("STUDENT_MASTER_URL", "").strip()
    if env_master:
        cfg["master_url"] = env_master
    env_master_tab = os.environ.get("STUDENT_MASTER_NAME", "").strip()
    if env_master_tab:
        cfg["master_tab"] = env_master_tab
    env_refresh = os.environ.get("REFRESH_SECONDS", "").strip()
    if env_refresh.isdigit():
        cfg["refresh_seconds"] = int(env_refresh)

    for key in ("good_min", "average_min"):
        try:
            cfg[key] = float(stored_thresholds.get(key, cfg[key]))
        except (TypeError, ValueError):
            cfg[key] = DEFAULT_CONFIG[key]
    if cfg["average_min"] > cfg["good_min"]:
        cfg["average_min"] = cfg["good_min"]

    try:
        cfg["refresh_seconds"] = max(10, min(120, int(cfg["refresh_seconds"])))
    except (TypeError, ValueError):
        cfg["refresh_seconds"] = DEFAULT_CONFIG["refresh_seconds"]

    return cfg


def save_config(updates: dict) -> dict:
    with _config_lock:
        cfg = load_config()
        cfg.update(updates)
        payload = {
            "sheet_url": cfg.get("sheet_url", ""),
            "sheet_name": cfg.get("sheet_name", ""),
            "master_url": cfg.get("master_url", ""),
            "master_tab": cfg.get("master_tab", ""),
            "good_min": cfg.get("good_min", 0),
            "average_min": cfg.get("average_min", -10),
            "refresh_seconds": cfg.get("refresh_seconds", 20),
            "portal_title": cfg.get("portal_title", "G-Status"),
            "portal_subtitle": cfg.get("portal_subtitle", "Student Status Portal"),
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
        except OSError:
            # Read-only filesystem (some Render plans): keep it in memory only.
            pass
        return payload


def admin_token() -> str:
    return os.environ.get("ADMIN_TOKEN", "").strip()


# ---------------------------------------------------------------------------
# Google Sheet access (read-only, no credentials)
# ---------------------------------------------------------------------------
SHEET_ID_PATTERNS = (
    r"/spreadsheets/d/([a-zA-Z0-9-_]{20,})",
    r"/spreadsheets/d/e/([a-zA-Z0-9-_]{20,})",
    r"[?&]id=([a-zA-Z0-9-_]{20,})",
)


class SheetError(Exception):
    """Raised when the configured sheet cannot be read."""


def extract_sheet_id(sheet_url: str) -> str:
    """Pull the spreadsheet ID out of any Google Sheets URL (or a raw ID)."""
    value = (sheet_url or "").strip()
    if not value:
        raise SheetError("No Google Sheet has been configured yet.")
    for pattern in SHEET_ID_PATTERNS:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9-_]{20,}", value):
        return value
    raise SheetError("That does not look like a valid Google Sheets link.")


def extract_gid(sheet_url: str) -> str | None:
    match = re.search(r"[#&?]gid=([0-9]+)", sheet_url or "")
    return match.group(1) if match else None


def _candidate_urls(sheet_url: str, sheet_name: str = "") -> list[str]:
    sheet_id = extract_sheet_id(sheet_url)
    gid = extract_gid(sheet_url)
    tab = (sheet_name or "").strip()

    urls: list[str] = []
    base = f"https://docs.google.com/spreadsheets/d/{sheet_id}"

    # gviz endpoint handles both "shared with anyone" and "published" sheets.
    if tab:
        urls.append(
            f"{base}/gviz/tq?tqx=out:csv&sheet={urllib.request.quote(tab)}"
        )
    if gid:
        urls.append(f"{base}/gviz/tq?tqx=out:csv&gid={gid}")
        urls.append(f"{base}/export?format=csv&gid={gid}")
    urls.append(f"{base}/gviz/tq?tqx=out:csv")
    urls.append(f"{base}/export?format=csv")
    return urls


def _http_get(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "G-Status/1.0 (+read-only student portal)"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def fetch_sheet_csv(cfg: dict) -> str:
    """CSV text of the Form Responses sheet."""
    return fetch_csv(cfg.get("sheet_url", ""), cfg.get("sheet_name", ""))


def fetch_master_csv(cfg: dict) -> str:
    """CSV text of the Student Master sheet."""
    return fetch_csv(cfg.get("master_url", ""), cfg.get("master_tab", ""))


def fetch_csv(sheet_url: str, sheet_name: str = "") -> str:
    """Try each CSV endpoint until one returns something usable."""
    last_error: Exception | None = None
    for url in _candidate_urls(sheet_url, sheet_name):
        try:
            text = _http_get(url)
        except urllib.error.HTTPError as exc:  # 401/403/404 etc.
            last_error = exc
            continue
        except urllib.error.URLError as exc:
            last_error = exc
            continue
        except Exception as exc:  # pragma: no cover - defensive
            last_error = exc
            continue

        stripped = text.lstrip()
        if stripped.lower().startswith("<!doctype") or stripped.startswith("<HTML"):
            # Google served a sign-in page: the sheet is not shared publicly.
            last_error = SheetError("permission")
            continue
        if stripped:
            return text

    if isinstance(last_error, urllib.error.HTTPError) and last_error.code in (401, 403):
        raise SheetError(
            "The Google Sheet is private. Share it as "
            '"Anyone with the link - Viewer" or publish it to the web.'
        )
    if isinstance(last_error, SheetError) and str(last_error) == "permission":
        raise SheetError(
            "The Google Sheet is private. Share it as "
            '"Anyone with the link - Viewer" or publish it to the web.'
        )
    if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 404:
        raise SheetError("That Google Sheet could not be found. Check the link.")
    if isinstance(last_error, urllib.error.URLError):
        raise SheetError("Could not reach Google Sheets. Check the connection.")
    raise SheetError("The Google Sheet could not be read right now.")


# ---------------------------------------------------------------------------
# Automatic column detection
# ---------------------------------------------------------------------------
def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp": ("timestamp", "time", "date", "datetime", "submittedat", "dateandtime"),
    "member_id": (
        "memberid",
        "memberno",
        "membercode",
        "studentid",
        "id",
        "gid",
        "gstatusid",
        "admissionno",
        "rollno",
        "rollnumber",
    ),
    "name": ("studentname", "name", "fullname", "student", "membername"),
    "class": ("class", "grade", "standard", "std", "classsection"),
    "room": ("roomno", "room", "roomnumber", "roomno1"),
    "cupboard": (
        "cupboardno",
        "cupboard",
        "cupboardnumber",
        "almirahno",
        "almirah",
        "lockerno",
        "locker",
    ),
    "type": (
        "hostelday",
        "hosteldayschool",
        "hostelordayschool",
        "hosteldayscholar",
        "type",
        "studenttype",
        "category",
        "hostel",
    ),
    "status": ("status", "currentstatus", "attendance", "presentabsent"),
    "activity": (
        "activity",
        "activityname",
        "activitytype",
        "event",
        "task",
        "observation",
        "reason",
        "remarkstype",
    ),
    "points": (
        "points",
        "point",
        "score",
        "marks",
        "pointsawarded",
        "pointsdeducted",
        "totalpoints",
    ),
    "description": (
        "description",
        "details",
        "remarks",
        "remark",
        "comment",
        "comments",
        "note",
        "notes",
    ),
}


def detect_columns(headers: list[str]) -> dict[str, str]:
    """Map logical field names to the actual header text found in row 1."""
    mapping: dict[str, str] = {}
    normalized = [(h, _norm(h)) for h in headers]

    for field, aliases in FIELD_ALIASES.items():
        # 1) exact normalized match
        for header, norm in normalized:
            if norm in aliases and header not in mapping.values():
                mapping[field] = header
                break
        if field in mapping:
            continue
        # 2) substring match ("Member ID (e.g. G001)")
        for header, norm in normalized:
            if header in mapping.values():
                continue
            if any(alias in norm for alias in aliases):
                mapping[field] = header
                break
    return mapping


def parse_points(value) -> float:
    """'+5', '-1', '5 points', '' -> float. Never raises."""
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "")
    if not text:
        return 0.0
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return 0.0
    try:
        return float(match.group(0))
    except ValueError:
        return 0.0


def format_points(value: float) -> str:
    number = int(value) if float(value).is_integer() else round(value, 2)
    return f"+{number}" if value > 0 else str(number)


TIMESTAMP_FORMATS = (
    "%m/%d/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%d/%m/%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%d %b %Y",
    "%d %B %Y",
)


def parse_timestamp(value: str):
    text = (value or "").strip()
    if not text:
        return None
    cleaned = text.replace("Z", "").split(".")[0]
    for fmt in TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def humanize_timestamp(dt: datetime | None, raw: str) -> str:
    if dt is None:
        return raw or ""
    stamp = dt.strftime("%d %b %Y")
    if (dt.hour, dt.minute) != (0, 0):
        stamp += " • " + dt.strftime("%I:%M %p").lstrip("0")
    return stamp


# Timestamps from the Google Form are naive (no timezone attached) and are assumed to already
# be in the school's local time. LOCAL_UTC_OFFSET_HOURS lets that be corrected if the server
# and the Form's timezone ever disagree; it defaults to India Standard Time (UTC+5:30).
LOCAL_TZ = timezone(timedelta(hours=float(os.environ.get("LOCAL_UTC_OFFSET_HOURS", "5.5"))))


def current_week_bounds() -> tuple[datetime, datetime, str]:
    """(Monday 00:00, next Monday 00:00, 'DD Mon - DD Mon') for the week we're in right now."""
    now_local = datetime.now(LOCAL_TZ).replace(tzinfo=None)
    monday = now_local.date() - timedelta(days=now_local.weekday())
    week_start = datetime.combine(monday, datetime.min.time())
    week_end = week_start + timedelta(days=7)
    sunday_label = (week_start + timedelta(days=6)).strftime("%d %b")
    label = f"{week_start.strftime('%d %b')} – {sunday_label}"
    return week_start, week_end, label


# ---------------------------------------------------------------------------
# "AG7890 NAYANDEEP PRAJAPATI 202 6" style cells
#   <Member ID> <Student name> <Room no> <Cupboard no>
# ---------------------------------------------------------------------------
ID_TOKEN_RE = re.compile(r"^[A-Za-z]{1,6}[-_/]?\d{1,8}$")
# School class-section such as VI-AQUA, III-AQUA, V-AQUA, 10-A
CLASS_TOKEN_RE = re.compile(
    r"^(?:[IVX]{1,5}|\d{1,2}|NURSERY|LKG|UKG|PREP|KG)-[A-Za-z]{1,12}$", re.I
)
NUM_TOKEN_RE = re.compile(r"^\d{1,4}[A-Za-z]?$")


def parse_combined(text: str) -> dict | None:
    """Split a student cell into Member ID / class / name / room / cupboard.

    Hostel cells:      'AG6748 VI-AQUA YUG GARG 203 6'
    Day-school cells:  'VII-AQUA AG5993 KARTIK'
    Simple cells:      'AG7890 NAYANDEEP PRAJAPATI 202 6'

    The class-section, room and cupboard parts are optional. A bare Member ID
    such as 'G001' or a plain name is *not* a combined cell.
    """
    tokens = re.sub(r"\s+", " ", (text or "").strip()).split(" ")
    tokens = [t for t in tokens if t]
    if len(tokens) < 2:
        return None

    id_idx = next((i for i, t in enumerate(tokens) if ID_TOKEN_RE.match(t)), None)
    if id_idx is None:
        return None
    member_id = tokens[id_idx].upper()
    rest = tokens[:id_idx] + tokens[id_idx + 1:]

    class_section = ""
    for i, token in enumerate(rest):
        if CLASS_TOKEN_RE.match(token):
            class_section = token.upper()
            rest.pop(i)
            break

    room = cupboard = ""
    if len(rest) >= 3 and NUM_TOKEN_RE.match(rest[-1]) and NUM_TOKEN_RE.match(rest[-2]):
        room, cupboard = rest[-2], rest[-1]
        rest = rest[:-2]

    name = " ".join(rest).strip()
    if not name or not re.search(r"[A-Za-z]", name) or re.search(r"\d", name):
        return None
    return {
        "member_id": member_id,
        "name": name,
        "class": class_section,
        "room": room,
        "cupboard": cupboard,
        "full": bool(room and cupboard),
    }


def detect_identity_column(headers: list[str], data_rows: list[list[str]]) -> str | None:
    """Find the column whose cells look like 'AG7890 NAME 202 6' (by content)."""
    best: str | None = None
    best_score = 0.0
    for idx, header in enumerate(headers):
        values = [
            row[idx].strip()
            for row in data_rows
            if idx < len(row) and row[idx] and row[idx].strip()
        ]
        if not values:
            continue
        hits = [h for h in (parse_combined(v) for v in values) if h]
        if not hits:
            continue
        ratio = len(hits) / len(values)
        if ratio < 0.5:
            continue
        score = ratio + 0.1 * (sum(1 for h in hits if h["full"]) / len(hits))
        if score > best_score:
            best, best_score = header, score
    return best


def _row_identity(row: dict, columns: dict, identity_col: str | None) -> dict | None:
    """Parsed id / name / room / cupboard for one sheet row, if it has a combined cell."""
    candidates = []
    if identity_col:
        candidates.append(row.get(identity_col, ""))
    for field_name in ("member_id", "name"):
        col = columns.get(field_name)
        if col and col != identity_col:
            candidates.append(row.get(col, ""))
    for text in candidates:
        parsed = parse_combined(text)
        if parsed:
            return parsed
    return None


def _clean_rows(csv_text: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(csv_text))
    return [row for row in reader if any((cell or "").strip() for cell in row)]


def _row_dict(headers: list[str], raw_row: list[str]) -> dict:
    return {
        headers[i]: (raw_row[i].strip() if i < len(raw_row) and raw_row[i] else "")
        for i in range(len(headers))
    }


# ---------------------------------------------------------------------------
# Sheet parsing + cache
# ---------------------------------------------------------------------------
_cache: dict = {"fetched_at": 0.0, "payload": None, "error": None}
_cache_lock = threading.Lock()
_master_cache: dict = {"fetched_at": 0.0, "payload": None}


class SheetEmptyError(SheetError):
    """The responses sheet is readable but has no rows yet."""


def empty_log() -> dict:
    return {"headers": [], "columns": {}, "records": [], "row_count": 0}


SIGNED_ITEM_RE = re.compile(
    r"^(?P<text>.*?)\s*(?P<sign>[+\-\u2013\u2212])?\s*(?P<num>\d+(?:\.\d+)?)\s*$",
    re.S,
)


def detect_activity_layout(headers: list[str]) -> dict | None:
    """Recognise the hostel form: Name (Hostel) | Name (DAY SCHOOL) | Positive | Negative."""
    layout: dict = {}
    for header in headers:
        n = _norm(header)
        if "posit" in n and "pos" not in layout:
            layout["pos"] = header
        elif "negat" in n and "neg" not in layout:
            layout["neg"] = header
        elif "name" in n and "hostel" in n and "hostel" not in layout:
            layout["hostel"] = header
        elif "name" in n and "day" in n and "day" not in layout:
            layout["day"] = header
        elif "disciplin" in n and "disc" not in layout:
            layout["disc"] = header
        elif "timestamp" in n and "ts" not in layout:
            layout["ts"] = header
    if "pos" in layout and "neg" in layout and ("hostel" in layout or "day" in layout):
        layout.setdefault("ts", headers[0] if headers else "")
        return layout
    return None


def split_activity_items(cell: str, kind: str) -> list[dict]:
    """'Late for Prayer -5' -> one item; 'A -5, B -3' or a multi-line cell -> several.

    The Positive column always adds points, the Negative column always deducts,
    whatever sign is typed. An item with no number ('Disobeying instructions.')
    is kept with 0 points.
    """
    text = (cell or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"(?<=\d)\s*,\s*|\s*[\r\n]+\s*", text) if p.strip()]
    items = []
    for part in parts:
        match = SIGNED_ITEM_RE.match(part)
        if match and match["text"].strip():
            amount = abs(float(match["num"]))
            points = amount if kind == "pos" else -amount
            label = match["text"].strip().rstrip("-+ ").strip()
            items.append({"activity": label or part, "points": points, "kind": kind})
        else:
            items.append({"activity": part.rstrip(". ").strip() or part, "points": 0.0, "kind": kind})
    return items


DISCIPLINE_MAX = 10.0


def parse_discipline(cell: str) -> list[dict]:
    """'Hostel discipline' score out of 10: '8', '8/10', '7.5 out of 10' -> +8 / +7.5.

    It always adds points and is capped at 10. A cell without a number is ignored.
    """
    match = re.search(r"\d+(?:\.\d+)?", cell or "")
    if not match:
        return []
    score = min(float(match.group(0)), DISCIPLINE_MAX)
    shown = int(score) if score.is_integer() else score
    return [
        {
            "activity": "Hostel discipline",
            "points": score,
            "kind": "disc",
            "description": f"{shown} / {int(DISCIPLINE_MAX)}",
        }
    ]


def parse_activity_sheet(rows: list[list[str]], headers: list[str], layout: dict) -> dict:
    """One record per (student, activity) from the hostel/day-school form."""
    name_cols = [layout[k] for k in ("hostel", "day") if k in layout]
    columns = {
        "timestamp": layout["ts"],
        "identity": name_cols[0],
        "activity": "__activity",
        "points": "__points",
        "act_pos": layout["pos"],
        "act_neg": layout["neg"],
        "description": "__description",
    }
    if "disc" in layout:
        columns["act_disc"] = layout["disc"]
    if "day" in layout and "hostel" in layout:
        columns["identity_day"] = layout["day"]

    records: list[dict] = []
    skipped = 0
    for raw_row in rows[1:]:
        row = _row_dict(headers, raw_row)
        identities = []
        for col in name_cols:
            ident = parse_combined(row.get(col, ""))
            if ident:
                # Filled in the "Name (Hostel)" column -> a hostel student.
                ident["hostel"] = col == layout.get("hostel")
                identities.append(ident)
        items = split_activity_items(row.get(layout["pos"], ""), "pos") + split_activity_items(
            row.get(layout["neg"], ""), "neg"
        )
        # The hostel discipline score belongs to the hostel student only.
        disc_items = parse_discipline(row.get(layout["disc"], "")) if "disc" in layout else []
        if not identities or not (items or disc_items):
            if any(row.get(col) for col in name_cols) or items or disc_items:
                skipped += 1
            continue
        for identity in identities:
            person_items = items + (disc_items if identity.get("hostel") else [])
            for item in person_items:
                values = dict(row)
                values["__activity"] = item["activity"]
                values["__description"] = item.get("description", "")
                number = int(item["points"]) if float(item["points"]).is_integer() else item["points"]
                values["__points"] = f"{number:+d}" if isinstance(number, int) and number else str(number)
                records.append(
                    {
                        "member_id": identity["member_id"],
                        "identity": identity,
                        "values": values,
                        "kind": item["kind"],
                    }
                )

    return {
        "headers": headers,
        "columns": columns,
        "records": records,
        "row_count": len(records),
        "skipped_rows": skipped,
    }


def parse_sheet(csv_text: str) -> dict:
    rows = _clean_rows(csv_text)
    if len(rows) <= 1:
        # Completely empty, or only the header row: no responses yet.
        raise SheetEmptyError("The Google Sheet is empty - no form responses yet.")

    headers = [(cell or "").strip() for cell in rows[0]]
    layout = detect_activity_layout(headers)
    if layout:
        return parse_activity_sheet(rows, headers, layout)
    columns = detect_columns(headers)
    identity_col = detect_identity_column(headers, rows[1:])
    if identity_col:
        columns["identity"] = identity_col
    if "member_id" not in columns and not identity_col:
        raise SheetError(
            "No Member ID column was found in the sheet. Add a column named "
            '"Member ID" (or one holding cells like "AG7890 NAME 202 6").'
        )

    records: list[dict] = []
    for raw_row in rows[1:]:
        row = _row_dict(headers, raw_row)
        identity = _row_identity(row, columns, identity_col)
        member_id = identity["member_id"] if identity else ""
        if not member_id and columns.get("member_id"):
            member_id = row.get(columns["member_id"], "").strip()
        if not member_id:
            continue
        records.append({"member_id": member_id, "identity": identity, "values": row})

    return {
        "headers": headers,
        "columns": columns,
        "records": records,
        "row_count": len(records),
        "skipped_rows": 0,
    }


TYPE_WORDS = {"HOSTEL", "DAYSCHOOL", "DAYSCHOLAR"}


def _infer_headerless_master(rows: list[list[str]]) -> tuple[list[str], list[list[str]]] | None:
    """Master with no header row, e.g.

        AF9846 | DAKSHIT ARORA | VI | VI-AQUA | | HOSTEL | | NEW | | | GURUGRAM

    Columns are recognised by what they contain. Returns (headers, data_rows).
    """
    # gviz sometimes prepends a label row like A,B,C,...
    if rows and all(re.fullmatch(r"[A-Za-z]", (c or "").strip()) for c in rows[0] if (c or "").strip()):
        rows = rows[1:]
    if not rows:
        return None

    width = max(len(r) for r in rows)

    def values(i: int) -> list[str]:
        return [(r[i].strip() if i < len(r) and r[i] else "") for r in rows]

    def ratio(i: int, pred) -> float:
        nonempty = [v for v in values(i) if v]
        return sum(1 for v in nonempty if pred(v)) / len(nonempty) if nonempty else 0.0

    id_col = max(range(width), key=lambda i: ratio(i, lambda v: bool(ID_TOKEN_RE.match(v))), default=None)
    if id_col is None or ratio(id_col, lambda v: bool(ID_TOKEN_RE.match(v))) < 0.8:
        return None
    first_id = rows[0][id_col].strip() if id_col < len(rows[0]) else ""
    if not ID_TOKEN_RE.match(first_id):
        return None  # first row is a normal header row

    roles: dict[int, str] = {id_col: "Member ID"}

    def find(pred, label: str, threshold: float = 0.6) -> None:
        for i in range(width):
            if i not in roles and ratio(i, pred) >= threshold:
                roles[i] = label
                return

    find(lambda v: bool(CLASS_TOKEN_RE.match(v)), "Class")
    find(lambda v: v.upper().replace(" ", "") in TYPE_WORDS, "Hostel / Day School")
    find(lambda v: bool(re.fullmatch(r"\d{3}", v)), "Room No")
    find(lambda v: bool(re.fullmatch(r"\d{1,2}", v)), "Cupboard No")
    find(
        lambda v: len(re.findall(r"[A-Za-z]", v)) >= 3 and not CLASS_TOKEN_RE.match(v),
        "Student Name",
    )
    if "Student Name" not in roles.values():
        return None
    # Name is the left-most text column, so re-run in position order if needed.
    name_col = next(i for i, label in roles.items() if label == "Student Name")
    earlier = [
        i for i in range(id_col + 1, name_col)
        if i not in roles and ratio(i, lambda v: len(re.findall(r"[A-Za-z]", v)) >= 3) >= 0.6
    ]
    if earlier:
        roles[name_col] = f"Column {name_col + 1}"
        roles[earlier[0]] = "Student Name"

    headers = [roles.get(i, f"Column {i + 1}") for i in range(width)]
    return headers, rows


def parse_master(csv_text: str) -> dict:
    """Parse the Student Master sheet: one row per student on the roll."""
    rows = _clean_rows(csv_text)
    if not rows:
        raise SheetError("Student Master: the sheet is empty.")

    first = [(cell or "").strip() for cell in rows[0]]
    inferred = None
    if any(parse_combined(cell) for cell in first):
        # One column of 'AG7890 NAME 202 6' cells, no header row.
        headers = [f"Column {i + 1}" for i in range(len(first))]
        data_rows = rows
    elif (inferred := _infer_headerless_master(rows)) is not None:
        headers, data_rows = inferred
    else:
        headers = first
        data_rows = rows[1:]

    columns = detect_columns(headers)
    identity_col = detect_identity_column(headers, data_rows)
    if identity_col:
        columns["identity"] = identity_col
    if "member_id" not in columns and not identity_col:
        raise SheetError(
            "Student Master: no Member ID column was found. Use a column named "
            '"Member ID" or cells like "AG7890 NAME 202 6".'
        )

    students: list[dict] = []
    by_id: dict[str, dict] = {}
    for raw_row in data_rows:
        row = _row_dict(headers, raw_row)
        identity = _row_identity(row, columns, identity_col)
        member_id = identity["member_id"] if identity else ""
        if not member_id and columns.get("member_id"):
            member_id = row.get(columns["member_id"], "").strip()
        if not member_id:
            continue

        def pick(key: str) -> str:
            if identity and identity.get(key):
                return identity[key]
            col = columns.get(key)
            if col and col != identity_col:
                return row.get(col, "")
            return ""

        key = normalize_member_id(member_id)
        if key in by_id:
            continue  # first row for an ID wins
        record = {
            "member_id": member_id,
            "name": pick("name"),
            "room": pick("room"),
            "cupboard": pick("cupboard"),
            "class": pick("class"),
            "type": pick("type"),
            "values": row,
        }
        by_id[key] = record
        students.append(record)

    if not students:
        raise SheetError("Student Master: no students found in the sheet.")

    return {
        "headers": headers,
        "columns": columns,
        "students": students,
        "by_id": by_id,
        "row_count": len(students),
    }


def get_sheet_data(force: bool = False) -> dict:
    """Return parsed sheet data, using a short-lived server-side cache."""
    cfg = load_config()
    now = time.time()
    with _cache_lock:
        fresh = (
            _cache["payload"] is not None
            and not force
            and (now - _cache["fetched_at"]) < CACHE_TTL_SECONDS
        )
        if fresh:
            return _cache["payload"]

    try:
        payload = parse_sheet(fetch_sheet_csv(cfg))
    except SheetError:
        with _cache_lock:
            # Serve slightly stale data rather than failing (Google hiccups,
            # Render cold starts, brief network loss).
            if _cache["payload"] is not None:
                return _cache["payload"]
        raise

    payload["fetched_at"] = time.time()
    with _cache_lock:
        _cache["fetched_at"] = payload["fetched_at"]
        _cache["payload"] = payload
    return payload


def get_master_data(force: bool = False) -> dict | None:
    """Parsed Student Master, or None when no master sheet is configured."""
    cfg = load_config()
    if not (cfg.get("master_url") or "").strip():
        return None
    now = time.time()
    with _cache_lock:
        if (
            _master_cache["payload"] is not None
            and not force
            and (now - _master_cache["fetched_at"]) < CACHE_TTL_SECONDS
        ):
            return _master_cache["payload"]

    try:
        payload = parse_master(fetch_master_csv(cfg))
    except SheetError as exc:
        with _cache_lock:
            if _master_cache["payload"] is not None:
                return _master_cache["payload"]
        message = str(exc)
        if not message.startswith("Student Master"):
            message = f"Student Master: {message}"
        raise SheetError(message) from exc

    with _cache_lock:
        _master_cache["fetched_at"] = time.time()
        _master_cache["payload"] = payload
    return payload


def get_master_safe(force: bool = False) -> tuple[dict | None, str | None]:
    """Like get_master_data, but a broken master never takes the portal down."""
    try:
        return get_master_data(force), None
    except SheetError as exc:
        return None, str(exc)


def load_portal_data(force: bool = False) -> tuple[dict, dict | None, str | None]:
    """(responses data, student master or None, master error or None)."""
    master, master_error = get_master_safe(force)
    try:
        data = get_sheet_data(force)
    except SheetEmptyError:
        if master is None:
            raise
        # Nobody has been marked yet: every student on the roll shows 0.
        data = empty_log()
    return data, master, master_error


# ---------------------------------------------------------------------------
# Student lookup
# ---------------------------------------------------------------------------
MEMBER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,31}$")


def normalize_member_id(value: str) -> str:
    return re.sub(r"\s+", "", (value or "")).upper()


ACTIVITY_ICONS = (
    (("hostel discipline",), "🛡️"),
    (("clean", "swachh", "room clean", "tidy"), "🧹"),
    (("study", "class", "homework", "read", "learn"), "📚"),
    (("sport", "game", "yoga", "run", "fitness", "pt"), "🏅"),
    (("prayer", "puja", "satsang", "bhajan", "temple", "aarti"), "🪔"),
    (("observ", "warning", "late", "penalt", "discipline", "misconduct"), "⚠️"),
    (("competition", "win", "prize", "award", "trophy", "champ"), "🏆"),
    (("music", "dance", "art", "craft", "draw"), "🎨"),
    (("help", "service", "seva", "volunteer"), "🤝"),
)


def activity_icon(activity: str, points: float) -> str:
    text = (activity or "").lower()
    for keywords, icon in ACTIVITY_ICONS:
        if any(keyword in text for keyword in keywords):
            return icon
    return "⭐" if points >= 0 else "⚠️"


def build_student(member_id: str, data: dict, master: dict | None = None) -> dict | None:
    columns = data["columns"]
    identity_col = columns.get("identity")
    target = normalize_member_id(member_id)

    matched = [
        rec for rec in data["records"] if normalize_member_id(rec["member_id"]) == target
    ]
    master_rec = master["by_id"].get(target) if master else None
    if not matched and not master_rec:
        return None

    ts_col = columns.get("timestamp")
    entries = []
    seen_keys = set()

    for index, rec in enumerate(matched):
        row = rec["values"]
        raw_ts = row.get(ts_col, "") if ts_col else ""
        dt = parse_timestamp(raw_ts)
        activity = row.get(columns.get("activity", ""), "") if columns.get("activity") else ""
        description = (
            row.get(columns.get("description", ""), "")
            if columns.get("description")
            else ""
        )
        raw_points = row.get(columns.get("points", ""), "") if columns.get("points") else ""
        points = parse_points(raw_points)

        # De-duplicate identical form responses (same time, activity, points).
        key = (raw_ts, activity.strip().lower(), description.strip().lower(), str(raw_points).strip())
        if key in seen_keys and raw_ts:
            continue
        seen_keys.add(key)

        entries.append(
            {
                "order": index,
                "timestamp_raw": raw_ts,
                "timestamp_iso": dt.isoformat() if dt else "",
                "timestamp_label": humanize_timestamp(dt, raw_ts),
                "sort_key": dt.timestamp() if dt else float(index),
                "activity": activity or ("Activity" if raw_points != "" else "Record"),
                "description": description,
                "points": points,
                "points_label": format_points(points) if raw_points != "" else "",
                "has_points": str(raw_points).strip() != "",
                "icon": activity_icon(activity, points),
                "status": row.get(columns.get("status", ""), "") if columns.get("status") else "",
            }
        )

    entries.sort(key=lambda e: (e["sort_key"], e["order"]), reverse=True)

    latest_rec = matched[-1] if matched else None
    if matched and ts_col:
        dated = [
            (parse_timestamp(rec["values"].get(ts_col, "")), rec) for rec in matched
        ]
        dated = [pair for pair in dated if pair[0] is not None]
        if dated:
            latest_rec = max(dated, key=lambda pair: pair[0])[1]
    latest_row = latest_rec["values"] if latest_rec else {}

    def field(name: str) -> str:
        col = columns.get(name)
        if not col or col == identity_col:
            return ""
        value = latest_row.get(col, "")
        if value:
            return value
        for rec in reversed(matched):
            if rec["values"].get(col):
                return rec["values"][col]
        return ""

    def pick(name: str) -> str:
        """Student Master first, then the 'ID NAME ROOM CUPBOARD' cell, then a column."""
        if master_rec and master_rec.get(name):
            return master_rec[name]
        if latest_rec:
            for rec in [latest_rec, *reversed(matched)]:
                identity = rec.get("identity")
                if identity and identity.get(name):
                    return identity[name]
        return field(name)

    earned = sum(e["points"] for e in entries if e["points"] > 0)
    deducted = sum(e["points"] for e in entries if e["points"] < 0)
    total = earned + deducted

    sheet_status = field("status") if matched else ""
    if sheet_status:
        status_value = sheet_status
        status_key = status_slug(sheet_status)
    else:
        # No Status column in the form: grade the student from the points total.
        status_value, status_key = points_status(total, load_config())

    profile_fields = []
    for label, key in (
        ("Class", "class"),
        ("Room", "room"),
        ("Cupboard", "cupboard"),
        ("Type", "type"),
    ):
        value = pick(key)
        if value:
            profile_fields.append({"label": label, "value": value})

    # Any extra sheet columns that aren't already shown, from the latest record.
    known = {columns.get(k) for k in columns}
    extras = [
        {"label": header, "value": latest_row.get(header, "")}
        for header in data["headers"]
        if header and header not in known and latest_row.get(header)
    ]

    return {
        "member_id": (
            matched[-1]["member_id"] if matched else master_rec["member_id"]
        )
        or target,
        "name": pick("name"),
        "photo_url": student_photo_url(target),
        "status": status_value,
        "status_key": status_key,
        "fields": profile_fields,
        "extras": extras[:6],
        "has_points_column": bool(columns.get("points")) or master is not None,
        "marked": bool(matched),
        "total_points": format_points(total),
        "total_points_value": total,
        "points_earned": format_points(earned),
        "points_deducted": format_points(deducted),
        "activity_count": len(entries),
        "history": entries,
    }


def _rank(rows: list[dict], score) -> None:
    """Add a 'rank' key; students with equal scores share a rank."""
    previous, rank = None, 0
    for position, row in enumerate(rows, start=1):
        value = score(row)
        if value != previous:
            rank, previous = position, value
        row["rank"] = rank


def _room_sort_key(room: str):
    match = re.match(r"^(\d+)", room)
    return (int(match.group(1)) if match else 10**9, room)


def is_hostel_student(st: dict) -> bool:
    """Hostel or day school? The Student Master's type column wins; without it,
    a room number or a "Name (Hostel)" form entry means hostel."""
    kind = re.sub(r"[^a-z]", "", (st.get("type") or "").lower())
    if kind:
        if "hostel" in kind:
            return True
        if "day" in kind:
            return False
    return bool(st.get("hostel_src") or st.get("room"))


def compute_leaderboard(
    data: dict, master: dict | None, by: str = "avg", cfg: dict | None = None, need_photos: bool = False
) -> dict:
    """Room ranking, students who only ever earn points, and the hostel roll."""
    cfg = cfg or load_config()
    columns = data["columns"]
    ts_col = columns.get("timestamp")
    status_col = columns.get("status")
    students: dict[str, dict] = {}
    week_students: dict[str, dict] = {}
    week_start, week_end, week_label = current_week_bounds()

    def blank(member_id: str) -> dict:
        return {
            "member_id": member_id, "name": "", "class": "", "room": "", "cupboard": "",
            "type": "", "hostel_src": False, "sheet_status": "",
            "total": 0.0, "earned": 0.0, "deducted": 0.0, "pos_n": 0, "neg_n": 0, "entries": 0,
        }

    if master:
        for rec in master["students"]:
            st = students.setdefault(normalize_member_id(rec["member_id"]), blank(rec["member_id"]))
            for key in ("name", "class", "room", "cupboard", "type"):
                st[key] = rec.get(key, "") or st[key]

    seen = set()
    for rec in data["records"]:
        key = normalize_member_id(rec["member_id"])
        row = rec["values"]
        raw_ts = row.get(ts_col, "") if ts_col else ""
        activity = row.get(columns["activity"], "") if columns.get("activity") else ""
        raw_points = row.get(columns["points"], "") if columns.get("points") else ""
        dedupe = (key, raw_ts, activity.strip().lower(), str(raw_points).strip())
        if raw_ts and dedupe in seen:
            continue
        seen.add(dedupe)

        points = parse_points(raw_points)
        identity = rec.get("identity") or {}

        def apply(target: dict) -> None:
            for field_name in ("name", "class", "room", "cupboard"):
                if not target[field_name] and identity.get(field_name):
                    target[field_name] = identity[field_name]
            if identity.get("hostel"):
                target["hostel_src"] = True
            if status_col and row.get(status_col):
                target["sheet_status"] = row[status_col]
            target["entries"] += 1
            target["total"] += points
            if points > 0:
                target["earned"] += points
                target["pos_n"] += 1
            if points < 0 or rec.get("kind") == "neg":
                target["deducted"] += min(points, 0)
                target["neg_n"] += 1

        apply(students.setdefault(key, blank(rec["member_id"])))

        entry_dt = parse_timestamp(raw_ts)
        if entry_dt is not None and week_start <= entry_dt < week_end:
            apply(week_students.setdefault(key, blank(rec["member_id"])))

    # ---- rooms ----
    rooms: dict[str, dict] = {}
    for st in students.values():
        room = (st["room"] or "").strip()
        if not room:
            continue
        r = rooms.setdefault(
            room,
            {"room": room, "students": 0, "marked": 0, "total": 0.0, "earned": 0.0, "deducted": 0.0},
        )
        r["students"] += 1
        r["marked"] += 1 if st["entries"] else 0
        r["total"] += st["total"]
        r["earned"] += st["earned"]
        r["deducted"] += st["deducted"]
    room_rows = list(rooms.values())
    for r in room_rows:
        r["average"] = r["total"] / r["students"] if r["students"] else 0.0
    if by == "total":
        room_rows.sort(key=lambda r: (-r["total"], -r["average"], _room_sort_key(r["room"])))
        _rank(room_rows, lambda r: r["total"])
    else:
        room_rows.sort(key=lambda r: (-r["average"], -r["total"], _room_sort_key(r["room"])))
        _rank(room_rows, lambda r: round(r["average"], 4))

    # ---- always-positive students ----
    disciplined = [st for st in students.values() if st["pos_n"] > 0 and st["neg_n"] == 0]
    disciplined.sort(key=lambda st: (-st["total"], -st["pos_n"], st["name"], st["member_id"]))
    _rank(disciplined, lambda st: (st["total"], st["pos_n"]))

    def fmt(value: float) -> str:
        return format_points(round(value, 2))

    for r in room_rows:
        r["total_label"] = fmt(r["total"])
        r["average_label"] = fmt(r["average"])
        r["earned_label"] = fmt(r["earned"])
        r["deducted_label"] = fmt(r["deducted"])
    for st in disciplined:
        st["total_label"] = fmt(st["total"])

    # ---- every hostel student, with the same status the student page shows ----
    hostel = [st for st in students.values() if is_hostel_student(st)]
    for st in hostel:
        if st["sheet_status"]:
            st["status"], st["status_key"] = st["sheet_status"], status_slug(st["sheet_status"])
        else:
            st["status"], st["status_key"] = points_status(st["total"], cfg)
        st["total_label"] = fmt(st["total"])
    hostel.sort(key=lambda st: ((st["name"] or st["member_id"]).upper(), st["member_id"]))
    hostel_rooms = sorted({st["room"] for st in hostel if st["room"]}, key=_room_sort_key)
    preferred = ["Good", "Average", "Bad"]
    present = {st["status"] for st in hostel}
    hostel_statuses = [s for s in preferred if s in present] + sorted(present - set(preferred))

    # ---- every day-school student, same status logic as the hostel roll ----
    day = [st for st in students.values() if not is_hostel_student(st)]
    for st in day:
        if st["sheet_status"]:
            st["status"], st["status_key"] = st["sheet_status"], status_slug(st["sheet_status"])
        else:
            st["status"], st["status_key"] = points_status(st["total"], cfg)
        st["total_label"] = fmt(st["total"])
    day.sort(key=lambda st: ((st["name"] or st["member_id"]).upper(), st["member_id"]))
    day_classes = sorted({st["class"] for st in day if st["class"]})
    present_day = {st["status"] for st in day}
    day_statuses = [s for s in preferred if s in present_day] + sorted(present_day - set(preferred))

    # ---- GStar: hostel + day school mixed into one points-ranked list ----
    # The photo lookup is one Supabase read total (not one per student) - and only happens
    # at all when the GStar/Weekly tab is actually being shown, so every other tab (and every
    # rank-ceremony fill) stays fast and never touches Supabase for this.
    photo_index = load_student_photo_index() if need_photos else {}

    def photo_for(member_id: str) -> str:
        entry = photo_index.get(normalize_member_id(member_id))
        return f"/students/photo/{normalize_member_id(member_id)}?v={entry.get('v', 0)}" if entry else ""

    gstar = [st for st in students.values() if st["entries"]]
    for st in gstar:
        st["is_hostel"] = is_hostel_student(st)
        st["total_label"] = fmt(st["total"])
        st["photo_url"] = photo_for(st["member_id"])
    gstar.sort(key=lambda st: (-st["total"], (st["name"] or st["member_id"]).upper(), st["member_id"]))
    for position, st in enumerate(gstar, start=1):
        st["rank"] = position  # plain 1, 2, 3, 4... - never shares a number even when points tie

    # ---- Weekly report: every known student, but only this Monday-Sunday's points ----
    # (0 for anyone with no activity yet this week). Resets itself every Monday because
    # week_students only ever holds records whose timestamp falls in [week_start, week_end).
    weekly = []
    for key, st in students.items():
        week_st = week_students.get(key)
        row = dict(st)
        row["total"] = week_st["total"] if week_st else 0.0
        row["earned"] = week_st["earned"] if week_st else 0.0
        row["deducted"] = week_st["deducted"] if week_st else 0.0
        row["entries"] = week_st["entries"] if week_st else 0
        row["pos_n"] = week_st["pos_n"] if week_st else 0
        row["neg_n"] = week_st["neg_n"] if week_st else 0
        row["is_hostel"] = is_hostel_student(row)
        row["total_label"] = fmt(row["total"])
        row["photo_url"] = photo_for(row["member_id"])
        weekly.append(row)
    weekly.sort(key=lambda st: (-st["total"], (st["name"] or st["member_id"]).upper(), st["member_id"]))
    for position, st in enumerate(weekly, start=1):
        st["rank"] = position  # plain 1, 2, 3, 4... - the 0-point tie doesn't collapse into one number

    return {
        "rooms": room_rows,
        "disciplined": disciplined,
        "hostel": hostel,
        "hostel_rooms": hostel_rooms,
        "hostel_statuses": hostel_statuses,
        "day": day,
        "day_classes": day_classes,
        "day_statuses": day_statuses,
        "gstar": gstar,
        "weekly": weekly,
        "week_label": week_label,
        "student_count": len(students),
        "clean_count": len(disciplined),
        "by": "total" if by == "total" else "avg",
    }


def points_status(total: float, cfg: dict) -> tuple[str, str]:
    """(label, css key) from total points: Good / Average / Bad."""
    if total >= cfg["good_min"]:
        return "Good", "present"
    if total >= cfg["average_min"]:
        return "Average", "pending"
    return "Bad", "absent"


def status_slug(status: str) -> str:
    text = (status or "").strip().lower()
    if not text:
        return "unknown"
    if text.startswith("present") or "active" in text and "in" not in text[:2]:
        return "present"
    if text.startswith("absent"):
        return "absent"
    if text.startswith("inactive"):
        return "inactive"
    if text.startswith("active"):
        return "present"
    if text.startswith("pending") or "leave" in text:
        return "pending"
    if "hostel" in text or "day scholar" in text or "dayscholar" in text:
        return "present"
    if "suspend" in text or "expel" in text or "blocked" in text:
        return "absent"
    return "other"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
RESERVED_PATHS = {
    "favicon.ico",
    "robots.txt",
    "static",
    "admin",
    "leaderboard",
    "rank",
    "students",
    "api",
    "healthz",
    "health",
}


def sheet_configured() -> bool:
    return bool(load_config().get("sheet_url", "").strip())


@app.route("/")
def home():
    # Support /?memberId=G001 and the bare /?G001 form.
    member_id = (request.args.get("memberId") or request.args.get("member_id") or "").strip()
    if not member_id:
        for key, value in request.args.items():
            if not value and MEMBER_ID_RE.match(key):
                member_id = key
                break
    if member_id:
        return redirect(url_for("student_page", member_id=normalize_member_id(member_id)))

    cfg = load_config()
    return render_template(
        "index.html",
        config=cfg,
        configured=sheet_configured(),
    )


@app.route("/leaderboard")
def leaderboard():
    cfg = load_config()
    if not sheet_configured():
        return (
            render_template(
                "error.html",
                config=cfg,
                title="Portal not set up yet",
                icon="⚙️",
                message="No Google Sheet has been connected. An administrator can add it on the setup page.",
                action_url="/admin",
                action_label="OPEN SETUP",
            ),
            503,
        )
    try:
        data, master, master_error = load_portal_data()
    except SheetError as exc:
        return (
            render_template(
                "error.html", config=cfg, title="Data unavailable", icon="📡", message=str(exc)
            ),
            503,
        )

    # Averages are only fair when the master lists every student's room;
    # otherwise default to total room points.
    has_room_data = bool(master) and any(rec.get("room") for rec in master["students"])
    requested = request.args.get("rank")
    by = requested if requested in ("avg", "total") else ("avg" if has_room_data else "total")
    try:
        limit = max(5, min(200, int(request.args.get("limit", 25))))
    except ValueError:
        limit = 25
    view = request.args.get("view")
    view = view if view in ("hostel", "day", "gstar", "weekly") else "rank"
    board = compute_leaderboard(data, master, by, cfg, need_photos=view in ("gstar", "weekly"))
    return render_template(
        "leaderboard.html",
        config=cfg,
        board=board,
        view=view,
        limit=limit,
        master_error=master_error,
        has_master=master is not None,
        has_room_data=has_room_data,
        refresh_seconds=cfg["refresh_seconds"],
    )


@app.route("/<member_id>")
def student_page(member_id: str):
    if member_id.lower() in RESERVED_PATHS or "." in member_id:
        abort(404)
    if not MEMBER_ID_RE.match(member_id):
        return (
            render_template(
                "error.html",
                config=load_config(),
                title="Invalid Member ID",
                icon="🔍",
                message="That Member ID does not look right. Please check it and try again.",
            ),
            400,
        )

    cfg = load_config()
    if not sheet_configured():
        return (
            render_template(
                "error.html",
                config=cfg,
                title="Portal not set up yet",
                icon="⚙️",
                message="No Google Sheet has been connected. An administrator can add it on the setup page.",
                action_url="/admin",
                action_label="OPEN SETUP",
            ),
            503,
        )

    try:
        data, master, master_error = load_portal_data()
    except SheetError as exc:
        return (
            render_template(
                "error.html",
                config=cfg,
                title="Data unavailable",
                icon="📡",
                message=str(exc),
            ),
            503,
        )

    student = build_student(member_id, data, master)
    if student is None:
        message = "The Member ID you entered could not be found. Please check the Member ID and try again."
        if master_error:
            message += f" ({master_error})"
        return (
            render_template(
                "error.html",
                config=cfg,
                title="Student Not Found",
                icon="🔍",
                message=message,
            ),
            404,
        )

    return render_template(
        "status.html",
        config=cfg,
        student=student,
        member_id=student["member_id"],
        refresh_seconds=cfg["refresh_seconds"],
    )


@app.route("/api/student/<member_id>")
def api_student(member_id: str):
    """JSON for the live-refresh loop. Returns ONLY this student's data."""
    if not MEMBER_ID_RE.match(member_id):
        return jsonify({"ok": False, "error": "Invalid Member ID."}), 400
    try:
        data, master, _master_error = load_portal_data(force=request.args.get("force") == "1")
    except SheetError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503

    student = build_student(member_id, data, master)
    if student is None:
        return jsonify({"ok": False, "error": "Student not found."}), 404

    return jsonify(
        {
            "ok": True,
            "student": student,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.route("/admin", methods=["GET", "POST"])
def admin():
    cfg = load_config()
    token = admin_token()
    message = None
    error = None
    master_error = None

    if request.method == "POST":
        supplied = (request.form.get("token") or "").strip()
        if IS_VERCEL:
            error = (
                "Settings cannot be saved from this page on Vercel. Set GOOGLE_SHEET_URL, "
                "STUDENT_MASTER_URL etc. under Project → Settings → Environment Variables "
                "and redeploy."
            )
        elif token and supplied != token:
            error = "Incorrect admin token."
        else:
            sheet_url = (request.form.get("sheet_url") or "").strip()
            sheet_name = (request.form.get("sheet_name") or "").strip()
            master_url = (request.form.get("master_url") or "").strip()
            master_tab = (request.form.get("master_tab") or "").strip()
            refresh = (request.form.get("refresh_seconds") or "20").strip()
            try:
                good_min = float(request.form.get("good_min") or 0)
                average_min = float(request.form.get("average_min") or -10)
            except ValueError:
                good_min, average_min = 0, -10
            try:
                sheet_id = extract_sheet_id(sheet_url)
                if master_url:
                    extract_sheet_id(master_url)  # validate the link
                cfg = save_config(
                    {
                        "sheet_url": sheet_url,
                        "sheet_name": sheet_name,
                        "master_url": master_url,
                        "master_tab": master_tab,
                        "good_min": good_min,
                        "average_min": average_min,
                        "refresh_seconds": int(refresh) if refresh.isdigit() else 20,
                    }
                )
                with _cache_lock:
                    _cache["payload"] = None
                    _cache["fetched_at"] = 0.0
                    _master_cache["payload"] = None
                    _master_cache["fetched_at"] = 0.0
                cfg = load_config()
                message = f"Saved. Spreadsheet ID {sheet_id[:8]}… "
                try:
                    data = get_sheet_data(force=True)
                    message += (
                        f"• {data['row_count']} response rows "
                        f"• {len(data['columns'])} fields detected."
                    )
                except SheetEmptyError as exc:
                    message += f"• {exc}"
                if (cfg.get("master_url") or "").strip():
                    master = get_master_data(force=True)
                    message += f" Student Master: {master['row_count']} students loaded."
            except SheetError as exc:
                error = str(exc)

    detected = None
    if not error and sheet_configured():
        try:
            data = get_sheet_data()
            detected = {
                "columns": data["columns"],
                "headers": data["headers"],
                "rows": data["row_count"],
                "skipped": data.get("skipped_rows", 0),
            }
        except SheetEmptyError:
            detected = {"columns": {}, "headers": [], "rows": 0}
        except SheetError as exc:
            error = error or str(exc)

    detected_master = None
    if (load_config().get("master_url") or "").strip():
        master, master_error = get_master_safe()
        if master:
            try:
                log = get_sheet_data()
            except SheetError:
                log = empty_log()
            marked_ids = {normalize_member_id(r["member_id"]) for r in log["records"]}
            master_ids = set(master["by_id"])
            marked = len(master_ids & marked_ids)
            detected_master = {
                "columns": master["columns"],
                "students": master["row_count"],
                "marked": marked,
                "unmarked": master["row_count"] - marked,
                "not_in_master": len(marked_ids - master_ids),
            }

    return render_template(
        "admin.html",
        config=cfg,
        message=message,
        error=error,
        master_error=master_error,
        detected=detected,
        detected_master=detected_master,
        token_required=bool(token),
        read_only=IS_VERCEL,
    )


# ---------------------------------------------------------------------------
# /rank - "1st / 2nd / 3rd" reveal presentation  (+ /rank/admin to set it up)
# /students/admin - a small photo library, one photo per Member ID, used to
#   auto-fill rank ceremony photos instead of re-uploading them every time.
#
# Everything here used to live in a local SQLite file. It now lives in
# Supabase (see supabase_store.py) so the ceremony and the photo library are
# the same for every visitor / every server, and survive restarts on hosts
# with an ephemeral disk (Render free plan, Vercel).
# ---------------------------------------------------------------------------
RANK_MAX_PHOTO_BYTES = 10 * 1024 * 1024
RANK_NUMBERS = (1, 2, 3)
RANK_MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
RANK_KEY = "rank_ceremony"
STUDENT_PHOTO_KEY = "student_photos"
# Photos shipped with the project: static/rank/default/rank1.png, rank2.png, rank3.png (jpg/webp/gif also work).
# They are uploaded to Supabase the first time the ceremony is set up, for any rank that has no photo yet.
RANK_DEFAULT_PHOTO_DIR = os.path.join(APP_ROOT, "static", "rank", "default")


def sniff_image(blob: bytes) -> str | None:
    """File extension for a JPEG / PNG / WebP / GIF, judged by content (not name)."""
    if blob.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def _safe_photo_id(member_id: str) -> str:
    """Member IDs may contain '/', which would create nested storage paths -
    flatten anything that isn't a plain filename character."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", normalize_member_id(member_id))


def default_rank() -> dict:
    return {
        "title": "Rank Ceremony",
        "subtitle": "Shree Swaminarayan Gurukul International School · Gurugram",
        "order": "desc",  # desc = 3rd, 2nd, 1st (countdown) · asc = 1st, 2nd, 3rd
        "ranks": {
            str(n): {"name": "", "subtitle": "", "photo_ext": "", "photo_v": 0} for n in RANK_NUMBERS
        },
    }


def _seed_default_rank_photos(data: dict) -> None:
    """Give every rank that has no photo yet the one shipped in static/rank/default/."""
    for number in RANK_NUMBERS:
        slot = data["ranks"][str(number)]
        if slot.get("photo_ext"):
            continue  # already has a photo - never overwrite
        for ext in RANK_MIME:
            photo_path = os.path.join(RANK_DEFAULT_PHOTO_DIR, f"rank{number}.{ext}")
            try:
                with open(photo_path, "rb") as fh:
                    blob = fh.read(RANK_MAX_PHOTO_BYTES + 1)
            except OSError:
                continue
            real_ext = sniff_image(blob)
            if real_ext and len(blob) <= RANK_MAX_PHOTO_BYTES:
                try:
                    supabase_store.photo_upload(f"rank/{number}.{real_ext}", blob, RANK_MIME[real_ext])
                except SupabaseError:
                    return  # can't seed without Supabase - leave the slot photo-less
                slot["photo_ext"] = real_ext
                slot["photo_v"] = supabase_store.new_version()
                break


def load_rank() -> dict:
    """Ceremony text + which slots have a photo. Falls back to blank defaults
    if Supabase isn't configured or can't be reached."""
    try:
        stored = supabase_store.kv_get(RANK_KEY)
    except SupabaseError:
        return default_rank()
    if stored:
        data = default_rank()
        data.update({k: stored[k] for k in ("title", "subtitle", "order") if k in stored})
        for number in RANK_NUMBERS:
            slot = stored.get("ranks", {}).get(str(number))
            if slot:
                data["ranks"][str(number)] = {
                    "name": slot.get("name", ""),
                    "subtitle": slot.get("subtitle", ""),
                    "photo_ext": slot.get("photo_ext", ""),
                    "photo_v": slot.get("photo_v", 0),
                }
        return data
    # Nothing saved yet: seed the defaults once, best-effort.
    data = default_rank()
    try:
        _seed_default_rank_photos(data)
        supabase_store.kv_set(RANK_KEY, data)
    except SupabaseError:
        pass
    return data


def save_rank(data: dict, photo_changes: dict | None = None) -> None:
    """Save text and photo changes together.

    photo_changes maps a rank number to (image_bytes, ext) to set a photo, or
    to None to remove it. Ranks not listed keep whatever photo they already have.
    """
    for number, change in (photo_changes or {}).items():
        slot = data["ranks"][str(number)]
        old_ext = slot.get("photo_ext", "")
        if change is None:
            if old_ext:
                try:
                    supabase_store.photo_delete(f"rank/{number}.{old_ext}")
                except SupabaseError:
                    pass
            slot["photo_ext"], slot["photo_v"] = "", supabase_store.new_version()
        else:
            blob, ext = change
            if old_ext and old_ext != ext:
                try:
                    supabase_store.photo_delete(f"rank/{number}.{old_ext}")
                except SupabaseError:
                    pass
            supabase_store.photo_upload(f"rank/{number}.{ext}", blob, RANK_MIME[ext])
            slot["photo_ext"], slot["photo_v"] = ext, supabase_store.new_version()
    supabase_store.kv_set(RANK_KEY, data)


def rank_public(data: dict) -> dict:
    ranks = []
    for number in RANK_NUMBERS:
        slot = data["ranks"][str(number)]
        photo = f"/rank/photo/{number}?v={slot['photo_v']}" if slot["photo_ext"] else ""
        ranks.append(
            {"rank": number, "name": slot["name"], "subtitle": slot["subtitle"], "photo": photo}
        )
    return {
        "title": data["title"],
        "subtitle": data["subtitle"],
        "order": data["order"],
        "ranks": ranks,
    }


# ---------------------------------------------------------------------------
# Student photo library - one photo per Member ID, reused automatically when
# filling the rank ceremony from the leaderboard (so nobody re-uploads the
# same photo before every ceremony).
# ---------------------------------------------------------------------------
def load_student_photo_index() -> dict:
    try:
        return supabase_store.kv_get(STUDENT_PHOTO_KEY) or {}
    except SupabaseError:
        return {}


def get_student_photo(member_id: str) -> dict | None:
    return load_student_photo_index().get(normalize_member_id(member_id))


def set_student_photo(member_id: str, blob: bytes, ext: str) -> None:
    key = normalize_member_id(member_id)
    safe_id = _safe_photo_id(member_id)
    index = load_student_photo_index()
    old = index.get(key)
    if old and old.get("ext") and old.get("ext") != ext:
        try:
            supabase_store.photo_delete(f"students/{safe_id}.{old['ext']}")
        except SupabaseError:
            pass
    supabase_store.photo_upload(f"students/{safe_id}.{ext}", blob, RANK_MIME[ext])
    index[key] = {"ext": ext, "v": supabase_store.new_version(), "photo_id": safe_id}
    supabase_store.kv_set(STUDENT_PHOTO_KEY, index)


def remove_student_photo(member_id: str) -> None:
    key = normalize_member_id(member_id)
    index = load_student_photo_index()
    entry = index.pop(key, None)
    if entry and entry.get("ext"):
        try:
            supabase_store.photo_delete(f"students/{entry.get('photo_id', _safe_photo_id(member_id))}.{entry['ext']}")
        except SupabaseError:
            pass
    supabase_store.kv_set(STUDENT_PHOTO_KEY, index)


def student_photo_url(member_id: str) -> str:
    """Empty string if this student has no stored photo."""
    entry = get_student_photo(member_id)
    if not entry:
        return ""
    return f"/students/photo/{normalize_member_id(member_id)}?v={entry.get('v', 0)}"


def leaderboard_top3(source: str) -> list[dict]:
    """Top 3 from /leaderboard as [{'name', 'subtitle', 'member_id'}].

    'member_id' is omitted for room picks - rooms never get a ceremony photo.
    Raises SheetError if the leaderboard data can't be read.
    """
    cfg = load_config()
    if not sheet_configured():
        raise SheetError("No Google Sheet has been connected yet (see /admin).")
    data, master, _master_error = load_portal_data()
    has_room_data = bool(master) and any(rec.get("room") for rec in master["students"])
    board = compute_leaderboard(data, master, "avg" if has_room_data else "total", cfg)
    picks: list[dict] = []
    if source == "rooms":
        for room in board["rooms"][:3]:
            picks.append({
                "name": f"Room {room['room']}",
                "subtitle": f"{room['total_label']} pts · {room['students']} students",
            })
    elif source == "gstar":
        for st in board["gstar"][:3]:
            parts = [st.get("class", ""), "🏠 Hostel" if st.get("is_hostel") else "🏫 Day School", f"{st['total_label']} pts"]
            picks.append({
                "name": st.get("name") or st["member_id"],
                "subtitle": " · ".join(part for part in parts if part),
                "member_id": st["member_id"],
            })
    else:
        for st in board["disciplined"][:3]:
            parts = [st.get("class", ""), f"Room {st['room']}" if st.get("room") else "", f"{st['total_label']} pts"]
            picks.append({
                "name": st.get("name") or st["member_id"],
                "subtitle": " · ".join(part for part in parts if part),
                "member_id": st["member_id"],
            })
    return picks


@app.route("/rank")
def rank_show():
    return render_template("rank.html", config=load_config())


@app.route("/api/rank")
def api_rank():
    response = jsonify(rank_public(load_rank()))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/rank/photo/<int:number>")
def rank_photo(number: int):
    if number not in RANK_NUMBERS:
        abort(404)
    slot = load_rank()["ranks"].get(str(number))
    ext = slot.get("photo_ext") if slot else ""
    if not ext or ext not in RANK_MIME:
        abort(404)
    try:
        blob = supabase_store.photo_download(f"rank/{number}.{ext}")
    except SupabaseError:
        abort(404)
    if not blob:
        abort(404)
    response = Response(blob, mimetype=RANK_MIME[ext])
    # The URL carries ?v=<upload time>, so a new upload always gets a new URL.
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.route("/students/photo/<member_id>")
def student_photo(member_id: str):
    if not MEMBER_ID_RE.match(member_id):
        abort(404)
    entry = get_student_photo(member_id)
    ext = entry.get("ext") if entry else ""
    if not ext or ext not in RANK_MIME:
        abort(404)
    safe_id = entry.get("photo_id", _safe_photo_id(member_id))
    try:
        blob = supabase_store.photo_download(f"students/{safe_id}.{ext}")
    except SupabaseError:
        abort(404)
    if not blob:
        abort(404)
    response = Response(blob, mimetype=RANK_MIME[ext])
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.route("/rank/admin", methods=["GET", "POST"])
def rank_admin():
    cfg = load_config()
    token = admin_token()
    message = None
    errors: list[str] = []

    if not supabase_store.configured():
        errors.append(
            "Supabase is not configured, so nothing can be saved. Set SUPABASE_URL and "
            "SUPABASE_KEY (see README)."
        )

    if request.method == "POST" and supabase_store.configured():
        supplied = (request.form.get("token") or "").strip()
        if token and supplied != token:
            errors.append("Incorrect admin token.")
        elif request.form.get("action") == "fill":
            source = request.form.get("source")
            if source not in ("students", "rooms", "gstar"):
                source = "students"
            try:
                picks = leaderboard_top3(source)
            except SheetError as exc:
                errors.append(str(exc))
            else:
                data = load_rank()
                photo_changes: dict = {}
                for number in RANK_NUMBERS:
                    slot = data["ranks"][str(number)]
                    pick = picks[number - 1] if number <= len(picks) else {"name": "", "subtitle": ""}
                    slot["name"], slot["subtitle"] = pick["name"], pick.get("subtitle", "")
                    member_id = pick.get("member_id")
                    library_entry = get_student_photo(member_id) if member_id else None
                    if library_entry:
                        # Reuse the stored library photo automatically.
                        safe_id = library_entry.get("photo_id", _safe_photo_id(member_id))
                        try:
                            blob = supabase_store.photo_download(f"students/{safe_id}.{library_entry['ext']}")
                        except SupabaseError:
                            blob = None
                        if blob:
                            photo_changes[number] = (blob, library_entry["ext"])
                        else:
                            photo_changes[number] = None
                    else:
                        # No stored photo for this pick (or it's a room) - clear any old one.
                        photo_changes[number] = None
                try:
                    save_rank(data, photo_changes)
                    message = f"Filled {len(picks)} rank(s) from the leaderboard."
                    if source != "rooms":
                        message += " Photos came from the student photo library where available - upload one at /students/admin if a slot is missing its photo."
                except SupabaseError as exc:
                    errors.append(str(exc))
        else:
            data = load_rank()
            photo_changes = {}
            data["title"] = (request.form.get("title") or "").strip()[:80] or default_rank()["title"]
            data["subtitle"] = (request.form.get("subtitle") or "").strip()[:120]
            data["order"] = "asc" if request.form.get("order") == "asc" else "desc"
            for number in RANK_NUMBERS:
                slot = data["ranks"][str(number)]
                slot["name"] = (request.form.get(f"name{number}") or "").strip()[:80]
                slot["subtitle"] = (request.form.get(f"subtitle{number}") or "").strip()[:100]

                if request.form.get(f"remove{number}"):
                    photo_changes[number] = None

                upload = request.files.get(f"photo{number}")
                if upload and upload.filename:
                    blob = upload.read(RANK_MAX_PHOTO_BYTES + 1)
                    ext = sniff_image(blob)
                    if len(blob) > RANK_MAX_PHOTO_BYTES:
                        errors.append(f"Rank {number}: photo is over 10 MB.")
                    elif not ext:
                        errors.append(f"Rank {number}: use a JPG, PNG, WebP or GIF photo.")
                    else:
                        photo_changes[number] = (blob, ext)
            try:
                save_rank(data, photo_changes)
                message = "Saved."
            except SupabaseError as exc:
                errors.append(str(exc))
                message = None

    data = load_rank()
    public = rank_public(data)
    return render_template(
        "rank_admin.html",
        config=cfg,
        data=data,
        photos={r["rank"]: r["photo"] for r in public["ranks"]},
        message=message,
        errors=errors,
        token_required=bool(token),
        supabase_configured=supabase_store.configured(),
    )


@app.route("/students/admin", methods=["GET", "POST"])
def students_admin():
    cfg = load_config()
    token = admin_token()
    message = None
    errors: list[str] = []

    if not supabase_store.configured():
        errors.append(
            "Supabase is not configured, so nothing can be saved. Set SUPABASE_URL and "
            "SUPABASE_KEY (see README)."
        )

    if request.method == "POST" and supabase_store.configured():
        supplied = (request.form.get("token") or "").strip()
        if token and supplied != token:
            errors.append("Incorrect admin token.")
        else:
            member_id = (request.form.get("member_id") or "").strip()
            if not member_id or not MEMBER_ID_RE.match(member_id):
                errors.append("Enter a valid Member ID (e.g. G001).")
            elif request.form.get("action") == "remove":
                try:
                    remove_student_photo(member_id)
                    message = f"Removed the photo for {normalize_member_id(member_id)}."
                except SupabaseError as exc:
                    errors.append(str(exc))
            else:
                upload = request.files.get("photo")
                if not upload or not upload.filename:
                    errors.append("Choose a photo to upload.")
                else:
                    blob = upload.read(RANK_MAX_PHOTO_BYTES + 1)
                    ext = sniff_image(blob)
                    if len(blob) > RANK_MAX_PHOTO_BYTES:
                        errors.append("Photo is over 10 MB.")
                    elif not ext:
                        errors.append("Use a JPG, PNG, WebP or GIF photo.")
                    else:
                        try:
                            set_student_photo(member_id, blob, ext)
                            message = f"Saved the photo for {normalize_member_id(member_id)}."
                        except SupabaseError as exc:
                            errors.append(str(exc))

    index = load_student_photo_index() if supabase_store.configured() else {}
    photos = [
        {"member_id": member_id, "url": f"/students/photo/{member_id}?v={entry.get('v', 0)}"}
        for member_id, entry in sorted(index.items())
    ]
    return render_template(
        "students_admin.html",
        config=cfg,
        photos=photos,
        message=message,
        errors=errors,
        token_required=bool(token),
        supabase_configured=supabase_store.configured(),
    )




@app.errorhandler(413)
def handle_413(_exc):
    return (
        render_template(
            "error.html",
            config=load_config(),
            title="Upload too large",
            icon="📦",
            message="That upload is too big. Keep each photo under 10 MB.",
            action_url="/rank/admin",
            action_label="BACK TO RANK SETUP",
        ),
        413,
    )


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(PUBLIC_DIR, "favicon.ico")


@app.route("/robots.txt")
def robots():
    return send_from_directory(PUBLIC_DIR, "robots.txt", mimetype="text/plain")


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "configured": sheet_configured()})


@app.route("/__hmr_gate")
def hmr_gate():
    # Preview environment health-check endpoint; always answer OK.
    return "ok", 200, {"Content-Type": "text/plain"}


@app.errorhandler(404)
def handle_404(_exc):
    return (
        render_template(
            "error.html",
            config=load_config(),
            title="Page Not Found",
            icon="🧭",
            message="That page does not exist. Try opening a Member ID such as /G001.",
        ),
        404,
    )


@app.errorhandler(500)
def handle_500(_exc):  # pragma: no cover - safety net, never show a traceback
    return (
        render_template(
            "error.html",
            config=load_config(),
            title="Something went wrong",
            icon="🛠️",
            message="The portal hit an unexpected problem. Please try again in a moment.",
        ),
        500,
    )


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
