export type Category =
  | "results" | "M&A" | "fund_raise" | "management_change" | "litigation"
  | "insider_trading" | "earnings_call" | "investor_meetings" | "credit_rating"
  | "dividend" | "bonus_split" | "capex" | "press_release" | "postal_ballot"
  | "agm_egm" | "pledging" | "unclassified";

export type ResultsRow = { metric: string; value: number | null; qoq: number | null; yoy: number | null };

export type ResultsCard = {
  type: "results";
  period: string;
  basis: "consolidated" | "standalone";
  unit: string;
  bank: boolean;
  ebitda_derived: boolean;
  rows: ResultsRow[];
  margins: Record<string, { now: number; qoq_bps: number | null; yoy_bps: number | null }>;
  source: { url: string; page?: number | null };
  checks: string[];
  check?: { against: string; metrics: string[] } | null;
};

export type Person = {
  name: string;
  role: string;
  change: string;
  effective_date?: string | null;
  reason?: string | null;
  bio?: string | null;
  bio_source?: "filing" | "web";
  bio_links?: string[];
};

export type LeadershipCard = {
  type: "leadership";
  incoming: Person[];
  outgoing: Person[];
  source: { url: string };
};

export type SummaryCard = {
  type: "summary";
  category: Category;
  summary: string;
  facts: Record<string, string | number>;
  source: { url: string | null };
  routine: boolean;
};

export type Card = ResultsCard | LeadershipCard | SummaryCard | { error: string; type?: undefined };

export type Story = {
  id: string;
  symbol: string;
  category: Category;
  headline: string;
  summary: string | null;
  first_at: string;
  last_at: string;
  filing_count: number;
  lead_url: string | null;
  base_score: number;
  card: Card | null;
  card_status: "pending" | "done" | "failed" | "skipped";
};

export type Stock = { symbol: string; name: string; sector: string; size: string; weight: number };

export type Signal = {
  symbol: string;
  date: string;
  ret: number;
  ret_vs_index: number | null;
  vol: number | null;
  z: number | null;
  vol_mult: number | null;
};

export type Filing = {
  id: string;
  filed_at: string;
  subject: string;
  detail: string | null;
  attachment_url: string | null;
  category: Category;
};

export type AlertMode = "fixed" | "vs_index" | "volatility";

export type Rules = { mode: AlertMode; movePct: number; zMult: number; volMult: number };
