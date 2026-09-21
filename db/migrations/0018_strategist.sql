-- 0018_strategist.sql
--
-- Conversations with the AI Strategist.
--
-- Two ideas, and the second is the one that matters:
--
--   1. A conversation is tenant data like any other. Same one-predicate RLS,
--      same organization_id, no exceptions for the shiny feature.
--
--   2. EVERY TURN RECORDS WHICH TOOLS IT CALLED AND WHAT THEY RETURNED THE
--      SHAPE OF. When a customer says "it told me my traffic fell 40%", the
--      only useful reply is to look at which query ran, over which window,
--      and what it returned. Without that, a disputed sentence is one
--      person's memory against another's.
--
-- The model never sees this table and never writes to it. It answers, the
-- application stores.

create table conversations (
    id              uuid primary key default gen_random_uuid(),
    organization_id uuid not null references organizations(id) on delete cascade,
    website_id      uuid not null references websites(id) on delete cascade,
    created_by      uuid references users(id) on delete set null,
    -- Taken from the first question rather than generated: a title that needed
    -- a model call would make opening a chat cost money before it answered
    -- anything.
    title           text,
    message_count   int not null default 0,
    created_at      timestamptz not null default now(),
    last_message_at timestamptz not null default now()
);
create index on conversations (website_id, last_message_at desc);

create table conversation_messages (
    id              bigserial primary key,
    organization_id uuid not null,
    conversation_id uuid not null references conversations(id) on delete cascade,
    -- Explicit rather than relying on id ordering: a bigserial is shared
    -- across every tenant, so gaps in it say nothing useful about a single
    -- conversation's shape.
    seq             int not null,
    role            text not null check (role in ('user', 'assistant')),
    content         text not null default '',

    -- The audit trail: [{"tool": "get_movers", "input": {...},
    --                    "rows": 12, "ms": 34, "error": null}, ...]
    -- Inputs are recorded because a wrong answer is usually a wrong window,
    -- and the window is in the input.
    steps           jsonb not null default '[]'::jsonb,

    -- Accountability, exactly as for every other generation (0011): what
    -- produced these words, under which prompt, and why it stopped.
    model           text,
    model_provider  text,
    prompt_version  text,
    stop_reason     text,

    created_at      timestamptz not null default now(),
    unique (conversation_id, seq)
);
create index on conversation_messages (conversation_id, seq);

comment on column conversation_messages.steps is
    'Which typed tools ran, with their inputs and row counts. The evidence behind an answer, kept so a disputed sentence can be traced rather than remembered.';

-- ---------------------------------------------------------------------------
-- RLS, the same one predicate as everything else.
-- ---------------------------------------------------------------------------
alter table conversations         enable row level security;
alter table conversation_messages enable row level security;

create policy conversations_read on conversations for select
    using (app.is_org_member(organization_id));
create policy conversations_write on conversations for all
    using (app.can_write_org(organization_id))
    with check (app.can_write_org(organization_id));

create policy conversation_messages_read on conversation_messages for select
    using (app.is_org_member(organization_id));
create policy conversation_messages_write on conversation_messages for all
    using (app.can_write_org(organization_id))
    with check (app.can_write_org(organization_id));
