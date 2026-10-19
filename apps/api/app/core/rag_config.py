"""Opt-in RAG configuration. Parsing never opens providers or databases."""
from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from typing import Literal, Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator


class RagError(ValueError):
    def __init__(self, code: str, status: int = 503) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class ChatModel(StrictModel):
    id: str = Field(min_length=1)
    context_tokens: int = Field(default=8192, ge=1024, le=1000000)
    output_tokens: int = Field(default=1024, ge=64, le=32768)

    @model_validator(mode='after')
    def validate_budget(self):
        if self.output_tokens + 512 >= self.context_tokens:
            raise ValueError('invalid_budget')
        return self


class Provider(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: Literal['ollama', 'openai_compatible']
    base_url: str = Field(repr=False, exclude=True)
    api_key_env: str | None = Field(default=None, repr=False, exclude=True)
    api_key: SecretStr = Field(default=SecretStr(''), repr=False, exclude=True)
    external_data_enabled: bool = False
    timeout_seconds: float = Field(default=30, gt=0, le=60)
    chat_models: list[ChatModel] = Field(default_factory=list)
    embedding_models: list[str] = Field(default_factory=list)

    @property
    def is_external(self) -> bool:
        host = urlsplit(self.base_url).hostname
        if host == 'localhost':
            return False
        try:
            return not ipaddress.ip_address(host or '').is_loopback
        except ValueError:
            return True


class EmbeddingProfile(StrictModel):
    id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    dimensions: int = Field(ge=1, le=16384)
    chunk_revision: Literal['utf8-v1'] = 'utf8-v1'


@dataclass(frozen=True)
class ChatSelection:
    provider: Provider
    model: ChatModel


@dataclass(frozen=True)
class RagSettings:
    enabled: bool = False
    database_url: SecretStr = field(default_factory=lambda: SecretStr(''), repr=False)
    providers: tuple[Provider, ...] = ()
    profiles: tuple[EmbeddingProfile, ...] = ()
    max_k: int = 10
    prompt_version: str = 'meeting_qa_v1'
    min_cosine_similarity: float = .35


def load_rag_settings(environment: Mapping[str, str]) -> RagSettings:
    try:
        enabled = environment.get('RAG_ENABLED', 'false').lower()
        if enabled not in {'true', 'false'}:
            raise ValueError()
        if enabled == 'false':
            return RagSettings()
        providers = []
        for item in json.loads(environment.get('RAG_PROVIDERS_JSON', '[]')):
            provider = Provider.model_validate(item)
            parsed = urlsplit(provider.base_url)
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError()
            if provider.is_external and (not provider.external_data_enabled or parsed.scheme != 'https'):
                raise ValueError()
            if provider.api_key_env:
                key = environment.get(provider.api_key_env, '')
                if not key:
                    raise ValueError()
                provider = provider.model_copy(update={'api_key': SecretStr(key)})
            if len({m.id for m in provider.chat_models}) != len(provider.chat_models):
                raise ValueError()
            providers.append(provider)
        profiles = tuple(EmbeddingProfile.model_validate(p) for p in json.loads(environment.get('RAG_EMBEDDING_PROFILES_JSON', '[]')))
        by_id = {p.id: p for p in providers}
        if len(by_id) != len(providers) or len({p.id for p in profiles}) != len(profiles):
            raise ValueError()
        for profile in profiles:
            provider = by_id[profile.provider_id]
            if profile.model not in provider.embedding_models:
                raise ValueError()
            if provider.kind == 'ollama' and not profile.model.endswith('@' + profile.revision):
                raise ValueError()
        max_k = int(environment.get('RAG_MAX_K', '10'))
        minimum = float(environment.get('RAG_MIN_COSINE_SIMILARITY', '.35'))
        version = environment.get('RAG_PROMPT_VERSION', 'meeting_qa_v1')
        dsn = environment.get('RAG_DATABASE_URL', '')
        if not 1 <= max_k <= 100 or not 0 <= minimum <= 1 or version != 'meeting_qa_v1' or not dsn:
            raise ValueError()
        return RagSettings(True, SecretStr(dsn), tuple(providers), profiles, max_k, version, minimum)
    except (ValueError, TypeError, KeyError, ValidationError):
        raise RagError('invalid_configuration') from None


class ProviderRegistry:
    def __init__(self, settings: RagSettings) -> None:
        self.settings = settings

    def provider(self, provider_id: str) -> Provider:
        for provider in self.settings.providers:
            if provider.id == provider_id:
                return provider
        raise RagError('unknown_provider', 422)

    def resolve_chat(self, provider_id: str, model_id: str) -> ChatSelection:
        provider = self.provider(provider_id)
        for model in provider.chat_models:
            if model.id == model_id:
                return ChatSelection(provider, model)
        raise RagError('unknown_model', 422)

    def resolve_profile(self, profile_id: str) -> EmbeddingProfile:
        for profile in self.settings.profiles:
            if profile.id == profile_id:
                return profile
        raise RagError('unknown_profile', 422)

    def catalog(self, ready: list[str]):
        from app.schemas.chat import ProviderCatalog
        return ProviderCatalog(max_k=self.settings.max_k, providers=[
            dict(id=p.id, name=p.name, is_external=p.is_external, chat_models=[m.id for m in p.chat_models])
            for p in self.settings.providers], embedding_profiles=[
                dict(id=p.id, provider_id=p.provider_id, model=p.model, revision=p.revision)
                for p in self.settings.profiles if p.id in ready])
