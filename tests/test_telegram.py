from pipeline.telegram import move_text, rules_from_start, story_text, trips


def test_start_parameter_carries_the_apps_rules():
    r = rules_from_start("i_25_40_30")
    assert r == {"mode": "vs_index", "z_mult": 2.5, "move_pct": 4.0, "vol_mult": 3.0}
    assert rules_from_start("garbage")["mode"] == "volatility"


def test_rules_match_the_app():
    sig = {"symbol": "TITAN", "date": "2026-11-06", "ret": 3.4, "ret_vs_index": 1.4, "z": 2.5, "vol_mult": 1.2}
    assert trips(sig, {"mode": "volatility", "z_mult": 2.5, "move_pct": 5, "vol_mult": 2})
    assert not trips(sig, {"mode": "fixed", "z_mult": 2.5, "move_pct": 5, "vol_mult": 2})


def test_results_alert_reads_like_a_person_wrote_it():
    s = {"symbol": "BHARTIARTL", "headline": "x", "card": {"type": "results", "period": "Q2 FY27", "rows": [
        {"metric": "Revenue", "value": 60878, "qoq": 4.0, "yoy": 16.7},
        {"metric": "PAT", "value": 8888, "qoq": 8.8, "yoy": 30.9}]}}
    text = story_text(s)
    assert "Q2 FY27 results. Revenue ₹60,878 cr (+4.0% QoQ, +16.7% YoY); PAT ₹8,888 cr (+30.9% YoY)." in text


def test_unexplained_move_says_so():
    sig = {"symbol": "MARUTI", "date": "2026-11-05", "ret": 5.5, "z": 3.8, "vol_mult": 2.6}
    assert "No filing found" in move_text(sig, None)
