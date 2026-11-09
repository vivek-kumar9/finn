"""The demo portfolio. Weights are made up but shaped like a real long-only book."""

INDEX = "^NSEI"

# symbol, name, sector, size, weight %
HOLDINGS = [
    ("RELIANCE", "Reliance Industries", "Conglomerate", "large", 10),
    ("HDFCBANK", "HDFC Bank", "Private bank", "large", 10),
    ("INFY", "Infosys", "IT services", "large", 8),
    ("BHARTIARTL", "Bharti Airtel", "Telecom", "large", 8),
    ("LT", "Larsen & Toubro", "Engineering", "large", 7),
    ("BAJFINANCE", "Bajaj Finance", "NBFC", "large", 6),
    ("SUNPHARMA", "Sun Pharmaceutical", "Pharma", "large", 5),
    ("MARUTI", "Maruti Suzuki", "Auto", "large", 5),
    ("TITAN", "Titan Company", "Consumer", "large", 5),
    ("HAL", "Hindustan Aeronautics", "Defence", "large", 5),
    ("ETERNAL", "Eternal", "Internet", "large", 5),
    ("DIXON", "Dixon Technologies", "Electronics mfg", "mid", 5),
    ("TATAPOWER", "Tata Power", "Power", "large", 4),
    ("POLYCAB", "Polycab India", "Cables", "mid", 4),
    ("PERSISTENT", "Persistent Systems", "IT services", "mid", 4),
    ("CDSL", "Central Depository Services", "Capital markets", "mid", 3),
    ("KPRMILL", "K.P.R. Mill", "Textiles", "small", 3),
    ("DEEPAKNTR", "Deepak Nitrite", "Chemicals", "mid", 3),
]

assert sum(h[4] for h in HOLDINGS) == 100

SYMBOLS = [h[0] for h in HOLDINGS]
WEIGHTS = {h[0]: h[4] for h in HOLDINGS}


def yahoo(symbol: str) -> str:
    return symbol if symbol.startswith("^") else f"{symbol}.NS"
