import copy

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


def test_cross_check_rejects_a_misread(monkeypatch):
    from pipeline import cards, screener

    card = results_card(extraction(), "r.pdf")  # revenue 1,200 cr, PAT 150 cr
    monkeypatch.setattr(screener, "quarters", lambda sym, basis: {"2026-09-30": {"sales": 1201.0, "pat": 150.0}})
    assert cards.cross_check(card, "X", "2026-09-30")["metrics"] == ["Revenue", "PAT"]
    monkeypatch.setattr(screener, "quarters", lambda sym, basis: {"2026-09-30": {"sales": 1200.0, "pat": 120.0}})
    with pytest.raises(cards.CheckFailed, match="PAT read as 150"):
        cards.cross_check(card, "X", "2026-09-30")


def test_numbers_returned_as_text_are_understood():
    x = extraction()
    for p in x["periods"]:
        p["revenue"], p["pat"] = f"{p['revenue']:,}", str(p["pat"])
    x["periods"][1]["pbt"] = "(500)"  # a loss in brackets
    rows = {r["metric"]: r for r in results_card(x, "r.pdf")["rows"]}
    assert rows["Revenue"]["value"] == 1200.0 and rows["PBT"]["qoq"] is None


STORY = {"category": "results", "lead_url": "u", "symbol": "X", "id": "X:results:1",
         "first_at": "2026-11-06T14:00:00+05:30", "last_at": "2026-11-06T14:00:00+05:30"}

SCREENER_1200 = {"2026-09-30": {"sales": 1200.0, "pat": 150.0, "pbt": 200.0},
                 "2026-06-30": {"sales": 1000.0, "pat": 110.0, "pbt": 150.0},
                 "2025-09-30": {"sales": 960.0, "pat": 90.0, "pbt": 120.0}}


def _stub(monkeypatch, reading, table, models=("a",)):
    """No network and no database: every model returns `reading`, screener returns `table`."""
    from pipeline import cards, llm, screener

    monkeypatch.setattr(cards, "download", lambda url: b"%PDF")
    monkeypatch.setattr(cards, "results_pages", lambda pdf: (b"%PDF", [3]))
    monkeypatch.setattr(cards, "_pages_text", lambda pdf: "")
    monkeypatch.setattr(cards, "RESULTS_MODELS", list(models))
    monkeypatch.setattr(cards, "results_candidates", lambda story: [])
    monkeypatch.setattr(cards, "pick_results_pdf", lambda story: ("u", b"%PDF"))
    monkeypatch.setattr(llm, "GEMINI_FALLBACKS", [])
    monkeypatch.setattr(llm, "gemini_json", lambda *a, **k: copy.deepcopy(reading))
    monkeypatch.setattr(llm, "cached", lambda key, fn, stats=None: fn())
    monkeypatch.setattr(llm, "lookup", lambda key: None)
    monkeypatch.setattr(screener, "quarters", lambda s, b: table)


def _build(monkeypatch, reading, table):
    from pipeline import cards

    _stub(monkeypatch, reading, table)
    return cards.build(dict(STORY), "X Ltd")


def values(card):
    return {r["metric"]: r["value"] for r in card["rows"]}


def test_two_models_agreeing_against_screener_is_shown_with_a_flag(monkeypatch):
    from pipeline import cards

    reading = extraction(unit="crore")  # revenue 120000 cr, PAT 15000 cr
    _stub(monkeypatch, reading, {"2026-09-30": {"sales": 110000.0, "pat": 15000.0}}, models=("a", "b"))
    card = cards.build(dict(STORY), "X Ltd")
    assert card["check"] is None
    assert any("screener differs" in c for c in card["checks"])


def test_standalone_table_read_as_consolidated_is_relabelled(monkeypatch):
    from pipeline import cards, screener

    card = results_card(extraction(), "r.pdf")  # says consolidated; revenue 1,200, PAT 150
    tables = {"consolidated": {"2026-09-30": {"sales": 1400.0, "pat": 170.0}},
              "standalone": {"2026-09-30": {"sales": 1200.0, "pat": 150.0}}}
    monkeypatch.setattr(screener, "quarters", lambda sym, basis: tables[basis])
    check = cards.cross_check(card, "TITAN", "2026-09-30")
    assert card["basis"] == "standalone" and check["relabelled"] == "consolidated → standalone"


def test_pbt_is_derived_from_pat_and_tax_when_not_printed():
    x = extraction()
    for p in x["periods"]:
        p["tax_expense"], p["pbt"], p["pbt_before_exceptional"] = p["pbt"] - p["pat"], None, None
    card = results_card(x, "r.pdf")
    assert values(card)["PBT"] == 200.0 and card["sourced"]["PBT"] == "derived"


def test_missing_pbt_is_rejected_without_a_fill():
    x = extraction()
    for p in x["periods"]:
        p["pbt"], p["pbt_before_exceptional"] = None, None
    with pytest.raises(ValueError, match="PBT missing"):
        results_card(x, "r.pdf")


def test_unread_pbt_comes_from_screener_only_after_revenue_and_pat_match(monkeypatch):
    reading = extraction(unit="crore")
    for p in reading["periods"]:
        p["pbt"] = p["pbt_before_exceptional"] = p["finance_costs"] = None
    table = {"2026-09-30": {"sales": 120000.0, "pat": 15000.0, "pbt": 20000.0, "op_profit": 22000.0},
             "2026-06-30": {"sales": 100000.0, "pat": 11000.0, "pbt": 15000.0, "op_profit": 17000.0},
             "2025-09-30": {"sales": 96000.0, "pat": 9000.0, "pbt": 12000.0, "op_profit": 13500.0}}
    card = _build(monkeypatch, reading, table)
    rows = {r["metric"]: r for r in card["rows"]}
    assert rows["PBT"]["value"] == 20000.0 and rows["PBT"]["qoq"] == pytest.approx(33.3, abs=0.1)
    assert card["sourced"] == {"PBT": "screener", "EBITDA": "screener"} and not card["ebitda_derived"]

    table["2026-09-30"]["pat"] = 14000.0  # PAT disagrees: nothing is borrowed and the card fails
    with pytest.raises(ValueError, match="PAT read as"):
        _build(monkeypatch, reading, table)


