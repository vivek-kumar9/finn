"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { loadBrief, loadFilings, type BriefData } from "@/lib/data";
import {
  anchorOf, buildBrief, CATEGORY_LABEL, DEFAULT_RULES, rangeOf, whyLine,
  type Move, type Window,
} from "@/lib/brief";
import { day, istDate, pct, tone, when } from "@/lib/format";
import type { Filing, Rules, Stock, Story } from "@/lib/types";
import { CardBody } from "./cards";
import { RulesSheet, rulesSentence } from "./Rules";

const store = {
  get<T>(k: string, fallback: T): T {
    try {
      const v = localStorage.getItem(`finn.${k}`);
      return v === null ? fallback : (JSON.parse(v) as T);
    } catch {
      return fallback;
    }
  },
  set(k: string, v: unknown) {
    try {
      localStorage.setItem(`finn.${k}`, JSON.stringify(v));
    } catch {
      /* private mode: preferences just won't stick */
    }
  },
};

const WINDOWS: { kind: Window["kind"]; label: string }[] = [
  { kind: "since", label: "Since last visit" },
  { kind: "24h", label: "24 hours" },
  { kind: "3d", label: "3 days" },
  { kind: "7d", label: "7 days" },
];

export default function Brief() {
  const [data, setData] = useState<BriefData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [win, setWin] = useState<Window>({ kind: "since" });
  const [rules, setRules] = useState<Rules>(DEFAULT_RULES);
  const [lastVisit, setLastVisit] = useState<number | null>(null);
  const [done, setDone] = useState<string[]>([]);
  const [pinned, setPinned] = useState<string[]>([]);
  const [showCleared, setShowCleared] = useState(false);
  const [open, setOpen] = useState<Story | null>(null);
  const [rulesOpen, setRulesOpen] = useState(false);
  const [undo, setUndo] = useState<Story | null>(null);

  useEffect(() => {
    setRules(store.get("rules", DEFAULT_RULES));
    setDone(store.get("done", []));
    setPinned(store.get("pinned", []));
    setWin(store.get("window", { kind: "since" }));
    setLastVisit(store.get<number | null>("lastVisit", null));
    // The visit that just started becomes "last visit" when the page is left.
    const leave = () => document.visibilityState === "hidden" && store.set("lastVisit", Date.now());
    document.addEventListener("visibilitychange", leave);
    loadBrief().then(setData).catch((e) => setError(String(e.message ?? e)));
    return () => document.removeEventListener("visibilitychange", leave);
  }, []);

  const chooseWindow = (w: Window) => {
    setWin(w);
    store.set("window", w);
  };
  const saveRules = (r: Rules) => {
    setRules(r);
    store.set("rules", r);
  };
  const markDone = useCallback((s: Story) => {
    setDone((d) => {
      const next = [...d, s.id];
      store.set("done", next);
      return next;
    });
    setUndo(s);
  }, []);
  const restore = (s: Story) => {
    setDone((d) => {
      const next = d.filter((x) => x !== s.id);
      store.set("done", next);
      return next;
    });
    setUndo(null);
  };
  const togglePin = (s: Story) => {
    setPinned((p) => {
      const next = p.includes(s.id) ? p.filter((x) => x !== s.id) : [...p, s.id];
      store.set("pinned", next);
      return next;
    });
  };

  useEffect(() => {
    if (!undo) return;
    const t = setTimeout(() => setUndo(null), 5000);
    return () => clearTimeout(t);
  }, [undo]);

  const anchor = useMemo(() => (data ? anchorOf(data.stories, data.signals) : Date.now()), [data]);
  const stocks = useMemo(() => new Map((data?.stocks ?? []).map((s) => [s.symbol, s])), [data]);
  const range = rangeOf(win, anchor, lastVisit);
  const brief = useMemo(
    () => (data ? buildBrief(data.stories, data.signals, data.stocks, rules, range) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [data, rules, range[0], range[1]],
  );

  if (error) {
    return (
      <main className="shell">
        <p className="state">FINN couldn&rsquo;t load the Brief: {error}. Pull to refresh, or try again in a minute.</p>
      </main>
    );
  }
  if (!data || !brief) {
    return (
      <main className="shell" aria-busy="true">
        <Masthead asOf={null} onRules={() => {}} />
        <p className="state">Loading the Brief.</p>
      </main>
    );
  }

  const isDone = (s: Story) => done.includes(s.id);
  const live = brief.needsYou.filter((s) => !isDone(s));
  const ordered = [...live.filter((s) => pinned.includes(s.id)), ...live.filter((s) => !pinned.includes(s.id))];
  const cleared = brief.needsYou.filter(isDone);
  const asOf = data.lastRun ? Math.max(Date.parse(data.lastRun), anchor) : anchor;

  return (
    <main className="shell">
      <Masthead asOf={asOf} onRules={() => setRulesOpen(true)} />

      <nav className="windows" aria-label="Time window">
        {WINDOWS.map((w) => (
          <button key={w.kind} aria-pressed={win.kind === w.kind} onClick={() => chooseWindow({ kind: w.kind })}>
            {w.label}
          </button>
        ))}
        <label className={`pick ${win.kind === "day" ? "on" : ""}`}>
          <span>{win.kind === "day" && win.day ? day(`${win.day}T12:00:00+05:30`) : "Pick a day"}</span>
          <input
            type="date"
            max={istDate(anchor)}
            value={win.kind === "day" ? win.day : ""}
            onChange={(e) => e.target.value && chooseWindow({ kind: "day", day: e.target.value })}
          />
        </label>
      </nav>
      <p className="range">
        {win.kind === "since" && lastVisit === null ? "First visit, so showing the last 3 days. " : ""}
        {when(range[0])} to {when(Math.min(range[1], asOf))}
      </p>

      <section aria-labelledby="needs">
        <h2 id="needs">
          Needs you <span className="count">{ordered.length}</span>
        </h2>
        {ordered.length === 0 ? (
          <p className="state">
            You&rsquo;re caught up. The last filing FINN saw was {when(Math.max(...data.stories.map((s) => Date.parse(s.last_at))))}.
          </p>
        ) : (
          <ol className="stories">
            {ordered.map((s) => (
              <StoryRow
                key={s.id}
                story={s}
                stock={stocks.get(s.symbol)}
                score={brief.score.get(s.id) ?? s.base_score}
                why={whyLine(s, stocks.get(s.symbol), brief.moves)}
                pinned={pinned.includes(s.id)}
                onOpen={() => setOpen(s)}
                onDone={() => markDone(s)}
              />
            ))}
          </ol>
        )}
        {cleared.length > 0 && (
          <button className="link" onClick={() => setShowCleared((v) => !v)}>
            {showCleared ? "Hide" : "Show"} {cleared.length} cleared
          </button>
        )}
        {showCleared && (
          <ul className="cleared">
            {cleared.map((s) => (
              <li key={s.id}>
                <button className="link" onClick={() => restore(s)}>Restore</button> {s.symbol}: {s.headline}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="moves">
        <h2 id="moves">
          Moves <span className="count">{brief.moves.length}</span>
        </h2>
        <p className="rule-line">
          {rulesSentence(rules)}{" "}
          <button className="link" onClick={() => setRulesOpen(true)}>Change</button>
        </p>
        {brief.moves.length === 0 ? (
          <p className="state">No stock crossed your thresholds in this window.</p>
        ) : (
          <ol className="moves">
            {brief.moves.map((m) => (
              <MoveRow key={`${m.symbol}-${m.date}`} move={m} onOpen={setOpen} />
            ))}
          </ol>
        )}
      </section>

      <section aria-labelledby="routine">
        <details className="routine">
          <summary>
            <h2 id="routine">
              Routine <span className="count">{brief.routine.length}</span>
            </h2>
            <span className="hint">Investor meetings, compliance paperwork, notices</span>
          </summary>
          <ul>
            {brief.routine.map((s) => (
              <li key={s.id}>
                <button onClick={() => setOpen(s)}>
                  <span className="sym">{s.symbol}</span> {s.headline}
                  <time>{day(s.last_at)}</time>
                </button>
              </li>
            ))}
          </ul>
        </details>
      </section>

      {open && (
        <StorySheet
          story={open}
          stock={stocks.get(open.symbol)}
          why={whyLine(open, stocks.get(open.symbol), brief.moves)}
          pinned={pinned.includes(open.id)}
          done={isDone(open)}
          onPin={() => togglePin(open)}
          onDone={() => {
            isDone(open) ? restore(open) : markDone(open);
            setOpen(null);
          }}
          onClose={() => setOpen(null)}
        />
      )}
      {rulesOpen && (
        <RulesSheet
          rules={rules}
          onChange={saveRules}
          onClose={() => setRulesOpen(false)}
          preview={(r) => buildBrief(data.stories, data.signals, data.stocks, r, range).moves.length}
        />
      )}
      {undo && (
        <div className="toast" role="status">
          Cleared {undo.symbol}. <button className="link" onClick={() => restore(undo)}>Undo</button>
        </div>
      )}
    </main>
  );
}

function Masthead({ asOf, onRules }: { asOf: number | null; onRules: () => void }) {
  return (
    <header className="masthead">
      <h1>FINN</h1>
      <p className="asof">{asOf ? <>Data as of {when(asOf)}</> : " "}</p>
      <button className="ghost" onClick={onRules}>Alert rules</button>
    </header>
  );
}

function tier(score: number): "high" | "mid" | "low" {
  return score >= 13 ? "high" : score >= 9 ? "mid" : "low";
}

function StoryRow(props: {
  story: Story; stock?: Stock; score: number; why: string; pinned: boolean;
  onOpen: () => void; onDone: () => void;
}) {
  const { story: s, stock, score, why, pinned, onOpen, onDone } = props;
  const [dx, setDx] = useState(0);
  const start = useRef<{ x: number; y: number; id: number } | null>(null);
  const swiping = useRef(false);
  const SWIPE = 96;

  return (
    <li className="story-wrap">
      <span className="behind" aria-hidden style={{ opacity: Math.min(1, -dx / SWIPE) }}>Clear</span>
      <article
        className={`story ${tier(score)} ${pinned ? "pinned" : ""}`}
        style={{ transform: dx ? `translateX(${dx}px)` : undefined }}
        onPointerDown={(e) => {
          start.current = { x: e.clientX, y: e.clientY, id: e.pointerId };
          swiping.current = false;
        }}
        onPointerMove={(e) => {
          if (!start.current) return;
          const ddx = e.clientX - start.current.x;
          const ddy = e.clientY - start.current.y;
          if (!swiping.current && Math.abs(ddx) > 12 && Math.abs(ddx) > Math.abs(ddy) * 1.5) {
            swiping.current = true;
            (e.currentTarget as HTMLElement).setPointerCapture(start.current.id);
          }
          if (swiping.current) setDx(Math.min(0, ddx));
        }}
        onPointerUp={() => {
          if (swiping.current && dx < -SWIPE) onDone();
          setDx(0);
          start.current = null;
        }}
        onPointerCancel={() => {
          setDx(0);
          start.current = null;
        }}
        onClick={(e) => {
          if (swiping.current) return;
          if ((e.target as HTMLElement).closest("a")) return;
          onOpen();
        }}
      >
        <header>
          <span className="sym">{s.symbol}</span>
          <span className="cat">{CATEGORY_LABEL[s.category]}</span>
          {pinned && <span className="pin">Pinned</span>}
          <time dateTime={s.last_at}>{day(s.last_at)}</time>
        </header>
        {!(s.card && "type" in s.card && s.card.type === "results") && <h3>{s.headline.replace(/^[\s,.:;-]+/, "")}</h3>}
        {why && <p className="why">{why}</p>}
        <CardBody story={s} />
        {s.filing_count > 1 && <p className="related">{s.filing_count} filings in this story</p>}
        <span className="sr">{stock?.name}. Swipe left or open to clear.</span>
      </article>
    </li>
  );
}

function MoveRow({ move: m, onOpen }: { move: Move; onOpen: (s: Story) => void }) {
  const lead = m.explainedBy[0];
  return (
    <li className="move">
      <div className="move-top">
        <span className="sym">{m.symbol}</span>
        <span className={`ret ${tone(m.ret)}`}>{pct(m.ret)}</span>
        <time>{day(`${m.date}T12:00:00+05:30`)}</time>
      </div>
      <p className="move-facts">
        {m.ret_vs_index !== null && <>Nifty-relative {pct(m.ret_vs_index)}. </>}
        {m.z !== null && <>{Math.abs(m.z).toFixed(1)}× its usual daily move. </>}
        {m.vol_mult !== null && <>Volume {m.vol_mult.toFixed(1)}× its 20-day average.</>}
      </p>
      {lead ? (
        <button className="explain" onClick={() => onOpen(lead)}>
          Likely driver: {lead.headline}
        </button>
      ) : (
        <p className="unexplained">No filing found around this move.</p>
      )}
    </li>
  );
}

function StorySheet(props: {
  story: Story; stock?: Stock; why: string; pinned: boolean; done: boolean;
  onPin: () => void; onDone: () => void; onClose: () => void;
}) {
  const { story: s, stock, why, pinned, done, onPin, onDone, onClose } = props;
  const [filings, setFilings] = useState<Filing[] | null>(null);
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    ref.current?.showModal();
    loadFilings(s.id).then(setFilings).catch(() => setFilings([]));
  }, [s.id]);

  return (
    <dialog ref={ref} className="sheet" onClose={onClose} onClick={(e) => e.target === ref.current && onClose()}>
      <div className="sheet-body">
        <header>
          <p>
            <span className="sym">{s.symbol}</span> {stock?.name}
          </p>
          <button className="ghost" onClick={onClose}>Close</button>
        </header>
        <p className="meta">
          {CATEGORY_LABEL[s.category]}, {when(s.last_at)}
        </p>
        <h3>{s.headline}</h3>
        {why && <p className="why">{why}</p>}
        <CardBody story={s} full />
        <div className="actions">
          {s.lead_url && (
            <a className="button" href={s.lead_url} target="_blank" rel="noreferrer">Open filing</a>
          )}
          <button onClick={onPin}>{pinned ? "Unpin" : "Pin"}</button>
          <button onClick={onDone}>{done ? "Restore" : "Clear"}</button>
        </div>
        {filings && filings.length > 0 && (
          <>
            <h4>Filings in this story</h4>
            <ul className="filings">
              {filings.map((f) => (
                <li key={f.id}>
                  <time>{when(f.filed_at)}</time>
                  {f.attachment_url ? (
                    <a href={f.attachment_url} target="_blank" rel="noreferrer">{f.subject}</a>
                  ) : (
                    <span>{f.subject}</span>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </dialog>
  );
}
