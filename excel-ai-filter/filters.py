"""Filter specs: load/clean tables, resolve AI or manual filter specs against real
columns and values, and apply them to a DataFrame.

A filter spec looks like:
    {"conditions": [{"column": "City", "op": "equals", "values": ["Chennai"]}],
     "logic": "and", "sort_by": "Sales", "sort_desc": True, "limit": 10}
All condition values are strings; they are parsed per column type when applied.
"""
import difflib
import io
import re

import numpy as np
import pandas as pd

OPS = [
    "equals", "not_equals", "contains", "not_contains", "starts_with",
    "gt", "gte", "lt", "lte", "between", "is_empty", "not_empty",
]
NO_VALUE_OPS = {"is_empty", "not_empty"}

OP_LABELS = {
    "equals": "is", "not_equals": "is not", "contains": "contains",
    "not_contains": "does not contain", "starts_with": "starts with",
    "gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "between": "between",
    "is_empty": "is empty", "not_empty": "is not empty",
    "has_any": "includes", "has_none": "does not include",
}
# has_any / has_none are for cells listing several options ("Farmer, NGO"). They are set
# by resolve_spec and the option filters, never asked of the AI.

MULTI_SEP = r"\s*[,;|\n]\s*"  # separators between options inside one cell
MAX_OPTIONS = 100  # more distinct values than this is not an option list

# How a number column's name tells whether adding it up makes sense, checked in this order:
# labels and positions never ("Plant ID", "Phone Number", "Longitude"), then amounts
# ("No. of trees", "Seedlings", "Area"), then serial numbers ("Sl No", "Account No").
LABEL = re.compile(
    r"(?i)\b(id|code|phone|mobile|pin|pincode|zip|year|lat|latitude|long|lng|longitude|gps|"
    r"coordinates?)\b")
AMOUNT = re.compile(
    r"(?i)(seedling|sapling|plant|tree|bag|qty|quantit|\bcount\b|area|acre|hectare|amount|cost|"
    r"price|value|total|sales|revenue|income|expense|budget|weight|\bkg\b|tonn?e|litre|"
    r"beneficiar|member|people|persons|households|families|number of|no\.? ?of|\bnos\b)")
SERIAL = re.compile(r"(?i)\b(no|sl|s\.?no|number|age)\b")


def number_role(name: str) -> str | None:
    """"sum" or "count" when the column name makes it clear, else None (ask the AI)."""
    if LABEL.search(name):
        return "count"
    if AMOUNT.search(name):
        return "sum"
    if SERIAL.search(name):
        return "count"
    return None

_MULTIPLIERS = {
    "k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6,
    "l": 1e5, "lac": 1e5, "lakh": 1e5, "lakhs": 1e5,
    "cr": 1e7, "crore": 1e7, "crores": 1e7,
}
_DATE_LIKE = r"^\s*\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}"


class FilterError(ValueError):
    pass


# ---------- loading ----------

def list_sheets(data: bytes, filename: str) -> list[str]:
    if filename.lower().endswith(".csv"):
        return ["(csv)"]
    return pd.ExcelFile(io.BytesIO(data)).sheet_names


def load_table(data: bytes, filename: str, sheet: str, header_row: int) -> pd.DataFrame:
    if filename.lower().endswith(".csv"):
        try:
            df = pd.read_csv(io.BytesIO(data), header=header_row, encoding="utf-8-sig")
        except UnicodeDecodeError:
            df = pd.read_csv(io.BytesIO(data), header=header_row, encoding="latin-1")
    else:
        df = pd.read_excel(io.BytesIO(data), sheet_name=sheet, header=header_row)
    return clean_table(df)


