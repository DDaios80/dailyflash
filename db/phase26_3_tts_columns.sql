-- Phase 26.3 — Add TTS telemetry columns to daios_qa_log so we can
-- track ElevenLabs usage per Q&A row.

alter table daios_qa_log
  add column if not exists tts_chars      int,
  add column if not exists tts_cost_usd   numeric(10,5),
  add column if not exists tts_voice_id   text,
  add column if not exists tts_model      text,
  add column if not exists tts_latency_ms int;

-- Update the super-admin overview to include TTS spend.
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
    'llm_cost_usd', (select round(coalesce(sum(cost_usd), 0)::numeric, 2)
                     from daios_qa_log
                     where created_at >= now() - (p_window_days || ' days')::interval),
    'tts_cost_usd', (select round(coalesce(sum(tts_cost_usd), 0)::numeric, 2)
                     from daios_qa_log
                     where created_at >= now() - (p_window_days || ' days')::interval),
    'total_cost_usd', (select round(coalesce(sum(coalesce(cost_usd,0) + coalesce(tts_cost_usd,0)), 0)::numeric, 2)
                       from daios_qa_log
                       where created_at >= now() - (p_window_days || ' days')::interval),
    'voice_questions_pct', (
      select case when count(*) = 0 then null
        else round(100.0 * count(*) filter (where client = 'voice') / count(*), 1)
      end
      from daios_qa_log
      where created_at >= now() - (p_window_days || ' days')::interval
    ),
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
        'language', language,
        'client', client,
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
