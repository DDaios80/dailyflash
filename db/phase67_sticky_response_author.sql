-- Phase 67 — Sticky author attribution for idea responses (2026-05-14)
--
-- User feedback: "Author = idea's assignee (sticky)". When any user types
-- a response (via idea_add_response), the response author is set to the
-- idea's assigned_user_id (sticky from Phase 66's auto-assign), NOT the
-- caller (auth.uid()). The committee responds with one voice = the
-- assignee's voice.
--
-- Use case: Valia was chair last week and responded to idea cf2f5211. This
-- week Olivier is chair, but the idea still belongs to Valia (her work
-- continues to resolution). When Valia (or Dimitrios on her behalf) types
-- a follow-up resolution response, it should appear as Valia's response
-- in the thread, not as Dimitrios's.
--
-- Phase 66 already made assigned_user_id sticky (set at submission time
-- to the chair-that-week). This phase makes the response thread honor
-- that stickiness for authorship.
--
-- Track the actual submitter separately for audit. Edit permission goes
-- to author OR submitter OR super_admin (so the assignee can edit her
-- own response, and the submitter can edit what they typed on her behalf).
--
-- Apply via Supabase SQL editor.

-- ─── 1. Add submitted_by_user_id column ────────────────────────────────

alter table idea_responses
  add column if not exists submitted_by_user_id uuid references auth.users(id);

-- ─── 2. Backfill ───────────────────────────────────────────────────────
-- For historical rows, best guess: author and submitter were the same.

update idea_responses
set submitted_by_user_id = author_user_id
where submitted_by_user_id is null
  and author_user_id is not null;

-- ─── 3. Recreate idea_add_response with sticky-assignee author ─────────
-- Behavior changes vs Lovable's prior version:
--   - author_user_id   = ideas.assigned_user_id  (was: auth.uid())
--   - author_name      = display name of assignee
--   - author_role      = user_roles.role of assignee
--   - submitted_by_user_id = auth.uid()  (new, who actually clicked submit)
-- Preserved behavior:
--   - Min 10 char response
--   - Refuses if new status equals current status (no-op guard)
--   - Atomic with idea status update + committee_response pointer +
--     committee_response_is_public flag
--   - Returns the new response_id

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

  select assigned_user_id, status
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

-- ─── 4. Update idea_response_edit: allow author OR submitter ───────────

create or replace function idea_response_edit(
  p_response_id uuid,
  p_new_text text
) returns void
  language plpgsql security definer set search_path = public
as $$
declare
  v_author uuid;
  v_submitter uuid;
begin
  if p_new_text is null or length(trim(p_new_text)) < 10 then
    raise exception 'response must be at least 10 characters';
  end if;

  select author_user_id, submitted_by_user_id
    into v_author, v_submitter
    from idea_responses where id = p_response_id;

  if not found then
    raise exception 'response not found';
  end if;

  if not (
    auth.uid() = v_author
    or auth.uid() = v_submitter
    or is_super_admin()
  ) then
    raise exception 'only the author, the submitter, or super_admin can edit this response'
      using errcode = '42501';
  end if;

  update idea_responses set
    response_text = p_new_text,
    updated_at = now(),
    revision_count = revision_count + 1
  where id = p_response_id;
end $$;

grant execute on function idea_response_edit(uuid, text)
  to authenticator, anon, authenticated, service_role;

-- ─── 5. PostgREST cache reload ─────────────────────────────────────────

notify pgrst, 'reload schema';

-- ─── 6. Verification ───────────────────────────────────────────────────

select column_name, data_type, is_nullable
from information_schema.columns
where table_name = 'idea_responses'
  and column_name in ('author_user_id', 'submitted_by_user_id')
order by column_name;

-- Expected: 2 rows. Both uuid. Both nullable.
