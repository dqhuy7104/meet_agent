import type {Citation, ProviderCatalog, RagQuery} from '../types/chat';
export function validateChatInput(input:RagQuery,catalog:ProviderCatalog):string{
  const question=input.question.trim();
  if(!question||question.length>2000||question.includes('\0'))return 'Nhập câu hỏi tối đa 2.000 ký tự.';
  if(!Number.isInteger(input.k)||input.k<1||input.k>catalog.max_k)return `Chọn từ 1 đến ${catalog.max_k} cuộc họp.`;
  if(!catalog.providers.some(p=>p.id===input.chat_provider_id&&p.chat_models.includes(input.chat_model_id)))return 'Chọn provider và model hợp lệ.';
  if(!catalog.embedding_profiles.some(p=>p.id===input.embedding_profile_id))return 'Chưa có embedding profile sẵn sàng.';
  return '';
}
export function selectChatProvider(input:RagQuery,id:string,catalog:ProviderCatalog):RagQuery{
  const models=catalog.providers.find(p=>p.id===id)?.chat_models??[];
  return {...input,chat_provider_id:id,chat_model_id:models.includes(input.chat_model_id)?input.chat_model_id:models[0]??''};
}
export function citationLocation(citation:Pick<Citation,'meeting_id'|'start_seconds'>):string{
  return `/workspace/${encodeURIComponent(citation.meeting_id)}?t=${citation.start_seconds}`;
}
