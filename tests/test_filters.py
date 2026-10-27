"""Checks for the filter engine (no AI needed). Run:  .venv\\Scripts\\python.exe tests\\test_filters.py
Also writes samples\\sample_data.xlsx to try in the app."""
import io
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from filters import (  # noqa: E402
    apply_spec, can_add_up, clean_table, group_totals, is_multi_value, is_other, load_table,
    looks_like_options, match_others, option_values, resolve_spec,
)

raw = pd.DataFrame({
    "Customer Name": ["Arun", "Priya", "Karthik", "Divya", "Suresh", "Meena"],
    "City": ["Chennai", "Madurai", "Chennai", "Coimbatore", "Trichy", None],
    "Sales Amount": ["12,500", "₹4,000", "48000", "9,999", "150000", "700"],
    "Order Date": ["05/01/2024", "17/02/2024", "28/02/2024", "03/03/2025", "15/07/2025", "01/08/2025"],
    "Paid": ["Yes", "No", "Yes", "Yes", "No", "yes "],
})
buf = io.BytesIO()
raw.to_excel(buf, index=False)
raw.to_excel(ROOT / "samples" / "sample_data.xlsx", index=False)

df = load_table(buf.getvalue(), "x.xlsx", "Sheet1", 0)
assert str(df["Sales Amount"].dtype).startswith(("int", "float")), df.dtypes
assert pd.api.types.is_datetime64_any_dtype(df["Order Date"]), df.dtypes
assert df.loc[1, "Order Date"] == pd.Timestamp("2024-02-17")  # day-first


def run(spec):
    resolved, notes = resolve_spec(spec, df)
    return apply_spec(df, resolved), resolved, notes


def names(out):
    return sorted(out["Customer Name"])


# Typo in column and value names gets matched
out, _, notes = run({"conditions": [{"column": "city name", "op": "equals", "values": ["chenai"]}]})
assert names(out) == ["Arun", "Karthik"], (names(out), notes)

# Numbers with k / lakh, between
out, *_ = run({"conditions": [{"column": "Sales Amount", "op": "between", "values": ["5k", "1 lakh"]}]})
assert names(out) == ["Arun", "Divya", "Karthik"], names(out)

# Whole year / month dates
out, *_ = run({"conditions": [{"column": "Order Date", "op": "equals", "values": ["2024-02"]}]})
assert names(out) == ["Karthik", "Priya"], names(out)
out, *_ = run({"conditions": [{"column": "Order Date", "op": "lte", "values": ["2024"]}]})
assert names(out) == ["Arun", "Karthik", "Priya"], names(out)

# OR logic, case/space-insensitive text, empty cells
out, *_ = run({"logic": "or", "conditions": [
    {"column": "Paid", "op": "equals", "values": ["YES"]},
    {"column": "City", "op": "is_empty", "values": []}]})
assert names(out) == ["Arun", "Divya", "Karthik", "Meena"], names(out)

# Top N
out, *_ = run({"conditions": [], "sort_by": "sales", "sort_desc": True, "limit": 2})
assert list(out["Customer Name"]) == ["Suresh", "Karthik"], list(out["Customer Name"])

# Unknown value falls back to contains; bad things are skipped with a note, not a crash
out, _, notes = run({"conditions": [{"column": "City", "op": "equals", "values": ["bato"]}]})
assert names(out) == ["Divya"] and notes, (names(out), notes)
_, resolved, notes = run({"conditions": [
    {"column": "Nonexistent", "op": "equals", "values": ["x"]},
    {"column": "City", "op": "gt", "values": ["5"]},
    {"column": "Sales Amount", "op": "gt", "values": ["lots"]}]})
assert resolved["conditions"] == [] and len(notes) == 3, notes

# Mixed-type column stays usable as text
mixed = clean_table(pd.DataFrame({"Code": [101, "A-7", None]}))
assert list(mixed["Code"].dropna()) == ["101", "A-7"]

