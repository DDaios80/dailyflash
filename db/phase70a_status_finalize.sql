-- Phase 70a — draft / finalize lifecycle for excom status snapshots (2026-05-14)
--
-- Phase 70 stored every submission immediately as visible to the chair. User
-- feedback: ExCom members want to save drafts, review formatted output, edit,
-- then explicitly submit. Two-state lifecycle:
--
--   is_finalized = false  (draft, member workspace, NOT shown in chair's
--                          assembled agenda Block A)
--   is_finalized = true   (submitted, official input, shown in agenda)
--
-- Editing a finalized snapshot REVERTS it to draft — member must re-finalize.
-- This prevents accidental edits from going live without re-confirmation.
--
-- Apply via Supabase SQL editor.

-- ─── 1. Add is_finalized column ─────────────────────────────────────────

alter table excom_status_snapshots
  add column if not exists is_finalized boolean not null default false,
  add column if not exists finalized_at timestamptz;

create index if not exists idx_excom_status_finalized
  on excom_status_snapshots(meeting_date, is_finalized);

-- ─── 2. Patch excom_submit_status: edits revert to draft ────────────────
-- An edit (revision) clears the finalized flag. The member must call
-- excom_finalize_status() again to re-submit. This is the safety mechanism:
-- you can't accidentally have stale text show in the chair's agenda after
-- editing.

create or replace function excom_submit_status(
  p_meeting_date date,
  p_bullets text[]
) returns uuid
  language plpgsql security definer set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_member_name text;
  v_existing_id uuid;
  v_result_id uuid;
begin
  if current_user_role() not in ('admin','management') then
    raise exception 'committee only' using errcode = '42501';
  end if;

  if array_length(p_bullets, 1) is null or array_length(p_bullets, 1) = 0 then
    raise exception 'at least one bullet required';
  end if;

  if array_length(p_bullets, 1) > 10 then
    raise exception 'maximum 10 bullets allowed';
  end if;

  insert into monday_agendas (meeting_date, generated_by, total_items)
    values (p_meeting_date, 'manual_excom_submit', 0)
    on conflict (meeting_date) do nothing;

  select coalesce(u.raw_user_meta_data->>'full_name', u.email)
    into v_member_name
    from auth.users u where u.id = v_user_id;

  select id into v_existing_id
    from excom_status_snapshots
    where meeting_date = p_meeting_date
      and member_user_id = v_user_id;

  if v_existing_id is not null then
    -- Edit path: revert to draft, bump revision count.
    update excom_status_snapshots set
      bullets = p_bullets,
      member_name = v_member_name,
      updated_at = now(),
      revision_count = revision_count + 1,
      is_finalized = false,
      finalized_at = null
    where id = v_existing_id;
    v_result_id := v_existing_id;
  else
    -- First save: created as draft.
    insert into excom_status_snapshots (
      meeting_date, member_user_id, member_name, bullets, is_finalized
    ) values (
      p_meeting_date, v_user_id, v_member_name, p_bullets, false
    ) returning id into v_result_id;
  end if;

  return v_result_id;
end $$;

grant execute on function excom_submit_status(date, text[])
  to authenticator, anon, authenticated, service_role;


-- ─── 3. New RPC: excom_finalize_status ──────────────────────────────────
-- Sets is_finalized = true for the current user's draft for that meeting.
-- Member must have already saved bullets (excom_submit_status). This is
-- the explicit "submit to Chair" action.

create or replace function excom_finalize_status(
  p_meeting_date date
) returns void
  language plpgsql security definer set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_existing record;
begin
  if current_user_role() not in ('admin','management') then
    raise exception 'committee only' using errcode = '42501';
  end if;

  select id, bullets, is_finalized
    into v_existing
    from excom_status_snapshots
    where meeting_date = p_meeting_date
      and member_user_id = v_user_id;

  if not found then
    raise exception 'no draft to finalize - call excom_submit_status first';
  end if;

  if array_length(v_existing.bullets, 1) is null or array_length(v_existing.bullets, 1) = 0 then
    raise exception 'cannot finalize empty bullets';
  end if;

  if v_existing.is_finalized then
    -- Idempotent: re-finalizing an already-finalized snapshot just updates
    -- the timestamp. No error.
    update excom_status_snapshots set
      finalized_at = now()
    where id = v_existing.id;
    return;
  end if;

  update excom_status_snapshots set
    is_finalized = true,
    finalized_at = now()
  where id = v_existing.id;
end $$;

grant execute on function excom_finalize_status(date)
  to authenticator, anon, authenticated, service_role;


-- ─── 4. New RPC: excom_unfinalize_status ────────────────────────────────
-- Reverts a finalized snapshot back to draft. Member-or-super_admin only.
-- Useful when a member realizes their submission has an error post-finalize.

create or replace function excom_unfinalize_status(
  p_meeting_date date
) returns void
  language plpgsql security definer set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_existing_id uuid;
