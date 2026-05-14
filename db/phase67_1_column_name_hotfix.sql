-- Phase 67.1 — Column name hotfix (2026-05-14)
--
-- Phase 66 and Phase 67 assumed the assignee column on `ideas` was named
-- `assigned_user_id`. The ACTUAL column is `assigned_to_user_id` (with
-- `_to_`), plus there's a denormalized `assigned_to_name` for display.
--
-- Both migrations therefore install functions that error at runtime:
--   - Phase 66 trigger errors on every new idea INSERT (blocks all submissions)
--   - Phase 67 idea_add_response errors on call (blocks all response submissions)
--
-- This hotfix:
-- 1. Recreates _autoassign_idea_to_chair using assigned_to_user_id +
--    populates assigned_to_name from the chair's display name
-- 2. Recreates idea_add_response using assigned_to_user_id (the bare
--    function body otherwise unchanged from Phase 67)
--
-- Apply via Supabase SQL editor.

-- ─── 1. Fix Phase 66 trigger ────────────────────────────────────────────

create or replace function _autoassign_idea_to_chair()
  returns trigger
  language plpgsql
  security definer
  set search_path = public
as $$
declare
  v_chair_id uuid;
  v_chair_name text;
begin
  if new.assigned_to_user_id is null then
    v_chair_id := current_committee_chair_id();
    if v_chair_id is not null then
      select coalesce(u.raw_user_meta_data->>'full_name', u.email)
        into v_chair_name
        from auth.users u where u.id = v_chair_id;
      new.assigned_to_user_id := v_chair_id;
      new.assigned_to_name := v_chair_name;
    end if;
  end if;
  return new;
end $$;

-- ─── 2. Fix Phase 67 idea_add_response ──────────────────────────────────
-- Only change vs Phase 67: assigned_user_id → assigned_to_user_id

create or replace function idea_add_response(
  p_idea_id uuid,
  p_status idea_status,
  p_response_text text,
  p_response_is_public boolean default false
) returns uuid
  language plpgsql security definer set search_path = public
as $$
declare
  v_response_id uuid;
  v_assignee_id uuid;
  v_assignee_name text;
  v_assignee_role text;
  v_current_status idea_status;
begin
  if p_response_text is null or length(trim(p_response_text)) < 10 then
    raise exception 'response must be at least 10 characters';
  end if;

  select assigned_to_user_id, status
    into v_assignee_id, v_current_status
    from ideas where id = p_idea_id;

  if not found then
    raise exception 'idea not found';
  end if;

  if v_assignee_id is null then
    raise exception 'idea has no assignee — cannot determine response author. Assign the idea first via committee_update_idea.';
  end if;

  if p_status = v_current_status then
    raise exception 'new status equals current status (% = %); response not added (use idea_response_edit to amend the latest response in place)',
      p_status, v_current_status;
  end if;

  select coalesce(u.raw_user_meta_data->>'full_name', u.email)
    into v_assignee_name
    from auth.users u where u.id = v_assignee_id;

  select role::text into v_assignee_role
    from user_roles where user_id = v_assignee_id;

  insert into idea_responses (
    idea_id,
    status_at_response,
    response_text,
    author_user_id,
    author_name,
    author_role,
    submitted_by_user_id
  ) values (
    p_idea_id,
    p_status,
    p_response_text,
    v_assignee_id,
    v_assignee_name,
    v_assignee_role,
    auth.uid()
  ) returning id into v_response_id;

  update ideas set
    status = p_status,
    committee_response = p_response_text,
    committee_response_is_public = p_response_is_public,
    updated_at = now()
  where id = p_idea_id;

  return v_response_id;
end $$;

grant execute on function idea_add_response(uuid, idea_status, text, boolean)
  to authenticator, anon, authenticated, service_role;

-- ─── 3. PostgREST cache reload ─────────────────────────────────────────

notify pgrst, 'reload schema';

-- ─── 4. Quick smoke test of column references ──────────────────────────
-- Confirms both column names that Phase 67.1 uses actually exist.

select
  column_name,
  data_type
from information_schema.columns
where table_name = 'ideas'
  and column_name in ('assigned_to_user_id', 'assigned_to_name', 'status', 'committee_response', 'committee_response_is_public')
order by column_name;

-- Expected: 5 rows (one per column).
