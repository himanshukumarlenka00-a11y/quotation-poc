"""Guards the money maths on a customer-facing quotation.

The export used to leave amount/GST/totals as Excel formulas; they are now
computed in Python (commit fd0d76b, because the user's Excel sits in manual
calc mode and showed the cells blank). That moved every rupee on the sheet
into our code, where a wrong figure does not crash or look broken -- it just
quietly goes out to a customer.

Reads the generated workbook back and asserts the cells. Plain asserts, no
framework:  python test_pricing.py
"""
import openpyxl

from app.export import build_company_quotation

# Column letters in the company template (see build_company_quotation).
QTY, PRICE, AMOUNT, GST_PCT, GST_VAL = "C", "J", "K", "L", "M"
FIRST_ITEM_ROW = 21


def _cells(ws, row):
    return (ws[f"{QTY}{row}"].value, ws[f"{PRICE}{row}"].value, ws[f"{AMOUNT}{row}"].value,
            ws[f"{GST_PCT}{row}"].value, ws[f"{GST_VAL}{row}"].value)


def _build(items, **quote):
    q = {"ref_no": "TEST-PRICING", "client_name": "Test"}
    q.update(quote)
    return openpyxl.load_workbook(build_company_quotation(q, items))["QUOTE"]


def item(**kw):
    base = {"product": "P", "qty": 1, "price_per_pc": 100.0, "gst_pct": 18.0,
            "model_no": "M", "brand": "B", "hsn_code": "1234", "specification": "s",
            "description": "d"}
    base.update(kw)
    return base


def test_line_amount_and_gst():
    ws = _build([item(qty=4, price_per_pc=318.0, gst_pct=18.0)])
    qty, price, amount, gst_pct, gst_val = _cells(ws, FIRST_ITEM_ROW)
    assert qty == 4, qty
    assert price == 318.0, price
    assert amount == 1272.0, f"qty*price wrong: {amount}"
    assert gst_pct == 0.18, f"GST% must be written as a decimal fraction: {gst_pct}"
    assert gst_val == 228.96, f"amount*gst wrong: {gst_val}"


def test_zero_gst_stays_zero():
    """A genuine 0% product must not silently become 18% -- this shipped once."""
    ws = _build([item(qty=2, price_per_pc=50.0, gst_pct=0.0)])
    _, _, amount, gst_pct, gst_val = _cells(ws, FIRST_ITEM_ROW)
    assert amount == 100.0, amount
    assert gst_pct == 0.0, f"0% GST flipped to {gst_pct}"
    assert gst_val == 0.0, f"0% GST produced tax of {gst_val}"


def test_values_not_formulas():
    """Manual-calc Excel renders formula cells blank, which is the bug that
    started this. Every money cell must be a number, never a '=' string."""
    ws = _build([item(qty=3, price_per_pc=99.99)])
    for v in _cells(ws, FIRST_ITEM_ROW):
        assert not (isinstance(v, str) and v.startswith("=")), f"formula leaked back in: {v}"


def test_totals_match_the_lines():
    items = [item(qty=4, price_per_pc=318.0, gst_pct=18.0),
             item(qty=2, price_per_pc=50.0, gst_pct=0.0),
             item(qty=1, price_per_pc=1010.0, gst_pct=12.0)]
    ws = _build(items)

    total_row = FIRST_ITEM_ROW + len(items) + 1   # +1 for the freight row
    gst_row, grand_row = total_row + 1, total_row + 2

    expected_amount = 4 * 318.0 + 2 * 50.0 + 1 * 1010.0            # 2382.0
    expected_gst = round(1272.0 * .18, 2) + 0.0 + round(1010.0 * .12, 2)

    assert ws[f"{AMOUNT}{total_row}"].value == expected_amount, ws[f"{AMOUNT}{total_row}"].value
    assert ws[f"{GST_VAL}{total_row}"].value == expected_gst, ws[f"{GST_VAL}{total_row}"].value
    assert ws[f"{AMOUNT}{gst_row}"].value == expected_gst
    assert ws[f"{AMOUNT}{grand_row}"].value == round(expected_amount + expected_gst, 2)


def test_freight_is_charged_and_taxed():
    items = [item(qty=1, price_per_pc=1000.0, gst_pct=18.0)]
    ws = _build(items, freight_charge=500.0)

    freight_row = FIRST_ITEM_ROW + len(items)
    total_row = freight_row + 1
    grand_row = total_row + 2

    assert ws[f"{AMOUNT}{freight_row}"].value == 500.0, "freight missing from the sheet"
    # freight GST uses the template's own rate on that row
    freight_gst = ws[f"{GST_VAL}{freight_row}"].value
    assert ws[f"{AMOUNT}{total_row}"].value == 1500.0, "freight not added to the total"
    assert ws[f"{AMOUNT}{grand_row}"].value == round(1500.0 + 180.0 + freight_gst, 2)


def test_rounding_holds_over_many_lines():
    """0.01 drift compounding across a large BOQ would be invisible per line."""
    items = [item(qty=3, price_per_pc=33.33, gst_pct=18.0) for _ in range(100)]
    ws = _build(items)
    total_row = FIRST_ITEM_ROW + len(items) + 1

    line_amount = round(3 * 33.33, 2)                     # 99.99
    assert ws[f"{AMOUNT}{total_row}"].value == round(line_amount * 100, 2)
    assert ws[f"{GST_VAL}{total_row}"].value == round(round(line_amount * .18, 2) * 100, 2)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("\npricing maths OK")
