from pipeline.telegram import driver, material, move_text, rules_from_start, story_text, trips


def test_start_parameter_carries_the_apps_rules():
    r = rules_from_start("i_25_40_30")
    assert r == {"mode": "vs_index", "z_mult": 2.5, "move_pct": 4.0, "vol_mult": 3.0}
    assert rules_from_start("garbage")["mode"] == "volatility"


def test_rules_match_the_app():
    sig = {"symbol": "TITAN", "date": "2026-11-06", "ret": 3.4, "ret_vs_index": 1.4, "z": 2.5, "vol_mult": 1.2}
    assert trips(sig, {"mode": "volatility", "z_mult": 2.5, "move_pct": 5, "vol_mult": 2})
    assert not trips(sig, {"mode": "fixed", "z_mult": 2.5, "move_pct": 5, "vol_mult": 2})


def test_results_alert_text():
    s = {"symbol": "BHARTIARTL", "headline": "x", "card": {"type": "results", "period": "Q2 FY27", "rows": [
        {"metric": "Revenue", "value": 60878, "qoq": 4.0, "yoy": 16.7},
        {"metric": "PAT", "value": 8888, "qoq": 8.8, "yoy": 30.9}]}}
    text = story_text(s)
    assert "Q2 FY27 results. Revenue ₹60,878 cr (+4.0% QoQ, +16.7% YoY); PAT ₹8,888 cr (+30.9% YoY)." in text


def test_unexplained_move_says_so():
    sig = {"symbol": "MARUTI", "date": "2026-11-05", "ret": 5.5, "z": 3.8, "vol_mult": 2.6}
    assert "No filing found" in move_text(sig, None)


def test_ampersand_in_a_headline_is_escaped():
    # Telegram rejects the whole message if the HTML doesn't parse.
    s = {"symbol": "LT", "headline": "Larsen & Toubro bags <large> order", "card": None}
    assert "Larsen &amp; Toubro bags &lt;large&gt; order" in story_text(s)


def test_results_alert_survives_a_missing_number():
    s = {"symbol": "HDFCBANK", "headline": "x", "card": {"type": "results", "period": "Q2 FY27", "rows": [
        {"metric": "NII", "value": None, "qoq": None, "yoy": None},
        {"metric": "PAT", "value": 18000, "qoq": 2.0, "yoy": -1.5}]}}
    assert "NII n/a" in story_text(s) and "PAT ₹18,000 cr (-1.5% YoY)" in story_text(s)


def story(sid, first_at, category="results", score=14.0, card=None):
    return {"id": sid, "symbol": "TITAN", "category": category, "headline": sid, "first_at": first_at,
            "base_score": score, "card": card}


def test_driver_is_news_between_the_previous_close_and_this_one():
    move = {"symbol": "TITAN", "date": "2026-11-09"}  # Monday; previous session Friday the 6th
    after_friday_close = story("results", "2026-11-06T17:00:00+05:30")
    before_friday_close = story("old", "2026-11-06T11:00:00+05:30", category="M&A")
    assert driver(move, [after_friday_close, before_friday_close], "2026-11-06")["id"] == "results"
    assert driver(move, [before_friday_close], "2026-11-06") is None


def test_small_fine_is_not_a_driver():
    fine = story("fine", "2026-11-03T12:00:00+05:30", category="litigation", score=12.0,
                 card={"type": "summary", "facts": {"amount_cr": 0.01}, "routine": True})
    assert not material(fine)
    assert driver({"symbol": "TITAN", "date": "2026-11-03"}, [fine], "2026-11-02") is None
