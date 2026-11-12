-- Hourly ingest and keep-alive, run by Supabase itself (pg_cron + pg_net).
--
-- These used to come from GitHub Actions, but its run log showed the "hourly"
-- ingest firing about twice a day at random times (17:13, 00:20, 04:08 IST).
-- GitHub drops scheduled runs for small repos when it's busy. The workflows are
-- still there as a backup; the API skips a run if the previous one is going.
--
-- Paste this into the Supabase SQL editor, replace YOUR_PROBE_TOKEN with the
-- PROBE_TOKEN from your .env, and run it. Running it again just updates the jobs.
-- pg_cron uses UTC: 03:30-13:30 UTC is 09:00-19:00 IST.

create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;

select cron.schedule(
  'finn-ingest',
  '30 3-13 * * 1-5',
  $$
  select net.http_post(
    url := 'https://finn-api-enea.onrender.com/ingest?days=3',
    headers := jsonb_build_object('X-Token', 'YOUR_PROBE_TOKEN'),
    timeout_milliseconds := 60000
  );
  $$
);

-- Render's free tier sleeps after 15 idle minutes and takes about 50 s to wake,
-- so this keeps it up for Telegram replies and research runs.
select cron.schedule(
  'finn-keepalive',
  '*/10 * * * *',
  $$ select net.http_get(url := 'https://finn-api-enea.onrender.com/health', timeout_milliseconds := 60000); $$
);

-- To check on it later:
--   select jobname, schedule, active from cron.job;
--   select start_time, status, return_message from cron.job_run_details order by start_time desc limit 10;
--   select created, status_code from net._http_response order by created desc limit 10;
