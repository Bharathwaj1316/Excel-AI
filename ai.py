"""Turn a plain-language request into a filter spec using a local Ollama model.

Everything runs on this computer: the app talks to Ollama at localhost, and no
data leaves the machine.
"""
import datetime
import json
import os

import pandas as pd
import requests

from filters import OPS, col_kind, is_multi_value, is_other, number_role, text_values

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("EXCEL_AI_MODEL", "qwen2.5:3b")
TIMEOUT_SECONDS = 300
# One size for every call: a different num_ctx makes Ollama reload the whole model.
NUM_CTX = 8192

SYSTEM_PROMPT = """You turn a person's request about a spreadsheet into filter conditions.
Reply with JSON only, using only the columns listed.

Filter types ("op"):
- equals / not_equals: "values" are exact cell values; several values means any of them.
- contains / not_contains / starts_with: part of the text.
- gt, gte, lt, lte: one value. between: two values [low, high], inclusive.
- is_empty / not_empty: "values" is [].

Rules:
- Pick the column whose name or sample values fit the request best, even if the person
  uses other words, short forms, spelling mistakes, Tamil or Tanglish.
- Numbers: plain digits, no commas or currency (5k = 5000, 2 lakh = 200000, 1 crore = 10000000).
- Dates: YYYY-MM-DD. A whole year can be "YYYY", a whole month "YYYY-MM".
  Today is {today}; work out words like "last month" or "this year" into real dates.
- "and"/"both" -> logic "and". "or"/"either" -> logic "or". Default "and".
- "top N / highest N" -> sort_by that column, sort_desc true, limit N.
  "lowest N / bottom N" -> sort_desc false, limit N. Otherwise sort_by "" and limit 0.
- If nothing should be filtered, return an empty conditions list.

Example. Columns: "Name" (text), "Dept" (text; e.g. Sales, HR), "Salary" (number; 15000 to 90000),
"Joined" (date; 2019-01-04 to 2024-11-30).
Request: "sales people earning more than 40k who joined in 2023"
Answer: {{"conditions": [{{"column": "Dept", "op": "equals", "values": ["Sales"]}},
{{"column": "Salary", "op": "gt", "values": ["40000"]}},
{{"column": "Joined", "op": "equals", "values": ["2023"]}}],
"logic": "and", "sort_by": "", "sort_desc": false, "limit": 0}}"""

CLASSIFY_PROMPT = """You decide which spreadsheet columns hold a fixed list of options.
"options": a set of values that repeat across rows, the kind someone picks from a dropdown
filter, e.g. type, category, status, department, stakeholder, district, taluk, village,
scheme, yes/no. A cell may list several options separated by commas.
"free": values that are mostly different per row or written freely, e.g. person or company
names, IDs, phone numbers, emails, addresses, remarks, descriptions.
A column of people's names is always "free". If almost every cell is different, it is "free".
Reply with JSON only: every column name mapped to "options" or "free"."""

NUMBERS_PROMPT = """You decide which number columns of a spreadsheet can be added up.
"sum": adding them up gives a real total, e.g. quantities, seedlings, bags, trees, area,
amount, cost, number of people.
"count": the numbers are labels or positions, so a total means nothing, e.g. IDs, serial
numbers, phone numbers, pincodes, years, latitude, longitude.
Reply with JSON only: every column name mapped to "sum" or "count"."""


OTHERS_PROMPT = """Some rows of a spreadsheet are missing their "{by}".
For each row, pick the {by} it belongs to: compare the row's values with what each {by}
usually looks like in this sheet (shown below) and choose the closest one. A shared
stakeholder or a nearby or similar place is enough to decide.
Answer "Others" only when the row has nothing in common with any {by}.
Reply with JSON only: each row number mapped to your answer."""

MAX_OTHER_PATTERNS = 40  # distinct Others rows sent to the AI; the rest stay "Others"
OTHERS_BATCH = 10


