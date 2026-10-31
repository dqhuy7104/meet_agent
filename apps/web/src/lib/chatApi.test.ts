import { afterEach, expect, it, vi } from 'vitest';
import { chatApi } from './chatApi';
afterEach(()=>{vi.unstubAllGlobals();});
it('scopes provider readiness and index status to the selected project',async()=>{
  const fetch=vi.fn().mockResolvedValue({ok:true,json:async()=>({profiles:[]})});
  vi.stubGlobal('fetch',fetch);
  await chatApi.providers(undefined,'project a');
  await chatApi.indexStatus(undefined,'project a');
  expect(fetch.mock.calls.map(call=>call[0])).toEqual([
    '/api/chat/providers?project_id=project%20a','/api/chat/index-status?project_id=project%20a',
  ]);
});
it('sends only query contract and passes abort signal',async()=>{
  const fetch=vi.fn().mockResolvedValue({ok:true,json:async()=>({retrieval_status:'insufficient_evidence'})});
  vi.stubGlobal('fetch',fetch);
  const controller=new AbortController();
  const query={question:'hello',k:1,chat_provider_id:'local',chat_model_id:'m',embedding_profile_id:'p'};
  await chatApi.query(query,controller.signal);
  expect(fetch.mock.calls[0][0]).toBe('/api/chat/queries');
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual(query);
  expect(fetch.mock.calls[0][1].signal).toBe(controller.signal);
});
it('does not expose provider error payloads',async()=>{
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:false,status:503,json:async()=>({detail:'secret'})}));
  await expect(chatApi.providers()).rejects.toThrow('Dịch vụ hỏi đáp chưa sẵn sàng.');
});
it('keeps abort rejection so the caller can ignore cancelled results',async()=>{
  vi.stubGlobal('fetch',vi.fn().mockRejectedValue(new DOMException('Aborted','AbortError')));
  await expect(chatApi.providers()).rejects.toMatchObject({name:'AbortError'});
});
