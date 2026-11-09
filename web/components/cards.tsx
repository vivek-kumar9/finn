import type { LeadershipCard, ResultsCard, Story, SummaryCard } from "@/lib/types";
import { bps, crore, pct, rupees, tone } from "@/lib/format";

export function ResultsGrid({ card }: { card: ResultsCard }) {
  const marginName = card.bank ? "Operating profit" : "EBITDA";
  const margin = card.margins[marginName];
  const page = card.source.page ? `#page=${card.source.page}` : "";
  return (
    <div className="results">
      <table>
        <caption>
          {card.period}, {card.basis}, {card.unit}
        </caption>
        <thead>
          <tr>
            <th scope="col"><span className="sr">Metric</span></th>
            <th scope="col">{card.period.split(" ")[0]}</th>
            <th scope="col">QoQ</th>
            <th scope="col">YoY</th>
          </tr>
        </thead>
        <tbody>
          {card.rows.map((r) => (
            <tr key={r.metric}>
              <th scope="row">
                {r.metric}
                {r.metric === "EBITDA" && card.ebitda_derived && <sup title="Derived: PBT + finance costs + depreciation − other income">d</sup>}
              </th>
              <td>{crore(r.value)}</td>
              <td className={tone(r.qoq)}>{pct(r.qoq)}</td>
              <td className={tone(r.yoy)}>{pct(r.yoy)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {margin && (
        <p className="margin">
          {card.bank ? "Operating" : "EBITDA"} margin {margin.now.toFixed(1)}%,{" "}
          <span className={tone(margin.qoq_bps)}>{bps(margin.qoq_bps)}</span> QoQ,{" "}
          <span className={tone(margin.yoy_bps)}>{bps(margin.yoy_bps)}</span> YoY
        </p>
      )}
      {card.checks.length > 0 && <p className="check">{card.checks.join(" ")}</p>}
      <p className="source">
        <a href={card.source.url + page} target="_blank" rel="noreferrer">
          Results filing{card.source.page ? `, page ${card.source.page}` : ""}
        </a>
        {card.ebitda_derived && <span> d: EBITDA derived from the filing&rsquo;s lines</span>}
      </p>
    </div>
  );
}

export function Leadership({ card }: { card: LeadershipCard }) {
  return (
    <div className="people">
      {card.incoming.map((p) => (
        <div className="person" key={`in-${p.name}`}>
          <p className="who">
            <span className="dir in">Joins</span> {p.name}, {p.role}
          </p>
          {p.bio && (
            <p className="bio">
              {p.bio}
              <span className="from">
                {" "}
                {p.bio_source === "web" ? "From web search" : "From the filing"}
              </span>
            </p>
          )}
        </div>
      ))}
      {card.outgoing.map((p) => (
        <div className="person" key={`out-${p.name}`}>
          <p className="who">
            <span className="dir out">Leaves</span> {p.name}, {p.role}
            {p.reason && <span className="reason">. {p.reason.replace(/\.$/, "")}</span>}
          </p>
        </div>
      ))}
    </div>
  );
}

const FACT_LABEL: Record<string, string> = {
  deal_value_cr: "Deal value", stake_pct: "Stake", amount_cr: "Amount", issue_price: "Issue price",
  pledged_pct_before: "Pledged before", pledged_pct_after: "Pledged after", order_value_cr: "Order value",
  value_cr: "Value", per_share: "Per share", yield_pct: "Yield", record_date: "Record date",
  counterparty: "Counterparty", authority: "Authority", agency: "Agency", rating: "Rating",
  action: "Action", ratio: "Ratio", instrument: "Instrument", target: "Target", capacity: "Capacity",
  timeline: "Timeline", person: "Person", promoter: "Promoter", consideration: "Consideration", kind: "Type",
};
const HIDDEN = new Set(["company_view"]);

function factValue(k: string, v: string | number): string {
  if (typeof v !== "number") return String(v);
  if (k.endsWith("_cr")) return rupees(v);
  if (k.endsWith("_pct")) return `${v}%`;
  if (k === "per_share" || k === "issue_price") return `₹${v}`;
  return String(v);
}

export function Summary({ card }: { card: SummaryCard }) {
  const facts = Object.entries(card.facts).filter(([k]) => !HIDDEN.has(k) && FACT_LABEL[k]);
  return (
    <div className="summary">
      <p>{card.summary}</p>
      {facts.length > 0 && (
        <dl>
          {facts.map(([k, v]) => (
            <div key={k}>
              <dt>{FACT_LABEL[k]}</dt>
              <dd>{factValue(k, v)}</dd>
            </div>
          ))}
        </dl>
      )}
      {typeof card.facts.company_view === "string" && <p className="view">Company says: {card.facts.company_view}</p>}
    </div>
  );
}

export function CardBody({ story, full = false }: { story: Story; full?: boolean }) {
  const c = story.card;
  if (story.card_status === "pending") return <p className="quiet">Reading the filing.</p>;
  if (!c || !("type" in c) || !c.type) {
    if (story.category === "results")
      return <p className="quiet">Results are filed, but FINN couldn&rsquo;t read the table. Open the filing for the numbers.</p>;
    return full ? <p className="quiet">{story.headline}</p> : null;
  }
  if (c.type === "results") {
    const read = c.rows.filter((r) => r.value !== null).length;
    if (read < 2)
      return <p className="quiet">Results are filed, but FINN couldn&rsquo;t read the table. Open the filing for the numbers.</p>;
    return <ResultsGrid card={c} />;
  }
  if (c.type === "leadership") return <Leadership card={c} />;
  return <Summary card={c} />;
}
