"""Evaluate annotated held-out meeting queries against the local API."""
from __future__ import annotations
import argparse
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time
import httpx


def evaluate_retrieval(expected_meeting_ids: set[str], retrieved_meeting_ids: list[str], k: int) -> dict[str, float]:
    retrieved = set(list(dict.fromkeys(retrieved_meeting_ids))[:k])
    return {'recall_at_k': len(expected_meeting_ids & retrieved) / len(expected_meeting_ids) if expected_meeting_ids else 0.}


def evaluate_citations(expected_source_ids: set[str], actual_source_ids: list[str]) -> dict[str, float]:
    actual = set(actual_source_ids)
    matches = len(actual & expected_source_ids)
    return {'precision': matches / len(actual) if actual else 0.,
            'recall': matches / len(expected_source_ids) if expected_source_ids else 0.}


def human_scores(ratings: list[dict] | None) -> dict[str, float] | None:
    if not ratings:
        return None
    if any(type(r.get('supported')) is not bool or type(r.get('usefulness')) is not int or not 1 <= r['usefulness'] <= 5 for r in ratings):
        raise ValueError('invalid_human_ratings')
    return {'factual_support': statistics.mean(r['supported'] for r in ratings),
            'usefulness': statistics.mean(r['usefulness'] for r in ratings)}


def validate_dataset(dataset: dict) -> None:
    if dataset.get('schema_version') != 1 or dataset.get('consent') is not True or not dataset.get('provenance') or dataset.get('split') != 'held_out' or not dataset.get('meeting_revisions') or not dataset.get('cases'):
        raise ValueError('dataset_requires_consent_provenance_revisions_and_held_out_annotations')
    seen = set()
    for case in dataset['cases']:
        required = {'id','question','category','expected_meeting_ids','expected_source_ids'}
        if not required.issubset(case) or not case['question'].strip() or case['id'] in seen or case['category'] not in {'vietnamese','mixed_language','unanswerable','contradictory','injection'}:
            raise ValueError('invalid_case_annotations')
        if any(m not in dataset['meeting_revisions'] for m in case['expected_meeting_ids']):
            raise ValueError('missing_meeting_revision')
        seen.add(case['id'])


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values)*fraction)-1)] if values else 0.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset','chat-provider-id','chat-model-id','embedding-profile-id','output'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--k', type=int, default=5)
    parser.add_argument('--human-ratings', help='Separate claim-level ratings keyed by case ID')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = Path(args.output).resolve()
    if not output.is_relative_to(root / 'ai_training/results'):
        parser.error('reports must be under ignored ai_training/results/')
    dataset = json.loads(Path(args.dataset).read_text())
    validate_dataset(dataset)
    ratings = json.loads(Path(args.human_ratings).read_text()) if args.human_ratings else {}
    sys.path.insert(0, str(root / 'apps/api'))
    from app.core.rag_config import load_rag_settings, ProviderRegistry
    from app.services.rag.prompt import PROMPT_HASH
    settings = load_rag_settings(os.environ)
    profile = ProviderRegistry(settings).resolve_profile(args.embedding_profile_id)
    cases = []
    latencies = []
    with httpx.Client(base_url='http://127.0.0.1:8000', timeout=65) as client:
        for case in dataset['cases']:
            start = time.monotonic()
            response = client.post('/api/chat/queries', json=dict(question=case['question'], k=args.k,
                chat_provider_id=args.chat_provider_id, chat_model_id=args.chat_model_id,
                embedding_profile_id=args.embedding_profile_id))
            elapsed = time.monotonic()-start
            latencies.append(elapsed)
            if response.status_code != 200:
                cases.append({'id':case['id'],'http_status':response.status_code,'latency_seconds':elapsed})
                continue
            answer = response.json()
            cases.append({'id':case['id'],'latency_seconds':elapsed,'status':answer['retrieval_status'],
                'retrieval':evaluate_retrieval(set(case['expected_meeting_ids']), [m['meeting_id'] for m in answer['selected_meetings']], args.k),
                'citations':evaluate_citations(set(case['expected_source_ids']), [c['source_id'] for c in answer['citations']]),
                'human':human_scores(ratings.get(case['id']))})
    report = {'schema_version':1,'sample_count':len(cases),'dataset_provenance':dataset['provenance'],
        'meeting_revisions':dataset['meeting_revisions'],'split':'held_out','prompt_version':settings.prompt_version,
        'prompt_hash':PROMPT_HASH,'embedding_profile':profile.model_dump(),'chat_provider':args.chat_provider_id,
        'chat_model':args.chat_model_id,'device':dataset.get('device','unavailable'),
        'provider_versions':dataset.get('provider_versions','unavailable'),
        'ranking':{'rrf_constant':60,'vector_weight':.7,'lexical_weight':.3,'min_cosine':settings.min_cosine_similarity,
                   'candidate_cap':40,'retrieval_cap_per_channel':50,'chunk_tokens':400,'overlap_tokens':60,'k':args.k},
        'cost':'unavailable: API does not expose provider usage/pricing',
        'latency_seconds':{'p50':percentile(latencies,.5),'p95':percentile(latencies,.95)},'cases':cases}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