begin
  if current_user_role() not in ('admin','management') then
    raise exception 'committee only' using errcode = '42501';
  end if;

  select id into v_existing_id
    from excom_status_snapshots
    where meeting_date = p_meeting_date
      and member_user_id = v_user_id;

  if not found then
    raise exception 'no snapshot to revert';
  end if;

  update excom_status_snapshots set
    is_finalized = false,
    finalized_at = null
  where id = v_existing_id;
end $$;

grant execute on function excom_unfinalize_status(date)
  to authenticator, anon, authenticated, service_role;


-- ─── 5. Update monday_agenda_full_view to separate finalized vs draft ───
-- block_A_status_snapshots: only finalized (this is what renders in agenda)
-- pending_status_drafts: drafts not yet finalized (chair sees who's drafted)

create or replace function monday_agenda_full_view(p_meeting_date date default null)
  returns jsonb
  language plpgsql stable security definer set search_path = public
as $$
declare
  v_meeting_date date := coalesce(p_meeting_date, next_monday());
  v_agenda monday_agendas;
  v_snapshots jsonb;
  v_drafts jsonb;
  v_items jsonb;
  v_total_time int;
begin
  if current_user_role() not in ('admin','management') then
    raise exception 'committee only' using errcode = '42501';
  end if;

  select * into v_agenda from monday_agendas where meeting_date = v_meeting_date;

  if not found then
    return jsonb_build_object(
      'meeting_date', v_meeting_date,
      'exists', false
    );
  end if;

  -- Finalized snapshots: visible in the chair's Block A.
  select coalesce(jsonb_agg(
    jsonb_build_object(
      'id', s.id,
      'member_user_id', s.member_user_id,
      'member_name', s.member_name,
      'bullets', s.bullets,
      'submitted_at', s.submitted_at,
      'updated_at', s.updated_at,
      'finalized_at', s.finalized_at,
      'revision_count', s.revision_count
    ) order by s.member_name
  ), '[]'::jsonb)
  into v_snapshots
  from excom_status_snapshots s
  where s.meeting_date = v_meeting_date
    and s.is_finalized = true;

  -- Pending drafts: not yet finalized. Chair sees member names (not bullets)
  -- to know who's working but hasn't submitted. The member sees their own
  -- draft bullets via a separate query (RLS write_excom_status_self).
  select coalesce(jsonb_agg(
    jsonb_build_object(
      'member_user_id', s.member_user_id,
      'member_name', s.member_name,
      'updated_at', s.updated_at,
      'bullet_count', array_length(s.bullets, 1)
    ) order by s.member_name
  ), '[]'::jsonb)
  into v_drafts
  from excom_status_snapshots s
  where s.meeting_date = v_meeting_date
    and s.is_finalized = false;

  select coalesce(jsonb_agg(
    jsonb_build_object(
      'id', a.id,
      'block', a.block,
      'position', a.position,
      'title', a.title,
      'description', a.description,
      'owner_label', a.owner_label,
      'owner_user_id', a.owner_user_id,
      'item_type', a.item_type,
      'time_minutes', a.time_minutes,
      'source_idea_id', a.source_idea_id
    ) order by a.block, a.position
  ), '[]'::jsonb)
  into v_items
  from agenda_items a
  where a.meeting_date = v_meeting_date;

  select coalesce(sum(time_minutes), 0) + 15
    into v_total_time
    from agenda_items
    where meeting_date = v_meeting_date;

  return jsonb_build_object(
    'meeting_date', v_meeting_date,
    'exists', true,
    'duration_minutes', v_agenda.duration_minutes,
    'generated_at', v_agenda.generated_at,
    'generated_by', v_agenda.generated_by,
    'published_at', v_agenda.published_at,
    'published_by_user_id', v_agenda.published_by_user_id,
    'published_markdown', v_agenda.published_markdown,
    'pre_reads', v_agenda.pre_reads,
    'meeting_started_at', v_agenda.meeting_started_at,
    'meeting_ended_at', v_agenda.meeting_ended_at,
    'block_A_status_snapshots', v_snapshots,
    'pending_status_drafts', v_drafts,
    'agenda_items', v_items,
    'total_time_allocated_minutes', v_total_time,
    'time_buffer_minutes', v_agenda.duration_minutes - v_total_time
  );
end $$;

grant execute on function monday_agenda_full_view(date)
  to authenticator, anon, authenticated, service_role;


-- ─── 6. PostgREST cache reload ─────────────────────────────────────────

notify pgrst, 'reload schema';


-- ─── 7. Verification ────────────────────────────────────────────────────

select column_name, data_type, is_nullable, column_default
from information_schema.columns
where table_schema = 'public'
  and table_name = 'excom_status_snapshots'
  and column_name in ('is_finalized', 'finalized_at')
order by column_name;

-- Expected: 2 rows.
--   finalized_at  | timestamp with time zone | YES | (null)
--   is_finalized  | boolean                  | NO  | false

select proname
from pg_proc
where pronamespace = 'public'::regnamespace
  and proname in ('excom_submit_status', 'excom_finalize_status', 'excom_unfinalize_status', 'monday_agenda_full_view')
order by proname;

-- Expected: 4 rows (all 4 RPCs present).
