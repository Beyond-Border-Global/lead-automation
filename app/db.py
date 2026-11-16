"""Tiny SQLite persistence layer. JSON blobs are stored as TEXT."""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import threading
import time
from typing import Any, Optional

from .config import DB_PATH

_lock = threading.Lock()

# Columns stored as JSON text.
_JSON_FIELDS = ("agent_note", "extracted", "queries", "candidates", "chosen", "screening", "pushed_row",
                "gtm_counted", "merged_from")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


# Expected columns -> SQL type. Used to create and to migrate older DBs.
_COLUMNS = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "name": "TEXT",
    "company": "TEXT",
    "channel": "TEXT",
    "email": "TEXT",
    "phone": "TEXT",
    "linkedin": "TEXT",
    "li_optout": "INTEGER DEFAULT 0",
    "other_link": "TEXT",          # a non-LinkedIn profile link the lead provided
    "link_raw": "TEXT",            # the profile-link cell exactly as the lead typed it
    "origin": "TEXT",              # 'sheet' (pulled from the source sheet) | 'upload' (CSV)
    "heard_about": "TEXT",         # website form: "Where have you heard about us" answer
    "gtm_counted": "TEXT",         # JSON: where this lead was +1'd in the GTM report
    "dup_of": "INTEGER",           # open "possible duplicate of lead #N" flag (Duplicates page)
    "dup_reason": "TEXT",          # why it was flagged / merged (same name, same email, …)
    "merged_into": "INTEGER",      # status 'duplicate': merged into lead #N
    "prior_status": "TEXT",        # status before it was merged away (accepted / rejected / …)
    "merged_from": "TEXT",         # JSON list of lead ids merged into this one
    "visa": "TEXT",
    "lead_date": "TEXT",
    "message": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'queued'",
    "stage": "TEXT",
    "extracted": "TEXT",
    "queries": "TEXT",
    "candidates": "TEXT",
    "chosen": "TEXT",
    "confidence": "REAL",
    "reasoning": "TEXT",
    "error": "TEXT",
    "screening": "TEXT",
    "screened": "INTEGER DEFAULT 0",
    "owner": "TEXT",
    "pushed": "INTEGER DEFAULT 0",
    "pushed_row": "TEXT",          # JSON: the dest-sheet cell values we last wrote
    "sheet_row": "INTEGER",        # dest-sheet row number we last wrote to (a hint)
    "created_at": "REAL",
    "updated_at": "REAL",
    "rejected_at": "REAL",         # when a reviewer rejected it (Leads page date filter)
    "assigned_at": "REAL",         # when the current AE (owner) was given the lead
    "agent_note": "TEXT",          # JSON: the review agent's suggestion for the reviewer
}


def init_db() -> None:
    cols_sql = ",\n                ".join(f"{c} {t}" for c, t in _COLUMNS.items())
    with _conn() as conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS leads (\n                {cols_sql}\n            )")
        # Migrate older databases: add any columns introduced after they were made.
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(leads)")}
        for col, decl in _COLUMNS.items():
            if col not in existing:
                # ALTER can't add PRIMARY KEY / NOT NULL-without-default; strip those.
                simple = decl.replace("PRIMARY KEY AUTOINCREMENT", "") \
                             .replace("NOT NULL", "").strip()
                conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {simple}")
        # Leads rejected before rejected_at existed: nothing updates them after that.
        conn.execute("UPDATE leads SET rejected_at = updated_at WHERE status = 'rejected' AND rejected_at IS NULL")
        # Leads given an AE before assigned_at existed: date them by the lead's own
        # day (what the AE Workload chart used then), at noon UTC so the browser's
        # local day is the same one anywhere from UTC-11 to UTC+11.
        old = conn.execute("SELECT id, lead_date, created_at FROM leads "
                           "WHERE owner IS NOT NULL AND owner != '' AND assigned_at IS NULL").fetchall()
        for r in old:
            d = _lead_day(r)
            noon = _dt.datetime(d.year, d.month, d.day, 12, tzinfo=_dt.timezone.utc).timestamp()
            conn.execute("UPDATE leads SET assigned_at = ? WHERE id = ?", (noon, r["id"]))
        # Small key/value table for round-robin counters etc.
        conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        # Dedup store: keys of source rows we've already ingested (or baselined).
        conn.execute("CREATE TABLE IF NOT EXISTS seen_leads (k TEXT PRIMARY KEY)")


def seen_keys() -> set:
    """All keys of source rows already ingested (or baselined). Keys are per
    channel, so the same person arriving on another channel still comes in —
    and is then merged as a duplicate rather than silently dropped."""
    with _conn() as conn:
        return {r["k"] for r in conn.execute("SELECT k FROM seen_leads")}


def mark_seen(keys: list[str]) -> None:
    keys = [k for k in keys if k]
    if not keys:
        return
    with _lock, _conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO seen_leads (k) VALUES (?)", [(k,) for k in keys])


def count_seen() -> int:
    with _conn() as conn:
        return int(conn.execute("SELECT COUNT(*) c FROM seen_leads").fetchone()["c"])


def meta_get(k: str, default: str = "") -> str:
    with _conn() as conn:
        row = conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return row["v"] if row else default


