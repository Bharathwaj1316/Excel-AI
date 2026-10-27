# Excel AI Filter

Upload an Excel or CSV file and filter it column by column, or ask in plain words
("Chennai customers with sales above 10k in 2024", "top 5 by amount").
Runs fully offline: a local web page plus a small AI model through Ollama.
Your data never leaves the computer.

## Project layout

```
excel-ai-filter/
├── app.py               Web page: upload, ask box, column filters, download
├── filters.py           Reads/cleans the sheet, matches names, applies filters
├── ai.py                Turns a question into filters using the local AI (Ollama)
├── tests/
│   ├── test_filters.py  Filter checks (no AI needed)
│   └── test_ai.py       Questions answered by the AI, end to end
├── samples/
│   └── sample_data.xlsx Small file to try the app with
├── .streamlit/config.toml  Local-only, no usage tracking
├── requirements.txt
├── setup.bat            One-time setup (needs internet)
└── run.bat              Start the app
```

## Setup (once, needs internet)
1. Install Python 3.12 (python.org) and Ollama (ollama.com/download).
2. Double-click `setup.bat`. It installs the Python packages, downloads the
   AI model `qwen2.5:3b` (about 2 GB), and puts an "Excel AI Filter" icon on the desktop.
   If you move the folder later, double-click `make_shortcut.bat` to fix the icon.

## Daily use (offline)
1. Double-click the "Excel AI Filter" icon on the desktop (or `run.bat`).
   The app opens at http://localhost:8501
2. Upload your Excel/CSV, pick the sheet, and set the header row if needed.
3. Filter using the sidebar, the search box, or by asking in your own words.
4. The dashboard above the table (row count, totals, a chart per option column and
   rows per month) follows every filter you change.
5. "Group and total" merges rows by an option column (every Chennai row, whatever the
   spelling or case) and totals another column for each value you pick, plus an
   "All selected" line. The AI decides which number columns can be added up
   (seedlings, area, amount); names are counted, and IDs, years, phone numbers and
   latitude/longitude are counted, never summed.
   If the grouped column has "Others", each such row is moved to the value used by rows
   with the same district, stakeholder etc.; rows that share nothing are guessed by the
   AI. Moved rows show in brackets, e.g. "Forest (incl. 3 from Others)", and a list shows
   where every Others row went and how (similar rows or AI guess).
6. Download the filtered rows as Excel or CSV, or the dashboard as a PDF
   (summary, charts and up to 1,000 rows on A4 pages). `dashboard.py` builds both.

## How the AI part works
`ai.py` sends the column names, types and a few sample values, plus the question,
to the local model. The model can only answer with a JSON filter, and its column
names are restricted to the real ones. It never writes or runs code.
`filters.py` then checks that answer, fixing spelling and skipping anything
invalid with a note on screen, and applies it with pandas.

### Option filters
When a file is opened, `ai.py` also asks the local model which text columns hold a fixed
list of options (stakeholder, bag type, department, status, yes/no…) and which hold free
text (names, IDs, phone numbers, addresses, remarks). Each option column gets a dropdown
in the sidebar. Free-text columns get none; use the search box for those. Cells that list
several options ("Farmer, NGO") are split, so each option appears once in the dropdown.
No column names are fixed in the code. Open "Which columns have options?" in the sidebar
to add or remove columns. If Ollama is not running, a simple rule picks the columns
instead (few values that repeat a lot).

To use another model: `ollama pull llama3.2:3b`, then `set EXCEL_AI_MODEL=llama3.2:3b`
before running `run.bat`.

## Tests
```
.venv\Scripts\python.exe tests\test_filters.py
.venv\Scripts\python.exe tests\test_ai.py
```

## Requirements
Windows 10/11, 8 GB RAM, about 5 GB free disk. No graphics card needed.
