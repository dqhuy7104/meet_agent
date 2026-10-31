CREATE TABLE rag_conversations (
    id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    creator_account_id text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    project_id text,
    title text NOT NULL DEFAULT 'Hội thoại mới',
    config jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rag_conversations_owner_updated ON rag_conversations(creator_account_id,updated_at DESC,id DESC);
CREATE INDEX rag_conversations_updated ON rag_conversations(updated_at DESC,id DESC);

CREATE TABLE rag_conversation_turns (
    id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    conversation_id text NOT NULL REFERENCES rag_conversations(id) ON DELETE CASCADE,
    ordinal integer NOT NULL CHECK(ordinal>0),
    request_id uuid NOT NULL,
    question text NOT NULL,
    config jsonb NOT NULL,
    status text NOT NULL CHECK(status IN ('running','completed','failed')),
    claim_id uuid,
    lease_until timestamptz,
    resolved_question text,
    answer jsonb,
    metadata jsonb NOT NULL DEFAULT '{}',
    failure_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE(conversation_id,ordinal),
    UNIQUE(conversation_id,request_id),
    CHECK((status='running' AND claim_id IS NOT NULL AND lease_until IS NOT NULL AND answer IS NULL)
       OR (status='completed' AND claim_id IS NULL AND lease_until IS NULL AND answer IS NOT NULL)
       OR (status='failed' AND claim_id IS NULL AND lease_until IS NULL AND answer IS NULL))
);
CREATE UNIQUE INDEX rag_one_running_turn ON rag_conversation_turns(conversation_id) WHERE status='running';

CREATE TABLE rag_turn_sources (
    turn_id text NOT NULL REFERENCES rag_conversation_turns(id) ON DELETE CASCADE,
    meeting_id text NOT NULL,
    project_id text NOT NULL,
    revision text NOT NULL,
    PRIMARY KEY(turn_id,meeting_id,project_id,revision)
);
