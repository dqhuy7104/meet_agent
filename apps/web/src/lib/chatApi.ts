import { sessionOptions, expireSession, accessError } from './api';
import type {ProviderCatalog, RagAnswer, RagQuery} from '../types/chat';
const messages:Record<number,string>={409:'Index chưa sẵn sàng. Hãy thử lại sau.',422:'Kiểm tra câu hỏi và lựa chọn model.',503:'Dịch vụ hỏi đáp chưa sẵn sàng.',504:'Yêu cầu đã quá thời gian. Hãy thử lại.'};
async function request<T>(path:string,signal?:AbortSignal,query?:RagQuery):Promise<T>{
  const response=await fetch('/api/chat/'+path,sessionOptions({method:query?'POST':'GET',headers:query?{'Content-Type':'application/json'}:undefined,body:query?JSON.stringify(query):undefined,signal}));
  if(!response.ok) { expireSession(response.status); throw new Error(accessError(response.status)??messages[response.status]??'Không thể gửi yêu cầu hỏi đáp.'); }
  return response.json() as Promise<T>;
}
export const chatApi={
  providers:(signal?:AbortSignal)=>request<ProviderCatalog>('providers',signal),
  query:(query:RagQuery,signal?:AbortSignal)=>request<RagAnswer>('queries',signal,query),
};
