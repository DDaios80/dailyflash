-- Phase 56 — backfill ideas.acknowledged_at for rows where the inbound
-- email handler stored committee_response but forgot to stamp the
-- acknowledged fields.
--
-- Root cause (still pending in the inbound-reply edge function): the
-- handler that processes chair replies to flash@daioscove.com captures
-- the reply text into ideas.committee_response and updates updated_at,
-- but doesn't stamp acknowledged_at / acknowledged_by. Result: the
-- dashboard shows the idea as "Awaiting committee review" even though
-- the reply is captured and the response email already went out to the
-- submitter.
--
-- This SQL repairs every historical row in that broken state. It
-- preserves the original updated_at as the acknowledgment moment so
-- audit trails stay accurate.
--
-- acknowledged_by is left NULL for historical rows because we don't
-- have a reliable signal for which chair actually replied to each
-- one. Future replies should set both fields atomically (handled by
-- the Lovable handoff in docs/lovable-handoff-2026-05-09-ideas-inbound-handler.md).
--
-- Idempotent: only updates rows where acknowledged_at is NULL.

update ideas
set acknowledged_at = updated_at
where committee_response is not null
  and length(committee_response) > 0
  and acknowledged_at is null;

-- Verify
select
  count(*) filter (where acknowledged_at is null
                    and committee_response is not null
                    and length(committee_response) > 0) as still_broken,
  count(*) filter (where acknowledged_at is not null) as acknowledged,
  count(*) as total
from ideas;