def test_unit_names_are_understood():
    x = extraction(unit="Rupees")
    for p in x["periods"]:
        for key in ("revenue", "pbt", "pat"):
            p[key] *= 100_000  # the lakh fixture, in rupees
    assert values(results_card(x, "r.pdf"))["Revenue"] == 1200.0


def test_unstated_unit_is_the_scale_that_matches_screener(monkeypatch):
    card = _build(monkeypatch, extraction(unit="none"), SCREENER_1200)
    assert values(card)["Revenue"] == 1200.0
    assert any("unit not stated" in c.lower() for c in card["checks"])


def test_total_income_taken_for_revenue_is_repaired(monkeypatch):
    reading = extraction()
    for p in reading["periods"]:
        p["other_income"] = 7000  # about 6%, like CDSL's 417 cr against 394 cr
        p["total_income"] = p["revenue"] = p["revenue"] + p["other_income"]
    card = _build(monkeypatch, reading, SCREENER_1200)
    assert values(card)["Revenue"] == 1200.0
    assert any("total income for revenue" in c for c in card["checks"])


def test_statement_beats_press_release_and_deck():
    from pipeline.cards import statement_strength
    from tests.test_results_pages import pdf

    press = pdf(["Media release\nTotal Income 417 341 22%\nNet profit 173 118 47%"])
    deck = pdf(["Earnings presentation\nRevenue growth 53% YoY\nEBITDA margin\nPAT margin"])
    statement = pdf(["Statement of consolidated results\nQuarter ended 30.09.2026\nRevenue from operations\n"
                     "Other income\nTotal expenses\nFinance costs\nDepreciation\nProfit before tax\n"
                     "Tax expense\nEarnings per share"])
    assert statement_strength(statement) >= 7 > statement_strength(press) and statement_strength(deck) < 3


def test_misstated_unit_is_corrected_only_when_both_lines_match(monkeypatch):
    reading = extraction(unit="crore")  # the lakh fixture labelled crore: 100x too big
    card = _build(monkeypatch, reading, SCREENER_1200)
    assert values(card)["Revenue"] == 1200.0
    assert any("took the table to be in crore" in c for c in card["checks"])

    wrong_pat = {k: dict(v, pat=v["pat"] * 2) for k, v in SCREENER_1200.items()}
    with pytest.raises(ValueError, match="Revenue read as 120,000"):
        _build(monkeypatch, extraction(unit="crore"), wrong_pat)


def test_stored_reading_is_used_from_any_of_the_storys_pdfs(monkeypatch):
    """A regrouped story with a new lead still finds the reading stored for its statement PDF."""
    from pipeline import cards, llm

    table = {k: dict(v, op_profit=v["pbt"] + 20) for k, v in SCREENER_1200.items()}
    _stub(monkeypatch, extraction(), table)
    old = extraction()  # stored before the checks existed: no _check, and no finance costs
    for p in old["periods"]:
        p["finance_costs"] = None
    store = {"statement.pdf": old}
    reads = []
    monkeypatch.setattr(cards, "results_candidates", lambda story: ["statement.pdf"])
    monkeypatch.setattr(llm, "lookup", lambda key: copy.deepcopy(store.get(key[0])))
    monkeypatch.setattr(llm, "store", lambda key, value: store.update({key[0]: value}))
    monkeypatch.setattr(llm, "forget", lambda key: store.pop(key[0], None))
    monkeypatch.setattr(llm, "gemini_json", lambda *a, **k: reads.append(1) or copy.deepcopy(extraction()))

    card = cards.build(dict(STORY, lead_url="press-release.pdf"), "X Ltd")
    assert not reads and card["source"]["url"] == "statement.pdf"
    assert values(card)["EBITDA"] == 220.0 and card["sourced"] == {"EBITDA": "screener"}
    assert store["statement.pdf"]["_check"]["metrics"] == ["Revenue", "PAT"]


def test_stored_reading_that_fails_todays_checks_is_read_again(monkeypatch):
    from pipeline import cards, llm

    _stub(monkeypatch, extraction(), SCREENER_1200)
    wrong = extraction()
    for p in wrong["periods"]:
        p["pat"] *= 0.8  # screener has 150, this says 120
    store = {"u": wrong}
    reads = []
    monkeypatch.setattr(llm, "lookup", lambda key: copy.deepcopy(store.get(key[0])))
    monkeypatch.setattr(llm, "store", lambda key, value: store.update({key[0]: value}))
    monkeypatch.setattr(llm, "forget", lambda key: store.pop(key[0], None))
    monkeypatch.setattr(llm, "gemini_json", lambda *a, **k: reads.append(1) or copy.deepcopy(extraction()))

    card = cards.build(dict(STORY), "X Ltd")
    assert reads and values(card)["PAT"] == 150.0


def test_network_trouble_leaves_a_card_pending():
    import requests

    from pipeline import cards, llm

    assert cards._try_later(llm.ModelUnavailable("busy"))
    assert cards._try_later(requests.ConnectionError())
    gone = requests.HTTPError(response=type("R", (), {"status_code": 404})())
    assert not cards._try_later(gone)
