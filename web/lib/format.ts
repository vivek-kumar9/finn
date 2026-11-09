const MINUS = "−";

export function crore(v: number | null): string {
  if (v === null || v === undefined) return "–";
  const digits = Math.abs(v) >= 10 ? 0 : 1;
  const s = new Intl.NumberFormat("en-IN", { maximumFractionDigits: digits, minimumFractionDigits: digits })
    .format(Math.abs(v));
  return v < 0 ? `${MINUS}${s}` : s;
}

export function pct(v: number | null, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const s = Math.abs(v).toFixed(digits);
  return v > 0 ? `+${s}%` : v < 0 ? `${MINUS}${s}%` : `${s}%`;
}

export function bps(v: number | null): string {
  if (v === null || v === undefined) return "–";
  return v > 0 ? `+${v} bps` : v < 0 ? `${MINUS}${Math.abs(v)} bps` : "flat";
}

export function times(v: number | null): string {
  if (v === null || v === undefined) return "–";
  return `${v < 0 ? MINUS : ""}${Math.abs(v)}×`;
}

export function tone(v: number | null): "up" | "down" | "flat" {
  if (v === null || v === undefined || v === 0) return "flat";
  return v > 0 ? "up" : "down";
}

const IST = "Asia/Kolkata";

export function when(iso: string | number): string {
  const d = new Date(iso);
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: IST, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(d);
}

export function day(iso: string | number): string {
  return new Intl.DateTimeFormat("en-IN", { timeZone: IST, day: "numeric", month: "short" }).format(new Date(iso));
}

export function istDate(ms: number): string {
  // YYYY-MM-DD in IST
  return new Date(ms + 5.5 * 36e5).toISOString().slice(0, 10);
}

/** Amounts the model returns in crore can be tiny (a ₹1 lakh fine). */
export function rupees(cr: number): string {
  if (Math.abs(cr) < 1) return `₹${new Intl.NumberFormat("en-IN", { maximumFractionDigits: 1 }).format(cr * 100)} lakh`;
  return `₹${crore(cr)} cr`;
}
