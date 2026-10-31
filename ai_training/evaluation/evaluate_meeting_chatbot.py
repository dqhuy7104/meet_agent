"""Local synthetic chatbot development check; not a held-out quality benchmark."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time

import httpx

from app.core.rag_config import ChatModel, Provider, ProviderRegistry, RagError, RagSettings
from app.services.conversations.chatbot import ANSWER_HASH, ROUTING_HASH, route_message, generate_chat_answer
from app.services.conversations.context import CONTEXT_HASH, resolve_question
from app.services.rag.chunking import chunk_transcript, make_snapshot
from app.services.rag.providers import ModelClient
from app.services.rag.types import SourceSegment

CASES = [
    ('greeting','Xin chào',[],False,'conversation'),
    ('meeting','Thủ tướng đề nghị hợp tác gì với Lào?',[],False,'retrieval'),
    ('followup','Còn Nhật Bản?', [{'question':'Thủ tướng đề nghị hợp tác gì với Lào?', 'answer':'Hợp tác giáo dục.'}],False,'retrieval'),
    ('ambiguous','Còn bên kia?',[],False,'clarification'),
    ('general_off','Làm thế nào để học tiếng Việt hiệu quả?',[],False,'retrieval'),
    ('general_on','Làm thế nào để học tiếng Việt hiệu quả?',[],True,'retrieval'),
    ('transform','Viết ngắn lại', [{'question':'Lào hợp tác gì?', 'answer':'Lào hợp tác về giáo dục.'}],False,'transform'),
]


async def evaluate(model: str, url: str) -> dict:
    provider = Provider(id='local',name='Local',kind='ollama',base_url=url,
        chat_models=[ChatModel(id=model,context_tokens=8192,output_tokens=1024)])
    if provider.is_external:
        raise ValueError('only_loopback_ollama_is_allowed')
    registry = ProviderRegistry(RagSettings(True,(provider,),()))
    selection = registry.resolve_chat('local',model)
    sources = chunk_transcript(make_snapshot('synthetic','Synthetic meeting',(
        SourceSegment(0,'Thủ tướng đề nghị hợp tác giáo dục với Lào và hợp tác nguồn nhân lực với Nhật Bản.','A',0,15),)))
    results = []
    async with httpx.AsyncClient() as http:
        tags = await http.get(url.rstrip('/')+'/api/tags')
        tags.raise_for_status()
        digest = next(row['digest'] for row in tags.json()['models'] if row['name']==model)
        client = ModelClient(registry,http)
        for case_id, question, history, enabled, expected in CASES:
            row = {'id':case_id,'expected_intent':expected,'allow_model_knowledge':enabled}
            start = time.monotonic()
            try:
                row['baseline_resolution'] = await resolve_question(question,history,selection,client)
            except RagError as error:
                row['baseline_error'] = error.code
            row['baseline_seconds'] = time.monotonic()-start
            start = time.monotonic()
            try:
                decision = await route_message(question,history,selection,client)
                row['intent'] = decision.intent
                row['routing_correct'] = decision.intent==expected
                if decision.intent!='clarification':
                    evidence = sources if case_id in {'meeting','followup','transform'} else []
                    answer = await generate_chat_answer(decision.question or question,history,evidence,enabled,selection,client,intent=decision.intent)
                    row['answer'] = answer.model_dump()
                    row['mode_violation'] = not enabled and any(part.kind=='model' for part in answer.parts)
            except RagError as error:
                row['candidate_error'] = error.code
            row['candidate_seconds'] = time.monotonic()-start
            results.append(row)
    return {'dataset_version':'synthetic-chatbot-v1','split':'development','sample_count':len(CASES),
        'dataset_hash':hashlib.sha256(json.dumps(CASES,ensure_ascii=False).encode()).hexdigest(),
        'model':model,'model_digest':digest,'routing_hash':ROUTING_HASH,'answer_hash':ANSWER_HASH,'baseline_hash':CONTEXT_HASH,
        'generation':{'context_tokens':8192,'output_tokens':1024,'format':'json','seed':'provider default','temperature':'provider default'},
        'cost':'local CPU inference; no external API','limitations':'One synthetic run per case; fixed evidence supplied, not retrieval evaluation. Requires manual factual/intent review.',
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


if __name__=='__main__':
    main()
