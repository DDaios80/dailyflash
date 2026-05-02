-- Phase 28 backfill — set approver + token on the OneDrive auto-imports
-- that landed before the edge function was patched (commit 98273b1).
--
-- These trips have status='pending_approval' but no approver_user_id,
-- approver_name, or approval_token, so the approval email function
-- silently fails. This backfill assigns the admin (Dimitris) as
-- approver, generates a token, and re-fires the approval email via the
-- existing submit-side cron_private.secrets path.
--
-- Idempotent — only updates rows that are missing the approver.

do $$
declare
  v_admin_id uuid;
  v_admin_name text;
  v_admin_email text;
  v_trip record;
  v_url text;
  v_secret text;
  v_token text;
begin
  -- Find the admin (one row — Dimitris is the only admin)
  select u.id, u.email,
         coalesce((u.raw_user_meta_data ->> 'full_name'),
                  (u.raw_user_meta_data ->> 'name'),
                  u.email)
  into v_admin_id, v_admin_email, v_admin_name
  from auth.users u
  inner join user_roles ur on ur.user_id = u.id
  where ur.role = 'admin'
  limit 1;

  if v_admin_id is null then
    raise exception 'no admin user found';
  end if;

  -- Pull the email-send URL + pipeline secret for net.http_post
  select value into v_url    from cron_private.secrets where key = 'send_fam_trip_approval_url';
  select value into v_secret from cron_private.secrets where key = 'pipeline_secret';

  -- Each unbacked OneDrive auto-import: assign approver, generate token, fire email
  for v_trip in
    select id, name from fam_trips
    where created_by_name = 'OneDrive auto-import'
      and status = 'pending_approval'
      and (approver_user_id is null or approval_token is null)
  loop
    v_token := replace(gen_random_uuid()::text, '-', '');

    update fam_trips set
      approver_user_id = v_admin_id,
      approver_name    = v_admin_name,
      approval_token   = v_token
    where id = v_trip.id;

    if v_url is not null and v_secret is not null then
      perform net.http_post(
        url := v_url,
        headers := jsonb_build_object(
          'Content-Type', 'application/json',
          'Authorization', 'Bearer ' || v_secret
        ),
        body := jsonb_build_object('trip_id', v_trip.id::text)
      );
      raise notice 'fired approval email for %', v_trip.name;
    else
      raise warning 'send_fam_trip_approval_url or pipeline_secret missing — email not sent for %', v_trip.name;
    end if;
  end loop;
end $$;
