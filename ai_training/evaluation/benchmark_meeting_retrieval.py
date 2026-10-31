"""Reproducible synthetic development benchmark; never uses application meetings."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import statistics
import platform
import time
import uuid

import httpx
import psycopg
from psycopg import sql

from app.core.rag_config import EmbeddingProfile, Provider, ProviderRegistry, RagSettings
from app.db.migrate import migrate_database
from app.schemas.transcript import AudioProcessResponse, TranscriptSegment
from app.services.access.repository import AccessRepository
from app.services.meetings.repository import MeetingRepository
from app.services.rag.chunking import chunk_transcript, snapshot_meeting
from app.services.rag.providers import ModelClient
from app.services.rag.ranking import rank_candidates, select_context, RANKING_WEIGHTS
from app.services.rag.repository import RagRepository
from evaluate_meeting_rag import compare_reports, output_hash, percentile, ranked_metrics


def development_corpus(corpus: str = 'short-v1') -> tuple[list[dict], list[dict]]:
    topics = [
        ('atlas', 'Atlas', 'Lan', '15 tháng 11', '120 triệu đồng', 'di chuyển cơ sở dữ liệu', 'Minh'),
        ('lotus', 'Lotus', 'Hùng', '20 tháng 11', '80 triệu đồng', 'tích hợp thanh toán', 'Mai'),
        ('bamboo', 'Bamboo', 'Thảo', '25 tháng 11', '95 triệu đồng', 'kiểm thử bảo mật', 'Phúc'),
        ('river', 'River', 'Quân', '30 tháng 11', '150 triệu đồng', 'triển khai tìm kiếm', 'An'),
        ('cloud', 'Cloud', 'Hà', '5 tháng 12', '200 triệu đồng', 'nâng cấp hạ tầng', 'Bình'),
        ('sun', 'Sun', 'Nam', '10 tháng 12', '60 triệu đồng', 'thiết kế giao diện', 'Chi'),
        ('moon', 'Moon', 'Linh', '15 tháng 12', '110 triệu đồng', 'đồng bộ dữ liệu', 'Dũng'),
        ('star', 'Star', 'Trang', '20 tháng 12', '75 triệu đồng', 'tối ưu hiệu năng', 'Sơn'),
    ]
    meetings, cases = [], []
    for key, name, owner, deadline, budget, work, reviewer in topics:
        meetings.append({'id': key, 'title': f'Họp dự án {name}', 'segments': [
            f'Cuộc họp dự án {name} tập trung vào {work}. {owner} chịu trách nhiệm {work} cho dự án {name}. Hạn hoàn thành là {deadline}.',
            f'Ngân sách được duyệt cho dự án {name} là {budget}. Không có ngân sách bổ sung cho giai đoạn này.',
            f'{reviewer} phụ trách review dự án {name}. Nhóm cần gửi báo cáo kiểm thử trước khi phát hành. Quyết định này được toàn bộ nhóm thống nhất.',
        ]})
        for suffix, question, segment in [
            ('owner', f'Ai chịu trách nhiệm {work} cho dự án {name}?', 0),
            ('budget', f'Ngân sách của dự án {name} được duyệt là bao nhiêu?', 1),
            ('review', f'Who reviews project {name}?', 2),
        ]:
            cases.append({'id': f'{key}-{suffix}', 'question': question,
                          'category': 'mixed_language' if suffix == 'review' else 'vietnamese',
                          'expected_meeting_ids': [key], 'expected_segment_ids': [f'{key}:{segment}']})
    cases.extend([
        {'id':'multi-budget','question':'So sánh ngân sách Atlas và Lotus.', 'category':'multi_meeting',
         'expected_meeting_ids':['atlas','lotus'], 'expected_segment_ids':['atlas:1','lotus:1']},
        {'id':'unanswerable','question':'Thực đơn bữa trưa công ty ngày mai gồm những món gì?',
         'category':'unanswerable','expected_meeting_ids':[], 'expected_segment_ids':[]},
    ])
    if corpus == 'long-v1':
        notes = [
            'Nhóm thảo luận cách phân chia công việc giữa các thành viên. Mỗi thay đổi phải được ghi lại trong tài liệu dự án. '
            'Các thành viên cần cập nhật tiến độ vào cuối ngày và báo sớm khi gặp trở ngại. Việc bàn giao phải đi kèm hướng dẫn vận hành. '
            'Nhóm thống nhất tiếp tục sử dụng quy trình kiểm tra hiện tại trong giai đoạn chuyển tiếp. ',
            'Chi phí được theo dõi theo từng hạng mục và đối chiếu với chứng từ thực tế. Các khoản phát sinh cần được phê duyệt trước khi thanh toán. '
            'Bộ phận tài chính yêu cầu lưu lại báo giá và giải trình lựa chọn nhà cung cấp. Số liệu sẽ được rà soát trong cuộc họp định kỳ tiếp theo. ',
            'Quy trình kiểm tra gồm kiểm thử chức năng, đánh giá trải nghiệm và xác nhận các trường hợp lỗi. '
            'Báo cáo cần mô tả cách tái hiện vấn đề và kết quả mong đợi. Những thay đổi quan trọng phải được kiểm tra lại trước khi bàn giao. '
            'Nhóm vận hành cần được thông báo trước khi triển khai để chuẩn bị giám sát và hỗ trợ. ',
        ]
        for meeting in meetings:
            meeting['segments'] = [notes[i] + text + ' ' + notes[(i+1) % 3] + notes[i]
                                   for i, text in enumerate(meeting['segments'])]
    return meetings, cases


async def benchmark(args: argparse.Namespace) -> dict:
    dsn = os.environ.get('TEST_DATABASE_URL')
    if not dsn:
        raise ValueError('TEST_DATABASE_URL is required; application DATABASE_URL is never used')
    meetings, cases = development_corpus(args.corpus)
    async with httpx.AsyncClient() as http:
        version_response = await http.get(args.ollama_url.rstrip('/') + '/api/version')
        version_response.raise_for_status()
        ollama_version = version_response.json()['version']
        response = await http.get(args.ollama_url.rstrip('/') + '/api/tags')
        response.raise_for_status()
        model = next(row for row in response.json()['models'] if row['name'] == args.embedding_model)
        probe = await http.post(args.ollama_url.rstrip('/') + '/api/embed',
                                json={'model':args.embedding_model, 'input':['dimension probe'], 'truncate':False}, timeout=60)
        probe.raise_for_status()
        profile = EmbeddingProfile(id='benchmark', provider_id='local', model=args.embedding_model,
            revision='sha256:' + model['digest'].removeprefix('sha256:'),
            dimensions=len(probe.json()['embeddings'][0]), chunk_revision=args.chunk_revision)
        settings = RagSettings(True, (Provider(id='local', name='Local benchmark', kind='ollama',
            base_url=args.ollama_url, embedding_models=[args.embedding_model], timeout_seconds=60),), (profile,))
        client = ModelClient(ProviderRegistry(settings), http)
        schema = 'rag_benchmark_' + uuid.uuid4().hex
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute('CREATE EXTENSION IF NOT EXISTS vector')
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            try:
                connection.execute(sql.SQL('SET search_path TO {},public').format(sql.Identifier(schema)))
                migrate_database(connection)
                access = AccessRepository(connection)
                account = access.create_account('synthetic@example.test', 'unusable-benchmark-hash')
                project = access.create_project('Synthetic retrieval benchmark')
                repository = MeetingRepository(connection)
                rag = RagRepository(connection)
                snapshots = []
                chunks = []
                indexing_start = time.monotonic()
                for meeting in meetings:
                    repository.create_meeting(meeting['id'], meeting['title'], 'synthetic.wav', meeting['id'], 'synthetic',
                        None, project_id=project.id, uploaded_by_account_id=account.id)
                    connection.execute("UPDATE meetings SET status='processing' WHERE id=%s", (meeting['id'],))
                    repository.complete(meeting['id'], AudioProcessResponse(filename='synthetic.wav',
                        duration_seconds=90, transcript=' '.join(meeting['segments']), vad_segments=[], diarization_segments=[],
                        transcript_segments=[TranscriptSegment(start=i*30, end=(i+1)*30, speaker=f'S{i}', text=text)
                                             for i, text in enumerate(meeting['segments'])]))
                    snapshot = snapshot_meeting(repository.get_detail(meeting['id']))
                    snapshots.append(snapshot)
                    current = chunk_transcript(snapshot, chunk_revision=profile.chunk_revision)
                    chunks.extend(current)
                    vectors = []
                    for offset in range(0, len(current), 2):
                        vectors.extend(await client.embed(profile, [c.text for c in current[offset:offset+2]]))
                    rag.publish(snapshot, profile, current, vectors)
                indexing_seconds = time.monotonic() - indexing_start
                rows, latencies = [], []
                for case in cases:
                    start = time.monotonic()
                    vector = (await client.embed(profile, [case['question']]))[0]
                    dense, lexical = rag.search(profile.id, case['question'], vector, [m['id'] for m in meetings], args.retrieval_version)
                    candidates = rank_candidates(case['question'], dense, lexical, retrieval_version=args.retrieval_version)
                    context = select_context(candidates, args.k, 6000)
                    elapsed = time.monotonic() - start
                    latencies.append(elapsed)
                    meeting_ids = list(dict.fromkeys(c.meeting_id for c in context))
                    segment_ids = list(dict.fromkeys(f'{c.meeting_id}:{s.ordinal}' for c in context for s in c.source_segments))
                    rows.append({'id':case['id'], 'category':case['category'], 'latency_seconds':elapsed,
                        'retrieval':ranked_metrics(set(case['expected_meeting_ids']), meeting_ids, args.k),
                        'segment_recall':len(set(case['expected_segment_ids']) & set(segment_ids)) / len(case['expected_segment_ids'])
                                         if case['expected_segment_ids'] else None,
                        'selected_meeting_ids':meeting_ids, 'retrieved_source_ids':[c.source_id for c in context],
                        'no_evidence':not context})
                answerable = [row for row in rows if row['category'] != 'unanswerable']
                return {'schema_version':2, 'evaluation_kind':'retrieval', 'dataset_hash':output_hash({'meetings':meetings,'cases':cases}),
                    'dataset_provenance':f'synthetic-{args.corpus}; authored fixtures; not held-out or production quality',
                    'split':'development', 'k':args.k, 'embedding_model':profile.model, 'embedding_revision':profile.revision,
                    'embedding_profile':profile.model_dump(), 'retrieval_version':args.retrieval_version,
                    'ranking_weights':RANKING_WEIGHTS,
                    'preprocessing':{'chunk_tokens':400,'overlap_tokens':60,'token_estimator':'ceil(utf8_bytes/3)',
                                     'context_tokens':6000,'min_cosine':.35},
                    'device':'unavailable: provider-managed', 'seed':'not applicable: fixed authored corpus',
                    'meeting_revisions':{s.meeting_id:s.revision for s in snapshots}, 'sample_count':len(rows),
                    'chunk_count':len(chunks), 'indexing_seconds':indexing_seconds,
                    'summary':{**{key:statistics.mean(row['retrieval'][key] for row in answerable)
                                 for key in ('recall_at_k','mrr','ndcg_at_k')},
                               'segment_recall':statistics.mean(row['segment_recall'] for row in answerable),
                               'unanswerable_rejected':all(row['no_evidence'] for row in rows if row['category']=='unanswerable')},
                    'latency_seconds':{'p50':percentile(latencies,.5),'p95':percentile(latencies,.95)},
                    'versions':{'psycopg':psycopg.__version__, 'httpx':httpx.__version__, 'ollama':ollama_version,
                                'python':platform.python_version(),
                                'postgres':connection.execute('SHOW server_version').fetchone()[0],
                                'pgvector':connection.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0]},
                    'cases':rows}
            finally:
                connection.execute('SET search_path TO public')
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--embedding-model', required=True)
    parser.add_argument('--corpus', choices=['short-v1', 'long-v1'], default='short-v1')
    parser.add_argument('--ollama-url', default='http://127.0.0.1:11434')
    parser.add_argument('--chunk-revision', choices=['utf8-v1', 'turns-v2'], default='utf8-v1')
    parser.add_argument('--retrieval-version', choices=['hybrid-v1', 'lexical-v2'], default='hybrid-v1')
    parser.add_argument('--k', type=int, default=3)
    parser.add_argument('--output', required=True)
    parser.add_argument('--compare-report')
    args = parser.parse_args()
    if not 1 <= args.k <= 100:
        parser.error('k must be between 1 and 100')
    output = Path(args.output).resolve()
    if not output.is_relative_to(Path(__file__).resolve().parents[1] / 'results'):
        parser.error('reports must be under ignored ai_training/results/')
    report = asyncio.run(benchmark(args))
    if args.compare_report:
        report['comparison'] = compare_reports(json.loads(Path(args.compare_report).read_text()), report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.touch(mode=0o600, exist_ok=True)
    output.chmod(0o600)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'summary':report['summary'], 'latency_seconds':report['latency_seconds'], 'chunk_count':report['chunk_count']}))


if __name__ == '__main__':
    main()
