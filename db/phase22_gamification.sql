-- Phase 22 — Mild gamification.
--
-- Design principles (see Notion "Phase 22 — Gamification" for rationale):
--   * Hotel-grade, private-first. No public leaderboards below GM.
--   * Reward service-aligned participation, not raw clicks.
--   * Bilingual (EN + EL) milestone copy.
--   * Opt-out per user. Defaults on.
--   * Nightly evaluation at 03:15 Athens so unlocks surface at breakfast,
--     never mid-work.
--
-- No edge functions — everything is SQL + pg_cron. UI reads via RPCs.

-- ─── Per-user preferences ─────────────────────────────────────────────────
create table if not exists gamification_prefs (
  user_id               uuid primary key references auth.users(id) on delete cascade,
  enabled               boolean not null default true,
  notify_on_unlock      boolean not null default true,
  language              text not null default 'en' check (language in ('en','el')),
  updated_at            timestamptz not null default now()
);

alter table gamification_prefs enable row level security;

drop policy if exists gamification_prefs_read on gamification_prefs;
create policy gamification_prefs_read on gamification_prefs
  for select using (user_id = auth.uid() or is_super_admin());

drop policy if exists gamification_prefs_write on gamification_prefs;
create policy gamification_prefs_write on gamification_prefs
  for all using (user_id = auth.uid())
  with check (user_id = auth.uid());


-- ─── Streak counters (denormalised for fast reads) ────────────────────────
create table if not exists user_streaks (
  user_id               uuid primary key references auth.users(id) on delete cascade,
  daily_login_current   int not null default 0,
  daily_login_best      int not null default 0,
  daily_login_last_date date,
  briefing_current      int not null default 0,
  briefing_best         int not null default 0,
  briefing_last_date    date,
  upload_current        int not null default 0,
  upload_best           int not null default 0,
  upload_last_date      date,
  updated_at            timestamptz not null default now()
);

alter table user_streaks enable row level security;

drop policy if exists user_streaks_read on user_streaks;
create policy user_streaks_read on user_streaks
  for select using (user_id = auth.uid() or is_super_admin());


-- ─── Milestone catalogue (EN + EL) ────────────────────────────────────────
create table if not exists milestones (
  key                text primary key,
  name_en            text not null,
  name_el            text not null,
  description_en     text not null,
  description_el     text not null,
  category           text not null default 'general'
                     check (category in ('voice','hospitality','hygiene','general','service')),
  sort_order         int not null default 100,
  is_active          boolean not null default true,
  created_at         timestamptz not null default now()
);

-- Read-only for anyone signed in; mutations super-admin only.
alter table milestones enable row level security;

drop policy if exists milestones_read on milestones;
create policy milestones_read on milestones
  for select using (is_active);

drop policy if exists milestones_write on milestones;
create policy milestones_write on milestones
  for all using (is_super_admin()) with check (is_super_admin());


-- Seed the initial catalogue. Shippable milestones only — data-dependent
-- ones (9+ Week, Passkey Pioneer, etc.) are added in later phases when
-- the underlying signals exist.
insert into milestones (key, name_en, name_el, description_en, description_el, category, sort_order) values
  ('first_voice',
   'First Voice', 'Πρώτη Φωνή',
   'Submitted your first idea, opinion, or issue.',
   'Υποβάλατε την πρώτη σας ιδέα, γνώμη ή θέμα.',
   'voice', 10),
  ('heard',
   'Heard', 'Ακούστηκε',
   'Received the first committee response on one of your ideas.',
   'Λάβατε την πρώτη απάντηση της επιτροπής σε μία από τις ιδέες σας.',
   'voice', 20),
  ('prolific_voice',
   'Prolific Voice', 'Ενεργή Φωνή',
   'Submitted five ideas across any category.',
   'Υποβάλατε πέντε ιδέες σε οποιαδήποτε κατηγορία.',
   'voice', 30),
  ('host',
   'Host', 'Οικοδεσπότης',
   'Approved or submitted your first FAM trip.',
   'Εγκρίνατε ή υποβάλατε το πρώτο σας FAM trip.',
   'hospitality', 40),
  ('inspector',
   'Inspector', 'Επιθεωρητής',
   'Completed your first site inspection.',
   'Ολοκληρώσατε την πρώτη σας επιθεώρηση.',
   'hospitality', 50),
  ('committee_member',
   'Committee Member', 'Μέλος Επιτροπής',
   'Acted on the committee to respond to an idea.',
   'Ενεργήσατε στην επιτροπή για να απαντήσετε σε μια ιδέα.',
   'service', 60),
  ('early_bird',
   'Early Bird', 'Πρωινός Τύπος',
   'Opened the dashboard before 09:00 Athens on ten separate days.',
   'Ανοίξατε τον πίνακα ελέγχου πριν τις 09:00 Αθήνας σε δέκα ξεχωριστές ημέρες.',
   'general', 70),
  ('consistent',
   'Consistent', 'Σταθερός',
   'Saved the daily briefing on time for seven days in a row.',
   'Αποθηκεύσατε την καθημερινή ενημέρωση εγκαίρως για επτά συνεχόμενες ημέρες.',
   'hygiene', 80),
  ('reliable',
   'Reliable', 'Αξιόπιστος',
   'Uploaded the Opera report on time for seven days in a row.',
   'Ανεβάσατε την αναφορά Opera εγκαίρως για επτά συνεχόμενες ημέρες.',
   'hygiene', 90),
  ('explorer',
   'Explorer', 'Εξερευνητής',
   'Visited five different sections of the platform.',
   'Επισκεφθήκατε πέντε διαφορετικά τμήματα της πλατφόρμας.',
   'general', 100),
  ('veteran',
   'Veteran', 'Βετεράνος',
   'Signed in on thirty different days.',
   'Συνδεθήκατε σε τριάντα διαφορετικές ημέρες.',
   'general', 110),
  ('polyglot',
   'Polyglot', 'Πολύγλωσσος',
   'Used the platform in both English and Greek.',
   'Χρησιμοποιήσατε την πλατφόρμα στα Αγγλικά και στα Ελληνικά.',
   'general', 120)
