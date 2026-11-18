"""The "Review" and "Rejected" tabs of the Pipedrive (destination) sheet.

Every lead lives in the sheet, not only on the dashboard:
  - "Master leads" (the destination tab) — accepted leads; Pipedrive reads it.
  - "Review"   — leads waiting for a person, with a Decision dropdown.
  - "Rejected" — rejected leads, newest first, with the reason.

Both tabs are rewritten from the database on each sync, so they always match the
dashboard. Before rewriting Review, its Decision column is read: "Approve"
accepts the lead (it gets its AE and goes to Master leads like a dashboard
approval), "Reject" rejects it.

The tabs never fill the "Source" column: a row with Source set becomes a
Pipedrive deal, so the channel goes in a separate "Channel" column instead.
"""
from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

from . import config, db, sheets

REVIEW_TAB = "Review"
REJECTED_TAB = "Rejected"
_DECISIONS = ("Approve", "Reject")
_SOURCE = "source"


def _client_sheet():
    try:
        return sheets._client().open_by_key(config.GOOGLE_DEST_SHEET_ID)
    except sheets.SheetsError:
        raise
    except Exception as e:  # noqa: BLE001
        raise sheets.SheetsError(f"Could not open the Pipedrive sheet: {e}") from e


def _tab(sh, title: str, cols: int):
    """The tab, created (after every other tab, so it's never the first one,
    which Pipedrive reads) with a frozen header row if it doesn't exist yet."""
    try:
        return sh.worksheet(title), False
    except Exception:  # noqa: BLE001 — gspread.WorksheetNotFound
        ws = sh.add_worksheet(title=title, rows=200, cols=max(cols, 10), index=len(sh.worksheets()))
        ws.freeze(rows=1)
        return ws, True


def _when(ts) -> str:
    if not ts:
        return ""
    return _dt.datetime.fromtimestamp(ts, ZoneInfo(config.AUTO_PIPE_TZ)).strftime("%d %b %Y %H:%M")


def _fields_row(header: list[str], f: dict, hkey) -> list[str]:
    return ["" if hkey(h) == _SOURCE else f.get(hkey(h), "") for h in header]


def _write(sh, ws, rows: list[list[str]], current: list[list[str]], dropdown_col: int | None) -> bool:
    """Replace the tab's contents with rows (skipped when nothing changed)."""
    width = max(len(r) for r in rows)
    norm = lambda rs: [[str(c) for c in r] + [""] * (width - len(r)) for r in rs]
    if norm(current) == norm(rows):
        return False
    if ws.row_count < len(rows) + 1:
        ws.resize(rows=len(rows) + 100)
    if ws.col_count < width:
        ws.resize(cols=width)
    ws.clear()
    ws.update(values=rows, range_name="A1", value_input_option="RAW")
    if dropdown_col is not None:
        sh.batch_update({"requests": [{"setDataValidation": {
            "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": max(len(rows), 2),
                      "startColumnIndex": dropdown_col, "endColumnIndex": dropdown_col + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST",
                                   "values": [{"userEnteredValue": d} for d in _DECISIONS]},
                     "showCustomUi": True, "strict": True}}}]})
    return True


def sync(dest_fields, hkey, accept, reject) -> dict:
    """Apply the Review tab's decisions, then rewrite both tabs from the database.
    accept(lead) / reject(lead) change the lead exactly as the dashboard buttons do."""
    out = {"approved": 0, "rejected": 0, "review_rows": 0, "rejected_rows": 0, "written": []}
    sh = _client_sheet()
    _master, values = sheets.open_dest()
    header = [h for h in (values[0] if values else []) if h.strip()]
    if not header:
        return out                       # Master leads has no header yet: nothing to copy
    base = [h for h in header if hkey(h) != _SOURCE] + ["Channel"]

    # 1. Decisions people picked in the Review tab.
    rv_head = ["Decision", "Lead ID"] + base + ["Why it's in review", "Agent suggests"]
    rv, _new = _tab(sh, REVIEW_TAB, len(rv_head))
    rv_vals = rv.get_all_values()
    if rv_vals:
        h = [hkey(x) for x in rv_vals[0]]
        if "decision" in h and "lead id" in h:
            di, ii = h.index("decision"), h.index("lead id")
            for r in rv_vals[1:]:
                pick = (r[di] if di < len(r) else "").strip().lower()
                lid = (r[ii] if ii < len(r) else "").strip()
                if pick not in ("approve", "reject") or not lid.isdigit():
                    continue
                lead = db.get_lead(int(lid))
                if not lead or lead.get("status") != "review":
                    continue             # already decided (e.g. on the dashboard)
                if pick == "approve":
                    accept(lead)
                    out["approved"] += 1
                else:
                    reject(lead)
                    out["rejected"] += 1

    # 2. Review tab = the leads in review now.
    rows = [rv_head]
    for l in sorted(db.list_leads("review"), key=lambda x: -(x.get("created_at") or 0)):
        note = l.get("agent_note") if isinstance(l.get("agent_note"), dict) else {}
        f = dest_fields(l)
        rows.append(["", str(l["id"])] + _fields_row(base[:-1], f, hkey) + [l.get("channel") or "",
                     (note.get("reason") or l.get("reasoning") or "")[:500], note.get("suggest") or ""])
    out["review_rows"] = len(rows) - 1
    if _write(sh, rv, rows, rv_vals, dropdown_col=0):
        out["written"].append(REVIEW_TAB)

    # 3. Rejected tab = every rejected lead, newest first.
    rj_head = ["Lead ID", "Rejected on", "Why rejected"] + base
    rj, _new = _tab(sh, REJECTED_TAB, len(rj_head))
    rows = [rj_head]
    for l in sorted(db.list_leads("rejected"), key=lambda x: -(x.get("rejected_at") or x.get("updated_at") or 0)):
        f = dest_fields(l)
        rows.append([str(l["id"]), _when(l.get("rejected_at") or l.get("updated_at")),
                     (l.get("reasoning") or "")[:500]] + _fields_row(base[:-1], f, hkey) + [l.get("channel") or ""])
    out["rejected_rows"] = len(rows) - 1
    if _write(sh, rj, rows, rj.get_all_values(), dropdown_col=None):
        out["written"].append(REJECTED_TAB)
    return out
