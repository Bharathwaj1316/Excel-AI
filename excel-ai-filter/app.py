"""Excel AI Filter: upload a spreadsheet, filter it column by column or by asking
in plain words. Runs fully offline (Streamlit UI + local Ollama model)."""
import hashlib
import io

import altair as alt
import pandas as pd
import streamlit as st

import ai
from dashboard import pdf_report, summary
from filters import (
    FilterError, apply_spec, can_add_up, col_kind, describe_condition, group_totals,
    is_multi_value, is_other, list_sheets, load_table, looks_like_options, match_others,
    option_values, resolve_spec, text_values,
)

MAX_PICKLIST = 500  # never offer a dropdown with more choices than this

st.set_page_config(page_title="Excel AI Filter", page_icon="📊", layout="wide")


@st.cache_data(show_spinner="Reading file…")
def cached_sheets(data: bytes, name: str) -> list[str]:
    return list_sheets(data, name)


@st.cache_data(show_spinner="Reading file…")
def cached_table(data: bytes, name: str, sheet: str, header_row: int) -> pd.DataFrame:
    return load_table(data, name, sheet, header_row)


@st.cache_data(show_spinner="The offline AI is looking at your columns…")
def cached_column_roles(key: str, ai_ready: bool, _df: pd.DataFrame) -> tuple[list[str], list[str], str]:
    """(columns holding a fixed list of options, number columns that can be added up,
    a note saying who picked them)."""
    guess = [c for c in _df.columns if looks_like_options(_df[c])]
    sums = [c for c in _df.columns if can_add_up(c, _df[c])]
    if not ai_ready:
        return guess, sums, "Picked by a simple rule because the offline AI is not running."
    try:
        roles = ai.classify_columns(_df)
    except ai.AIUnavailable:
        return guess, sums, "Picked by a simple rule because the offline AI couldn't answer."
    picked = [c for c in _df.columns
              if roles.get(c, "options" if c in guess else "free") == "options"
              and len(option_values(_df[c])) <= MAX_PICKLIST]
    sums = [c for c in _df.columns if roles.get(c, "sum" if c in sums else "") == "sum"]
    return picked, sums, "Picked by the offline AI."


