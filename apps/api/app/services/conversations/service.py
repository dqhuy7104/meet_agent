from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
from typing import TYPE_CHECKING

import psycopg
from pydantic import Field

from app.core.rag_config import RagError, StrictModel
from app.schemas.chat import ConversationConfig, RagAnswer, RagQuery
from app.schemas.conversations import ConversationCreate, ConversationDetail, ConversationList, ConversationSummary, ConversationTurn, TurnRequest
from app.services.access.repository import AccessRepository, Account
from app.services.conversations.context import select_history
from app.services.conversations.repository import ConversationRepository
from app.services.conversations.chatbot import route_message, generate_chat_answer, source_budget, ROUTING_PROMPT, ANSWER_PROMPT, ROUTING_HASH, ANSWER_HASH
from app.services.rag.chunking import snapshot_meeting, chunk_transcript
from app.services.rag.relevance import FILTER_HASH, FILTER_VERSION
from app.services.rag.ranking import select_context
from app.services.rag.types import Candidate

if TYPE_CHECKING:
    from app.services.rag.query import QueryService
    from app.services.rag.request import RequestDeadline


class PageCursor(StrictModel):
    updated_at: datetime
    id: str = Field(min_length=1,max_length=200)


class ConversationService:
    def __init__(self, connection: psycopg.Connection, query: QueryService, account_id: str, deadline: RequestDeadline) -> None:
        self.connection, self.query, self.account_id, self.deadline = connection, query, account_id, deadline
        self.repository = ConversationRepository(connection)
        self.access = AccessRepository(connection)

    def account(self) -> Account:
        self.deadline.remaining()
        account = self.access.account_by_id(self.account_id)
        if account is None or account.is_disabled:
            raise RagError('conversation_not_found',404)
        return account

    def conversation(self, conversation_id: str, write: bool = False) -> dict:
        account = self.account()
        row = self.repository.get(conversation_id)
        if row is None or (row['creator_account_id'] != account.id and not account.is_system_admin):
            raise RagError('conversation_not_found',404)
        if write and row['creator_account_id'] != account.id:
            raise RagError('conversation_read_only',403)
        return row

    def require_scope(self, project_id: str | None) -> None:
        if project_id is not None and not self.access.role(self.account(),project_id):
            raise RagError('project_not_found',404)

    def source_state(self, dependencies: list[tuple[str,str,str]]) -> str:
        account = self.account()
        projects = {project.id for project in self.access.visible_projects(account)}
        details = {}
        stale = False
        for meeting_id, project_id, revision in dependencies:
            if project_id not in projects:
                return 'redacted'
            if meeting_id not in details:
                details[meeting_id] = self.query.meetings.get_detail(meeting_id)
            detail = details[meeting_id]
            if detail is None or detail.meeting.status != 'completed' or detail.meeting.project_id != project_id:
                return 'redacted'
            if snapshot_meeting(detail).revision != revision:
                stale = True
        return 'stale' if stale else 'current'

    def turn_view(self, row: dict, conversation: dict) -> ConversationTurn:
        state = self.source_state(self.repository.dependencies(row['id']))
        if conversation['project_id'] and not self.access.role(self.account(),conversation['project_id']):
            state = 'redacted'
        if state == 'redacted':
            return ConversationTurn(id=row['id'],ordinal=row['ordinal'],question=None,config=None,status='redacted',
                answer=None,created_at=row['created_at'],finished_at=row['finished_at'])
        status = row['status']
        if status == 'running' and row['lease_until'] < datetime.now(timezone.utc):
            status = 'interrupted'
        elif state == 'stale' and status == 'completed':
            status = 'stale'
        return ConversationTurn(id=row['id'],ordinal=row['ordinal'],question=row['question'],config=row['config'],
            status=status,answer=row['answer'],failure_code=row['failure_code'],created_at=row['created_at'],finished_at=row['finished_at'])

    def summary(self, row: dict) -> ConversationSummary:
        title, config, project_id = row['title'], row['config'], row['project_id']
        first = self.repository.turns(row['id'],before=2,limit=1)
        if first and self.turn_view(first[0],row).status == 'redacted':
            title = 'Hội thoại'
        latest = self.repository.turns(row['id'],limit=1)
        if latest and self.turn_view(latest[0],row).status == 'redacted':
            config = None
        if project_id and not self.access.role(self.account(),project_id):
            project_id, config = None,None
        return ConversationSummary(id=row['id'],title=title,creator_account_id=row['creator_account_id'],
            creator_email=row['creator_email'] if row['creator_account_id'] != self.account_id else None,
            project_id=project_id,config=config,created_at=row['created_at'],updated_at=row['updated_at'],
            last_ordinal=row['last_ordinal'],read_only=row['creator_account_id'] != self.account_id)

    async def create_conversation(self, body: ConversationCreate) -> ConversationSummary:
        self.require_scope(body.project_id)
        row = self.repository.create(self.account().id,body.project_id)
        return self.summary(self.conversation(row['id']))

    async def list_conversations(self, view: str, cursor: str | None) -> ConversationList:
        account = self.account()
        if view == 'others' and not account.is_system_admin:
            raise RagError('conversation_read_only',403)
        boundary = None
        if cursor:
            try:
                position = PageCursor.model_validate_json(base64.urlsafe_b64decode(cursor.encode()))
                if position.updated_at.tzinfo is None:
                    raise ValueError()
                boundary = (position.updated_at,position.id)
            except (ValueError,TypeError,UnicodeError):
                raise RagError('invalid_cursor',422) from None
        rows = self.repository.list(account.id,view=='others',boundary)
        items = [self.summary(self.conversation(row['id'])) for row in rows[:20]]
        next_cursor = None
        if len(rows)>20:
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(PageCursor(updated_at=last.updated_at,id=last.id).model_dump_json().encode()).decode()
        return ConversationList(items=items,next_cursor=next_cursor)

    async def get_conversation(self, conversation_id: str, before: int | None) -> ConversationDetail:
        row = self.conversation(conversation_id)
        turns = self.repository.turns(conversation_id,before,51)
        next_before = turns[49]['ordinal'] if len(turns)>50 else None
        return ConversationDetail(conversation=self.summary(row),turns=[self.turn_view(turn,row) for turn in reversed(turns[:50])],
                                  next_before_ordinal=next_before)

    async def update_conversation(self, conversation_id: str, body: ConversationCreate) -> ConversationSummary:
        self.conversation(conversation_id,write=True)
        self.require_scope(body.project_id)
        self.repository.set_scope(conversation_id,body.project_id)
        return self.summary(self.conversation(conversation_id))

    async def delete_conversation(self, conversation_id: str) -> None:
        self.conversation(conversation_id,write=True)
        self.repository.delete(conversation_id)

    async def append_turn(self, conversation_id: str, request: TurnRequest) -> ConversationTurn:
        conversation = self.conversation(conversation_id,write=True)
        existing = self.repository.find_request(conversation_id,request.request_id)
        if existing:
            if existing['question'] != request.question or ConversationConfig.model_validate(existing['config']).model_dump() != request.config.model_dump():
                raise RagError('idempotency_conflict',409)
            return self.turn_view(existing,conversation)
        if not self.query.settings.enabled:
            raise RagError('rag_disabled')
        self.require_scope(conversation['project_id'])
        selection = self.query.registry.resolve_chat(request.config.chat_provider_id,request.config.chat_model_id)
        profile = self.query.registry.resolve_profile(request.config.embedding_profile_id)
        if request.config.k > self.query.settings.max_k:
            raise RagError('invalid_k',422)
        turn, claimed = self.repository.claim(conversation_id,request)
        if not claimed:
            return self.turn_view(turn,conversation)
        dependencies = set()
        original_client = self.query.client
        service = self

        def guard():
            service.deadline.remaining()
            current = service.conversation(conversation_id,write=True)
            service.require_scope(current['project_id'])
            if service.source_state(list(dependencies)) != 'current':
                raise RagError('source_unavailable',409)
            service.deadline.remaining()

        def record_sources(sources):
            for source in sources:
                detail = service.query.meetings.get_detail(source.meeting_id)
                if detail is None or detail.meeting.project_id is None:
                    raise RagError('source_unavailable',409)
                dependencies.add((source.meeting_id,detail.meeting.project_id,source.revision))
            service.repository.add_dependencies(turn['id'],list(dependencies))
            guard()

        class GuardedClient:
            async def embed(inner, profile, texts):
                guard()
                result = await original_client.embed(profile,texts)
                guard()
                return result
            async def generate(inner, selected, messages):
                guard()
                result = await original_client.generate(selected,messages)
                guard()
                return result

        try:
            conversation = self.conversation(conversation_id,write=True)
            self.require_scope(conversation['project_id'])
            pairs = []
            previous = self.repository.turns(conversation_id,before=turn['ordinal'],limit=6)
            for row in previous:
                view = self.turn_view(row,conversation)
                if view.status != 'completed' or view.answer is None:
                    break
                prior_text = view.answer.answer
                if view.answer.parts and not request.config.allow_model_knowledge:
                    prior_text = '\n\n'.join(part.text for part in view.answer.parts if part.kind != 'model')
                pairs.append({'id':row['id'],'question':view.question,'answer':prior_text})
            history = select_history(request.question,pairs,selection,prompt=ROUTING_PROMPT)
            used_pairs = pairs[:len(history)]
            for pair in used_pairs:
                dependencies.update(self.repository.dependencies(pair['id']))
            self.repository.add_dependencies(turn['id'],list(dependencies))
            self.query.client = GuardedClient()
            decision = await route_message(request.question,history,selection,self.query.client)
            resolved = decision.question
            sources = []
            if decision.intent == 'clarification':
                answer = RagAnswer(answer='Bạn vui lòng nêu rõ nội dung hoặc cuộc họp muốn hỏi tiếp.',
                    retrieval_status='insufficient_evidence',prompt_version='chat_answer_v1')
            else:
                budget = source_budget(resolved or request.question,history,request.config.allow_model_knowledge,
                    selection,self.query.system_prompts.rag_system_prompt,decision.intent)
                if decision.intent == 'retrieval':
                    config = request.config.model_dump(exclude={'allow_model_knowledge'})
                    query = RagQuery(**config,question=resolved,project_id=conversation['project_id'])
                    sources, _ = await self.query.retrieve(query,record_sources=record_sources,token_budget=budget)
                elif decision.intent == 'transform':
                    if not used_pairs:
                        raise RagError('context_unavailable',409)
                    prior = self.repository.get_turn(used_pairs[len(history)-decision.target_turn]['id'])
                    prior_answer = RagAnswer.model_validate(prior['answer'])
                    for citation in prior_answer.citations:
                        detail = self.query.meetings.get_detail(citation.meeting_id)
                        if detail is None:
                            raise RagError('source_unavailable',409)
                        snapshot = snapshot_meeting(detail)
                        revision = citation.source_id.rsplit(':',2)[-2]
                        if revision not in {'utf8-v1','turns-v2'}:
                            raise RagError('source_unavailable',409)
                        chunk = next((chunk for chunk in chunk_transcript(snapshot,chunk_revision=revision) if chunk.source_id==citation.source_id),None)
                        if chunk is None:
                            raise RagError('source_unavailable',409)
                        sources.append(chunk)
                    record_sources(sources)
                    sources = select_context([Candidate(source) for source in sources],request.config.k,budget)
                answer = await generate_chat_answer(resolved or request.question,history,sources,
                    request.config.allow_model_knowledge,selection,self.query.client,
                    self.query.system_prompts.rag_system_prompt,decision.intent)
            guard()
            metadata = {'context_version':'chat_routing_v1','context_hash':ROUTING_HASH,'context_turn_ids':[p['id'] for p in reversed(used_pairs)],
                'routing_hash':ROUTING_HASH,'chat_answer_hash':ANSWER_HASH,'intent':decision.intent,
                'ranking_weights':{'cosine':request.config.context_weight,'keyword':1-request.config.context_weight},
                'relevance_filter_version':FILTER_VERSION,'relevance_filter_hash':FILTER_HASH,
                'prompt_version':'chat_answer_v1',
                'prompt_hash':hashlib.sha256(((self.query.system_prompts.rag_system_prompt+'\n\n'+ANSWER_PROMPT)
                    if self.query.system_prompts.rag_system_prompt else ANSWER_PROMPT).encode()).hexdigest(),
                'chat_model':selection.model.model_dump(),'embedding_profile':profile.model_dump()}
            if not self.repository.finish(turn['id'],turn['claim_id'],answer.model_dump(),resolved,metadata,before_commit=guard):
                raise RagError('conversation_changed',409)
            return self.turn_view(self.repository.get_turn(turn['id']),self.conversation(conversation_id))
        except RagError as error:
            self.fail_turn(turn,error.code)
            raise
        except BaseException:
            self.fail_turn(turn,'turn_interrupted')
            raise
        finally:
            self.query.client = original_client

    def fail_turn(self, turn: dict, code: str) -> None:
        try:
            self.connection.execute("SELECT set_config('statement_timeout','2000',false)")
            self.repository.fail(turn['id'],turn['claim_id'],code)
        except psycopg.Error:
            pass
