"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { crore, pct } from "@/lib/format";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

type Row = {
  metric: string; promise: string; period: string; low: number | null; high: number | null; unit: string;
  quote: string; page: number; call: string; url: string; status: string; actual: string | null;
  basis: string | null; revisions: { call: string; low: number | null; high: number | null; direction: string }[];
};
type Quarter = { label: string; sales: number | null; op_profit: number | null; pat: number | null; opm: number | null };
type Numbers = {
  bank: boolean; basis: string; quarters: Quarter[];
  annual: { label: string; sales: number | null; pat: number | null; opm: number | null }[];
  growth: Record<string, number | null>;
  balance: { label: string; equity: number | null; debt: number | null; de: number | null }[];
  working_capital: Record<string, [string, number | null][]>;
  cash: { label: string; pat: number | null; cfo: number | null; ratio: number | null; fcf: number | null }[];
  cash_5y_ratio: number | null;
  top: Record<string, number | null>;
};
type Fact = { id: string; text: string };
type State = {
  company?: { symbol: string; name: string; basis: string };
  numbers?: Numbers;
  ledger?: { rows: Row[]; calls: { call_period: string; url: string; items: number; dropped?: number; error?: string }[]; note?: string };
  snapshot?: { what_it_does?: string; segments?: { name: string; share_pct: number | null; detail: string | null }[];
    customers_and_markets?: string | null; page?: number | null; verified?: boolean; url?: string; year?: number; error?: string };
  views?: { bull?: { point: string; refs: string[] }[]; bear?: { point: string; refs: string[] }[]; facts: Fact[]; error?: string };
  cost?: { model_calls: number; tokens: number; cache_hits: number; seconds: number };
  progress?: string;
  fail?: string;
  ended?: boolean;
};

const STATUS: Record<string, { label: string; tone: string }> = {
  met: { label: "Met", tone: "up" },
  on_track: { label: "On track", tone: "up" },
  missed: { label: "Missed", tone: "down" },
  off_track: { label: "Off track", tone: "down" },
  open: { label: "Not yet due", tone: "flat" },
  unverifiable: { label: "Not checkable", tone: "flat" },
};

export default function Research({ symbol }: { symbol: string }) {
  const [s, setS] = useState<State>({});
  const [slow, setSlow] = useState(false);
  const got = useRef(false);

  useEffect(() => {
    const es = new EventSource(`${API}/research/${encodeURIComponent(symbol)}`);
    const timer = setTimeout(() => !got.current && setSlow(true), 6000);
    const on = (name: string) => (e: MessageEvent) => {
      got.current = true;
      setSlow(false);
      const data = JSON.parse(e.data);
      setS((prev) =>
        name === "progress" ? { ...prev, progress: data.text }
        : name === "fail" ? { ...prev, fail: data.error }
        : { ...prev, [name]: data },
      );
    };
    for (const n of ["company", "numbers", "ledger", "snapshot", "views", "cost", "progress", "fail"])
      es.addEventListener(n, on(n) as EventListener);
    es.addEventListener("end", () => {
      setS((p) => ({ ...p, ended: true, progress: undefined }));
      es.close();
    });
    es.onerror = () => {
      if (!got.current) setS((p) => ({ ...p, fail: "The research server didn’t answer. It may be waking up; reload in a minute." }));
      es.close();
    };
    return () => {
      clearTimeout(timer);
      es.close();
    };
  }, [symbol]);

  const n = s.numbers;
  return (
    <main className="shell research">
      <nav className="back"><Link href="/">Back to the Brief</Link></nav>
      <header className="r-head">
        <h1><span className="sym big">{symbol}</span> {s.company?.name}</h1>
        {n && (
          <p className="asof">
            {[
              n.top["Market Cap"] != null && `Market cap ₹${crore(n.top["Market Cap"])} cr`,
              n.top["Stock P/E"] != null && `P/E ${n.top["Stock P/E"]}`,
              n.top["ROCE"] != null && `ROCE ${n.top["ROCE"]}%`,
              `${n.basis} figures`,
            ].filter(Boolean).join(", ")}
          </p>
        )}
      </header>

      {slow && <p className="state">Waking the research server. Free hosting sleeps when idle, so the first run can take up to a minute.</p>}
      {s.fail && <p className="state warn">{s.fail}</p>}
      {s.progress && !s.ended && <p className="progress" role="status">{s.progress}</p>}

      <Ledger data={s.ledger} />
      {s.snapshot && <Snapshot data={s.snapshot} />}
      {n && <Trajectory n={n} />}
      {n && !n.bank && <Balance n={n} />}
      {n && <Cash n={n} />}
      {s.views && <Views data={s.views} />}

      {s.cost && (
        <p className="cost">
          This run: {s.cost.model_calls} model {s.cost.model_calls === 1 ? "call" : "calls"},{" "}
          {s.cost.tokens.toLocaleString("en-IN")} tokens, {s.cost.cache_hits} read from cache, {s.cost.seconds}s, ₹0.
        </p>
      )}
      <p className="sources-note">
        Sources: the company&rsquo;s concall transcripts and annual report, and screener.in&rsquo;s tables compiled from its
        filings. No news, broker notes or model background knowledge.
      </p>
    </main>
  );
}

