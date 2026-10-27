"""Dashboard of the filtered rows: summary numbers and charts, shown in the app and
saved as a PDF. Works for any sheet; nothing here knows the column names."""
import datetime
from pathlib import Path

import pandas as pd
from fpdf import FPDF, FontFace

from filters import col_kind, option_counts

MAX_BARS = 10          # options per chart; the rest are summed into "Other"
MAX_OPTION_CHARTS = 6
MAX_TOTALS = 3         # number columns summed in the summary row
MAX_PDF_ROWS = 1000


def summary(df: pd.DataFrame, total_rows: int, option_cols: list[str], multi_cols: set[str],
            sum_cols: list[str]) -> tuple[list[tuple[str, str]], list[tuple[str, pd.Series]]]:
    """(numbers, charts) for the filtered rows. sum_cols: number columns that can be added up.

    numbers: [(label, value)]. charts: [(title, counts indexed by label)]."""
    numbers = [("Rows shown", f"{len(df):,} of {total_rows:,}")]
    for c in sum_cols[:MAX_TOTALS]:
        numbers.append((f"Total {c}", _fmt_number(df[c].sum())))
    dates = [c for c in df.columns if col_kind(df[c]) == "date" and df[c].notna().any()]
    if dates and df[dates[0]].notna().any():
        s = df[dates[0]].dropna()
        numbers.append((dates[0], f"{s.min():%d %b %Y} to {s.max():%d %b %Y}"))

    charts = []
    for c in option_cols[:MAX_OPTION_CHARTS]:
        counts = option_counts(df[c], c in multi_cols)
        if counts.empty:
            continue
        if len(counts) > MAX_BARS:
            top = counts.iloc[:MAX_BARS - 1]
            counts = pd.concat([top, pd.Series({"Other": counts.iloc[MAX_BARS - 1:].sum()})])
        charts.append((f"Rows by {c}", counts))
    if dates:
        s = df[dates[0]].dropna()
        if not s.empty:
            months = s.dt.to_period("M").value_counts().sort_index()
            if len(months) > 24:
                months = s.dt.to_period("Y").value_counts().sort_index()
            months.index = months.index.astype(str)
            charts.append((f"Rows by {dates[0]}", months))
    return numbers, charts


def _fmt_number(x) -> str:
    x = float(x)
    return f"{x:,.0f}" if x.is_integer() or abs(x) >= 1000 else f"{x:,.2f}"


# ---------- PDF ----------

GREEN = (26, 127, 75)
INK = (33, 33, 33)
MUTED = (110, 110, 105)
LINE = (225, 224, 217)


class _Report(FPDF):
    def footer(self):
        self.set_y(-10)
        self.set_font(self.base_font, size=8)
        self.set_text_color(*MUTED)
        self.cell(0, 5, f"Page {self.page_no()}/{{nb}}", align="R")


def _setup_fonts(pdf: _Report) -> None:
    """Windows fonts so ₹ and Tamil letters print; plain Helvetica elsewhere."""
    fonts = Path(r"C:\Windows\Fonts")
    if (fonts / "arial.ttf").exists():
        pdf.add_font("Arial", "", str(fonts / "arial.ttf"))
        pdf.add_font("Arial", "B", str(fonts / "arialbd.ttf"))
        pdf.base_font = "Arial"
        if (fonts / "Nirmala.ttc").exists():
            pdf.add_font("Nirmala", "", str(fonts / "Nirmala.ttc"), collection_font_number=0)
            pdf.set_fallback_fonts(["Nirmala"], exact_match=False)
    else:
        pdf.base_font = "Helvetica"


def _text(pdf: _Report, value) -> str:
    text = "" if pd.isna(value) else str(value)
    if isinstance(value, pd.Timestamp):
        text = f"{value:%d-%m-%Y}" if value == value.normalize() else f"{value:%d-%m-%Y %H:%M}"
    if pdf.base_font == "Helvetica":
        text = text.replace("₹", "Rs.").encode("latin-1", "replace").decode("latin-1")
    return text


def _fit(pdf: _Report, text: str, width: float) -> str:
    if pdf.get_string_width(text) <= width:
        return text
    while text and pdf.get_string_width(text + "…") > width:
        text = text[:-1]
    return text + "…"


