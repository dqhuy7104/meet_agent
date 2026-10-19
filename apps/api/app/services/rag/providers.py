"""Bounded, non-redirecting provider HTTP adapters with safe errors."""
import json
import math
import httpx
from app.core.rag_config import ChatSelection, EmbeddingProfile, ProviderRegistry, RagError


def validate_vectors(vectors: list[list[float]], count: int, dimensions: int) -> None:
    try:
        if len(vectors) != count:
            raise ValueError()
        for vector in vectors:
            if len(vector) != dimensions or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector) or not any(vector):
                raise ValueError()
    except (TypeError, ValueError):
        raise RagError('invalid_provider_response') from None


class ModelClient:
    def __init__(self, registry: ProviderRegistry, http: httpx.AsyncClient) -> None:
        self.registry = registry
        self.http = http

    async def _post(self, provider, path: str, payload: dict) -> dict:
        headers = {}
        if provider.api_key.get_secret_value():
            headers['Authorization'] = 'Bearer ' + provider.api_key.get_secret_value()
        try:
            async with self.http.stream('POST', provider.base_url.rstrip('/') + path, json=payload,
                                        headers=headers, timeout=provider.timeout_seconds, follow_redirects=False) as response:
                response.raise_for_status()
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body) > 131072:
                        raise RagError('invalid_provider_response')
                return json.loads(body)
        except httpx.TimeoutException:
            raise RagError('provider_timeout', 504) from None
        except httpx.HTTPError:
            raise RagError('provider_unavailable') from None
        except (ValueError, TypeError):
            raise RagError('invalid_provider_response') from None

    async def embed(self, profile: EmbeddingProfile, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        provider = self.registry.provider(profile.provider_id)
        is_ollama = provider.kind == 'ollama'
        data = await self._post(provider, '/api/embed' if is_ollama else '/embeddings',
                                {'model': profile.model, 'input': texts, **({'truncate': False} if is_ollama else {})})
        try:
            if is_ollama:
                vectors = data['embeddings']
            else:
                rows = sorted(data['data'], key=lambda r: r['index'])
                if [r['index'] for r in rows] != list(range(len(texts))):
                    raise ValueError()
                vectors = [row['embedding'] for row in rows]
            validate_vectors(vectors, len(texts), profile.dimensions)
            return vectors
        except (KeyError, TypeError, ValueError):
            raise RagError('invalid_provider_response') from None

    async def generate(self, selection: ChatSelection, messages: list[dict[str, str]]) -> str:
        is_ollama = selection.provider.kind == 'ollama'
        payload = {'model': selection.model.id, 'messages': messages, 'stream': False}
        if is_ollama:
            payload.update(format='json', options={'num_predict': selection.model.output_tokens, 'num_ctx': selection.model.context_tokens})
        else:
            payload.update(max_tokens=selection.model.output_tokens, response_format={'type': 'json_object'})
        data = await self._post(selection.provider, '/api/chat' if is_ollama else '/chat/completions', payload)
        try:
            result = data['message']['content'] if is_ollama else data['choices'][0]['message']['content']
            if type(result) is not str:
                raise TypeError()
            return result
        except (KeyError, TypeError, IndexError):
            raise RagError('invalid_provider_response') from None