on conflict (key) do update set
  name_en = excluded.name_en, name_el = excluded.name_el,
  description_en = excluded.description_en, description_el = excluded.description_el,
  category = excluded.category, sort_order = excluded.sort_order;


-- ─── Unlocked achievements ────────────────────────────────────────────────
create table if not exists user_achievements (
  user_id       uuid references auth.users(id) on delete cascade,
  milestone_key text references milestones(key),
  unlocked_at   timestamptz not null default now(),
  primary key (user_id, milestone_key)
);

create index if not exists user_achievements_unlocked_idx
  on user_achievements (user_id, unlocked_at desc);

alter table user_achievements enable row level security;

drop policy if exists user_achievements_read on user_achievements;
create policy user_achievements_read on user_achievements
  for select using (user_id = auth.uid() or is_super_admin());


-- ─── Streak recomputation ─────────────────────────────────────────────────
-- Idempotent: safe to run repeatedly. Uses platform_events (page_view),
-- daily_briefing.saved_at, and uploads.created_at as signal sources.
create or replace function recompute_user_streaks()
  returns void
  language plpgsql security definer set search_path = public
as $$
declare
  v_today date := (now() at time zone 'Europe/Athens')::date;
begin
  -- Daily login streak based on page_view events.
  with login_days as (
    select
      user_id,
      ((pe.created_at at time zone 'Europe/Athens')::date) as d
    from platform_events pe
    where pe.event_type = 'page_view' and pe.user_id is not null
    group by user_id, d
  ), ranked as (
    select user_id, d,
           d - (row_number() over (partition by user_id order by d))::int as run_key
    from login_days
  ), runs as (
    select user_id, run_key, count(*) as len, max(d) as last_d
    from ranked group by user_id, run_key
  ), current_runs as (
    select user_id, len, last_d from runs where last_d >= v_today - 1
  ), best_runs as (
    select user_id, max(len) as best from runs group by user_id
  )
  insert into user_streaks (user_id, daily_login_current, daily_login_best, daily_login_last_date, updated_at)
  select
    b.user_id,
    coalesce(c.len, 0),
    b.best,
    c.last_d,
    now()
  from best_runs b
  left join current_runs c on c.user_id = b.user_id
  on conflict (user_id) do update
    set daily_login_current   = excluded.daily_login_current,
        daily_login_best      = greatest(user_streaks.daily_login_best, excluded.daily_login_best),
        daily_login_last_date = excluded.daily_login_last_date,
        updated_at            = now();

  -- Briefing streak — days with a saved briefing (taken from daily_briefing.report_date
  -- whenever the row was saved by a human). Placeholder: use updated_at.
  -- (Refined in Phase 22.1 once we have explicit saved_at on daily_briefing.)
  perform 1; -- no-op guard for this placeholder path
end $$;
grant execute on function recompute_user_streaks() to authenticated;