def clean_table(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)

    names, seen = [], set()
    for c in df.columns:
        name = str(c).strip() or "Column"
        base, i = name, 2
        while name in seen:
            name, i = f"{base} ({i})", i + 1
        seen.add(name)
        names.append(name)
    df.columns = names

    for c in df.columns:
        if col_kind(df[c]) != "text" or pd.api.types.is_bool_dtype(df[c]):
            continue
        s = df[c].replace(r"^\s*$", np.nan, regex=True)
        nonnull = s.dropna()
        if nonnull.empty:
            df[c] = s
            continue
        # Numbers stored as text ("1,200", "₹500") -> numeric, only if every cell parses.
        nums = pd.to_numeric(
            nonnull.astype(str).str.replace(r"[,\s₹$€£%]", "", regex=True), errors="coerce")
        if nums.notna().all():
            df[c] = pd.to_numeric(
                s.astype(str).str.replace(r"[,\s₹$€£%]", "", regex=True), errors="coerce")
            continue
        # Dates stored as text ("05/03/2024") -> datetime, only if every cell parses.
        text = nonnull.astype(str)
        if text.str.match(_DATE_LIKE).all():
            dates = pd.to_datetime(text, errors="coerce", dayfirst=True, format="mixed")
            if dates.notna().all():
                df[c] = pd.to_datetime(s, errors="coerce", dayfirst=True, format="mixed")
                continue
        # Mixed-type columns break the table view; keep them as text.
        df[c] = s.map(lambda v: v if pd.isna(v) else str(v).strip())
    return df


# ---------- column types and matching ----------

def col_kind(s: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(s):
        return "text"
    if pd.api.types.is_numeric_dtype(s):
        return "number"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "date"
    return "text"


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z\u0080-￿]", "", str(text).lower())


def match_column(name: str, columns) -> str | None:
    """Find the real column for a name the user/AI gave (case, spacing, typos)."""
    columns = list(columns)
    if name in columns:
        return name
    target = _norm(name)
    if not target:
        return None
    by_norm = {_norm(c): c for c in columns}
    if target in by_norm:
        return by_norm[target]
    close = difflib.get_close_matches(target, list(by_norm), n=1, cutoff=0.6)
    if close:
        return by_norm[close[0]]
    contains = [c for n, c in by_norm.items() if n and (target in n or n in target)]
    return contains[0] if len(contains) == 1 else None


def text_values(s: pd.Series) -> pd.Series:
    return s.fillna("").astype(str).str.strip()


def _filled(s: pd.Series) -> pd.Series:
    t = text_values(s)
    return t[t != ""]


def _split(t: pd.Series) -> pd.Series:
    parts = t.str.split(MULTI_SEP, regex=True).explode().str.strip()
    return parts[parts != ""]


def is_multi_value(s: pd.Series) -> bool:
    """True when cells list several options ("Farmer, NGO") drawn from a shared set.

    Free text with commas (addresses, remarks) gives more distinct pieces than cells,
    so it is not mistaken for options."""
    if col_kind(s) != "text":
        return False
    vals = _filled(s)
    has_sep = vals.str.contains(MULTI_SEP, regex=True)
    if has_sep.sum() < 2 or has_sep.mean() < 0.1:
        return False
    return _split(vals).str.lower().nunique() < vals.str.lower().nunique()


def option_values(s: pd.Series) -> list[str]:
    """The distinct options in a column, split apart if cells hold several,
    ignoring case and spaces (the most common spelling is kept)."""
    vals = _filled(s)
    if is_multi_value(s):
        vals = _split(vals)
    best = {}
    for v in vals.value_counts().index:
        best.setdefault(v.lower(), v)
    return sorted(best.values(), key=str.lower)


def option_counts(s: pd.Series, multi: bool) -> pd.Series:
    """Rows per option, most common first (an option listed in a cell counts for that row).
    Pass `multi` from the full column: a filtered slice may no longer look multi-value."""
    vals = _filled(s)
    if multi:
        vals = _split(vals)
    low = vals.str.lower()
    spelling = vals.groupby(low).agg(lambda x: x.value_counts().index[0])
    counts = low.value_counts()
    counts.index = spelling[counts.index].values
    return counts


def looks_like_options(s: pd.Series) -> bool:
    """Simple rule used when the AI is not available: few values that repeat a lot."""
    if col_kind(s) != "text":
        return False
    vals = _filled(s)
    if vals.empty:
        return False
    pool = _split(vals) if is_multi_value(s) else vals
    distinct = pool.str.lower().nunique()
    return distinct <= MAX_OPTIONS and (distinct <= 2 or distinct / len(pool) <= 0.5)


def match_value(value: str, s: pd.Series) -> str | None:
    """Find the real cell value closest to what the user typed (case, typos)."""
    return _closest(value, pd.unique(text_values(s)))


