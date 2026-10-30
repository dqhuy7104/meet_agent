"""Persistent workspace prompts; empty fields select built-in defaults."""
import psycopg
from app.schemas.system_prompts import SystemPrompts


class PromptRepository:
    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    def get(self) -> SystemPrompts:
        row = self.connection.execute(
            'SELECT summary_system_prompt,rag_system_prompt FROM workspace_system_prompts WHERE id=1').fetchone()
        return SystemPrompts(summary_system_prompt=row[0], rag_system_prompt=row[1]) if row else SystemPrompts()

    def save(self, prompts: SystemPrompts) -> SystemPrompts:
        self.connection.execute('''INSERT INTO workspace_system_prompts(id,summary_system_prompt,rag_system_prompt)
            VALUES (1,%s,%s) ON CONFLICT(id) DO UPDATE SET
            summary_system_prompt=excluded.summary_system_prompt,
            rag_system_prompt=excluded.rag_system_prompt,updated_at=now()''',
            (prompts.summary_system_prompt, prompts.rag_system_prompt))
        return prompts
