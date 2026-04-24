-- Phase 22.1 — Fix evaluator column names.
--
-- Two bugs in the original Phase 22 evaluate_achievements():
--   * fam_trips.created_by     → should be created_by_user_id (Phase 9)
--   * site_inspections.created_by → should be created_by_user_id (Phase 8)
-- Also simplifies the early_bird rule to a single HAVING ≥ 10 check
-- instead of the insert-then-delete hack.

create or replace function evaluate_achievements()
  returns int
  language plpgsql security definer set search_path = public
as $$
declare
  v_inserted int := 0;
begin
  -- first_voice: first idea submitted
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select i.submitter_user_id, 'first_voice', min(i.created_at)
  from ideas i
  where i.submitter_user_id is not null
  group by i.submitter_user_id
  on conflict do nothing;
  get diagnostics v_inserted = row_count;

  -- heard: first committee response on own idea
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select i.submitter_user_id, 'heard', min(i.updated_at)
  from ideas i
  where i.submitter_user_id is not null
    and i.committee_response is not null
  group by i.submitter_user_id
  on conflict do nothing;

  -- prolific_voice: 5 ideas
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select i.submitter_user_id, 'prolific_voice', now()
  from ideas i
  where i.submitter_user_id is not null
  group by i.submitter_user_id
  having count(*) >= 5
  on conflict do nothing;

  -- host: first FAM trip created (column: created_by_user_id)
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select f.created_by_user_id, 'host', min(f.created_at)
  from fam_trips f
  where f.created_by_user_id is not null
  group by f.created_by_user_id
  on conflict do nothing;

  -- inspector: first site inspection created (column: created_by_user_id)
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select si.created_by_user_id, 'inspector', min(si.created_at)
  from site_inspections si
  where si.created_by_user_id is not null
  group by si.created_by_user_id
  on conflict do nothing;

  -- committee_member: any committee_update action on an idea
  -- (tracked via admin_user_events). Will naturally fire once Phase 22.2
  -- wires admin_user_events logging into committee_update_idea RPC.
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select distinct ae.actor_user_id, 'committee_member', min(ae.created_at)
  from admin_user_events ae
  where ae.action = 'committee_update'
  group by ae.actor_user_id
  on conflict do nothing;

  -- early_bird: 10 distinct Athens-local days with a page_view before 09:00
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select pe.user_id, 'early_bird', now()
  from platform_events pe
  where pe.event_type = 'page_view'
    and pe.user_id is not null
    and extract(hour from (pe.created_at at time zone 'Europe/Athens')) < 9
  group by pe.user_id
  having count(distinct (pe.created_at at time zone 'Europe/Athens')::date) >= 10
  on conflict do nothing;

  -- consistent: briefing_best >= 7
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select user_id, 'consistent', updated_at
  from user_streaks
  where briefing_best >= 7
  on conflict do nothing;

  -- reliable: upload_best >= 7
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select user_id, 'reliable', updated_at
  from user_streaks
  where upload_best >= 7
  on conflict do nothing;

  -- explorer: 5 distinct paths visited
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select pe.user_id, 'explorer', now()
  from platform_events pe
  where pe.event_type = 'page_view' and pe.user_id is not null
  group by pe.user_id
  having count(distinct pe.path) >= 5
  on conflict do nothing;

  -- veteran: 30 distinct sign-in days
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select pe.user_id, 'veteran', now()
  from platform_events pe
  where pe.event_type = 'page_view' and pe.user_id is not null
  group by pe.user_id
  having count(distinct (pe.created_at at time zone 'Europe/Athens')::date) >= 30
  on conflict do nothing;

  -- polyglot: gamification_prefs.language has been set to both en and el
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select user_id, 'polyglot', now()
  from gamification_language_history
  group by user_id
  having count(distinct language) >= 2
  on conflict do nothing;

  return v_inserted;
end $$;
