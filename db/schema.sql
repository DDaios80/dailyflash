-- Daios Cove Daily Flash — Supabase schema
-- Phase 1: uploads, reservations, special_attention_overrides, daily_briefing, pool_heating
-- Phase 2/3 placeholders: comment_extractions, alister_findings

create extension if not exists "pgcrypto";

-- ─── Uploads ────────────────────────────────────────────────────────────────
create table if not exists uploads (
  id uuid primary key default gen_random_uuid(),
  filename text not null,
  uploaded_at timestamptz not null default now(),
  report_date date not null,
  row_count int not null,
  uploaded_by uuid references auth.users(id)
);

create index if not exists uploads_report_date_idx on uploads (report_date desc);

-- ─── Reservations (one row per Opera reservation per upload) ────────────────
create table if not exists reservations (
  id uuid primary key default gen_random_uuid(),
  upload_id uuid not null references uploads(id) on delete cascade,

  -- Opera natural keys
  resv_name_id bigint,
  confirmation_no text,
  guest_name_id bigint,

  -- Status
  resv_status text not null,

  -- Guest
  guest_name text,
  guest_first_name text,
  guest_middle_name text,
  guest_title text,
  guest_title_desc text,
  guest_email text,
  guest_phone text,
  birth_date date,
  guest_country text,
  guest_country_desc text,
  nationality text,
  guest_language text,
  guest_language_desc text,

  -- Stay
  arrival timestamptz,
  departure timestamptz,
  arrival_time text,
  departure_time text,
  actual_check_in_date timestamptz,
  actual_check_out_date timestamptz,
  nights int,
  adults int,
  children int,
  pax int,
  ages text,
  cribs int,
  extra_beds int,
  accompanying_names text,

  -- Room
  room text,
  room_category_label text,
  booked_room_category_label text,
  room_class text,

  -- Classification
  vip text,
  guest_vip_desc text,
  special_requests text,
  comments text,
  house_use_yn text,
  complimentary_yn text,

  -- Source
  travel_agent_name text,
  group_name text,
  source_code text,
  source_desc text,
  source_name text,
  market_code text,
  market_desc text,
  channel text,
  company_name text,
  rate_code text,
  block_code text,
  no_of_rooms int,
  total_rate numeric,
  booked_date timestamptz,

  -- Everything else from Opera
  raw jsonb not null,

  created_at timestamptz not null default now(),
  unique (upload_id, resv_name_id)
);

create index if not exists reservations_upload_idx on reservations (upload_id);
create index if not exists reservations_arrival_idx on reservations (arrival);
create index if not exists reservations_departure_idx on reservations (departure);
create index if not exists reservations_room_idx on reservations (room);
create index if not exists reservations_status_idx on reservations (resv_status);
create index if not exists reservations_vip_idx on reservations (vip) where vip is not null;
create index if not exists reservations_comp_idx on reservations (complimentary_yn) where complimentary_yn = 'Y';
create index if not exists reservations_market_idx on reservations (market_desc);

-- ─── Guest Relations: promote someone to special attention ──────────────────
create table if not exists special_attention_overrides (
  id uuid primary key default gen_random_uuid(),
  report_date date not null,
  room text not null,
  reason text,
  added_by uuid references auth.users(id),
  added_at timestamptz not null default now(),
  unique (report_date, room)
);

-- ─── Daily admin-entered data (not in PMS export) ───────────────────────────
create table if not exists daily_briefing (
  report_date date primary key,
  mod_name text,
  mod_phone text,
  weather jsonb,
  hotel_events text,
  site_inspections text,
  group_events text,
  show_rooms text,
  notes text,
  updated_by uuid references auth.users(id),
  updated_at timestamptz not null default now()
);

-- ─── Pool heating / fence schedule ──────────────────────────────────────────
create table if not exists pool_heating (
  id uuid primary key default gen_random_uuid(),
  report_date date not null,
  room text not null,
  service text not null,           -- 'HP' | 'PF' | 'HP/PF'
  end_date date,
  source text not null default 'manual', -- 'comments' | 'manual'
  unique (report_date, room)
);

-- ─── Phase 2 placeholder: LLM extractions from COMMENTS ─────────────────────
create table if not exists comment_extractions (
  id uuid primary key default gen_random_uuid(),
  reservation_id uuid not null references reservations(id) on delete cascade,
  allergies_present boolean not null default false,
  allergies_text text,
  pool_fence boolean not null default false,
  pool_heating boolean not null default false,
  free_transfer boolean not null default false,
  free_upgrade boolean not null default false,
  lco boolean not null default false,
  honeymoon boolean not null default false,
  amenities text[],
  ops_notes text,
  extracted_at timestamptz not null default now()
);

create index if not exists comment_extractions_allergy_idx
  on comment_extractions (allergies_present) where allergies_present = true;

-- ─── Phase 3 cache: persistent per-person findings, TTL'd in code ──────────
-- Avoids re-researching repeater guests. Keyed by (first, last, nationality);
-- upserted on every research call. Matching is case-insensitive on names.
create table if not exists researched_subjects (
  id uuid primary key default gen_random_uuid(),
  first_name text not null,
  last_name text not null,
  nationality text,
  matched_name text,
  relationship text,
  is_notable boolean not null,
  confidence int not null check (confidence between 0 and 100),
  category text,
  summary text,
  reasoning text,
  evidence_urls text[],
  researched_at timestamptz not null default now(),
  unique (first_name, last_name, nationality)
);

create index if not exists researched_subjects_recent_idx
  on researched_subjects (researched_at desc);

alter table researched_subjects enable row level security;
drop policy if exists read_researched_subjects on researched_subjects;
create policy read_researched_subjects on researched_subjects
  for select using (auth.role() = 'authenticated');

-- ─── Phase 3 placeholder: A-lister findings ─────────────────────────────────
create table if not exists alister_findings (
  id uuid primary key default gen_random_uuid(),
  reservation_id uuid not null references reservations(id) on delete cascade,
  matched_name text not null,
  relationship text not null,        -- 'self' | 'partner' | 'accompanying'
  is_notable boolean not null,
  confidence int not null check (confidence between 0 and 100),
  category text,
  summary text,
  evidence_urls text[],
  researched_at timestamptz not null default now()
);

create index if not exists alister_findings_notable_idx
  on alister_findings (is_notable, confidence desc) where is_notable = true;

-- ─── RLS (roles wired in Phase 4; policies stay permissive for service role) ─
alter table uploads                    enable row level security;
alter table reservations               enable row level security;
alter table special_attention_overrides enable row level security;
alter table daily_briefing             enable row level security;
alter table pool_heating               enable row level security;
alter table comment_extractions        enable row level security;
alter table alister_findings           enable row level security;

-- Permissive read for authenticated users; tightened by role in Phase 4.
create policy if not exists read_uploads               on uploads                    for select using (auth.role() = 'authenticated');
create policy if not exists read_reservations          on reservations               for select using (auth.role() = 'authenticated');
create policy if not exists read_overrides             on special_attention_overrides for select using (auth.role() = 'authenticated');
create policy if not exists read_briefing              on daily_briefing             for select using (auth.role() = 'authenticated');
create policy if not exists read_pool_heating          on pool_heating               for select using (auth.role() = 'authenticated');
create policy if not exists read_comment_extractions   on comment_extractions        for select using (auth.role() = 'authenticated');
create policy if not exists read_alister_findings      on alister_findings           for select using (auth.role() = 'authenticated');
