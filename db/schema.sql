-- FINN schema. Paste into the Supabase SQL editor and run it. Safe to run again.
-- The browser reads with the anon key (select only, via RLS). Everything else
-- is written by the pipeline with the service key, which bypasses RLS.

create table if not exists stocks (
    symbol       text primary key,
    name         text not null,
    sector       text not null,
    size         text not null,          -- large / mid / small
    weight       numeric(5, 2) not null, -- % of the demo portfolio, sums to 100
    in_portfolio boolean not null default true
);

create table if not exists prices (
    symbol text not null,
    date   date not null,
    open   numeric,
    high   numeric,
    low    numeric,
    close  numeric not null,
    volume bigint,
    primary key (symbol, date)
);

-- One row per stock per trading day. Only the raw numbers: thresholds are
-- applied by whoever reads them (the app or the Telegram sender), so changing
-- your rules re-ranks straight away, with no new ingest.
create table if not exists signals (
    symbol       text not null,
    date         date not null,
    ret          numeric not null, -- day return, %
    ret_vs_index numeric,          -- stock return minus Nifty 50 return, % points
    vol          numeric,          -- 20-day stdev of daily returns, %
    z            numeric,          -- ret / vol
    vol_mult     numeric,          -- volume / 20-day average volume
    primary key (symbol, date)
);

create table if not exists stories (
    id           text primary key,  -- symbol:category:first filing id
    symbol       text not null references stocks (symbol),
    category     text not null,
    headline     text not null,
    summary      text,
    first_at     timestamptz not null,
    last_at      timestamptz not null,
    filing_count int not null default 1,
    lead_url     text,              -- the most informative attachment
    base_score   numeric not null,  -- category weight + position weight
    card         jsonb,             -- depends on the category, see web/lib/types.ts
    card_status  text not null default 'pending', -- pending / done / failed / skipped
    updated_at   timestamptz not null default now()
);
create index if not exists stories_last_at on stories (last_at desc);

create table if not exists filings (
    id             text primary key, -- NSE seq_id
    symbol         text not null,
    filed_at       timestamptz not null,
    subject        text not null,
    detail         text,
    attachment_url text,
    category       text not null,
    classified_by  text not null,    -- rule / model
    story_id       text references stories (id),
    raw            jsonb
);
create index if not exists filings_symbol_filed on filings (symbol, filed_at desc);

create table if not exists watch_rules (
    chat_id    bigint primary key,   -- Telegram chat
    mode       text not null default 'fixed', -- fixed / vs_index / volatility
    move_pct   numeric not null default 5,
    z_mult     numeric not null default 2.5,
    vol_mult   numeric not null default 2,
    categories text[] not null default '{results,management_change,M&A,fund_raise,pledging}',
    created_at timestamptz not null default now()
);

create table if not exists sent_alerts (
    chat_id bigint not null,
    key     text not null,            -- s:<story id> or m:<symbol>:<date>
    sent_at timestamptz not null default now(),
    primary key (chat_id, key)
);

create table if not exists research_cache (
    key        text primary key,     -- sha256 of the source url, prompt version and kind
    value      jsonb not null,
    created_at timestamptz not null default now()
);

create table if not exists usage (
    id         bigint generated always as identity primary key,
    at         timestamptz not null default now(),
    provider   text not null,
    model      text not null,
    purpose    text not null,
    tokens_in  int,
    tokens_out int,
    cache_hit  boolean not null default false
);

create table if not exists ingest_runs (
    id          bigint generated always as identity primary key,
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    ok          boolean,
    note        text
);

alter table stocks         enable row level security;
alter table prices         enable row level security;
alter table signals        enable row level security;
alter table stories        enable row level security;
alter table filings        enable row level security;
alter table watch_rules    enable row level security;
alter table research_cache enable row level security;
alter table sent_alerts    enable row level security;
alter table usage          enable row level security;
alter table ingest_runs    enable row level security;

drop policy if exists "public read" on stocks;
create policy "public read" on stocks      for select using (true);
drop policy if exists "public read" on prices;
create policy "public read" on prices      for select using (true);
drop policy if exists "public read" on signals;
create policy "public read" on signals     for select using (true);
drop policy if exists "public read" on stories;
create policy "public read" on stories     for select using (true);
drop policy if exists "public read" on filings;
create policy "public read" on filings     for select using (true);
drop policy if exists "public read" on ingest_runs;
create policy "public read" on ingest_runs for select using (true);
-- watch_rules, sent_alerts, research_cache and usage stay server-only.
