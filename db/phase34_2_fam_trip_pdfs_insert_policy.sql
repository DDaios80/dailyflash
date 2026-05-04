-- Phase 34.2 — Add the missing INSERT policy on fam-trip-pdfs storage bucket.
--
-- User report: 'system does not allow us to upload a pdf file in Beleon
-- FAM trip'. Diagnostic showed the bucket has DELETE, SELECT, and UPDATE
-- policies but NO INSERT policy. With RLS enabled, missing policies block
-- the operation entirely. OneDrive auto-imports work because they go
-- through the service role (bypassing RLS), but interactive uploads from
-- Lovable's dashboard UI use the user's JWT and hit the RLS gate.
--
-- Fix: add an INSERT policy that mirrors the existing UPDATE policy (admin
-- / management / guest_relations / sales can insert into fam-trip-pdfs).
-- Also defensively check site-inspection-pdfs since it likely has the same
-- gap from the same Phase 9 / Phase 8 era.

-- ─── fam-trip-pdfs INSERT ────────────────────────────────────────────────
drop policy if exists "fam_trip_pdfs_insert" on storage.objects;
create policy "fam_trip_pdfs_insert"
  on storage.objects for insert
  with check (
    bucket_id = 'fam-trip-pdfs'
    and current_user_role() = any (array[
      'admin'::user_role,
      'management'::user_role,
      'guest_relations'::user_role,
      'sales'::user_role
    ])
  );

-- ─── site-inspection-pdfs INSERT (defensive — same gap likely) ──────────
do $$ begin
  -- Only add if the bucket exists; some older deployments may not have
  -- a separate inspection PDF bucket.
  if exists (select 1 from storage.buckets where id = 'site-inspection-pdfs') then
    drop policy if exists "site_inspection_pdfs_insert" on storage.objects;
    create policy "site_inspection_pdfs_insert"
      on storage.objects for insert
      with check (
        bucket_id = 'site-inspection-pdfs'
        and current_user_role() = any (array[
          'admin'::user_role,
          'management'::user_role,
          'guest_relations'::user_role,
          'sales'::user_role
        ])
      );
    raise notice 'Added INSERT policy on site-inspection-pdfs bucket';
  else
    raise notice 'Skipped site-inspection-pdfs (bucket not present)';
  end if;
end $$;

-- Verify
select policyname, cmd
from pg_policies
where schemaname = 'storage' and tablename = 'objects'
  and qual::text ilike '%fam-trip-pdfs%' or with_check::text ilike '%fam-trip-pdfs%'
order by cmd;
