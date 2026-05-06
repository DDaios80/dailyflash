-- Phase 44 — Create site-inspection-pdfs storage bucket + policies.
--
-- Mirror of fam-trip-pdfs from Phase 9. Required by the new
-- ingest-site-inspection-from-onedrive edge function. Once this bucket
-- exists, Phase 34.2's defensive INSERT policy block will also fire on
-- the next migration if re-run, but we install all 4 policies here
-- explicitly so the bucket is fully usable on first deploy.
--
-- Idempotent: safe to re-run.

-- Bucket
insert into storage.buckets (id, name, public)
values ('site-inspection-pdfs', 'site-inspection-pdfs', false)
on conflict (id) do nothing;

-- Storage policies — same shape as fam-trip-pdfs (Phase 9).
do $$ begin
  -- Read: any authenticated user (dashboard signed-URL rendering).
  drop policy if exists site_inspection_pdfs_read on storage.objects;
  create policy site_inspection_pdfs_read on storage.objects
    for select using (
      bucket_id = 'site-inspection-pdfs' and auth.role() = 'authenticated'
    );

  -- Insert: admin / management / guest_relations / sales.
  drop policy if exists site_inspection_pdfs_write on storage.objects;
  create policy site_inspection_pdfs_write on storage.objects
    for insert with check (
      bucket_id = 'site-inspection-pdfs'
      and current_user_role() in ('admin','management','guest_relations','sales')
    );

  -- Update: same set as insert.
  drop policy if exists site_inspection_pdfs_update on storage.objects;
  create policy site_inspection_pdfs_update on storage.objects
    for update using (
      bucket_id = 'site-inspection-pdfs'
      and current_user_role() in ('admin','management','guest_relations','sales')
    );

  -- Delete: admin only.
  drop policy if exists site_inspection_pdfs_delete on storage.objects;
  create policy site_inspection_pdfs_delete on storage.objects
    for delete using (
      bucket_id = 'site-inspection-pdfs' and can_admin()
    );
exception when others then
  raise notice 'storage policy setup skipped: %', sqlerrm;
end $$;