function range(r: Row) {
  const u = r.unit === "pct" ? "%" : r.unit === "crore" ? " cr" : "";
  const pre = r.unit === "crore" ? "₹" : "";
  const f = (v: number) => (r.unit === "crore" ? crore(v) : String(v));
  if (r.low != null && r.high != null && r.low !== r.high) return `${pre}${f(r.low)}–${f(r.high)}${u}`;
  if (r.low != null) return `${r.high == null ? "at least " : ""}${pre}${f(r.low)}${u}`;
  if (r.high != null) return `up to ${pre}${f(r.high)}${u}`;
  return "";
}

function Ledger({ data }: { data?: State["ledger"] }) {
  if (!data) return <section><h2>Promised vs delivered</h2><p className="state">Reading the last four earnings calls.</p></section>;
  const checkable = data.rows.filter((r) => ["met", "missed", "on_track", "off_track"].includes(r.status));
  const good = checkable.filter((r) => r.status === "met" || r.status === "on_track").length;
  const cuts = data.rows.filter((r) => r.revisions.some((v) => v.direction === "cut")).length;
  const dropped = data.calls.reduce((a, c) => a + (c.dropped ?? 0), 0);
  return (
    <section aria-labelledby="ledger">
      <h2 id="ledger">Promised vs delivered</h2>
      {data.note ? <p className="state">{data.note}</p> : (
        <p className="lede">
          {checkable.length === 0 ? "No promise in the last four calls can be checked against reported numbers yet."
            : `${good} of ${checkable.length} checkable promises met or on track.`}
          {cuts > 0 && ` Guidance was cut ${cuts === 1 ? "once" : `${cuts} times`}.`}
        </p>
      )}
      <ol className="ledger">
        {data.rows.map((r, i) => (
          <li key={i}>
            <div className="l-top">
              <span className={`chip ${STATUS[r.status]?.tone}`}>{STATUS[r.status]?.label ?? r.status}</span>
              <span className="l-period">{r.period}</span>
            </div>
            <p className="l-promise">{r.promise}{range(r) && <span className="l-range"> ({range(r)})</span>}</p>
            {r.actual && <p className="l-actual">Reported: {r.actual}</p>}
            {!r.actual && r.basis && <p className="l-actual">{r.basis}</p>}
            {r.revisions.map((v, j) => (
              <p key={j} className={`l-rev ${v.direction === "cut" ? "down" : v.direction === "raised" ? "up" : ""}`}>
                {v.direction === "cut" ? "Cut" : v.direction === "raised" ? "Raised" : "Changed"} from{" "}
                {range({ ...r, low: v.low, high: v.high })} on the {v.call} call
              </p>
            ))}
            <blockquote>
              &ldquo;{r.quote}&rdquo;{" "}
              <a href={`${r.url}#page=${r.page}`} target="_blank" rel="noreferrer">{r.call} call, page {r.page}</a>
            </blockquote>
          </li>
        ))}
      </ol>
      {data.calls.length > 0 && (
        <p className="foot">
          Read {data.calls.map((c) => c.call_period).join(", ")} calls.
          {dropped > 0 && ` ${dropped} ${dropped === 1 ? "quote was" : "quotes were"} dropped because the words couldn’t be found in the transcript.`}
          {data.calls.some((c) => c.error) && " Some transcripts couldn’t be opened."}
        </p>
      )}
    </section>
  );
}