-- ─── Achievement evaluator ────────────────────────────────────────────────
-- Idempotent: insert-or-ignore per (user_id, milestone_key) composite PK.
-- Called nightly. Cheap enough to re-run fully; no cursor logic needed.
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

  -- host: first FAM trip approved or submitted
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select distinct f.created_by, 'host', min(f.created_at)
  from fam_trips f
  where f.created_by is not null
  group by f.created_by
  on conflict do nothing;

  -- inspector: first site inspection submitted
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select si.created_by, 'inspector', min(si.created_at)
  from site_inspections si
  where si.created_by is not null
  group by si.created_by
  on conflict do nothing;

  -- committee_member: any committee_update action on an idea
  -- (tracked via admin_user_events or ideas.committee_response_by)
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select distinct ae.actor_user_id, 'committee_member', min(ae.created_at)
  from admin_user_events ae
  where ae.action = 'committee_update'
  group by ae.actor_user_id
  on conflict do nothing;

  -- early_bird: 10 distinct days with a page_view before 09:00 Athens
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select pe.user_id, 'early_bird', now()
  from platform_events pe
  where pe.event_type = 'page_view'
    and pe.user_id is not null
    and extract(hour from (pe.created_at at time zone 'Europe/Athens')) < 9
  group by pe.user_id, ((pe.created_at at time zone 'Europe/Athens')::date)
  having count(*) > 0
  on conflict do nothing
  returning 1;
  -- The double-group-by means: one row per (user_id, day) meeting the rule.
  -- We further collapse by inserting with on conflict do nothing so each
  -- user unlocks only once; but we gate on ≥10 days via a separate query:
  delete from user_achievements
  where milestone_key = 'early_bird'
    and user_id in (
      select pe.user_id
      from platform_events pe
      where pe.event_type = 'page_view' and pe.user_id is not null
        and extract(hour from (pe.created_at at time zone 'Europe/Athens')) < 9
      group by pe.user_id
      having count(distinct (pe.created_at at time zone 'Europe/Athens')::date) < 10
    );

  -- consistent: briefing_current >= 7 on any day (from user_streaks)
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

  -- explorer: visited 5 distinct paths (page_view event)
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select pe.user_id, 'explorer', now()
  from platform_events pe
  where pe.event_type = 'page_view' and pe.user_id is not null
  group by pe.user_id
  having count(distinct pe.path) >= 5
  on conflict do nothing;

  -- veteran: logged in on 30 different calendar days
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select pe.user_id, 'veteran', now()
  from platform_events pe
  where pe.event_type = 'page_view' and pe.user_id is not null
  group by pe.user_id
  having count(distinct (pe.created_at at time zone 'Europe/Athens')::date) >= 30
  on conflict do nothing;

  -- polyglot: gamification_prefs.language has been set to both 'en' and 'el'
  -- at some point in the past. We store a marker in a side table once set.
  -- Minimal implementation: check if gamification_prefs_history exists
  -- (seeded below) for both values.
  insert into user_achievements (user_id, milestone_key, unlocked_at)
  select user_id, 'polyglot', now()
  from gamification_language_history
  group by user_id
  having count(distinct language) >= 2
  on conflict do nothing;

  return v_inserted;
end $$;
grant execute on function evaluate_achievements() to authenticated;


-- ─── Language history (for polyglot) ──────────────────────────────────────
create table if not exists gamification_language_history (
  user_id   uuid references auth.users(id) on delete cascade,
  language  text check (language in ('en','el')),
  first_at  timestamptz default now(),
  primary key (user_id, language)
);
alter table gamification_language_history enable row level security;
drop policy if exists glh_read on gamification_language_history;
create policy glh_read on gamification_language_history
  for select using (user_id = auth.uid() or is_super_admin());

-- When gamification_prefs.language is updated, append to the history.
create or replace function record_language_usage()
  returns trigger language plpgsql security definer
as $$
begin
  insert into gamification_language_history (user_id, language)
  values (new.user_id, new.language)
  on conflict do nothing;
  return new;
end $$;

drop trigger if exists trg_record_language on gamification_prefs;
create trigger trg_record_language
  after insert or update of language on gamification_prefs
  for each row execute function record_language_usage();


-- ─── UI RPCs ──────────────────────────────────────────────────────────────

-- Full "Your journey" payload — used by the /my card.
create or replace function my_journey()
  returns table (
    streaks jsonb,
    recent_unlocks jsonb,
    total_unlocked int,
    total_catalog int,
    prefs jsonb
  )
  language plpgsql stable security invoker
as $$
declare
  v_uid uuid := auth.uid();