# Option columns: single choice, several choices per cell, and free text
opts = clean_table(pd.DataFrame({
    "Beneficiary": ["Ravi", "Lakshmi", "Anand", "Selvi", "Gopal", "Rani", "Mani", "Usha"],
    "Bag Type": ["Jute", "Cloth", "jute ", "Cloth", "Paper", "Jute", "Cloth", "Paper"],
    "Stakeholders": ["Farmer, NGO", "NGO", "Farmer", "Shop; NGO", "Farmer, Shop", "NGO",
                     "Farmer", "Shop"],
    "Address": ["12, Anna Nagar, Chennai", "4, Gandhi St, Madurai", "7, Main Rd, Salem",
                "9, Lake View, Trichy", "2, North St, Erode", "3, West St, Vellore",
                "8, Bazaar St, Karur", "1, East Rd, Hosur"],
}))
assert looks_like_options(opts["Bag Type"]) and looks_like_options(opts["Stakeholders"])
assert not looks_like_options(opts["Beneficiary"]) and not looks_like_options(opts["Address"])
assert is_multi_value(opts["Stakeholders"]) and not is_multi_value(opts["Address"])
assert option_values(opts["Bag Type"]) == ["Cloth", "Jute", "Paper"], option_values(opts["Bag Type"])
assert option_values(opts["Stakeholders"]) == ["Farmer", "NGO", "Shop"], option_values(opts["Stakeholders"])

out = apply_spec(opts, {"conditions": [{"column": "Stakeholders", "op": "has_any", "values": ["Shop"]}]})
assert sorted(out["Beneficiary"]) == ["Gopal", "Selvi", "Usha"], sorted(out["Beneficiary"])
# The AI saying "equals NGO" on a multi-option column matches cells that include NGO
spec, notes = resolve_spec({"conditions": [{"column": "stakeholder", "op": "equals", "values": ["ngo"]}]}, opts)
out = apply_spec(opts, spec)
assert sorted(out["Beneficiary"]) == ["Lakshmi", "Rani", "Ravi", "Selvi"], (sorted(out["Beneficiary"]), notes)

# Group and total: spelling variants merge, numbers add up, names and map points are counted
grp = clean_table(pd.DataFrame({
    "District": ["Chennai", "chennai ", "Kancheepuram", "Madurai", "CHENNAI", "Kancheepuram"],
    "Farmer Name": ["Ravi", "Lakshmi", None, "Selvi", "Gopal", "Rani"],
    "Seedlings": [100, 250, 40, 75, 5, 60],
    "Longitude": [80.27, 80.21, 79.70, 78.12, 80.25, 79.71],
}))
assert can_add_up("Seedlings", grp["Seedlings"]) and not can_add_up("Longitude", grp["Longitude"])
assert option_values(grp["District"]) == ["Chennai", "Kancheepuram", "Madurai"], option_values(grp["District"])
t = group_totals(grp, "District", False, ["Chennai", "Kancheepuram"], "Seedlings", "sum")
assert t.to_dict("records") == [
    {"District": "Chennai", "Rows": 3, "Total Seedlings": 355},
    {"District": "Kancheepuram", "Rows": 2, "Total Seedlings": 100},
    {"District": "All selected", "Rows": 5, "Total Seedlings": 455}], t
t = group_totals(grp, "District", False, ["Kancheepuram"], "Farmer Name", "count")
assert t.to_dict("records") == [{"District": "Kancheepuram", "Rows": 2, "Number of Farmer Name": 1}], t
t = group_totals(grp, "District", False, ["Chennai"], "Longitude", "count")
assert t["Number of Longitude"].tolist() == [3], t
# A cell listing two options counts for both, but once in "All selected"
t = group_totals(opts, "Stakeholders", True, ["Farmer", "NGO"], None, "count")
assert t["Rows"].tolist() == [4, 4, 7], t

# "Others" rows take the department of rows that share their district and stakeholder
dept = clean_table(pd.DataFrame({
    "Department": ["Forest", "Forest", "Revenue", "Revenue", "Others", "Other", "others - misc", "Others"],
    "District": ["Namakkal", "Namakkal", "Salem", "Salem", "Namakkal", "Salem", "Salem", "Ooty"],
    "Stakeholder": ["Farmer", "NGO", "Farmer", "Shop", "Farmer", "Shop", "Farmer", "School"],
    "Seedlings": [10, 20, 30, 40, 5, 6, 7, 8],
}))
assert [is_other(v) for v in dept["Department"]] == [False] * 4 + [True] * 4
moved = match_others(dept, "Department", ["District", "Stakeholder"])
assert moved[[4, 5, 6]].tolist() == ["Forest", "Revenue", "Revenue"] and pd.isna(moved[7]), moved
filled = dept.copy()
filled.loc[[4, 5, 6], "Department"] = moved[[4, 5, 6]]
flag = pd.Series(False, index=dept.index)
flag[[4, 5, 6]] = True
t = group_totals(filled, "Department", False, ["Forest", "Revenue"], "Seedlings", "sum", flag)
assert t["Department"].tolist() == ["Forest (incl. 1 from Others)", "Revenue (incl. 2 from Others)",
                                    "All selected (incl. 3 from Others)"], t
assert t["Total Seedlings"].tolist() == [35, 83, 118], t

print("All filter checks passed.")
