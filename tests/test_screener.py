from pathlib import Path

from pipeline import research, screener

FIX = Path(__file__).parent / "fixtures"


def page(name):
    p = screener.parse((FIX / f"{name}.html").read_text())
    return {**p, "basis": "consolidated", "symbol": name.upper()}


def test_company_page_parses():
    p = page("kaynes")
    assert p["name"] == "Kaynes Technology India Ltd"
    assert len(p["quarters"]) >= 12 and p["quarters"][-1]["sales"] > 0
    assert p["annual_reports"][0]["url"].endswith(".pdf")
    assert sum(1 for c in p["concalls"] if c["transcript"]) >= 4


def test_numbers_for_a_manufacturer():
    n = research.numbers(page("kaynes"))
    assert not n["bank"]
    assert n["growth"]["sales_3y"] is not None
    assert n["balance"][-1]["de"] is not None
    assert any(c["fcf"] is not None for c in n["cash"])


def test_bank_skips_margin_and_leverage():
    n = research.numbers(page("hdfcbank"))
    assert n["bank"]
    assert all(q["opm"] is None for q in n["quarters"])
    assert all(b["de"] is None for b in n["balance"])
    assert not any("margin" in f["text"] or "Debt to equity" in f["text"] for f in research.facts(n))
