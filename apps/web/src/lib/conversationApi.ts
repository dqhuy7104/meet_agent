import {sessionOptions,expireSession,accessError} from './api';
import type {ConversationConfig,ConversationSummary,ConversationTurn,ConversationDetail,ConversationList} from '../types/chat';
async function request<T>(path:string,method='GET',body?:unknown,signal?:AbortSignal):Promise<T>{
 const response=await fetch('/api/chat/conversations'+path,sessionOptions({method,signal,headers:body?{'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined}));
 if(!response.ok){expireSession(response.status);throw new Error(accessError(response.status)??(response.status===409?'Hội thoại đã thay đổi. Hãy tải lại.':'Không thể cập nhật hội thoại.'));}
 return response.status===204?undefined as T:response.json() as Promise<T>;
}
export const conversationApi={
 list:(view:string,cursor?:string,signal?:AbortSignal)=>request<ConversationList>('?'+new URLSearchParams({view,...(cursor?{cursor}:{})}),'GET',undefined,signal),
 create:(project_id:string|null,signal?:AbortSignal)=>request<ConversationSummary>('','POST',{project_id},signal),
 detail:(id:string,before?:number,signal?:AbortSignal)=>request<ConversationDetail>('/'+encodeURIComponent(id)+(before?'?before_ordinal='+before:''),'GET',undefined,signal),
 scope:(id:string,project_id:string|null,signal?:AbortSignal)=>request<ConversationSummary>('/'+encodeURIComponent(id),'PATCH',{project_id},signal),
 delete:(id:string,signal?:AbortSignal)=>request<void>('/'+encodeURIComponent(id),'DELETE',undefined,signal),
 append:(id:string,question:string,expected_last_ordinal:number,config:ConversationConfig,signal?:AbortSignal)=>request<ConversationTurn>('/'+encodeURIComponent(id)+'/turns','POST',{question,expected_last_ordinal,config,request_id:crypto.randomUUID()},signal),
};
