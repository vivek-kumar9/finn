from datetime import date

from pipeline import fiscal
from pipeline.guidance import Promise, Quarter, judge, ledger, verify


def q(y, m, d, sales, op, pat):
    return Quarter(date(y, m, d), sales, op, pat, depreciation=3)


# FY26 and the first half of FY27
QUARTERS = [
    q(2025, 6, 30, 100, 15, 8), q(2025, 9, 30, 110, 16, 9), q(2025, 12, 31, 120, 18, 10), q(2026, 3, 31, 130, 20, 11),
    q(2026, 6, 30, 118, 19, 10), q(2026, 9, 30, 128, 21, 11),
]


def promise(metric, period, low, high=None, unit="pct", scope="company", segment=None):
    return Promise(metric, scope, segment, "x", period, low, high, unit, "quote", 1, "Q4 FY26", "u")


def test_fiscal_periods():
    assert fiscal.label(date(2026, 9, 30)) == "Q2 FY27"
    assert fiscal.quarters_in("H2 FY27") == [date(2026, 12, 31), date(2027, 3, 31)]
    assert fiscal.quarters_in("Q4 FY26") == [date(2026, 3, 31)]
    assert fiscal.parse("FY2027") == ("fy", 2027, None)


def test_growth_under_way_is_judged_on_ytd():
    # H1 FY27 sales 246 vs H1 FY26 210: +17.1%
    v = judge(promise("revenue_growth", "FY27", 15, 18), QUARTERS)
    assert v.status == "on_track" and v.actual.startswith("+17.1%")
    assert judge(promise("revenue_growth", "FY27", 20), QUARTERS).status == "off_track"


def test_finished_period_is_met_or_missed():
    assert judge(promise("ebitda_margin", "Q2 FY27", 16), QUARTERS).status == "met"  # 16.4% before depreciation
    assert judge(promise("ebitda_margin", "Q2 FY27", 17), QUARTERS).status == "missed"
    assert judge(promise("ebitda_margin", "Q2 FY27", 16.6), QUARTERS).status == "met"  # within half a point


def test_unreported_and_unverifiable():
    assert judge(promise("revenue_growth", "FY28", 15), QUARTERS).status == "open"
    assert judge(promise("capex", "FY27", 500, unit="crore"), QUARTERS).status == "unverifiable"


def test_revisions_are_tracked_across_calls():
    item = lambda low, high: {"metric": "revenue_growth", "scope": "company", "promise": "FY27 growth", "period": "FY27",
                              "low": low, "high": high, "unit": "pct", "quote": "q", "page": 3}
    calls = [
        {"call_period": "Q4 FY26", "url": "a", "items": [item(18, 20)]},
        {"call_period": "Q1 FY27", "url": "b", "items": [item(18, 20)]},
        {"call_period": "Q2 FY27", "url": "c", "items": [item(15, 18)]},
    ]
    [row] = ledger(calls, QUARTERS)
    assert row["low"] == 15 and row["call"] == "Q2 FY27"
    assert row["revisions"] == [{"call": "Q4 FY26", "low": 18, "high": 20, "direction": "cut"}]


def test_quote_must_exist_in_the_pdf_text():
    pages = ["", "we expect revenue growth of 15 to 18% for fy27 driven by exports", "other text"]
    assert verify("We expect revenue growth of 15 to 18% for FY27", 1, pages) == 2  # off by one is fixed
    assert verify("We expect revenue to double next year", 2, pages) is None


def test_segment_guidance_is_never_judged_against_company_totals():
    # The bug this guards: "smart metering revenue ₹700-800 cr" marked met against total revenue.
    v = judge(promise("revenue", "FY26", 700, 800, unit="crore", scope="segment", segment="Smart metering"), QUARTERS)
    assert v.status == "unverifiable" and "Smart metering" in v.basis


def test_company_and_segment_promises_stay_separate():
    company = {"metric": "revenue_growth", "scope": "company", "promise": "a", "period": "FY27",
               "low": 15, "high": 18, "unit": "pct", "quote": "q", "page": 1}
    rail = {**company, "scope": "segment", "segment": "Rail", "promise": "b", "low": 20, "high": 25}
    rows = ledger([{"call_period": "Q2 FY27", "url": "u", "items": [company, rail]}], QUARTERS)
    assert len(rows) == 2 and all(r["revisions"] == [] for r in rows)


def test_two_statements_on_one_call_are_not_a_revision():
    a = {"metric": "revenue", "scope": "segment", "segment": "Smart metering", "promise": "FY26 smart metering ₹700-800 cr",
         "period": "FY26", "low": 700, "high": 800, "unit": "crore", "quote": "q", "page": 12}
    b = {**a, "promise": "Smart metering order book ₹4,000 cr", "low": 4000, "high": None}
    rows = ledger([{"call_period": "Q3 FY26", "url": "u", "items": [b, a]}], QUARTERS)
    assert len(rows) == 2 and all(r["revisions"] == [] for r in rows)


def test_constant_currency_guidance_is_not_judged_on_rupee_revenue():
    # Infosys guides growth in constant currency; rupee growth includes currency and acquisitions.
    p = promise("revenue_growth", "FY27", 1, 2)
    p.promise, p.quote = "Revenue growth 1% to 2% in constant currency", "1% to 2% YoY in constant currency terms"
    v = judge(p, QUARTERS)
    assert v.status == "unverifiable" and "constant currency" in v.basis


def test_operating_margin_means_ebit():
    # Q2 FY27: op 21, depreciation 3, sales 128, so EBIT 14.1% and EBITDA 16.4%
    p = promise("ebitda_margin", "Q2 FY27", 15)
    p.promise, p.quote = "Operating margin of 15%", "operating margin should be about 15%"
    v = judge(p, QUARTERS)
    assert v.status == "missed" and v.actual.startswith("EBIT margin 14.1%")
    p.promise, p.quote = "EBITDA margin of 15%", "EBITDA margin around 15%"
    assert judge(p, QUARTERS).status == "met"
