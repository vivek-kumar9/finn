from pipeline.stories import build, headline


def filing(fid, sym, when, cat, subject="", detail="", url=None):
    return {"id": fid, "symbol": sym, "filed_at": f"2026-{when}T10:00:00+05:30",
            "category": cat, "subject": subject, "detail": detail, "attachment_url": url}


def test_results_event_collapses_into_one_story():
    fs = [
        filing("1", "TITAN", "10-20", "results", "Board Meeting Intimation", "to consider financial results", "a.pdf"),
        filing("2", "TITAN", "11-03", "results", "Financial Result Updates", "financial results for Q2", "r.pdf"),
        filing("3", "TITAN", "11-03", "results", "Copy of Newspaper Publication", "newspaper", "n.pdf"),
        filing("4", "TITAN", "11-04", "earnings_call", "Con. Call", "transcript", "t.pdf"),
        filing("5", "TITAN", "11-04", "dividend", "Dividend", "interim dividend", "d.pdf"),
    ]
    stories, assign = build(fs, {"TITAN": 5})
    results = [s for s in stories if s["category"] == "results"]
    assert len(results) == 1 and results[0]["filing_count"] == 4
    assert results[0]["lead_url"] == "r.pdf"
    assert results[0]["base_score"] == 12.5
    assert assign["4"] == results[0]["id"]


def test_window_anchors_on_first_filing():
    fs = [filing(str(i), "LT", f"10-{d:02d}", "press_release", detail=f"order {i}") for i, d in
          enumerate([1, 5, 9, 13])]
    stories, _ = build(fs, {})
    assert [s["filing_count"] for s in stories] == [2, 2]


def test_junior_management_change_scores_lower():
    senior = build([filing("1", "INFY", "11-01", "management_change", detail="Appointment of Chief Financial Officer")], {})
    junior = build([filing("1", "INFY", "11-01", "management_change", detail="Cessation of independent director")], {})
    assert senior[0][0]["base_score"] > junior[0][0]["base_score"]


def test_headline_drops_exchange_boilerplate():
    f = {"subject": "Updates", "detail": "Larsen & Toubro Limited has informed the Exchange about receipt of a mega order."}
    assert headline(f) == "Receipt of a mega order"


def test_transcript_never_leads_a_results_story():
    fs = [
        filing("1", "ETERNAL", "10-21", "results", "Outcome of Board Meeting", "financial results", "o.pdf"),
        filing("2", "ETERNAL", "10-27", "earnings_call", "Con. Call", "transcript of the earnings call", "t.pdf"),
    ]
    stories, _ = build(fs, {})
    assert stories[0]["lead_url"] == "o.pdf"


def test_statement_leads_over_press_release_and_deck():
    # CDSL's results story used to lead with "Media Release relating to financial results".
    fs = [
        filing("1", "CDSL", "10-31", "results", "Press Release", "Media Release relating to financial results", "m.pdf"),
        filing("2", "CDSL", "10-31", "results", "Outcome of Board Meeting", "approved the financial results", "o.pdf"),
        filing("3", "CDSL", "10-31", "results", "Investor Presentation", "Earnings presentation on financial results", "p.pdf"),
    ]
    stories, _ = build(fs, {})
    assert stories[0]["lead_url"] == "o.pdf"
