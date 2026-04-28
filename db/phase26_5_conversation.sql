-- Phase 26.5 — Conversation memory for Ask Daios.
--
-- Adds conversation_id to daios_qa_log so we can group turns within
-- a single drawer-open session. Frontend generates a UUID when the
-- drawer opens; resets when the drawer closes.
--
-- Backend uses the most recent N rows by conversation_id to assemble
-- the message history sent to Claude on each new turn.

alter table daios_qa_log
  add column if not exists conversation_id uuid;

create index if not exists daios_qa_log_conversation_idx
  on daios_qa_log (conversation_id, created_at)
  where conversation_id is not null;


-- Helper RPC: fetch the last N turns of the current conversation for
-- the calling user. The frontend can use this if it wants to rehydrate
-- the drawer with prior turns, but typical use is the frontend keeps
-- history client-side and just passes it on the next call.
create or replace function my_conversation_turns(
  p_conversation_id uuid,
  p_limit int default 10
)
  returns table (
    role        text,            -- 'user' or 'assistant'
    content     text,
    created_at  timestamptz
  )
  language sql stable security invoker as $$
  with rows as (
    select question, answer, created_at
    from daios_qa_log
    where user_id = auth.uid()
      and conversation_id = p_conversation_id
      and answer is not null
    order by created_at desc
    limit p_limit
  )
  select * from (
    select 'user'::text as role, question as content, created_at from rows
    union all
    select 'assistant'::text as role, answer as content, created_at + interval '1 millisecond' from rows
  ) t
  order by created_at;
$$;
grant execute on function my_conversation_turns(uuid, int) to authenticated;
