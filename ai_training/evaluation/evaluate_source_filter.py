"""Compare a fixed candidate set with and without the relevance filter, locally."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import platform
import time

import httpx

from evaluate_meeting_rag import output_hash, percentile


def source_metrics(expected: set[str], selected: set[str], universe: set[str]) -> dict:
    irrelevant = universe - expected
    return {'evidence_recall':len(expected & selected)/len(expected) if expected else None,
            'irrelevant_rejection_rate':len(irrelevant - selected)/len(irrelevant) if irrelevant else None,
            'precision':len(expected & selected)/len(selected) if selected else None,
            'abstained':not selected}


async def evaluate(args: argparse.Namespace, dataset: dict) -> dict:
    from app.core.rag_config import ChatModel, EmbeddingProfile, Provider, ProviderRegistry, RagError, RagSettings
    from app.services.rag.chunking import make_snapshot
    from app.services.rag.ranking import rank_candidates, select_context, tokens, RANKING_WEIGHTS
    from app.services.rag.relevance import FILTER_HASH, FILTER_VERSION, RelevantSources, filter_sources, select_relevance_context
    from app.services.rag.providers import ModelClient
    from app.services.rag.request import RequestDeadline
    from app.services.rag.types import Candidate, SourceChunk, SourceSegment
    provider = Provider(id='local', name='Local evaluation', kind='ollama', base_url=args.ollama_url,
                        timeout_seconds=60, chat_models=[ChatModel(id=args.chat_model, context_tokens=8192, output_tokens=1024)],
                        embedding_models=[args.embedding_model])
    if provider.is_external:
        raise ValueError('only_loopback_ollama_is_allowed')
    async with httpx.AsyncClient() as http:
        tags = await http.get(args.ollama_url.rstrip('/')+'/api/tags')
        tags.raise_for_status()
        models = {row['name']:row['digest'] for row in tags.json()['models']}
        probe = await http.post(args.ollama_url.rstrip('/')+'/api/embed',
            json={'model':args.embedding_model,'input':['dimension probe'],'truncate':False}, timeout=60)
        probe.raise_for_status()
        profile = EmbeddingProfile(id='local-fixture', provider_id='local', model=args.embedding_model,
            revision='sha256:'+models[args.embedding_model].removeprefix('sha256:'), dimensions=len(probe.json()['embeddings'][0]))
        registry = ProviderRegistry(RagSettings(True, (provider,), (profile,)))
        client = ModelClient(registry, http)
        selection = registry.resolve_chat('local', args.chat_model)
        sources = []
        labels = {}
        for index, row in enumerate(dataset['sources']):
            segment = SourceSegment(index, row['text'], row.get('speaker','unknown'), row['start'], row['end'])
            snapshot = make_snapshot(row['meeting_id'], row['title'], (segment,))
            source = SourceChunk(f'{snapshot.meeting_id}:{snapshot.revision}:fixture-v1:{index}', snapshot.meeting_id,
                                 snapshot.title, snapshot.revision, index, segment.text, (segment,))
            sources.append(source)
            labels[source.source_id] = row['id']
        vectors = []
        for offset in range(0, len(sources), 2):
            vectors.extend(await client.embed(profile, [s.text for s in sources[offset:offset+2]]))
        results = []
        for case in dataset['cases']:
            query_vector = (await client.embed(profile, [case['question']]))[0]
            cosine = lambda vector: sum(a*b for a,b in zip(vector, query_vector)) / (
                math.sqrt(sum(a*a for a in vector))*math.sqrt(sum(b*b for b in query_vector)))
            hits = sorted([Candidate(source, cosine(vector)) for source, vector in zip(sources, vectors)],
                          key=lambda hit:(-hit.cosine, hit.chunk.source_id))
            ranked = rank_candidates(case['question'], hits, [])
            query_tokens = tokens(case['question'])
            baseline_ranked = sorted([replace(hit, relevance_score=.7*max(0., min(1., hit.cosine)) + .3*(
                len(query_tokens & tokens(hit.chunk.text))/max(1,len(query_tokens)))) for hit in ranked],
                key=lambda hit:(-hit.relevance_score, hit.chunk.meeting_id, hit.chunk.source_id))
            baseline_start = time.monotonic()
            baseline = select_context(baseline_ranked, args.k, 6000)
            baseline_seconds = time.monotonic()-baseline_start
            deadline = RequestDeadline()
            model_client = ModelClient(registry, http, deadline.remaining)
            raw_outputs = []
            class RecordingClient:
                async def generate(self, selected, messages):
                    raw = await model_client.generate(selected, messages)
                    raw_outputs.append(raw)
                    return raw
            start = time.monotonic()
            error = None
            candidate_context = []
            filter_valid = True
            try:
                filter_context = select_relevance_context(case['question'], ranked, selection)
                filtered = await filter_sources(case['question'], filter_context, selection, RecordingClient())
                if raw_outputs:
                    try:
                        ids = RelevantSources.model_validate_json(raw_outputs[0]).source_ids
                        filter_valid = len(ids) == len(set(ids)) and set(ids).issubset({f'source_{i+1}' for i in range(len(filter_context))})
                    except ValueError:
                        filter_valid = False
                accepted = {s.source_id for s in filtered}
                candidate_context = select_context([c for c in ranked if c.chunk.source_id in accepted], args.k, 6000)
            except RagError as failure:
                error = failure.code
                filter_valid = False
            expected = set(case['expected_source_ids'])
            baseline_ids = {labels[s.source_id] for s in baseline}
            candidate_ids = {labels[s.source_id] for s in candidate_context}
            results.append({'id':case['id'], 'baseline':{'weights':{'cosine':.7,'keyword':.3},
                'selected_source_ids':sorted(baseline_ids), 'metrics':source_metrics(expected, baseline_ids, set(labels.values())),
                'context_selection_seconds':baseline_seconds},
                'candidate':{'weights':RANKING_WEIGHTS, 'selected_source_ids':sorted(candidate_ids),
                    'metrics':source_metrics(expected, candidate_ids, set(labels.values())) if filter_valid else None,
                    'filter_output_valid':filter_valid, 'failure_code':error,
                    'selection_and_filter_seconds':time.monotonic()-start,
                    'filter_output_hash':hashlib.sha256(raw_outputs[0].encode()).hexdigest() if raw_outputs else None},
                'candidate_cosines':{labels[h.chunk.source_id]:h.cosine for h in hits}})
        version = await http.get(args.ollama_url.rstrip('/')+'/api/version')
        version.raise_for_status()
        return {'schema_version':1,'evaluation_kind':'fixed-candidate-relevance','split':dataset['split'],
            'dataset_hash':output_hash(dataset), 'provenance':dataset['provenance'], 'sample_count':len(results),
            'k':args.k,'embedding_profile':profile.model_dump(),'chat_model':selection.model.model_dump(),
            'chat_digest':models[args.chat_model], 'relevance_filter_version':FILTER_VERSION,'relevance_filter_hash':FILTER_HASH,
            'versions':{'ollama':version.json()['version'],'httpx':httpx.__version__,'python':platform.python_version()},
            'device':'unavailable: provider-managed','generation_seed':'unavailable: provider defaults',
            'limitations':['Fixed candidate passages; no end-to-end hybrid retrieval or answer generation.',
                'Synthetic distractors are not the actual stored Super Bowl transcript.',
                'Single development run; not a held-out quality benchmark. Invalid model outputs receive no rejection credit.',
                'Baseline context selection excludes model time; candidate time includes the extra filter call.'],
            'latency_seconds':{'candidate_filter_p50':percentile([r['candidate']['selection_and_filter_seconds'] for r in results],.5),
                               'candidate_filter_p95':percentile([r['candidate']['selection_and_filter_seconds'] for r in results],.95)},
            'cases':results}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--embedding-model', required=True)
    parser.add_argument('--chat-model', required=True)
    parser.add_argument('--ollama-url', default='http://127.0.0.1:11434')
    parser.add_argument('--k', type=int, default=3)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if not output.is_relative_to(Path(__file__).resolve().parents[1]/'results'):
        parser.error('reports must stay under ignored ai_training/results/')
    dataset = json.loads(Path(args.dataset).read_text())
    if dataset.get('consent') is not True or dataset.get('split') != 'development' or not dataset.get('provenance'):
        parser.error('dataset needs consent, development split and provenance')
    if not 1 <= args.k <= 100:
        parser.error('invalid K')
    source_ids = [source['id'] for source in dataset['sources']]
    if len(set(source_ids)) != len(source_ids) or not dataset['cases']:
        parser.error('unique sources and nonempty cases are required')
    for case in dataset['cases']:
        if not set(case['expected_source_ids']).issubset(source_ids):
            parser.error('expected source IDs must exist in the fixture')
    report = asyncio.run(evaluate(args, dataset))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.touch(mode=0o600, exist_ok=True)
    output.chmod(0o600)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'cases':report['cases'], 'latency_seconds':report['latency_seconds']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