def meta_set(k: str, v: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("INSERT INTO meta (k, v) VALUES (?, ?) "
                     "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))


def relabel_channel(old: str, new: str) -> int:
    with _lock, _conn() as conn:
        return conn.execute("UPDATE leads SET channel = ? WHERE channel = ?", (new, old)).rowcount


def requeue_interrupted() -> int:
    """Leads left mid-pipeline by a restart/deploy go back to the queue."""
    with _lock, _conn() as conn:
        return conn.execute("UPDATE leads SET status = 'queued', stage = NULL "
                            "WHERE status = 'processing'").rowcount


def next_rr(name: str, n: int) -> int:
    """Return the next round-robin index for `name` (0..n-1) and advance it."""
    if n <= 0:
        return 0
    key = f"rr_{name}"
    with _lock, _conn() as conn:
        row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        cur = int(row["v"]) if row and str(row["v"]).isdigit() else 0
        conn.execute(
            "INSERT INTO meta (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, str(cur + 1)))
        return cur % n


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for jf in _JSON_FIELDS:
        if d.get(jf):
            try:
                d[jf] = json.loads(d[jf])
            except (json.JSONDecodeError, TypeError):
                d[jf] = None
    return d


def insert_lead(name: str, company: str, email: str, phone: str, message: str,
                linkedin: str = "", li_optout: int = 0, other_link: str = "",
                link_raw: str = "", visa: str = "", lead_date: str = "", channel: str = "",
                origin: str = "", heard_about: str = "") -> int:
    now = time.time()
    with _lock, _conn() as conn:
        cur = conn.execute(
            """INSERT INTO leads (name, company, channel, email, phone, linkedin,
                                  li_optout, other_link, link_raw, visa, lead_date,
                                  message, origin, heard_about, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
            (name, company, channel, email, phone, linkedin, li_optout, other_link,
             link_raw, visa, lead_date, message, origin, heard_about, now, now),
        )
        return int(cur.lastrowid)


def _lead_day(r) -> _dt.date:
    """The lead's "Date of lead" (else the day LeadSearch picked it up)."""
    from .gtm import _parse_date
    picked = _dt.datetime.utcfromtimestamp(r["created_at"] or 0).date()
    d = _parse_date(r["lead_date"] or "")
    if d and d > picked + _dt.timedelta(days=1):       # day/month swapped in the source
        try:
            d = _dt.date(d.year, d.day, d.month)
        except ValueError:
            d = None
        d = d if d and d <= picked + _dt.timedelta(days=1) else picked
    return d or picked


def analytics() -> dict:
    """One compact row per lead for the dashboard charts, which filter by period
    in the browser: [day, channel, status, fit tier, owner, assigned_at]. `day` is
    the lead's "Date of lead" (else the day LeadSearch picked it up); the AE
    Workload chart uses assigned_at instead. Merged duplicates are left out —
    they're the same person as another lead."""
    with _conn() as conn:
        rows = conn.execute("SELECT channel, status, screening, owner, lead_date, created_at, assigned_at "
                            "FROM leads WHERE status != 'duplicate'").fetchall()
    out = []
    for r in rows:
        tier = None
        if r["screening"]:
            try:
                tier = (json.loads(r["screening"]) or {}).get("tier")
            except (json.JSONDecodeError, TypeError):
                tier = None
        out.append([_lead_day(r).isoformat(), r["channel"] or "Not provided", r["status"] or "?",
                    tier or "", r["owner"] or "", r["assigned_at"]])
    return {"rows": out}


def update_lead(lead_id: int, **fields: Any) -> None:
    if not fields:
        return
    for jf in _JSON_FIELDS:
        if jf in fields and not isinstance(fields[jf], (str, type(None))):
            fields[jf] = json.dumps(fields[jf], ensure_ascii=False)
    fields["updated_at"] = time.time()
    if "owner" in fields and "assigned_at" not in fields:
        if not fields["owner"]:
            fields["assigned_at"] = None
        else:
            with _conn() as conn:
                row = conn.execute("SELECT owner FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if not row or row["owner"] != fields["owner"]:     # a new AE: the clock starts now
                fields["assigned_at"] = fields["updated_at"]
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [lead_id]
    with _lock, _conn() as conn:
        conn.execute(f"UPDATE leads SET {cols} WHERE id = ?", vals)


def get_lead(lead_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return _row_to_dict(row) if row else None


def list_leads(status: Optional[str] = None) -> list[dict]:
    with _conn() as conn:
        if status and status != "all":
            rows = conn.execute(
                "SELECT * FROM leads WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM leads ORDER BY id DESC").fetchall()
        return [_row_to_dict(r) for r in rows]


def queued_lead_ids() -> list[int]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id FROM leads WHERE status = 'queued' ORDER BY id ASC"
        ).fetchall()
        return [int(r["id"]) for r in rows]


def stats() -> dict:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) c FROM leads GROUP BY status"
        ).fetchall()
    out = {"total": 0, "queued": 0, "processing": 0, "accepted": 0,
           "review": 0, "rejected": 0, "error": 0}
    for r in rows:
        out[r["status"]] = r["c"]
        out["total"] += r["c"]
    return out


def delete_lead(lead_id: int) -> bool:
    """Delete one lead. Its dedup key stays in seen_leads, so a sheet pull
    won't re-ingest it."""
    with _lock, _conn() as conn:
        return conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,)).rowcount > 0


def clear_all() -> None:
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM leads")
