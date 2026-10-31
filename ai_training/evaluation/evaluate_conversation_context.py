"""Local synthetic smoke comparison of raw follow-ups and context resolution."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import platform
import time

import httpx

from app.core.rag_config import ChatModel, Provider, ProviderRegistry, RagError, RagSettings
from app.services.conversations.context import CONTEXT_HASH, CONTEXT_VERSION, resolve_question
from app.services.rag.providers import ModelClient


HISTORY = [{'question':'Thủ tướng đề nghị hợp tác gì với Lào và Nhật Bản?',
            'answer':'Với Lào là giáo dục; với Nhật Bản là vốn, công nghệ và nhân lực.'}]
CASES = [
    {'id':'standalone','question':'Thủ tướng đề nghị hợp tác gì với Lào?', 'history':[], 'entities':['Lào'], 'clarify':False},
    {'id':'followup','question':'Điểm chung giữa hai cuộc gặp là gì?', 'history':HISTORY, 'entities':['Lào','Nhật Bản'], 'clarify':False},
    {'id':'explicit_entity','question':'Còn Nhật Bản thì sao?', 'history':HISTORY, 'entities':['Nhật Bản'], 'clarify':False},
    {'id':'ambiguous','question':'Còn bên kia thì sao?', 'history':HISTORY, 'entities':[], 'clarify':True},
    {'id':'source_changed','question':'Điểm chung giữa hai cuộc gặp là gì?', 'history':[], 'entities':[], 'clarify':True},
]


async def evaluate(model: str, url: str) -> dict:
    provider = Provider(id='local',name='Local',kind='ollama',base_url=url,
                        chat_models=[ChatModel(id=model,context_tokens=8192,output_tokens=1024)])
    if provider.is_external:
        raise ValueError('only_loopback_ollama_is_allowed')
    registry = ProviderRegistry(RagSettings(True,(provider,),()))
    results = []
    async with httpx.AsyncClient() as http:
        tags = await http.get(url.rstrip('/')+'/api/tags')
        tags.raise_for_status()
        digest = next(row['digest'] for row in tags.json()['models'] if row['name']==model)
        client = ModelClient(registry,http)
        for case in CASES:
            start = time.monotonic()
            error, resolved = None, None
            try:
                resolved = await resolve_question(case['question'],case['history'],registry.resolve_chat('local',model),client)
            except RagError as failure:
                error = failure.code
            entities = case['entities']
            results.append({'id':case['id'],'baseline_question':case['question'],
                'resolved_question':resolved,'error':error,'added_seconds':time.monotonic()-start,
                'baseline_entity_coverage':sum(entity.casefold() in case['question'].casefold() for entity in entities)/len(entities) if entities else None,
                'candidate_entity_coverage':sum(entity.casefold() in (resolved or '').casefold() for entity in entities)/len(entities) if entities else None,
                'clarification_correct':error is None and (resolved is None)==case['clarify']})
    return {'dataset_version':'synthetic-context-v1','dataset_hash':hashlib.sha256(json.dumps(CASES,ensure_ascii=False).encode()).hexdigest(),
        'split':'development','sample_count':len(CASES),'model':model,'model_digest':digest,
        'prompt_version':CONTEXT_VERSION,'prompt_hash':CONTEXT_HASH,'python':platform.python_version(),
        'device':'local Ollama; inspect provider runtime','cost':'local inference; no external API',
        'limitations':'Synthetic resolver smoke only; entity coverage is not entailment or retrieval quality. First turns skip resolution in production.',
        'cases':results}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',required=True)
    parser.add_argument('--ollama-url',default='http://127.0.0.1:11434')
    parser.add_argument('--output',required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if not output.is_relative_to(Path(__file__).resolve().parents[2]/'ai_training/results'):
        parser.error('reports must be under ignored ai_training/results/')
    report = asyncio.run(evaluate(args.model,args.ollama_url))
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