def _closest(value: str, candidates) -> str | None:
    lowered = {}
    for u in candidates:
        if u:
            lowered.setdefault(u.lower(), u)
    v = value.strip().lower()
    if v in lowered:
        return lowered[v]
    close = difflib.get_close_matches(v, list(lowered)[:5000], n=1, cutoff=0.8)
    return lowered[close[0]] if close else None


# ---------- value parsing ----------

def parse_number(text) -> float | None:
    t = str(text).strip().lower()
    try:
        return float(t)
    except ValueError:
        pass
    t = re.sub(r"rs\.?|inr|[₹$€£%,]", "", t).strip()
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*([a-z]*)", t)
    if not m:
        return None
    n, suffix = float(m.group(1)), m.group(2)
    if suffix:
        if suffix not in _MULTIPLIERS:
            return None
        n *= _MULTIPLIERS[suffix]
    return n


def parse_date(text) -> pd.Timestamp | None:
    t = str(text).strip()
    for kwargs in ({"format": "%Y-%m-%d"}, {"dayfirst": True}):
        try:
            return pd.to_datetime(t, **kwargs)
        except (ValueError, TypeError):
            continue
    return None


# ---------- resolving a spec ----------

def resolve_spec(spec: dict, df: pd.DataFrame) -> tuple[dict, list[str]]:
    """Check a raw spec (e.g. from the AI) against the real data.

    Fixes column/value spelling, drops conditions that can't work, and returns
    (resolved spec, notes explaining every change).
    """
    notes, conditions = [], []
    for raw in spec.get("conditions") or []:
        asked = str(raw.get("column", ""))
        col = match_column(asked, df.columns)
        if col is None:
            notes.append(f"Skipped: no column like “{asked}”.")
            continue
        if col != asked:
            notes.append(f"Used column “{col}” for “{asked}”.")
        op = raw.get("op")
        if op not in OPS:
            notes.append(f"Skipped: unknown filter type “{op}”.")
            continue
        values = [str(v).strip() for v in raw.get("values") or [] if str(v).strip()]
        kind = col_kind(df[col])

        if op not in NO_VALUE_OPS and not values:
            notes.append(f"Skipped “{col}”: no value given.")
            continue
        if op == "between" and len(values) == 1:
            op = "gte"
        values = values[:2] if op == "between" else values

        if kind == "text" and op in ("equals", "not_equals"):
            multi = is_multi_value(df[col])
            pool = option_values(df[col]) if multi else pd.unique(text_values(df[col]))
            matched, missing = [], []
            for v in values:
                m = _closest(v, pool)
                (matched if m is not None else missing).append(m if m is not None else v)
                if m is not None and m != v:
                    notes.append(f"Matched “{v}” to “{m}” in {col}.")
            if not matched:
                op = "contains" if op == "equals" else "not_contains"
                notes.append(f"No exact {col} = {', '.join(values)}; searching for text containing it instead.")
            else:
                if missing:
                    notes.append(f"Ignored {', '.join(missing)}: not found in {col}.")
                values = matched
                if multi:
                    op = "has_any" if op == "equals" else "has_none"
        elif kind == "text" and op in ("gt", "gte", "lt", "lte", "between"):
            notes.append(f"Skipped “{col}”: it holds text, so it can't be compared as a number or date.")
            continue
        elif kind == "number" and op not in NO_VALUE_OPS:
            if op in ("contains", "starts_with", "not_contains"):
                op = {"contains": "equals", "starts_with": "equals", "not_contains": "not_equals"}[op]
            bad = [v for v in values if parse_number(v) is None]
            if bad:
                notes.append(f"Skipped “{col}”: {', '.join(bad)} is not a number.")
                continue
        elif kind == "date" and op not in NO_VALUE_OPS:
            if op in ("contains", "starts_with", "not_contains"):
                op = "not_equals" if op == "not_contains" else "equals"
            bad = [v for v in values if not _date_value_ok(v)]
            if bad:
                notes.append(f"Skipped “{col}”: {', '.join(bad)} is not a date.")
                continue

        conditions.append({"column": col, "op": op, "values": values})

    sort_by = spec.get("sort_by") or None
    if sort_by:
        found = match_column(sort_by, df.columns)
        if found is None:
            notes.append(f"Couldn't sort: no column like “{sort_by}”.")
        sort_by = found
    try:
        limit = int(spec.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0

    resolved = {
        "conditions": conditions,
        "logic": spec.get("logic") if spec.get("logic") in ("and", "or") else "and",
        "sort_by": sort_by,
        "sort_desc": bool(spec.get("sort_desc")),
        "limit": limit if limit > 0 else None,
    }
    return resolved, notes


def _date_value_ok(v: str) -> bool:
    return bool(re.fullmatch(r"\d{4}(-\d{1,2})?", v)) or parse_date(v) is not None


# ---------- applying ----------

def condition_mask(df: pd.DataFrame, cond: dict) -> pd.Series:
    col, op, values = cond["column"], cond["op"], cond["values"]
    s = df[col]
    kind = col_kind(s)

    if op == "is_empty":
        return s.isna() | (text_values(s) == "") if kind == "text" else s.isna()
    if op == "not_empty":
        return ~condition_mask(df, {**cond, "op": "is_empty"})

    if kind == "number":
        nums = [parse_number(v) for v in values]
        if op in ("equals", "not_equals"):
            mask = s.isin(nums)
            return ~mask if op == "not_equals" else mask
        return _compare(s, op, nums)

    if kind == "date":
        if op in ("equals", "not_equals"):
            mask = pd.Series(False, index=df.index)
            for v in values:
                if re.fullmatch(r"\d{4}", v):
                    mask |= s.dt.year == int(v)
                elif re.fullmatch(r"\d{4}-\d{1,2}", v):
                    y, m = map(int, v.split("-"))
                    mask |= (s.dt.year == y) & (s.dt.month == m)
                else:
                    mask |= s.dt.normalize() == parse_date(v).normalize()
            return ~mask if op == "not_equals" else mask
        dates = [_date_bound(v, upper=(op in ("lte", "gt") or i == 1)) for i, v in enumerate(values)]
        return _compare(s, op, dates)

    t = text_values(s).str.lower()
    vals = [v.lower() for v in values]
    if op == "equals":
        return t.isin(vals)
    if op == "not_equals":
        return ~t.isin(vals)
    if op in ("contains", "not_contains"):
        mask = pd.Series(False, index=df.index)
        for v in vals:
            mask |= t.str.contains(v, regex=False)
        return ~mask if op == "not_contains" else mask
    if op == "starts_with":
        return t.str.startswith(tuple(vals))
    if op in ("has_any", "has_none"):
        want = set(vals)
        parts = t.str.split(MULTI_SEP, regex=True)
        mask = parts.map(lambda ps: any(p.strip() in want for p in ps))
        return ~mask if op == "has_none" else mask
    raise FilterError(f"Can't use “{OP_LABELS[op]}” on text column {col}.")


def _date_bound(v: str, upper: bool) -> pd.Timestamp:
    """Year/month values cover the whole period: '2024' as an upper bound means end of 2024."""
    if re.fullmatch(r"\d{4}", v):
        start = pd.Timestamp(year=int(v), month=1, day=1)
        return start + pd.DateOffset(years=1) - pd.Timedelta(1) if upper else start
    if re.fullmatch(r"\d{4}-\d{1,2}", v):
        y, m = map(int, v.split("-"))
        start = pd.Timestamp(year=y, month=m, day=1)
        return start + pd.DateOffset(months=1) - pd.Timedelta(1) if upper else start
    d = parse_date(v).normalize()
    return d + pd.Timedelta(days=1) - pd.Timedelta(1) if upper else d


def _compare(s: pd.Series, op: str, vals: list) -> pd.Series:
    if op == "gt":
        return s > vals[0]
    if op == "gte":
        return s >= vals[0]
    if op == "lt":
        return s < vals[0]
    if op == "lte":
        return s <= vals[0]
    if op == "between":
        lo, hi = sorted(vals[:2]) if len(vals) > 1 else (vals[0], vals[0])
        return (s >= lo) & (s <= hi)
    raise FilterError(f"Unsupported filter “{op}”.")


def apply_spec(df: pd.DataFrame, spec: dict) -> pd.DataFrame:
    conds = spec.get("conditions") or []
    if conds:
        masks = [condition_mask(df, c).fillna(False) for c in conds]
        combined = masks[0]
        for m in masks[1:]:
            combined = (combined | m) if spec.get("logic") == "or" else (combined & m)
        df = df[combined]
    if spec.get("sort_by"):
        df = df.sort_values(spec["sort_by"], ascending=not spec.get("sort_desc"), na_position="last")
    if spec.get("limit"):
        df = df.head(spec["limit"])
    return df


# ---------- group and total ----------

_OTHER = re.compile(r"(?i)^\s*(others?|other\s*\(s\))\b")


def is_other(value) -> bool:
    """"Other", "Others", "Others - Fisheries", "other(s)"."""
    return bool(_OTHER.match(str(value)))


def match_others(df: pd.DataFrame, by: str, context: list[str]) -> pd.Series:
    """Guess the real `by` value for rows where it says Others, from the rows that share
    the most values in the `context` columns (same district, stakeholder, …): the most
    common `by` among them wins. Returns one entry per Others row, NA where nothing matched."""
    vals = text_values(df[by])
    other = vals.map(is_other)
    known = ~other & (vals != "")
    result = pd.Series(pd.NA, index=df.index[other], dtype=object)
    if not context or not known.any() or not other.any():
        return result
    spelling = {}
    for v in vals[known].value_counts().index:
        spelling.setdefault(v.lower(), v)
    known_by = vals[known].str.lower()
    ctx = pd.DataFrame({c: text_values(df[c]).str.lower() for c in context})
    known_ctx, other_ctx = ctx[known], ctx[other]
    # Rows with the same context values get the same answer, so work it out once per pattern.
    for pattern, idx in other_ctx.groupby(context).groups.items():
        pattern = pattern if isinstance(pattern, tuple) else (pattern,)
        score = sum((known_ctx[c] == v) & (v != "") for c, v in zip(context, pattern))
        if score.max() == 0:
            continue
        winner = known_by[score == score.max()].value_counts().index[0]
        result[idx] = spelling[winner]
    return result

def can_add_up(col: str, s: pd.Series) -> bool:
    """Rule used when the AI is not available: numbers whose name isn't an ID, phone, year,
    latitude/longitude etc."""
    return col_kind(s) == "number" and number_role(col) != "count"


def group_totals(df: pd.DataFrame, by: str, multi: bool, groups: list[str],
                 measure: str | None, how: str, from_others: pd.Series | None = None) -> pd.DataFrame:
    """One row per chosen value of `by` (spelling/case variants merged), plus a final
    "All selected" row.

    how: "sum" adds up `measure`; "count" counts its filled cells. A row whose cell lists
    several options ("Farmer, NGO") counts for each of them, but only once in the final row.
    from_others: True for rows whose `by` was filled in from "Others"; their count is
    shown in brackets after the group name.
    """
    op = "has_any" if multi else "equals"
    label = f"Total {measure}" if how == "sum" else f"Number of {measure}"
    out, any_mask = [], pd.Series(False, index=df.index)

    def line(name, mask):
        if from_others is not None and (n := int((mask & from_others).sum())):
            name = f"{name} (incl. {n} from Others)"
        row = {by: name, "Rows": int(mask.sum())}
        if measure:
            part = df.loc[mask, measure]
            row[label] = part.sum() if how == "sum" else int(
                (text_values(part) != "").sum() if col_kind(part) == "text" else part.notna().sum())
        return row

    for g in groups:
        mask = condition_mask(df, {"column": by, "op": op, "values": [g]}).fillna(False)
        any_mask |= mask
        out.append(line(g, mask))
    if len(groups) > 1:
        out.append(line("All selected", any_mask))
    return pd.DataFrame(out)


def describe_condition(cond: dict) -> str:
    op, values = cond["op"], cond["values"]
    if op in NO_VALUE_OPS:
        return f"{cond['column']} {OP_LABELS[op]}"
    if op == "between":
        return f"{cond['column']} between {values[0]} and {values[-1]}"
    joined = " or ".join(values) if len(values) <= 5 else f"{', '.join(values[:5])} (+{len(values) - 5} more)"
    return f"{cond['column']} {OP_LABELS[op]} {joined}"
