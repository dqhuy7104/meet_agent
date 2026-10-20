"""Evaluate annotated held-out meeting queries against the local API."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
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


def output_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def verify_server_state(dataset: dict, state: dict, profile_id: str) -> None:
    if state.get('meeting_revisions') != dataset['meeting_revisions'] or state.get('embedding_profile',{}).get('id') != profile_id or not state.get('prompt_hash'):
        raise ValueError('evaluation_state_drift')
    if dataset.get('server_state_hash') and dataset['server_state_hash'] != output_hash(state):
        raise ValueError('evaluation_state_drift')


def bound_human_scores(ratings: dict | None, generated_hash: str):
    if ratings is None:
        return None
    if ratings.get('output_hash') != generated_hash:
        raise ValueError('rating_output_mismatch')
    return human_scores(ratings.get('claims'))


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values)*fraction)-1)] if values else 0.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset','chat-provider-id','chat-model-id','embedding-profile-id'):
        parser.add_argument('--'+name)
    parser.add_argument('--output',required=True)
    parser.add_argument('--score-report',help='Attach human ratings to an existing report without regenerating answers')
    parser.add_argument('--k', type=int, default=5)
    parser.add_argument('--human-ratings', help='Separate output-hash-bound claim ratings keyed by case ID')
    parser.add_argument('--answers-output', help='Optional private output capture under ai_training/results/')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = Path(args.output).resolve()
    if not output.is_relative_to(root / 'ai_training/results'):
        parser.error('reports must be under ignored ai_training/results/')
    if args.score_report:
        if not args.human_ratings:
            parser.error('--score-report requires --human-ratings')
        report=json.loads(Path(args.score_report).read_text())
        ratings=json.loads(Path(args.human_ratings).read_text())
        for case in report['cases']:
            if 'output_hash' in case:
                case['human']=bound_human_scores(ratings.get(case['id']),case['output_hash'])
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        output.chmod(0o600)
        return
    if not all((args.dataset,args.chat_provider_id,args.chat_model_id,args.embedding_profile_id)):
        parser.error('dataset and provider/model/profile IDs are required for an API run')
    dataset = json.loads(Path(args.dataset).read_text())
    validate_dataset(dataset)
    ratings = json.loads(Path(args.human_ratings).read_text()) if args.human_ratings else {}
    answers_output = Path(args.answers_output).resolve() if args.answers_output else None
    if answers_output and not answers_output.is_relative_to(root / 'ai_training/results'):
        parser.error('answer capture must be under ignored ai_training/results/')
    captured_answers = {}
    cases = []
    latencies = []
    with httpx.Client(base_url='http://127.0.0.1:8000', timeout=65) as client:
        state_parameters = dict(embedding_profile_id=args.embedding_profile_id,chat_provider_id=args.chat_provider_id,chat_model_id=args.chat_model_id)
        def server_state():
            result = client.get('/api/chat/evaluation-state',params=state_parameters)
            if result.status_code != 200:
                raise ValueError('evaluation_state_unavailable')
            state = result.json()
            verify_server_state(dataset,state,args.embedding_profile_id)
            return state
        state = server_state()
        state_hash = output_hash(state)
        for case in dataset['cases']:
            if output_hash(server_state()) != state_hash:
                raise ValueError('evaluation_state_drift')
            start = time.monotonic()
            response = client.post('/api/chat/queries', json=dict(question=case['question'], k=args.k,
                chat_provider_id=args.chat_provider_id, chat_model_id=args.chat_model_id,
                embedding_profile_id=args.embedding_profile_id))
            elapsed = time.monotonic()-start
            if output_hash(server_state()) != state_hash:
                raise ValueError('evaluation_state_drift')
            latencies.append(elapsed)
            if response.status_code != 200:
                cases.append({'id':case['id'],'http_status':response.status_code,'latency_seconds':elapsed})
                continue
            answer = response.json()
            generated_hash = output_hash(answer)
            captured_answers[case['id']] = dict(output_hash=generated_hash,output=answer)
            cases.append({'id':case['id'],'latency_seconds':elapsed,'status':answer['retrieval_status'],
                'retrieval':evaluate_retrieval(set(case['expected_meeting_ids']), [m['meeting_id'] for m in answer['selected_meetings']], args.k),
                'citations':evaluate_citations(set(case['expected_source_ids']), [c['source_id'] for c in answer['citations']]),
                'output_hash':generated_hash,'human':bound_human_scores(ratings.get(case['id']),generated_hash)})
    report = {'schema_version':1,'sample_count':len(cases),'dataset_provenance':dataset['provenance'],
        'meeting_revisions':state['meeting_revisions'],'split':'held_out','prompt_version':state['prompt_version'],
        'server_state_hash':state_hash,'server_state':state,'prompt_hash':state['prompt_hash'],'embedding_profile':state['embedding_profile'],'chat_provider':args.chat_provider_id,
        'chat_model':args.chat_model_id,'device':state['device'],
        'provider_versions':state['provider_versions'],
        'ranking':{'rrf_constant':60,'vector_weight':.7,'lexical_weight':.3,'min_cosine':state['min_cosine_similarity'],
                   'candidate_cap':40,'retrieval_cap_per_channel':50,'chunk_tokens':400,'overlap_tokens':60,'k':args.k},
        'cost':'unavailable: API does not expose provider usage/pricing',
        'latency_seconds':{'p50':percentile(latencies,.5),'p95':percentile(latencies,.95)},'cases':cases}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    output.chmod(0o600)
    if answers_output:
        answers_output.parent.mkdir(parents=True,exist_ok=True)
        answers_output.write_text(json.dumps(dict(server_state_hash=state_hash,cases=captured_answers),ensure_ascii=False,indent=2))
        answers_output.chmod(0o600)


if __name__ == '__main__':
    main()
