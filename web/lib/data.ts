import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import type { Filing, Signal, Stock, Story } from "./types";

const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

let client: SupabaseClient | null = null;
function db(): SupabaseClient | null {
  if (!url || !key) return null;
  client ??= createClient(url, key, { auth: { persistSession: false } });
  return client;
}

export type BriefData = {
  stories: Story[];
  stocks: Stock[];
  signals: Signal[];
  lastRun: string | null;
};

const LOOKBACK_DAYS = 45;

export async function loadBrief(): Promise<BriefData> {
  const c = db();
  if (!c) return loadSample(); // running locally without keys

  const since = new Date(Date.now() - LOOKBACK_DAYS * 864e5).toISOString();
  const [stories, stocks, signals, runs] = await Promise.all([
    c.from("stories").select("*").gte("last_at", since).order("last_at", { ascending: false }).limit(1000),
    c.from("stocks").select("symbol,name,sector,size,weight"),
    c.from("signals").select("*").gte("date", since.slice(0, 10)).limit(5000),
    c.from("ingest_runs").select("finished_at").eq("ok", true).order("finished_at", { ascending: false }).limit(1),
  ]);
  for (const r of [stories, stocks, signals]) if (r.error) throw new Error(r.error.message);

  return {
    stories: (stories.data ?? []) as Story[],
    stocks: ((stocks.data ?? []) as Stock[]).map((s) => ({ ...s, weight: Number(s.weight) })),
    signals: ((signals.data ?? []) as Signal[]).map(numericSignal),
    lastRun: runs.data?.[0]?.finished_at ?? null,
  };
}

export async function loadFilings(storyId: string): Promise<Filing[]> {
  const c = db();
  if (!c) return [];
  const { data, error } = await c
    .from("filings")
    .select("id,filed_at,subject,detail,attachment_url,category")
    .eq("story_id", storyId)
    .order("filed_at", { ascending: false });
  if (error) throw new Error(error.message);
  return (data ?? []) as Filing[];
}

async function loadSample(): Promise<BriefData> {
  const r = await fetch("/sample.json");
  if (!r.ok) throw new Error("No database configured and no local sample found.");
  const d = await r.json();
  return {
    stories: d.stories,
    stocks: d.stocks.map((s: Stock) => ({ ...s, weight: Number(s.weight) })),
    signals: d.signals.map(numericSignal),
    lastRun: null,
  };
}

function numericSignal(s: Signal): Signal {
  const n = (v: unknown) => (v === null || v === undefined ? null : Number(v));
  return { ...s, ret: Number(s.ret), ret_vs_index: n(s.ret_vs_index), vol: n(s.vol), z: n(s.z), vol_mult: n(s.vol_mult) };
}
