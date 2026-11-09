import pytest

from pipeline.classify import by_rules

# Subjects and detail lines as NSE writes them.
CASES = [
    ("Financial Result Updates", "XYZ Limited has submitted to the Exchange the financial results for the period ended September 30, 2026.", "results"),
    ("Outcome of Board Meeting", "Approved unaudited standalone and consolidated financial results for the quarter ended September 30, 2026", "results"),
    ("Board Meeting Intimation", "to consider and approve the Financial Results for the quarter ended September 2026", "results"),
    ("Investor Presentation", "Investor presentation for Q2 FY27", "results"),
    ("Copy of Newspaper Publication", "Newspaper publication of financial results", "results"),
    ("Copy of Newspaper Publication", "Copy of newspaper advertisement regarding transfer of shares to IEPF", "unclassified"),
    ("Closure of Trading Window", "Trading window shall remain closed until 48 hours after declaration of financial results", "unclassified"),
    ("Analysts/Institutional Investor Meet/Con. Call Updates", "Transcript of the earnings call held on November 3, 2026", "earnings_call"),
    ("Analysts/Institutional Investor Meet/Con. Call Updates", "Schedule of meeting with Nomura Asset Management on 12 November", "investor_meetings"),
    ("Change in Directorate", "Appointment of Mr. A as Chief Financial Officer", "management_change"),
    ("Cessation", "Cessation of Mr. B as Independent Director on completion of tenure", "management_change"),
    ("Credit Rating", "CRISIL has reaffirmed the long-term rating at AAA/Stable", "credit_rating"),
    ("Acquisition", "Acquisition of 100% stake in ABC Private Limited", "M&A"),
    ("Updates", "Incorporation of a wholly owned subsidiary in Singapore", "M&A"),
    ("Allotment of Securities", "Allotment of equity shares under ESOP 2021", "unclassified"),
    ("Allotment of Securities", "Allotment of non-convertible debentures on private placement basis", "fund_raise"),
    ("Updates", "Issuance of commercial paper of Rs 500 crore", "fund_raise"),
    ("Action(s) initiated or orders passed", "GST demand order of Rs 12.4 crore received from the Commissioner", "litigation"),
    ("Dividend", "Interim dividend of Rs 8 per share; record date November 14", "dividend"),
    ("Bonus", "Recommendation of bonus issue in the ratio 1:1", "bonus_split"),
    ("Press Release", "Company bags order worth Rs 1,200 crore from NHAI", "press_release"),
    ("Bagging/Receiving of orders/contracts", "Receipt of order from Indian Railways", "press_release"),
    ("Disclosure of reasons for encumbrance", "Promoter encumbrance disclosure", "pledging"),
    ("Postal Ballot", "Notice of postal ballot", "postal_ballot"),
    ("Shareholders meeting", "Notice of the 45th Annual General Meeting", "agm_egm"),
    ("Updates", "Board approved capex of Rs 900 crore for a greenfield plant in Gujarat", "capex"),
]


@pytest.mark.parametrize("subject,detail,expected", CASES)
def test_rules(subject, detail, expected):
    assert by_rules(subject, detail) == expected


def test_unknown_goes_to_model():
    assert by_rules("General Updates", "Update on the company's website address") is None


# Leftovers from the first 30-day backfill, now handled by rules.
BACKFILL_CASES = [
    ("Committee Meeting Updates", "Persistent Systems Limited has informed the Exchange regarding Outcome of Committee Meeting", "unclassified"),
    ("General Updates", "Polycab India Limited has informed the Exchange about Analyst(s)/Investor(s) meet.", "investor_meetings"),
    ("General Updates", "Persistent Systems Limited has informed the Exchange about General Updates", "unclassified"),
    ("Updates", "Bharti Airtel Limited has informed the Exchange regarding 'Updates'.", "unclassified"),
    ("Updates", "Please find attached a media statement issued by the Company.", "press_release"),
    ("Updates", "Infosys Limited has informed the Exchange regarding 'Earnings Call Transcript'.", "earnings_call"),
    ("Updates", "Infosys Limited has informed the Exchange regarding ' Investor Conference'.", "investor_meetings"),
    ("Updates", "Infosys Limited has informed the Exchange regarding 'Infosys to Announce Second Quarter Results'", "results"),
    ("Allotment of Securities", "Bajaj Finance Limited has informed the Exchange regarding allotment of 2,12,000 securities", "unclassified"),
    ("General Updates", "Maruti Suzuki India Limited has informed the Exchange about Maruti Suzuki Production Volumes", "unclassified"),
    ("Change in Auditors", "Titan Company Limited has informed the Exchange regarding Change in Auditors of the company", "unclassified"),
    ("General Updates", "Polycab India Limited has informed the Exchange about Unaudited Interim Condensed (Standalone)", "results"),
    ("General Updates", "Deepak Nitrite Limited has informed the Exchange about General Updates- Q2 & H1 FY2027 Results", "results"),
    ("General Updates", "Updates on the Business Transfer Agreement with Vyoma.AI Limited (Vyoma), Wholly owned Subsidiary", "M&A"),
    ("Other Restructuring", "Disclosure under Regulation 30 of the Securities and Exchange Board of India", "M&A"),
]


@pytest.mark.parametrize("subject,detail,expected", BACKFILL_CASES)
def test_backfill_leftovers(subject, detail, expected):
    assert by_rules(subject, detail) == expected
