-- Phase 14.1 — FAM trip daily itineraries.
--
-- Extends fam_trips with a JSONB column holding day-by-day activities so
-- the calendar can render per-day drawers (and teams can navigate the
-- fortnight meaningfully without opening the full PDF).
--
-- Shape of itinerary_by_day:
--   {
--     "2026-04-25": [
--       {"time": "13:10", "title": "Gatwick arrival", "detail": "22 pax, EZY8215"},
--       ...
--     ],
--     "2026-04-26": [...]
--   }
--
-- Populated by: edge function `parse-fam-trip-itinerary` (Claude Haiku 4.5
-- reading the uploaded PDF natively). Triggered automatically on FAM trip
-- approval, plus a manual "Re-parse" button on the detail page.

alter table fam_trips
  add column if not exists itinerary_by_day    jsonb,
  add column if not exists itinerary_parsed_at timestamptz,
  add column if not exists itinerary_parse_error text;

-- Helper: fire the parse-fam-trip-itinerary edge function for a given trip.
-- Called from the admin UI ("Re-parse" button) and from the FAM trip
-- approval edge function once the PDF is known to be stable.
create or replace function trigger_parse_fam_trip_itinerary(p_trip_id uuid)
  returns void
  language plpgsql security definer set search_path = cron_private, public
as $$
declare
  v_url    text;
  v_secret text;
begin
  select value into v_url    from cron_private.secrets where key = 'parse_fam_trip_itinerary_url';
  select value into v_secret from cron_private.secrets where key = 'pipeline_secret';
  if v_url is null or v_secret is null then
    raise notice 'cron_private.secrets missing parse_fam_trip_itinerary_url or pipeline_secret';
    return;
  end if;
  perform net.http_post(
    url     := v_url,
    headers := jsonb_build_object(
      'Content-Type',  'application/json',
      'Authorization', 'Bearer ' || v_secret
    ),
    body    := jsonb_build_object('trip_id', p_trip_id::text)
  );
end $$;
grant execute on function trigger_parse_fam_trip_itinerary(uuid) to authenticated;


-- Post-migration: admin primes the secret (after edge function is deployed).
-- insert into cron_private.secrets (key, value) values
--   ('parse_fam_trip_itinerary_url',
--    'https://wgbghdbfmapuqbfeiygb.supabase.co/functions/v1/parse-fam-trip-itinerary')
-- on conflict (key) do update set value = excluded.value, updated_at = now();