class AIUnavailable(RuntimeError):
    pass


def status() -> tuple[bool, str]:
    """(ready, message) for showing in the UI."""
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        r.raise_for_status()
    except requests.RequestException:
        return False, ("The offline AI (Ollama) isn't running here, so asking in your own words is "
                       "off. On your own PC, start the Ollama app and reload this page.")
    names = [m.get("name", "") for m in r.json().get("models", [])]
    if not any(n == MODEL or n.split(":")[0] == MODEL for n in names):
        return False, f"Model {MODEL} is not downloaded. Run:  ollama pull {MODEL}"
    return True, f"Offline AI ready ({MODEL})"


def describe_columns(df: pd.DataFrame) -> str:
    lines = []
    for col in df.columns:
        s = df[col].dropna()
        kind = col_kind(df[col])
        if s.empty:
            detail = "empty"
        elif kind == "number":
            detail = f"{_fmt(s.min())} to {_fmt(s.max())}"
        elif kind == "date":
            detail = f"{s.min():%Y-%m-%d} to {s.max():%Y-%m-%d}"
        else:
            counts = s.astype(str).str.strip().value_counts()
            sample = [v[:30] for v in counts.index[:15]]
            more = f" … ({len(counts)} different)" if len(counts) > 15 else ""
            detail = "e.g. " + ", ".join(sample) + more
        lines.append(f'- "{col}" ({kind}; {detail})')
    return "\n".join(lines)


def _fmt(x) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.2f}"


def _schema(columns: list[str]) -> dict:
    # Restricting "column" to the real names stops the model inventing columns.
    return {
        "type": "object",
        "properties": {
            "conditions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "column": {"type": "string", "enum": columns},
                        "op": {"type": "string", "enum": OPS},
                        "values": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["column", "op", "values"],
                },
            },
            "logic": {"type": "string", "enum": ["and", "or"]},
            "sort_by": {"type": "string", "enum": [""] + columns},
            "sort_desc": {"type": "boolean"},
            "limit": {"type": "integer"},
        },
        "required": ["conditions", "logic", "sort_by", "sort_desc", "limit"],
    }


def question_to_spec(question: str, df: pd.DataFrame) -> dict:
    columns = [str(c) for c in df.columns]
    system = SYSTEM_PROMPT.format(today=datetime.date.today().isoformat())
    user = f"Columns:\n{describe_columns(df)}\n\nRequest: {question}"
    return _chat(system, user, _schema(columns))


def classify_columns(df: pd.DataFrame) -> dict[str, str]:
    """Map each text column to "options" (pick-list filter) or "free" (names, IDs, remarks),
    and each number column to "sum" (a total makes sense) or "count" (IDs, years, map points)."""
    cols = [str(c) for c in df.columns if col_kind(df[c]) == "text" and df[c].notna().any()]
    num_cols = [str(c) for c in df.columns if col_kind(df[c]) == "number" and df[c].notna().any()]
    if not cols and not num_cols:
        return {}
    lines, mostly_unique = [], set()
    for c in cols:
        vals = text_values(df[c])
        counts = vals[vals != ""].str.lower().value_counts()
        filled = counts.sum()
        if filled >= 5 and len(counts) > 0.9 * filled and not is_multi_value(df[c]):
            mostly_unique.add(c)
        sample = ", ".join(f"{v[:30]} ({n})" for v, n in counts.head(12).items())
        lines.append(f'- "{c}": {filled} filled cells, {len(counts)} different values; '
                     f"most common: {sample}")
    # Two short questions: asked together, the small model gets the text columns wrong.
    roles = {}
    if cols:
        answer = _chat(CLASSIFY_PROMPT, "Columns:\n" + "\n".join(lines),
                       _word_schema(cols, ["options", "free"]))
        roles.update({c: answer[c] for c in cols if answer.get(c) in ("options", "free")})
    # Number columns with a clear name ("Seedlings", "Longitude") are decided by name; the
    # small model gets those wrong too often. Only unclear names go to the AI.
    roles.update({c: number_role(c) for c in num_cols if number_role(c)})
    unclear = [c for c in num_cols if not number_role(c)]
    if unclear:
        num_lines = []
        for c in unclear:
            s = df[c].dropna()
            num_lines.append(f'- "{c}": {_fmt(s.min())} to {_fmt(s.max())}, '
                             f"e.g. {', '.join(_fmt(v) for v in s.head(5))}")
        answer = _chat(NUMBERS_PROMPT, "Columns:\n" + "\n".join(num_lines),
                       _word_schema(unclear, ["sum", "count"]))
        roles.update({c: answer[c] for c in unclear if answer.get(c) in ("sum", "count")})
    # Small models sometimes call a list of names "options"; values that never repeat aren't.
    for c in mostly_unique & roles.keys():
        roles[c] = "free"
    return roles