begin
  return query
  select
    (select to_jsonb(s) - 'user_id' - 'updated_at' from user_streaks s where s.user_id = v_uid),
    coalesce(
      (select jsonb_agg(to_jsonb(x) order by x.unlocked_at desc)
       from (
         select m.key, m.name_en, m.name_el, m.category, ua.unlocked_at
         from user_achievements ua
         join milestones m on m.key = ua.milestone_key
         where ua.user_id = v_uid
         order by ua.unlocked_at desc
         limit 3
       ) x),
      '[]'::jsonb),
    (select count(*)::int from user_achievements where user_id = v_uid),
    (select count(*)::int from milestones where is_active),
    (select to_jsonb(p) - 'user_id' - 'updated_at' from gamification_prefs p where p.user_id = v_uid);
end $$;
grant execute on function my_journey() to authenticated;


-- Full milestone list with unlock state for the current user.
create or replace function my_milestones()
  returns table (
    key text,
    name_en text,
    name_el text,
    description_en text,
    description_el text,
    category text,
    sort_order int,
    unlocked_at timestamptz
  )
  language sql stable security invoker as $$
  select
    m.key, m.name_en, m.name_el, m.description_en, m.description_el,
    m.category, m.sort_order,
    ua.unlocked_at
  from milestones m
  left join user_achievements ua
    on ua.milestone_key = m.key and ua.user_id = auth.uid()
  where m.is_active
  order by m.sort_order, m.key;
$$;
grant execute on function my_milestones() to authenticated;


-- Super-admin Engagement tab.
create or replace function engagement_scoreboard()
  returns jsonb
  language plpgsql stable security definer set search_path = public
as $$
declare
  v jsonb;
begin
  if not is_super_admin() then
    raise exception 'super admin only' using errcode = '42501';
  end if;

  select jsonb_build_object(
    'users_total',       (select count(*) from gamification_prefs),
    'users_enabled',     (select count(*) from gamification_prefs where enabled),
    'unlocks_7d',        (select count(*) from user_achievements where unlocked_at > now() - interval '7 days'),
    'unlocks_30d',       (select count(*) from user_achievements where unlocked_at > now() - interval '30 days'),
    'streaks_login_top', (select jsonb_agg(jsonb_build_object('user_id', user_id, 'current', daily_login_current, 'best', daily_login_best) order by daily_login_current desc)
                          from (select * from user_streaks order by daily_login_current desc limit 10) t),
    'milestones_distribution',
      (select jsonb_agg(jsonb_build_object('key', m.key, 'unlocks', c.n) order by c.n desc)
       from milestones m
       left join lateral (select count(*) as n from user_achievements ua where ua.milestone_key = m.key) c on true
       where m.is_active)
  ) into v;
  return v;
end $$;
grant execute on function engagement_scoreboard() to authenticated;


-- User preference update helper.
create or replace function set_gamification_preference(
  p_enabled boolean default null,
  p_notify_on_unlock boolean default null,
  p_language text default null
) returns jsonb
  language plpgsql security definer set search_path = public
as $$
declare
  v_uid uuid := auth.uid();
begin
  if v_uid is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;

  insert into gamification_prefs (user_id, enabled, notify_on_unlock, language)
  values (v_uid,
          coalesce(p_enabled, true),
          coalesce(p_notify_on_unlock, true),
          coalesce(p_language, 'en'))
  on conflict (user_id) do update set
    enabled          = coalesce(p_enabled, gamification_prefs.enabled),
    notify_on_unlock = coalesce(p_notify_on_unlock, gamification_prefs.notify_on_unlock),
    language         = coalesce(p_language, gamification_prefs.language),
    updated_at       = now();

  return (select to_jsonb(p) - 'user_id' - 'updated_at' from gamification_prefs p where p.user_id = v_uid);
end $$;
grant execute on function set_gamification_preference(boolean, boolean, text) to authenticated;


-- ─── Nightly runner + pg_cron ─────────────────────────────────────────────
create or replace function gamification_nightly()
  returns void
  language plpgsql security definer set search_path = public
as $$
begin
  perform recompute_user_streaks();
  perform evaluate_achievements();
end $$;

do $$
declare j_id bigint;
begin
  select jobid into j_id from cron.job where jobname = 'gamification-nightly';
  if j_id is not null then perform cron.unschedule(j_id); end if;
  -- 03:15 Athens (01:15 UTC winter, 00:15 UTC summer). Use UTC 01:15 so we
  -- drift predictably by one hour across DST — unlocks still surface at
  -- breakfast in both seasons.
  perform cron.schedule(
    'gamification-nightly',
    '15 1 * * *',
    'select gamification_nightly();'
  );
end $$;
