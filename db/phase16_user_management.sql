-- Phase 16 — User management (admin tools).
--
-- Adds admin lifecycle operations on users (reset / suspend / unsuspend /
-- delete / role_change) + a bulk credentials email tool. All destructive
-- operations are audit-logged in admin_user_events so there is always a
-- record of who did what to whom.
--
-- Supabase Auth admin operations (deleteUser, updateUserById, generateLink)
-- require the service_role key — they are invoked from the
-- admin-user-actions edge function, not from PostgREST. This migration
-- only adds the audit + batch tables plus helper RPCs.

-- ─── Audit log ──────────────────────────────────────────────────────────────
do $$ begin
  create type admin_user_action as enum (
    'reset_password',
    'suspend',
    'unsuspend',
    'delete',
    'role_change',
    'send_credentials',
    'credentials_batch_sent',
    'credentials_batch_failed'
  );
exception when duplicate_object then null; end $$;

create table if not exists admin_user_events (
  id               uuid primary key default gen_random_uuid(),
  actor_user_id    uuid references auth.users(id),
  target_user_id   uuid references auth.users(id),
  action           admin_user_action not null,
  details          jsonb not null default '{}'::jsonb,
  created_at       timestamptz not null default now()
);

create index if not exists admin_user_events_actor_idx
  on admin_user_events (actor_user_id, created_at desc);
create index if not exists admin_user_events_target_idx
  on admin_user_events (target_user_id, created_at desc);
create index if not exists admin_user_events_action_idx
  on admin_user_events (action, created_at desc);

alter table admin_user_events enable row level security;
drop policy if exists read_admin_user_events on admin_user_events;
create policy read_admin_user_events on admin_user_events
  for select using (can_admin());
-- No write policy — edge function uses service role.


-- ─── Bulk credentials batches ──────────────────────────────────────────────
create table if not exists credential_email_batches (
  id               uuid primary key default gen_random_uuid(),
  created_by       uuid references auth.users(id),
  subject_template text not null,
  body_template    text not null,
  total            int  not null default 0,
  sent             int  not null default 0,
  failed           int  not null default 0,
  status           text not null default 'in_progress'
                   check (status in ('in_progress','completed','failed')),
  created_at       timestamptz not null default now(),
  completed_at     timestamptz
);

create index if not exists credential_email_batches_created_idx
  on credential_email_batches (created_at desc);

alter table credential_email_batches enable row level security;
drop policy if exists read_credential_email_batches on credential_email_batches;
create policy read_credential_email_batches on credential_email_batches
  for select using (can_admin());


create table if not exists credential_email_deliveries (
  id                uuid primary key default gen_random_uuid(),
  batch_id          uuid not null references credential_email_batches(id) on delete cascade,
  recipient_email   text not null,
  status            text not null check (status in ('sent','failed','skipped')),
  resend_message_id text,
  error             text,
  sent_at           timestamptz not null default now()
);

create index if not exists credential_email_deliveries_batch_idx
  on credential_email_deliveries (batch_id, sent_at);

alter table credential_email_deliveries enable row level security;
drop policy if exists read_credential_email_deliveries on credential_email_deliveries;
create policy read_credential_email_deliveries on credential_email_deliveries
  for select using (can_admin());

-- CRITICAL: no password column anywhere. XLSX contents (including plaintext
-- passwords) are parsed client-side, passed in-memory to the edge function,
-- sent via Resend, then discarded. Never persisted.


-- ─── RPC: list all users for the /admin/users table ────────────────────────
create or replace function list_all_users()
  returns table (
    user_id       uuid,
    email         text,
    display_name  text,
    role          text,
    banned_until  timestamptz,
    last_sign_in  timestamptz,
    created_at    timestamptz,
    is_self       boolean
  )
  language sql stable security definer set search_path = public
as $$
  select
    u.id,
    u.email,
    coalesce(
      (u.raw_user_meta_data ->> 'full_name'),
      (u.raw_user_meta_data ->> 'name'),
      u.email
    ) as display_name,
    ur.role::text as role,
    u.banned_until,
    u.last_sign_in_at,
    u.created_at,
    u.id = auth.uid() as is_self
  from auth.users u
  left join user_roles ur on ur.user_id = u.id
  where can_admin()
  order by u.created_at desc;
$$;
grant execute on function list_all_users() to authenticated;


-- ─── RPC: event history for a specific target user ─────────────────────────
create or replace function list_user_events(p_target_user_id uuid)
  returns table (
    event_id     uuid,
    action       text,
    actor_email  text,
    actor_name   text,
    details      jsonb,
    created_at   timestamptz
  )
  language sql stable security definer set search_path = public
as $$
  select
    e.id,
    e.action::text,
    actor.email,
    coalesce(
      (actor.raw_user_meta_data ->> 'full_name'),
      (actor.raw_user_meta_data ->> 'name'),
      actor.email
    ),
    e.details,
    e.created_at
  from admin_user_events e
  left join auth.users actor on actor.id = e.actor_user_id
  where e.target_user_id = p_target_user_id
    and can_admin()
  order by e.created_at desc
  limit 100;
$$;
grant execute on function list_user_events(uuid) to authenticated;


-- ─── Helper: count remaining admins (prevents last-admin lockout) ─────────
create or replace function count_active_admins()
  returns int
  language sql stable security definer set search_path = public
as $$
  select count(*)::int
  from user_roles ur
  inner join auth.users u on u.id = ur.user_id
  where ur.role = 'admin'
    and (u.banned_until is null or u.banned_until < now());
$$;
grant execute on function count_active_admins() to authenticated;


-- ─── Post-migration: admin primes 1 secret ─────────────────────────────────
-- insert into cron_private.secrets (key, value) values
--   ('admin_user_actions_url',
--    'https://wgbghdbfmapuqbfeiygb.supabase.co/functions/v1/admin-user-actions')
-- on conflict (key) do update set value = excluded.value, updated_at = now();