def guess_others(df: pd.DataFrame, by: str, context: list[str], rows: pd.Index,
                 choices: list[str]) -> dict:
    """AI guess of the real `by` for the given Others rows: {row index: choice}.
    Rows the AI can't place, or beyond MAX_OTHER_PATTERNS distinct ones, are left out."""
    if rows.empty or not choices:
        return {}
    known = df[~text_values(df[by]).map(lambda v: v == "" or is_other(v))]
    profile = []
    for ch in choices[:30]:
        part = known[text_values(known[by]).str.lower() == ch.lower()]
        hints = []
        for c in context:
            top = text_values(part[c]).replace("", pd.NA).dropna().value_counts().head(3)
            if not top.empty:
                hints.append(f"{c}: {', '.join(top.index)}")
        profile.append(f'- "{ch}": ' + ("; ".join(hints) or "no examples"))
    # Identical rows get the same answer, so each distinct row is asked about once.
    # A plain "Others" is left out (it pulls the model towards answering Others), but
    # "Others - Fisheries" keeps its useful text.
    shown = [by] + context
    plain = text_values(df.loc[rows, by]).str.fullmatch(r"(?i)\s*(others?|other\s*\(s\))\s*")
    table = df.loc[rows, shown].astype(str)
    table.loc[plain, by] = ""
    patterns = table.apply(tuple, axis=1)
    distinct = list(dict.fromkeys(patterns))[:MAX_OTHER_PATTERNS]
    answers = {}
    for start in range(0, len(distinct), OTHERS_BATCH):
        batch = distinct[start:start + OTHERS_BATCH]
        lines = [f"{i + 1}. " + "; ".join(f"{c}: {v}" for c, v in zip(shown, p) if v not in ("", "nan"))
                 for i, p in enumerate(batch)]
        user = (f"What each {by} usually looks like:\n" + "\n".join(profile)
                + "\n\nRows:\n" + "\n".join(lines))
        reply = _chat(OTHERS_PROMPT.format(by=by), user,
                      _word_schema([str(i + 1) for i in range(len(batch))], choices + ["Others"]))
        for i, p in enumerate(batch):
            if reply.get(str(i + 1)) in choices:
                answers[p] = reply[str(i + 1)]
    return {i: answers[p] for i, p in patterns.items() if p in answers}


def _word_schema(columns: list[str], words: list[str]) -> dict:
    return {"type": "object", "required": columns,
            "properties": {c: {"type": "string", "enum": words} for c in columns}}


def _chat(system: str, user: str, schema: dict) -> dict:
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "format": schema,
                "stream": False,
                "keep_alive": "30m",
                "options": {"temperature": 0, "num_ctx": NUM_CTX},
            },
            timeout=TIMEOUT_SECONDS,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        raise AIUnavailable(f"Couldn't reach the offline AI: {e}") from e
    try:
        return json.loads(r.json()["message"]["content"])
    except (KeyError, json.JSONDecodeError) as e:
        raise AIUnavailable("The AI gave an answer that couldn't be read. Try rephrasing.") from e
