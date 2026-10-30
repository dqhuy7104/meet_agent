CREATE TABLE workspace_system_prompts (
    id smallint PRIMARY KEY CHECK (id = 1),
    summary_system_prompt text NOT NULL DEFAULT '' CHECK (char_length(summary_system_prompt) <= 8000),
    rag_system_prompt text NOT NULL DEFAULT '' CHECK (char_length(rag_system_prompt) <= 8000),
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO workspace_system_prompts(id) VALUES (1);
