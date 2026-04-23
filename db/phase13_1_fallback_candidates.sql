-- Phase 13.1 — list eligible users for the upload fallback role.
--
-- Lets the admin settings UI render a dropdown of real hotel staff instead
-- of a free-text email field, so rotating Kyrillos out (or expanding
-- coverage during holidays) is a one-click change.
--
-- Candidates = users with roles that would reasonably have Opera PMS access:
-- admin, management, guest_relations, reservations, front_office.
--
-- The admin UI resolves the selected user_id to an email (or reads email
-- directly from the RPC output) and stores it in the existing key
-- `app_settings.upload_fallback_recipient_email` — no schema change needed.

create or replace function list_upload_fallback_candidates()
  returns table(user_id uuid, email text, display_name text, role text)
  language sql stable security definer set search_path = public
as $$
  select u.id, u.email,
         coalesce(
           (u.raw_user_meta_data ->> 'full_name'),
           (u.raw_user_meta_data ->> 'name'),
           u.email
         ) as display_name,
         ur.role::text
  from auth.users u
  inner join user_roles ur on ur.user_id = u.id
  where ur.role in ('admin', 'management', 'guest_relations', 'reservations', 'front_office')
  order by ur.role, display_name;
$$;
grant execute on function list_upload_fallback_candidates() to authenticated;
