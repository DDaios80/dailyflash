-- Phase 57 — committee response emails from a dedicated sender address.
--
-- Currently the response email Maria received when Valia replied was
-- sent from the default flash@... address. Operations preference:
-- committee responses should come from a clearly committee-branded
-- address so submitters immediately recognise it as the chair's
-- official reply.
--
-- This row is read by the inbound-reply edge function (the same one
-- Phase 56 patches to stamp acknowledged_at). When it sends the
-- response email to the idea submitter, it uses this From address.
--
-- Also a NAME prefix is included in the value (RFC 5322 friendly)
-- so the inbox shows "Daios Cove Committee" not just the address.
--
-- Idempotent.

insert into public.app_settings (key, value, updated_at)
values (
  'committee_email_from',
  'Daios Cove Committee <committee@daioscove.com>',
  now()
)
on conflict (key) do update set
  value = excluded.value,
  updated_at = now();

-- Verify
select key, value, updated_at
from app_settings
where key = 'committee_email_from';
