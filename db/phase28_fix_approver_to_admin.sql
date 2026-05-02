-- Phase 28 follow-up — reassign mis-approved rows to the actual admin.
--
-- Both the OneDrive FAM trip backfill AND the existing OneDrive site-
-- inspection sync used a "first admin user found" heuristic that hit
-- Thelxi (because she's row 1 in user_roles with role='admin') instead
-- of Dimitris (the operations owner). This fixes:
--
--   - 4 site_inspections rows (created_by_name='OneDrive sync')
--   - 2 fam_trips rows (created_by_name='OneDrive auto-import')
--
-- Each row gets a fresh approval_token and the approval email re-fires
-- via cron_private.secrets path so Dimitris receives the approve/reject
-- email for each.
--
-- Idempotent — only touches rows where approver isn't already the admin.

do $$
declare
  v_admin_id uuid := 'a116987e-4351-459f-8347-14fa6cfdf5ae';   -- Dimitris
  v_admin_name text;
  v_inspection_url text;
  v_famtrip_url text;
  v_secret text;
  v_token text;
  v_row record;
begin
  -- Validate the admin user exists and has role admin
  select coalesce(
           (u.raw_user_meta_data ->> 'full_name'),
           (u.raw_user_meta_data ->> 'name'),
           u.email
         )
  into v_admin_name
  from auth.users u
  inner join user_roles ur on ur.user_id = u.id
  where u.id = v_admin_id and ur.role = 'admin';
  if v_admin_name is null then
    raise exception 'admin user_id % not found or not role=admin', v_admin_id;
  end if;

  -- Pull the email-send URLs + pipeline secret
  select value into v_inspection_url from cron_private.secrets where key = 'send_inspection_approval_url';
  select value into v_famtrip_url    from cron_private.secrets where key = 'send_fam_trip_approval_url';
  select value into v_secret         from cron_private.secrets where key = 'pipeline_secret';

  -- ── Site inspections ────────────────────────────────────────────────
  for v_row in
    select id, travel_agency from site_inspections
    where created_by_name = 'OneDrive sync'
      and status = 'pending_approval'
      and approver_user_id is distinct from v_admin_id
  loop
    v_token := replace(gen_random_uuid()::text, '-', '');
    update site_inspections set
      approver_user_id = v_admin_id,
      approver_name    = v_admin_name,
      approval_token   = v_token
    where id = v_row.id;

    if v_inspection_url is not null and v_secret is not null then
      perform net.http_post(
        url := v_inspection_url,
        headers := jsonb_build_object(
          'Content-Type', 'application/json',
          'Authorization', 'Bearer ' || v_secret
        ),
        body := jsonb_build_object('inspection_id', v_row.id::text)
      );
      raise notice 'reassigned inspection % (% ) and fired email', v_row.travel_agency, v_row.id;
    else
      raise warning 'inspection email URL or secret missing — % not emailed', v_row.travel_agency;
    end if;
  end loop;

  -- ── FAM trips ───────────────────────────────────────────────────────
  for v_row in
    select id, name from fam_trips
    where created_by_name = 'OneDrive auto-import'
      and status = 'pending_approval'
      and approver_user_id is distinct from v_admin_id
  loop
    v_token := replace(gen_random_uuid()::text, '-', '');
    update fam_trips set
      approver_user_id = v_admin_id,
      approver_name    = v_admin_name,
      approval_token   = v_token
    where id = v_row.id;

    if v_famtrip_url is not null and v_secret is not null then
      perform net.http_post(
        url := v_famtrip_url,
        headers := jsonb_build_object(
          'Content-Type', 'application/json',
          'Authorization', 'Bearer ' || v_secret
        ),
        body := jsonb_build_object('trip_id', v_row.id::text)
      );
      raise notice 'reassigned FAM trip % (%) and fired email', v_row.name, v_row.id;
    else
      raise warning 'FAM trip email URL or secret missing — % not emailed', v_row.name;
    end if;
  end loop;
end $$;