def to_excel(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Filtered")
    return buf.getvalue()


def option_filter_widgets(df: pd.DataFrame, key: str, detected: list[str],
                          source: str) -> tuple[list[dict], list[str]]:
    """A dropdown per column that holds a fixed list of options (found by the AI).
    Free-text columns (names, IDs, remarks) get no dropdown.
    Returns (conditions, all ANDed; the option columns)."""
    text_cols = [c for c in df.columns if col_kind(df[c]) == "text" and df[c].notna().any()]
    if not text_cols:
        return [], []
    st.sidebar.header("Filter by option")
    box = st.sidebar.container()
    with st.sidebar.expander("Which columns have options?"):
        st.caption(source + " Add or remove columns here.")
        cols = st.multiselect("Option columns", text_cols, default=detected, key=f"optcols-{key}")
    if not cols:
        box.caption("No option columns found. Use the search box or ask in your own words.")
    conds = []
    for col in cols:
        picked = box.multiselect(col, option_values(df[col]), placeholder="All",
                                 key=f"{key}-{col}-opt")
        if picked:
            op = "has_any" if is_multi_value(df[col]) else "equals"
            conds.append({"column": col, "op": op, "values": picked})
    return conds, cols


def column_filter_widgets(df: pd.DataFrame, key: str) -> list[dict]:
    """Sidebar filters for number and date columns. Returns conditions (all ANDed)."""
    range_cols = [c for c in df.columns if col_kind(df[c]) != "text"]
    if not range_cols:
        return []
    st.sidebar.header("Filter by number or date")
    chosen = st.sidebar.multiselect("Columns to filter", range_cols, key=f"cols-{key}")
    conds = []
    for col in chosen:
        s = df[col]
        kind = col_kind(s)
        wkey = f"{key}-{col}"
        with st.sidebar.expander(col, expanded=True):
            if s.dropna().empty:
                st.caption("This column is empty.")
            elif kind == "number":
                lo, hi = float(s.min()), float(s.max())
                a, b = st.columns(2)
                frm = a.number_input("From", value=lo, key=f"{wkey}-lo")
                to = b.number_input("To", value=hi, key=f"{wkey}-hi")
                if frm > lo:
                    conds.append({"column": col, "op": "gte", "values": [repr(frm)]})
                if to < hi:
                    conds.append({"column": col, "op": "lte", "values": [repr(to)]})
            elif kind == "date":
                lo, hi = s.min().date(), s.max().date()
                picked = st.date_input("Date range", value=(lo, hi), min_value=lo,
                                       max_value=hi, key=f"{wkey}-dates")
                if isinstance(picked, (tuple, list)) and len(picked) == 2 and picked != (lo, hi):
                    conds.append({"column": col, "op": "between",
                                  "values": [picked[0].isoformat(), picked[1].isoformat()]})
            if st.checkbox("Only blank cells", key=f"{wkey}-blank"):
                conds.append({"column": col, "op": "is_empty", "values": []})
    return conds


def ask_ai_section(df: pd.DataFrame, key: str, ready: bool, message: str):
    st.subheader("Ask in your own words")
    if not ready:
        st.warning(message + "  \nThe column filters on the left still work.")

    with st.form(f"ask-{key}", clear_on_submit=False):
        question = st.text_input(
            "What do you want to see?",
            placeholder=example_question(df),
            label_visibility="collapsed",
        )
        c1, c2 = st.columns([1, 6])
        asked = c1.form_submit_button("Filter", type="primary", disabled=not ready)
        cleared = c2.form_submit_button("Clear AI filter")

    if cleared:
        st.session_state.pop(f"ai-{key}", None)
    if asked and question.strip():
        with st.spinner("Thinking offline… (the first question can take a minute while the model loads)"):
            try:
                raw = ai.question_to_spec(question.strip(), df)
                spec, notes = resolve_spec(raw, df)
                st.session_state[f"ai-{key}"] = {"question": question.strip(), "spec": spec, "notes": notes}
            except ai.AIUnavailable as e:
                st.error(str(e))

    current = st.session_state.get(f"ai-{key}")
    if current:
        spec = current["spec"]
        parts = [describe_condition(c) for c in spec["conditions"]]
        joiner = "  **OR**  " if spec["logic"] == "or" else "  **AND**  "
        summary = joiner.join(parts) if parts else "no filter"
        if spec["sort_by"]:
            order = "highest first" if spec["sort_desc"] else "lowest first"
            summary += f" · sorted by {spec['sort_by']} ({order})"
        if spec["limit"]:
            summary += f" · first {spec['limit']} rows"
        st.info(f"**“{current['question']}”** → {summary}")
        for note in current["notes"]:
            st.caption("• " + note)
        return spec
    return None


def bar_chart(counts: pd.Series, color: str) -> alt.Chart:
    data = pd.DataFrame({"label": counts.index.astype(str), "rows": counts.values})
    base = alt.Chart(data).encode(
        y=alt.Y("label:N", sort=None, title=None, axis=alt.Axis(labelLimit=180)),
        x=alt.X("rows:Q", title="Rows", axis=alt.Axis(tickMinStep=1, grid=False)),
        tooltip=[alt.Tooltip("label:N", title="Value"), alt.Tooltip("rows:Q", title="Rows", format=",")],
    )
    bars = base.mark_bar(color=color, cornerRadiusEnd=4)
    labels = base.mark_text(align="left", dx=4).encode(text=alt.Text("rows:Q", format=","))
    return (bars + labels).properties(height=max(28 * len(data), 80))


def dashboard_section(result: pd.DataFrame, df: pd.DataFrame, option_cols: list[str],
                      sum_cols: list[str], filters_used: list[str], file_name: str):
    """Summary numbers and charts for the rows currently shown; updates with every filter."""
    st.subheader("Dashboard")
    multi = {c for c in option_cols if is_multi_value(df[c])}
    numbers, charts = summary(result, len(df), option_cols, multi, sum_cols)
    with st.container(horizontal=True):
        for label, value in numbers:
            st.metric(label, value, border=True)
    if result.empty:
        st.info("No rows match the filters, so there is nothing to chart.")
    else:
        # Validated against the light and dark chart surfaces (3:1 contrast or better).
        color = "#36a463" if st.context.theme.type == "dark" else "#1a7f4b"
        grid = st.columns(2)
        for i, (title, counts) in enumerate(charts):
            with grid[i % 2].container(border=True):
                st.markdown(f"**{title}**")
                st.altair_chart(bar_chart(counts, color), alt=title)
    base = file_name.rsplit(".", 1)[0]
    st.download_button(
        "Download dashboard as PDF", lambda: pdf_report(result, file_name, numbers, charts, filters_used),
        f"{base}_report.pdf", "application/pdf", icon=":material/picture_as_pdf:", on_click="ignore",
        help="Summary, charts and the filtered rows (up to 1,000) on A4 pages.")


def group_total_section(df: pd.DataFrame, option_cols: list[str], sum_cols: list[str], key: str,
                        ai_ready: bool):
    """Merge rows by an option column (e.g. every Chennai row) and total another column:
    numbers the AI says can be added up are summed, anything else is counted.
    Always uses every row of the file; the filters don't apply here."""
    st.subheader("Group and total")
    st.caption("Uses all rows in the file. The filters above and in the sidebar don't change it.")
    if not option_cols:
        st.caption("No option columns to group by. Add one under “Which columns have options?”.")
        return

    def describe(c):
        if c in sum_cols:
            return f"{c} (add up)"
        return f"{c} (count)"

    with st.container(horizontal=True):
        by = st.selectbox("Group by", option_cols, key=f"grp-by-{key}")
        measure = st.selectbox("Total of", [c for c in df.columns if c != by], index=None,
                               format_func=describe, placeholder="Rows only",
                               key=f"grp-measure-{key}")
    multi = is_multi_value(df[by])
    from_others, moves = None, None
    if not multi and any(is_other(v) for v in option_values(df[by])):
        if st.checkbox(f"Fill in “Others” in {by} from each row's other data", value=True,
                       key=f"grp-others-{key}-{by}",
                       help="Rows with the same district, stakeholder etc. decide first; "
                            "the offline AI guesses the rest. Unplaced rows stay as Others."):
            context = tuple(c for c in option_cols if c != by)
            moves = cached_fill_others(key, by, context, ai_ready, df)
            placed = moves[moves["Now"].notna()]
            df = df.copy()
            df.loc[placed.index, by] = placed["Now"]
            from_others = pd.Series(False, index=df.index)
            from_others[placed.index] = True

    groups = st.multiselect(f"{by} to show", option_values(df[by]), placeholder="All",
                            key=f"grp-values-{key}-{by}")
    how = "sum" if measure in sum_cols else "count"
    if measure and how == "count" and col_kind(df[measure]) == "number":
        st.caption(f"{measure} holds numbers like IDs, years or map positions, so adding them up "
                   f"means nothing. Rows that have a {measure} are counted instead.")

    table = group_totals(df, by, multi, groups or option_values(df[by]), measure, how, from_others)
    if not groups:
        table = table[table["Rows"] > 0]
    st.dataframe(table, hide_index=True, alt=f"Rows grouped by {by}", column_config={
        c: st.column_config.NumberColumn(format="localized") for c in table.columns if c != by})
    if moves is not None:
        others_summary(moves, by, df)


def others_summary(moves: pd.DataFrame, by: str, df: pd.DataFrame):
    """Say where every "Others" row went, so nobody mistakes a guess for real data."""
    placed = moves[moves["Now"].notna()]
    went = ", ".join(f"{v} {n}" for v, n in placed["Now"].value_counts().items())
    left = len(moves) - len(placed)
    text = f"{len(moves):,} rows said Others in {by}."
    if went:
        text += f" Moved to: {went}."
    if left:
        text += f" {left:,} could not be placed and stay as Others."
    st.caption(text)
    with st.expander(f"Where each Others row went ({len(moves):,})"):
        shown = moves.rename(columns={"Was": f"{by} was", "Now": f"{by} now", "How": "Found by"})
        shown[f"{by} now"] = shown[f"{by} now"].fillna("Others (not placed)")
        shown.insert(0, "Row", moves.index + 1)
        rest = df.loc[moves.index].drop(columns=[by])
        st.dataframe(pd.concat([shown, rest], axis=1), hide_index=True,
                     alt=f"Others rows in {by} and where they went")


@st.cache_data(show_spinner="Working out where the Others rows belong…", max_entries=50)
def cached_fill_others(key: str, by: str, context: tuple, ai_ready: bool,
                       _df: pd.DataFrame) -> pd.DataFrame:
    """One line per row whose `by` says Others: Was, Now (NA if not placed), How."""
    now = match_others(_df, by, list(context))
    found = pd.Series("Similar rows", index=now.index).where(now.notna(), "")
    left = now.index[now.isna()]
    if ai_ready and len(left):
        choices = [v for v in option_values(_df[by]) if not is_other(v)]
        try:
            for i, guess in ai.guess_others(_df, by, list(context), left, choices).items():
                now[i], found[i] = guess, "AI guess"
        except ai.AIUnavailable:
            pass
    return pd.DataFrame({"Was": text_values(_df[by])[now.index], "Now": now, "How": found})


def example_question(df: pd.DataFrame) -> str:
    text_col = next((c for c in df.columns if col_kind(df[c]) == "text" and df[c].notna().any()), None)
    num_col = next((c for c in df.columns if col_kind(df[c]) == "number"), None)
    parts = []
    if text_col:
        parts.append(f"{text_col} is {text_values(df[text_col]).replace('', pd.NA).dropna().iloc[0]}")
    if num_col:
        parts.append(f"{num_col} above {int(df[num_col].median())}")
    return "e.g. " + (" and ".join(parts) if parts else "show rows where …")


def main():
    st.title("📊 Excel AI Filter")
    st.caption("Everything stays on this computer. Upload a file, then filter by column or just ask.")

    uploaded = st.file_uploader("Upload an Excel or CSV file", type=["xlsx", "xlsm", "xls", "csv"])
    if not uploaded:
        st.stop()
    data, name = uploaded.getvalue(), uploaded.name

    try:
        sheets = cached_sheets(data, name)
    except Exception as e:
        st.error(f"Couldn't open this file: {e}")
        st.stop()
    c1, c2 = st.columns([3, 1])
    sheet = c1.selectbox("Sheet", sheets) if len(sheets) > 1 else sheets[0]
    header_row = c2.number_input("Column names are in row", min_value=1, max_value=100, value=1)

    try:
        df = cached_table(data, name, sheet, int(header_row) - 1)
    except Exception as e:
        st.error(f"Couldn't read this sheet: {e}")
        st.stop()
    if df.empty:
        st.warning("This sheet has no data. Try another sheet or header row.")
        st.stop()

    key = f"{hashlib.md5(data).hexdigest()[:10]}-{sheet}-{header_row}"
    ready, message = ai.status()
    ai_spec = ask_ai_section(df, key, ready, message)
    detected, sum_cols, source = cached_column_roles(key, ready, df)
    option_conds, option_cols = option_filter_widgets(df, key, detected, source)
    manual = option_conds + column_filter_widgets(df, key)
    search = st.sidebar.text_input("Search all columns", key=f"search-{key}")

    filters_used = [describe_condition(c) for c in manual]
    if ai_spec:
        filters_used.insert(0, f"Asked: “{st.session_state[f'ai-{key}']['question']}”")
    if search.strip():
        filters_used.append(f"Search: “{search.strip()}”")

    result = df
    try:
        if ai_spec:
            result = apply_spec(result, {**ai_spec, "sort_by": None, "limit": None})
        if manual:
            result = apply_spec(result, {"conditions": manual, "logic": "and"})
        if search.strip():
            needle = search.strip().lower()
            hit = result.astype(str).apply(lambda c: c.str.lower().str.contains(needle, regex=False))
            result = result[hit.any(axis=1)]
        if ai_spec and (ai_spec["sort_by"] or ai_spec["limit"]):
            result = apply_spec(result, {"sort_by": ai_spec["sort_by"], "sort_desc": ai_spec["sort_desc"],
                                         "limit": ai_spec["limit"]})
    except FilterError as e:
        st.error(str(e))

    dashboard_section(result, df, option_cols, sum_cols, filters_used, name)
    group_total_section(df, option_cols, sum_cols, key, ready)

    st.subheader(f"Showing {len(result):,} of {len(df):,} rows")
    st.dataframe(result, width="stretch", hide_index=True, alt="Filtered rows")

    base = name.rsplit(".", 1)[0]
    d1, d2, _ = st.columns([1, 1, 4])
    d1.download_button("⬇ Download Excel", to_excel(result), f"{base}_filtered.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    d2.download_button("⬇ Download CSV", result.to_csv(index=False).encode("utf-8-sig"),
                       f"{base}_filtered.csv", "text/csv")


main()
