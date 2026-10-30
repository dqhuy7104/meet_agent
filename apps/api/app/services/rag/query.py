"""Query orchestration with authoritative PostgreSQL eligibility checks."""
import hashlib
from app.core.rag_config import RagError, RagSettings, ProviderRegistry
from app.schemas.chat import RagQuery, SelectedMeeting
from app.services.rag.answering import generate_answer, insufficient
from app.services.rag.chunking import estimate_tokens, snapshot_meeting
from app.services.rag.prompt import build_messages
from app.services.rag.ranking import rank_candidates, select_context
from app.services.llm.summarizer import PROMPT_VERSION as SUMMARY_PROMPT_VERSION, summarize_transcript
from app.schemas.system_prompts import SystemPrompts
from app.services.llm.summarizer import summary_prompt
from app.services.rag.prompt import rag_prompt


class QueryService:
    def __init__(self, settings: RagSettings, registry: ProviderRegistry, meetings, rag, client, access=None,
                 system_prompts: SystemPrompts | None = None) -> None:
        self.settings, self.registry, self.meetings, self.rag, self.client = settings, registry, meetings, rag, client
        self.access = access
        self.system_prompts = system_prompts or SystemPrompts()

    def eligible(self, chunk) -> bool:
        if self.access and not self.access.can_read(chunk.meeting_id):
            return False
        detail = self.meetings.get_detail(chunk.meeting_id)
        return bool(detail and detail.meeting.status == 'completed' and snapshot_meeting(detail).revision == chunk.revision)

    async def summarize(self, meeting_id: str) -> dict[str, str]:
        if self.access and not self.access.can_read(meeting_id):
            raise RagError('meeting_not_found', 404)
        detail = self.meetings.get_detail(meeting_id)
        if detail is None:
            raise RagError('meeting_not_found', 404)
        if detail.meeting.status != 'completed':
            raise RagError('meeting_not_completed', 409)
        revision = snapshot_meeting(detail).revision
        saved = self.meetings.get_summary(meeting_id, revision)
        if saved is not None:
            current = self.meetings.get_detail(meeting_id)
            if current is None or current.meeting.status != 'completed' or snapshot_meeting(current).revision != revision:
                raise RagError('meeting_changed', 409)
            return dict(summary=saved.summary, provider_id=saved.provider_id,
                        model_id=saved.model_id, prompt_version=saved.prompt_version)
        provider = next((item for item in self.settings.providers if item.chat_models), None)
        if provider is None:
            raise RagError('chat_provider_not_configured')
        selection = self.registry.resolve_chat(provider.id, provider.chat_models[0].id)
        revision = snapshot_meeting(detail).revision
        claim = self.meetings.claim_summary(meeting_id, revision)
        if claim is None:
            current = self.meetings.get_detail(meeting_id)
            if current is None or current.meeting.status != 'completed' or snapshot_meeting(current).revision != revision:
                raise RagError('meeting_changed', 409)
            raise RagError('summary_generating', 409)

        def require_current_source():
            if self.access and not self.access.can_read(meeting_id):
                raise RagError('meeting_not_found', 404)
            current = self.meetings.get_detail(meeting_id)
            if current is None or current.meeting.status != 'completed' or snapshot_meeting(current).revision != revision:
                raise RagError('meeting_changed', 409)

        service = self
        class SummaryClient:
            async def generate(inner, selected, messages):
                require_current_source()
                return await service.client.generate(selected, messages)
        try:
            custom_prompt = self.system_prompts.summary_system_prompt
            prompt_version = SUMMARY_PROMPT_VERSION
            if custom_prompt:
                prompt_version += ':' + hashlib.sha256(summary_prompt(custom_prompt).encode()).hexdigest()
            summary = await summarize_transcript(detail.transcript_segments, selection, SummaryClient(), custom_prompt)
            require_current_source()
            if not self.meetings.save_summary(meeting_id, revision, claim, summary,
                                              provider.id, selection.model.id, prompt_version):
                raise RagError('meeting_changed', 409)
            return dict(summary=summary, provider_id=provider.id, model_id=selection.model.id,
                        prompt_version=prompt_version)
        except BaseException:
            self.meetings.release_summary(meeting_id, revision, claim)
            raise

    async def ready(self, profile_id: str, meeting_ids: list[str] | None = None) -> bool:
        await self.rag.call('validate_profile', self.registry.resolve_profile(profile_id))
        if self.access is None and not await self.rag.call('is_profile_ready', profile_id):
            return False
        rows = meeting_ids if meeting_ids is not None else self.visible_meeting_ids()
        for row in rows:
            detail = self.meetings.get_detail(row)
            if detail and detail.meeting.status == 'completed':
                snapshot = snapshot_meeting(detail)
                if await self.rag.call('manifest', profile_id, row) != snapshot.revision:
                    return False
        return True

    def visible_meeting_ids(self, project_id: str | None = None) -> list[str]:
        if self.access:
            return self.access.meeting_ids(project_id)
        return self.meetings.completed_meeting_ids()

    async def catalog(self):
        ready = []
        for profile in self.settings.profiles:
            try:
                if await self.ready(profile.id):
                    ready.append(profile.id)
            except RagError as error:
                if error.code not in {'profile_not_ready', 'immutable_profile_changed'}:
                    raise
        return self.registry.catalog(ready)

    async def evaluation_state(self, profile_id: str, provider_id: str, model_id: str) -> dict:
        profile = self.registry.resolve_profile(profile_id)
        selection = self.registry.resolve_chat(provider_id, model_id)
        if not await self.ready(profile_id):
            raise RagError('profile_not_ready', 409)
        revisions = {}
        for row in self.visible_meeting_ids():
            detail = self.meetings.get_detail(row)
            if detail and detail.meeting.status == 'completed':
                revisions[row] = snapshot_meeting(detail).revision
        revisions = {key: value for key, value in revisions.items()
                     if self.access is None or self.access.can_read(key)}
        return dict(meeting_revisions=revisions, embedding_profile=profile.model_dump(),
                    prompt_version=self.settings.prompt_version,
                    prompt_hash=hashlib.sha256(rag_prompt(self.system_prompts.rag_system_prompt).encode()).hexdigest(),
                    chat_provider_id=provider_id, chat_model=selection.model.model_dump(),
                    min_cosine_similarity=self.settings.min_cosine_similarity,
                    provider_versions='unavailable', device='unavailable')

    async def query(self, request: RagQuery):
        selection = self.registry.resolve_chat(request.chat_provider_id, request.chat_model_id)
        profile = self.registry.resolve_profile(request.embedding_profile_id)
        if request.k > self.settings.max_k:
            raise RagError('invalid_k', 422)
        meeting_ids = self.visible_meeting_ids(request.project_id)
        if request.meeting_ids is not None:
            meeting_ids = [item for item in meeting_ids if item in request.meeting_ids]
        if not meeting_ids:
            return insufficient(self.settings.prompt_version)
        if not await self.ready(profile.id, meeting_ids):
            raise RagError('profile_not_ready', 409)
        vector = (await self.client.embed(profile, [request.question]))[0]
        current_ids = self.visible_meeting_ids(request.project_id)
        meeting_ids = [item for item in meeting_ids if item in current_ids]
        if not meeting_ids:
            return insufficient(self.settings.prompt_version)
        vectors, texts = await self.rag.call('search', profile.id, request.question, vector, meeting_ids)
        ranked = rank_candidates(request.question, [c for c in vectors if c.chunk.meeting_id in meeting_ids and self.eligible(c.chunk)],
                                 [c for c in texts if c.chunk.meeting_id in meeting_ids and self.eligible(c.chunk)], self.settings.min_cosine_similarity)
        overhead = sum(estimate_tokens(m['content']) + 16 for m in build_messages(
            request.question, [], self.settings.prompt_version, self.system_prompts.rag_system_prompt))
        budget = selection.model.context_tokens - selection.model.output_tokens - overhead - 256
        if budget < 0:
            raise RagError('prompt_too_long', 422)
        sources = select_context(ranked, request.k, max(0, budget))
        # Recheck after all awaited retrieval work and immediately before dispatch.
        sources = [s for s in sources if self.eligible(s)]
        answer = await generate_answer(request.question, sources, selection, self.client,
                                       self.settings.prompt_version, self.system_prompts.rag_system_prompt)
        if any(not self.eligible(s) for s in sources):
            return insufficient(self.settings.prompt_version)
        selected = {}
        for source in sources:
            score = next(c.relevance_score for c in ranked if c.chunk.source_id == source.source_id)
            if source.meeting_id not in selected:
                selected[source.meeting_id] = SelectedMeeting(meeting_id=source.meeting_id, title=source.meeting_title, relevance_score=score)
        return answer.model_copy(update={'selected_meetings': list(selected.values())})
