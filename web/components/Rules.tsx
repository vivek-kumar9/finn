"use client";

import { useEffect, useRef, useState } from "react";
import type { AlertMode, Rules } from "@/lib/types";

const MODES: { mode: AlertMode; label: string; help: string }[] = [
  { mode: "volatility", label: "Unusual for the stock", help: "Compares the day's move with the stock's own typical daily move over 20 days. A 3% day is noise for some stocks and news for others." },
  { mode: "vs_index", label: "Against Nifty", help: "Ignores moves the whole market made. A 3% fall on a day Nifty fell 2.5% is not about the company." },
  { mode: "fixed", label: "Fixed percentage", help: "Any day the stock moves more than the set percentage." },
];

const BOT = process.env.NEXT_PUBLIC_TELEGRAM_BOT;

/** The rules go to the bot in the /start parameter, which only allows letters,
 *  digits, _ and -. Numbers are sent in tenths, so 2.5 becomes 25. */
function startParam(r: Rules): string {
  const m = r.mode === "fixed" ? "f" : r.mode === "vs_index" ? "i" : "v";
  return [m, Math.round(r.zMult * 10), Math.round(r.movePct * 10), Math.round(r.volMult * 10)].join("_");
}

export function rulesSentence(r: Rules): string {
  const price =
    r.mode === "volatility" ? `moves bigger than ${r.zMult}x the stock's usual day`
    : r.mode === "vs_index" ? `moves of ${r.movePct}% or more against Nifty`
    : `moves of ${r.movePct}% or more`;
  return `Showing ${price}, or volume ${r.volMult}x its 20-day average.`;
}

export function RulesSheet(props: {
  rules: Rules; onChange: (r: Rules) => void; onClose: () => void; preview: (r: Rules) => number;
}) {
  const [r, setR] = useState(props.rules);
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => ref.current?.showModal(), []);
  const count = props.preview(r);

  const save = () => {
    props.onChange(r);
    ref.current?.close();
  };

  return (
    <dialog ref={ref} className="sheet" onClose={props.onClose}>
      <div className="sheet-body rules">
        <header>
          <p>Alert rules</p>
          <button className="ghost" onClick={() => ref.current?.close()}>Close</button>
        </header>

        <fieldset>
          <legend>Price move</legend>
          {MODES.map((m) => (
            <label key={m.mode} className="mode">
              <input type="radio" name="mode" checked={r.mode === m.mode} onChange={() => setR({ ...r, mode: m.mode })} />
              <span>
                <strong>{m.label}</strong>
                <small>{m.help}</small>
              </span>
            </label>
          ))}
          {r.mode === "volatility" ? (
            <Slider label="Times the usual daily move" value={r.zMult} min={1.5} max={4} step={0.5} unit="x"
                    onChange={(v) => setR({ ...r, zMult: v })} />
          ) : (
            <Slider label={r.mode === "vs_index" ? "Move against Nifty" : "Move"} value={r.movePct} min={1} max={10}
                    step={0.5} unit="%" onChange={(v) => setR({ ...r, movePct: v })} />
          )}
        </fieldset>

        <fieldset>
          <legend>Volume</legend>
          <Slider label="Times the 20-day average" value={r.volMult} min={1.5} max={5} step={0.5} unit="x"
                  onChange={(v) => setR({ ...r, volMult: v })} />
        </fieldset>

        <p className="preview">{count === 1 ? "1 move matches" : `${count} moves match`} in the current window.</p>
        <div className="actions">
          <button className="primary" onClick={save}>Save rules</button>
        </div>

        {BOT && (
          <div className="telegram">
            <h4>Get these alerts on your phone</h4>
            <p>
              FINN checks filings and prices every hour on market days and messages you on Telegram when something
              crosses these rules, plus new results, leadership changes, deals, fund raises and pledges.
            </p>
            <a className="button" href={`https://t.me/${BOT}?start=${startParam(r)}`} target="_blank" rel="noreferrer"
               onClick={() => props.onChange(r)}>
              Connect Telegram
            </a>
          </div>
        )}
      </div>
    </dialog>
  );
}

function Slider(props: {
  label: string; value: number; min: number; max: number; step: number; unit: string; onChange: (v: number) => void;
}) {
  return (
    <label className="slider">
      <span>
        {props.label} <output>{props.value}{props.unit}</output>
      </span>
      <input type="range" min={props.min} max={props.max} step={props.step} value={props.value}
             onChange={(e) => props.onChange(Number(e.target.value))} />
    </label>
  );
}
