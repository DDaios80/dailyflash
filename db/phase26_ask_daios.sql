-- Phase 26 — "Ask Daios" voice assistant.
--
-- 26.1: text-only Q&A backend. Voice in/out comes in 26.2 / 26.3.
--
-- Audit-logs every Q&A so we can see which questions staff actually
-- ask, surface them on /super-admin → Engagement, and use them as
-- training signal for prompt tuning + future graph queries.

-- ─── Language preference (per user) ────────────────────────────────────────
-- Reuses gamification_prefs.language if set, else defaults to 'en'.
-- No new column needed.


-- ─── Q&A log ──────────────────────────────────────────────────────────────
create table if not exists daios_qa_log (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid references auth.users(id) on delete set null,
  user_role         text,
  user_name         text,                                    -- denormalised at log time
  question          text not null check (length(question) between 1 and 5000),
  language          text not null default 'en' check (language in ('en','el')),
  answer            text,
  -- Snapshot of which payload sections were sent to Claude. Useful for
  -- replaying without re-fetching, and for understanding which buckets
  -- staff lean on. Don't include PII beyond what the role can already see.
  context_keys      text[],
  context_token_count int,
  prompt_tokens     int,
  completion_tokens int,
  cache_read_tokens int,
  cost_usd          numeric(10,5),
  latency_ms        int,
  model             text default 'claude-opus-4-7',
  client            text default 'web' check (client in ('web','voice','api')),
  error             text,
  created_at        timestamptz not null default now()
);

create index if not exists daios_qa_log_user_idx
  on daios_qa_log (user_id, created_at desc);

create index if not exists daios_qa_log_recent_idx
  on daios_qa_log (created_at desc);

create index if not exists daios_qa_log_role_idx
  on daios_qa_log (user_role, created_at desc) where user_role is not null;


-- ─── RLS ──────────────────────────────────────────────────────────────────
alter table daios_qa_log enable row level security;

drop policy if exists daios_qa_log_read on daios_qa_log;
create policy daios_qa_log_read on daios_qa_log
  for select using (user_id = auth.uid() or is_super_admin());

drop policy if exists daios_qa_log_insert on daios_qa_log;
create policy daios_qa_log_insert on daios_qa_log
  for insert with check (user_id = auth.uid());
-- ^ The edge function uses service role to bypass RLS for inserts, so the
--   above is belt-and-braces.


-- ─── My recent conversation (used by the chat panel) ──────────────────────
create or replace function my_recent_qa(p_limit int default 10)
  returns table (
    id          uuid,
    question    text,
    answer      text,
    language    text,
    latency_ms  int,
    created_at  timestamptz
  )
  language sql stable security invoker as $$
  select id, question, answer, language, latency_ms, created_at
  from daios_qa_log
  where user_id = auth.uid()
    and answer is not null
  order by created_at desc
  limit p_limit;
$$;
grant execute on function my_recent_qa(int) to authenticated;


-- ─── Super admin: top questions / topics ──────────────────────────────────
create or replace function daios_qa_overview(p_window_days int default 30)
  returns jsonb
  language plpgsql stable security definer set search_path = public
as $$
declare v jsonb;
begin
  if not is_super_admin() then
    raise exception 'super admin only' using errcode = '42501';
  end if;
  select jsonb_build_object(
    'total_questions', (select count(*) from daios_qa_log
                        where created_at >= now() - (p_window_days || ' days')::interval),
    'unique_users',    (select count(distinct user_id) from daios_qa_log
                        where created_at >= now() - (p_window_days || ' days')::interval),
    'median_latency_ms', (
      select round(percentile_cont(0.5) within group (order by latency_ms))
      from daios_qa_log
      where created_at >= now() - (p_window_days || ' days')::interval
        and latency_ms is not null
    ),
    'errors_count', (select count(*) from daios_qa_log
                     where created_at >= now() - (p_window_days || ' days')::interval
                       and error is not null),
    'total_cost_usd', (select round(coalesce(sum(cost_usd), 0)::numeric, 2)
                       from daios_qa_log
                       where created_at >= now() - (p_window_days || ' days')::interval),
    'by_role', (
      select coalesce(jsonb_agg(jsonb_build_object('role', user_role, 'count', n)
                       order by n desc), '[]'::jsonb)
      from (select user_role, count(*) as n from daios_qa_log
            where created_at >= now() - (p_window_days || ' days')::interval
              and user_role is not null
            group by user_role) t
    ),
    'recent_questions', (
      select coalesce(jsonb_agg(jsonb_build_object(
        'question', question,
        'role', user_role,
        'latency_ms', latency_ms,
        'created_at', created_at
      ) order by created_at desc), '[]'::jsonb)
      from (select * from daios_qa_log
            where created_at >= now() - interval '7 days'
            order by created_at desc limit 50) t
    )
  ) into v;
  return v;
end $$;
grant execute on function daios_qa_overview(int) to authenticated;


-- ─── Soft delete for privacy: scrub questions older than 90 days ─────────
-- Schedule via pg_cron daily at 03:30 Athens.
create or replace function daios_qa_scrub_old()
  returns int
  language plpgsql security definer set search_path = public
as $$
declare v_count int;
begin
  with del as (
    delete from daios_qa_log
    where created_at < now() - interval '90 days'
    returning 1
  )
  select count(*) into v_count from del;
  return v_count;
end $$;

do $$ declare j_id bigint;
begin
  select jobid into j_id from cron.job where jobname = 'daios-qa-scrub';
  if j_id is not null then perform cron.unschedule(j_id); end if;
  perform cron.schedule(
    'daios-qa-scrub',
    '30 0 * * *',  -- 00:30 UTC = ~03:30 Athens summer
    'select daios_qa_scrub_old();'
  );
end $$;


-- ─── ask_daios_url secret slot ────────────────────────────────────────────
-- Optional — only needed if any pg_cron job ever wants to call the edge fn.
-- (Not used by Phase 26.1; reserved for 26.5 conversation-memory-trigger.)