def _bar_chart(pdf: _Report, x: float, y: float, w: float, title: str, counts: pd.Series) -> float:
    """Horizontal bars with the count at each bar's end. Returns the height used."""
    pdf.set_xy(x, y)
    pdf.set_font(pdf.base_font, "B", 10)
    pdf.set_text_color(*INK)
    pdf.cell(w, 6, _fit(pdf, _text(pdf, title), w))
    label_w, count_w, row_h = w * 0.36, 14, 5.6
    bar_max = w - label_w - count_w - 2
    top = max(int(counts.max()), 1)
    pdf.set_font(pdf.base_font, size=8)
    for i, (label, n) in enumerate(counts.items()):
        ry = y + 8 + i * row_h
        pdf.set_text_color(*INK)
        pdf.set_xy(x, ry)
        pdf.cell(label_w - 1, row_h - 1, _fit(pdf, _text(pdf, label), label_w - 2), align="R")
        bw = max(bar_max * int(n) / top, 0.8)
        pdf.set_fill_color(*GREEN)
        pdf.rect(x + label_w, ry + 0.6, bw, row_h - 2.2, style="F")
        pdf.set_text_color(*MUTED)
        pdf.set_xy(x + label_w + bw + 1, ry)
        pdf.cell(count_w, row_h - 1, f"{int(n):,}")
    return 8 + len(counts) * row_h + 4


def pdf_report(df: pd.DataFrame, file_name: str, numbers, charts, filters_used: list[str]) -> bytes:
    pdf = _Report(orientation="L", format="A4")
    pdf.set_auto_page_break(True, margin=14)
    pdf.set_margins(12, 12, 12)
    _setup_fonts(pdf)
    pdf.add_page()
    page_w = pdf.w - 24

    pdf.set_font(pdf.base_font, "B", 16)
    pdf.set_text_color(*GREEN)
    pdf.cell(0, 9, "Excel AI Filter report", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(pdf.base_font, size=9)
    pdf.set_text_color(*MUTED)
    made = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
    pdf.cell(0, 5, _text(pdf, f"File: {file_name}   ·   Made on {made}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)
    pdf.multi_cell(0, 5, _text(pdf, "Filters: " + ("; ".join(filters_used) or "none (all rows)")),
                   new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # Summary boxes
    box_w = (page_w - 4 * (len(numbers) - 1)) / len(numbers)
    y = pdf.get_y()
    for i, (label, value) in enumerate(numbers):
        x = 12 + i * (box_w + 4)
        pdf.set_draw_color(*LINE)
        pdf.rect(x, y, box_w, 17)
        pdf.set_xy(x + 3, y + 2)
        pdf.set_font(pdf.base_font, size=8)
        pdf.set_text_color(*MUTED)
        pdf.cell(box_w - 6, 5, _fit(pdf, _text(pdf, label), box_w - 6))
        pdf.set_xy(x + 3, y + 8)
        pdf.set_font(pdf.base_font, "B", 12)
        pdf.set_text_color(*INK)
        pdf.cell(box_w - 6, 7, _fit(pdf, _text(pdf, value), box_w - 6))
    pdf.set_y(y + 23)

    # Charts, two per row
    col_w = (page_w - 10) / 2
    for i in range(0, len(charts), 2):
        pair = charts[i:i + 2]
        need = max(8 + len(c) * 5.6 + 4 for _, c in pair)
        if pdf.get_y() + need > pdf.h - 14:
            pdf.add_page()
        y = pdf.get_y()
        used = [_bar_chart(pdf, 12 + j * (col_w + 10), y, col_w, t, c) for j, (t, c) in enumerate(pair)]
        pdf.set_y(y + max(used))

    # Rows
    pdf.add_page()
    pdf.set_font(pdf.base_font, "B", 12)
    pdf.set_text_color(*INK)
    shown = df.head(MAX_PDF_ROWS)
    title = f"Filtered rows ({len(df):,})"
    if len(df) > MAX_PDF_ROWS:
        title += f", first {MAX_PDF_ROWS:,} shown; download Excel for all"
    pdf.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
    if not shown.empty:
        size = 8 if len(shown.columns) <= 8 else 7 if len(shown.columns) <= 14 else 6
        pdf.set_font(pdf.base_font, size=size)
        pdf.set_fill_color(255, 255, 255)  # the bars left it green
        pdf.set_draw_color(*LINE)
        heading = FontFace(emphasis="BOLD", color=INK, fill_color=(220, 238, 226))
        with pdf.table(line_height=size * 0.5, padding=1, first_row_as_headings=True,
                       headings_style=heading, borders_layout="HORIZONTAL_LINES",
                       text_align="LEFT") as table:
            table.row([_text(pdf, c) for c in shown.columns])
            for row in shown.itertuples(index=False):
                table.row([_text(pdf, v) for v in row])
    return bytes(pdf.output())
