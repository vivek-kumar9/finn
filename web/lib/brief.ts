import type { Category, Rules, Signal, Stock, Story } from "./types";

// Mirrors pipeline/stories.py WEIGHT. A story is material when its category
// weight (without the position-size part of base_score) is at least 4.
export const CATEGORY_WEIGHT: Record<Category, number> = {
  results: 10, "M&A": 9, management_change: 9, fund_raise: 8, litigation: 8, pledging: 8,
  capex: 7, insider_trading: 6, credit_rating: 6, press_release: 5, bonus_split: 5,
  dividend: 4, earnings_call: 3, agm_egm: 2, postal_ballot: 2, investor_meetings: 1, unclassified: 0,
};

export const CATEGORY_LABEL: Record<Category, string> = {
  results: "Results", "M&A": "Deal", fund_raise: "Fund raise", management_change: "Leadership",
  litigation: "Legal", insider_trading: "Insider trade", earnings_call: "Earnings call",
  investor_meetings: "Investor meeting", credit_rating: "Credit rating", dividend: "Dividend",
  bonus_split: "Bonus or split", capex: "Capex", press_release: "Announcement",
  postal_ballot: "Postal ballot", agm_egm: "Shareholder meeting", pledging: "Pledge", unclassified: "Filing",
};

export const DEFAULT_RULES: Rules = { mode: "volatility", movePct: 5, zMult: 2.5, volMult: 2 };

export type WindowKind = "since" | "24h" | "3d" | "7d" | "day";
export type Window = { kind: WindowKind; day?: string };

const HOUR = 36e5;
const MOVE_BOOST = 3;

/** The newest moment we have data for. Windows count back from here, not from
 *  the wall clock, so the Brief still shows a real day when ingest is paused. */
export function anchorOf(stories: Story[], signals: Signal[]): number {
  const s = Math.max(0, ...stories.map((x) => Date.parse(x.last_at)));
  const p = Math.max(0, ...signals.map((x) => closeOf(x.date)));
  return Math.max(s, p) || Date.now();
}

/** NSE closes at 15:30 IST, which is 10:00 UTC. */
export function closeOf(date: string): number {
  return Date.parse(`${date}T10:00:00Z`);
}

export function rangeOf(w: Window, anchor: number, lastVisit: number | null): [number, number] {
  switch (w.kind) {
    case "since":
      return [lastVisit ?? anchor - 72 * HOUR, Math.max(anchor, Date.now())];
    case "24h":
      return [anchor - 24 * HOUR, anchor + 1];
    case "3d":
      return [anchor - 72 * HOUR, anchor + 1];
    case "7d":
      return [anchor - 168 * HOUR, anchor + 1];
    case "day": {
      const start = Date.parse(`${w.day}T00:00:00+05:30`);
      return [start, start + 24 * HOUR];
    }
  }
}

export type Move = Signal & { price: boolean; volume: boolean; explainedBy: Story[]; stock?: Stock };

export function tripped(s: Signal, r: Rules): { price: boolean; volume: boolean } {
  let price = false;
  if (r.mode === "fixed") price = Math.abs(s.ret) >= r.movePct;
  else if (r.mode === "vs_index") price = s.ret_vs_index !== null && Math.abs(s.ret_vs_index) >= r.movePct;
  else price = s.z !== null && Math.abs(s.z) >= r.zMult;
  const volume = s.vol_mult !== null && s.vol_mult >= r.volMult;
  return { price, volume };
}

/** Filings between the previous close and this close are the candidates for
 *  explaining a move: results after hours on Monday move the stock on Tuesday. */
function explainers(s: Signal, stories: Story[]): Story[] {
  const end = closeOf(s.date);
  const start = end - 30 * HOUR;
  return stories
    .filter((x) => x.symbol === s.symbol && Date.parse(x.last_at) > start && Date.parse(x.first_at) <= end)
    .filter((x) => CATEGORY_WEIGHT[x.category] >= 3)
    .sort((a, b) => b.base_score - a.base_score);
}

export type Brief = {
  needsYou: Story[];
  routine: Story[];
  moves: Move[];
  score: Map<string, number>;
};

export function isMaterial(s: Story, weights: Map<string, number>): boolean {
  const c = s.card;
  if (c && "type" in c && c.type === "summary") {
    if (c.routine) return false;
    // Same rules as pipeline/cards.py, applied here too so older cards follow them.
    const amount = c.facts.amount_cr;
    if (s.category === "litigation" && typeof amount === "number" && amount < 1) return false;
    if (s.category === "credit_rating" && String(c.facts.action ?? "").toLowerCase().startsWith("reaffirm")) return false;
  }
  const categoryPart = s.base_score - (weights.get(s.symbol) ?? 0) / 2;
  return categoryPart >= 4;
}

export function buildBrief(
  stories: Story[], signals: Signal[], stocks: Stock[], rules: Rules, range: [number, number],
): Brief {
  const [from, to] = range;
  const weights = new Map(stocks.map((s) => [s.symbol, s.weight]));
  const bySymbol = new Map(stocks.map((s) => [s.symbol, s]));
  // The same filing can sit in two stories after a regroup; keep the newer row.
  const seen = new Set<string>();
  const inRange = stories
    .filter((s) => {
      const t = Date.parse(s.last_at);
      return t >= from && t < to;
    })
    .filter((s) => {
      const key = `${s.symbol}|${s.category}|${s.lead_url ?? s.id}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });

  const moves: Move[] = signals
    .filter((s) => closeOf(s.date) >= from && closeOf(s.date) < to)
    .map((s) => ({ ...s, ...tripped(s, rules), explainedBy: explainers(s, stories), stock: bySymbol.get(s.symbol) }))
    .filter((m) => m.price || m.volume)
    .sort((a, b) => (b.stock?.weight ?? 0) * Math.abs(b.ret) - (a.stock?.weight ?? 0) * Math.abs(a.ret));

  const moved = new Set(moves.map((m) => m.symbol));
  const score = new Map(inRange.map((s) => [s.id, s.base_score + (moved.has(s.symbol) ? MOVE_BOOST : 0)]));
  const byScore = (a: Story, b: Story) =>
    score.get(b.id)! - score.get(a.id)! || Date.parse(b.last_at) - Date.parse(a.last_at);

  return {
    needsYou: inRange.filter((s) => isMaterial(s, weights)).sort(byScore),
    routine: inRange.filter((s) => !isMaterial(s, weights)).sort((a, b) => Date.parse(b.last_at) - Date.parse(a.last_at)),
    moves,
    score,
  };
}

/** One plain line, built from facts, saying why this card is in front of you. */
export function whyLine(s: Story, stock: Stock | undefined, moves: Move[]): string {
  const parts: string[] = [];
  if (stock) parts.push(`${stock.weight}% of portfolio`);
  const m = moves.find((x) => x.symbol === s.symbol);
  if (m) parts.push(`stock ${m.ret >= 0 ? "up" : "down"} ${Math.abs(m.ret).toFixed(1)}% that day`);
  const c = s.card;
  if (c && "type" in c && c.type === "results") {
    const top = c.rows[0];
    if (top?.qoq !== null && top?.yoy !== null && top) {
      const dir = (v: number) => (v >= 0 ? "up" : "down");
      parts.push(
        dir(top.qoq) === dir(top.yoy)
          ? `${top.metric.toLowerCase()} ${dir(top.qoq)} on both QoQ and YoY`
          : `${top.metric.toLowerCase()} ${dir(top.yoy)} YoY, ${dir(top.qoq)} QoQ`,
      );
    }
  }
  return parts.join(", ");
}
