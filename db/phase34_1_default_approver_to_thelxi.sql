-- Phase 34.1 — Reassign default approver from Dimitris to Thelxi.
--
-- Operations decision: Thelxi Smyrnaki (rooms division manageress + admin)
-- is the default approver for all FAM trips, site inspections, and groups
-- going forward. Earlier today we'd flipped the default to Dimitris after
-- the "first admin found" heuristic mistakenly picked Thelxi — that flip
-- is now reversed at Dimitris's explicit request.
--
-- Three things this migration does:
--   1. Reassign every currently pending_approval row (fam_trips +
--      site_inspections + groups) to Thelxi.
--   2. Generate fresh approval tokens (the Dimitris-tokens leaked into
--      his inbox; rotating eliminates any race where he clicks a stale
--      email after this).
--   3. Re-fire the approval emails via cron_private.secrets so Thelxi
--      gets fresh emails with working buttons (Phase 32 bulletproof
--      pattern).
--
-- The env var ONEDRIVE_FAM_IMPORT_USER_ID on Railway and the Lovable
-- site-inspection auto-import default also need to flip — those are
-- separate config changes outside SQL scope.

do $$
declare
  v_thelxi_id   uuid := 'd58e34cb-1d2e-492d-bbae-987fc0a80176';   -- Thelxi
  v_thelxi_name text;
  v_inspection_url text;
  v_famtrip_url    text;
  v_group_url      text;
  v_secret text;
  v_token text;
  v_row record;
begin
  -- Validate Thelxi exists and is admin or management
  select coalesce(
           (u.raw_user_meta_data ->> 'full_name'),
           (u.raw_user_meta_data ->> 'name'),
           u.email
         )
  into v_thelxi_name
  from auth.users u
  inner join user_roles ur on ur.user_id = u.id
  where u.id = v_thelxi_id and ur.role in ('admin','management');
  if v_thelxi_name is null then
    raise exception 'Thelxi user_id % not found or lacks admin/management role', v_thelxi_id;
  end if;

  -- Pull the email-send URLs + pipeline secret
  select value into v_inspection_url from cron_private.secrets where key = 'send_inspection_approval_url';
  select value into v_famtrip_url    from cron_private.secrets where key = 'send_fam_trip_approval_url';
  select value into v_group_url      from cron_private.secrets where key = 'send_group_approval_url';
  select value into v_secret         from cron_private.secrets where key = 'pipeline_secret';

  -- ── Site inspections still pending — reassign + re-fire ───────────
  for v_row in
    select id, travel_agency from site_inspections
    where status = 'pending_approval'
      and approver_user_id is distinct from v_thelxi_id
  loop
    v_token := replace(gen_random_uuid()::text, '-', '');
    update site_inspections set
      approver_user_id = v_thelxi_id,
      approver_name    = v_thelxi_name,
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
      raise notice 'reassigned inspection % and fired email', v_row.travel_agency;
    end if;
  end loop;

  -- ── FAM trips still pending — reassign + re-fire ──────────────────
  for v_row in
    select id, name from fam_trips
    where status = 'pending_approval'
      and approver_user_id is distinct from v_thelxi_id
  loop
    v_token := replace(gen_random_uuid()::text, '-', '');
    update fam_trips set
      approver_user_id = v_thelxi_id,
      approver_name    = v_thelxi_name,
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
      raise notice 'reassigned FAM trip % and fired email', v_row.name;
    end if;
  end loop;

  -- ── Groups still pending — reassign + re-fire (if Phase 34 applied) ──
  for v_row in
    select id, name from groups
    where approval_status = 'pending_approval'
      and approver_user_id is distinct from v_thelxi_id
  loop
    v_token := replace(gen_random_uuid()::text, '-', '');
    update groups set
      approver_user_id = v_thelxi_id,
      approver_name    = v_thelxi_name,
      approval_token   = v_token
    where id = v_row.id;

    if v_group_url is not null and v_secret is not null then
      perform net.http_post(
        url := v_group_url,
        headers := jsonb_build_object(
          'Content-Type', 'application/json',
          'Authorization', 'Bearer ' || v_secret
        ),
        body := jsonb_build_object('group_id', v_row.id::text)
      );
      raise notice 'reassigned group % and fired email', v_row.name;
    elsif v_group_url is null then
      raise notice 'group % reassigned (send_group_approval_url not configured yet — admin needs UI)', v_row.name;
    end if;
  end loop;
exception when undefined_table then
  -- groups table or approval_status column may not exist if Phase 34
  -- hasn't been applied yet. Continue silently — Phase 34 SQL handles
  -- groups separately.
  raise notice 'groups loop skipped (Phase 34 schema not applied yet)';
end $$;