function Snapshot({ data }: { data: NonNullable<State["snapshot"]> }) {
  return (
    <section aria-labelledby="snap">
      <h2 id="snap">The business</h2>
      {data.error ? <p className="state">{data.error}</p> : (
        <>
          <p className="lede">{data.what_it_does}</p>
          {data.segments && data.segments.length > 0 && (
            <ul className="segments">
              {data.segments.map((g) => (
                <li key={g.name}>
                  <span>{g.name}</span>
                  {g.share_pct != null && <span className="share">{g.share_pct}% of revenue</span>}
                  {g.detail && <small>{g.detail}</small>}
                </li>
              ))}
            </ul>
          )}
          {data.customers_and_markets && <p>{data.customers_and_markets}</p>}
          <p className="foot">
            From the FY{String((data.year ?? 0) % 100).padStart(2, "0")}{" "}
            <a href={`${data.url}${data.page ? `#page=${data.page}` : ""}`} target="_blank" rel="noreferrer">annual report
              {data.page ? `, page ${data.page}` : ""}</a>.
          </p>
        </>
      )}
    </section>
  );
}

function Bars({ values, labels }: { values: (number | null)[]; labels: string[] }) {
  const max = Math.max(1, ...values.map((v) => v ?? 0));
  const w = 100 / values.length;
  return (
    <svg className="bars" viewBox="0 0 100 40" preserveAspectRatio="none" role="img"
         aria-label={`Quarterly revenue: ${labels.map((l, i) => `${l} ${crore(values[i])}`).join(", ")}`}>
      {values.map((v, i) => (
        <rect key={i} x={i * w + w * 0.15} width={w * 0.7} y={40 - ((v ?? 0) / max) * 38} height={((v ?? 0) / max) * 38}
              className={i === values.length - 1 ? "last" : ""} />
      ))}
    </svg>
  );
}

function Trajectory({ n }: { n: Numbers }) {
  const q = n.quarters;
  const g = n.growth;
  const top = n.bank ? "Revenue" : "Revenue";
  return (
    <section aria-labelledby="traj">
      <h2 id="traj">Trajectory</h2>
      <p className="lede">
        {g.sales_3y != null && `Revenue ${pct(g.sales_3y)} a year over 3 years`}
        {g.sales_5y != null && `, ${pct(g.sales_5y)} over 5`}
        {g.pat_3y != null && `. Net profit ${pct(g.pat_3y)} a year over 3 years`}.
      </p>
      <Bars values={q.map((x) => x.sales)} labels={q.map((x) => x.label)} />
      <table className="num">
        <thead><tr><th scope="col"><span className="sr">Quarter</span></th><th scope="col">{top}</th><th scope="col">{n.bank ? "Margin" : "OPM"}</th><th scope="col">PAT</th></tr></thead>
        <tbody>
          {[...q].reverse().map((x) => (
            <tr key={x.label}><th scope="row">{x.label}</th><td>{crore(x.sales)}</td><td>{x.opm != null ? `${x.opm}%` : "–"}</td><td>{crore(x.pat)}</td></tr>
          ))}
        </tbody>
      </table>
      <p className="foot">₹ crore.</p>
    </section>
  );
}

function Balance({ n }: { n: Numbers }) {
  const wc = n.working_capital;
  const names = ["Debtor Days", "Inventory Days", "Days Payable", "Cash Conversion Cycle"].filter((k) => wc[k]);
  const cols = names.length ? wc[names[0]].map(([c]) => c) : [];
  return (
    <section aria-labelledby="bs">
      <h2 id="bs">Balance sheet</h2>
      <table className="num">
        <thead><tr><th scope="col"><span className="sr">Year</span></th><th scope="col">Equity</th><th scope="col">Debt</th><th scope="col">D/E</th></tr></thead>
        <tbody>
          {[...n.balance].reverse().map((b) => (
            <tr key={b.label}><th scope="row">{b.label}</th><td>{crore(b.equity)}</td><td>{crore(b.debt)}</td><td>{b.de ?? "–"}</td></tr>
          ))}
        </tbody>
      </table>
      {names.length > 0 && (
        <table className="num">
          <caption>Working capital, days</caption>
          <thead><tr><th scope="col"><span className="sr">Measure</span></th>{cols.map((c) => <th key={c} scope="col">{c.replace("Mar ", "FY").replace(/FY20(\d\d)/, "FY$1")}</th>)}</tr></thead>
          <tbody>
            {names.map((k) => (
              <tr key={k}><th scope="row">{k.replace("Cash Conversion Cycle", "Cash cycle")}</th>
                {wc[k].map(([c, v]) => <td key={c}>{v != null ? Math.round(v) : "–"}</td>)}</tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function Cash({ n }: { n: Numbers }) {
  const r = n.cash_5y_ratio;
  const verdict = r == null ? null : r >= 0.9 ? "Profits are turning into cash." : r >= 0.7 ? "Most profit turns into cash, with some leakage." : "Reported profit is running well ahead of cash.";
  return (
    <section aria-labelledby="cash">
      <h2 id="cash">Cash quality</h2>
      {r != null && <p className="lede">Cash from operations was {r}× net profit over five years. {verdict}</p>}
      <table className="num">
        <thead><tr><th scope="col"><span className="sr">Year</span></th><th scope="col">PAT</th><th scope="col">Cash from ops</th><th scope="col">Ratio</th><th scope="col">Free cash</th></tr></thead>
        <tbody>
          {[...n.cash].reverse().map((c) => (
            <tr key={c.label}><th scope="row">{c.label}</th><td>{crore(c.pat)}</td><td>{crore(c.cfo)}</td>
              <td className={c.ratio != null && c.ratio < 0.7 ? "down" : ""}>{c.ratio != null ? `${c.ratio}×` : "–"}</td>
              <td className={c.fcf != null && c.fcf < 0 ? "down" : ""}>{crore(c.fcf)}</td></tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Views({ data }: { data: NonNullable<State["views"]> }) {
  const byId = new Map(data.facts.map((f) => [f.id, f.text]));
  const side = (title: string, pts?: { point: string; refs: string[] }[]) => (
    <div className="side">
      <h3>{title}</h3>
      {!pts || pts.length === 0 ? <p className="state">No point could be backed by the facts above.</p> : (
        <ol>
          {pts.map((p, i) => (
            <li key={i}>
              <p>{p.point}</p>
              <p className="rests">Rests on: {p.refs.map((r) => byId.get(r)).filter(Boolean).join("; ")}.</p>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
  return (
    <section aria-labelledby="views">
      <h2 id="views">Bull and bear</h2>
      {data.error ? <p className="state">The case couldn&rsquo;t be written this time: {data.error}</p> : (
        <div className="views">
          {side("Bull", data.bull)}
          {side("Bear", data.bear)}
        </div>
      )}
    </section>
  );
}
