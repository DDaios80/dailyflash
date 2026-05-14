-- Phase 65.1 — Edit-in-place capability for idea_responses (2026-05-14)
--
-- Lovable shipped Phase 65 (a) — the response thread + idea_add_response
-- RPC + backfilled the existing committee_response as the first thread
-- entry. But did NOT ship the edit-in-place capability the user requested
-- the same day:
--
--   "allow the person to whom an answer was assigned to edit a response
--    they have filed as resolved"
--
-- This migration closes that gap. It adds two columns to idea_responses
-- (updated_at, revision_count) and a new SECURITY DEFINER RPC that lets
-- the original author (or super_admin) edit a response in place. The
-- created_at timestamp stays — only the body, updated_at, and
-- revision_count change on edit.
--
-- Permission model: only the original author can edit their own response.
-- Super_admin can edit any response (for moderation/correction). Chair
-- rotation across weeks doesn't grant edit access — the new chair creates
-- a NEW response (via idea_add_response) rather than editing the old one.
-- This preserves the audit trail.
--
-- Apply via Supabase SQL editor.

-- ─── 1. Add tracking columns ───────────────────────────────────────────

alter table idea_responses
  add column if not exists updated_at timestamptz,
  add column if not exists revision_count int not null default 0;

-- ─── 2. Edit RPC ───────────────────────────────────────────────────────

create or replace function idea_response_edit(
  p_response_id uuid,
  p_new_text text
) returns void
  language plpgsql security definer set search_path = public
as $$
declare
  v_author uuid;
begin
  -- Minimum-length guard, same as idea_solvable_now's threshold.
  if p_new_text is null or length(trim(p_new_text)) < 10 then
    raise exception 'response must be at least 10 characters';
  end if;

  -- Look up the original author. NULL means the response is a backfilled
  -- legacy entry (from when committee_response was a single column with no
  -- author attribution) — there's no clear "owner" to authorize the edit.
  select author_user_id into v_author
    from idea_responses where id = p_response_id;

  if not found then
    raise exception 'response not found';
  end if;

  if v_author is null then
    if not is_super_admin() then
      raise exception 'backfilled responses without author attribution can only be edited by super_admin'
        using errcode = '42501';
    end if;
  elsif v_author <> auth.uid() and not is_super_admin() then
    raise exception 'only the original author can edit this response'
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

-- ─── 3. PostgREST cache reload ─────────────────────────────────────────

notify pgrst, 'reload schema';

-- ─── 4. Verification ───────────────────────────────────────────────────

select
  column_name,
  data_type,
  is_nullable,
  column_default
from information_schema.columns
where table_name = 'idea_responses'
  and column_name in ('updated_at', 'revision_count')
order by column_name;

-- Expected output: 2 rows
--   revision_count | integer                  | NO  | 0
--   updated_at     | timestamp with time zone | YES |
