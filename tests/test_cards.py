import pytest

from pipeline.cards import fiscal_quarter, results_card


def extraction(**over):
    base = {
        "basis": "consolidated", "unit": "lakh", "is_bank": False, "page": 2,
        "periods": [
            {"role": "current", "period_end": "2026-09-30", "revenue": 120000, "other_income": 2000,
             "finance_costs": 1000, "depreciation": 3000, "pbt_before_exceptional": 20000, "pbt": 20000, "pat": 15000},
            {"role": "previous_quarter", "period_end": "2026-06-30", "revenue": 100000, "other_income": 2000,
             "finance_costs": 1000, "depreciation": 3000, "pbt_before_exceptional": 15000, "pbt": 15000, "pat": 11000},
            {"role": "year_ago_quarter", "period_end": "2025-09-30", "revenue": 96000, "other_income": 1000,
             "finance_costs": 1000, "depreciation": 2500, "pbt_before_exceptional": 12000, "pbt": 12000, "pat": 9000},
        ],
    }
    base.update(over)
    return base


def test_quarter_labels_follow_indian_fiscal_year():
    assert fiscal_quarter("2026-09-30") == "Q2 FY27"
    assert fiscal_quarter("2027-03-31") == "Q4 FY27"


def test_results_grid_converts_units_and_derives_ebitda():
    card = results_card(extraction(), "r.pdf")
    rows = {r["metric"]: r for r in card["rows"]}
    assert card["period"] == "Q2 FY27" and card["ebitda_derived"]
    assert rows["Revenue"]["value"] == 1200.0          # 120000 lakh = 1200 crore
    assert rows["Revenue"]["qoq"] == 20.0 and rows["Revenue"]["yoy"] == 25.0
    assert rows["EBITDA"]["value"] == 220.0             # 200 + 10 + 30 - 20
    assert card["margins"]["EBITDA"]["now"] == pytest.approx(18.3, abs=0.1)


def test_loss_to_profit_has_no_percentage():
    x = extraction()
    x["periods"][1]["pat"] = -500
    rows = {r["metric"]: r for r in results_card(x, "r.pdf")["rows"]}
    assert rows["PAT"]["qoq"] is None and rows["PAT"]["yoy"] is not None


def test_missing_current_quarter_is_rejected():
    x = extraction()
    x["periods"] = x["periods"][1:]
    with pytest.raises(ValueError):
        results_card(x, "r.pdf")


def test_bank_uses_net_interest_income():
    x = extraction(is_bank=True, unit="crore", periods=[
        {"role": "current", "period_end": "2026-09-30", "interest_earned": 80000, "interest_expended": 50000,
         "operating_profit": 24000, "pbt": 20000, "pat": 16000},
        {"role": "previous_quarter", "period_end": "2026-06-30", "interest_earned": 78000, "interest_expended": 49000,
         "operating_profit": 23000, "pbt": 19000, "pat": 15000},
    ])
    rows = {r["metric"]: r for r in results_card(x, "r.pdf")["rows"]}
    assert rows["NII"]["value"] == 30000.0 and "EBITDA" not in rows


def test_revenue_falls_back_to_total_income_less_other_income():
    x = extraction()
    for p in x["periods"]:
        p["total_income"], p["revenue"] = p["revenue"] + p["other_income"], None
    rows = {r["metric"]: r for r in results_card(x, "r.pdf")["rows"]}
    assert rows["Revenue"]["value"] == 1200.0


def test_half_read_table_is_rejected():
    x = extraction()
    x["periods"] = [x["periods"][0]]  # current column only: no QoQ or YoY possible
    with pytest.raises(ValueError, match="incomplete"):
        results_card(x, "r.pdf")
