import {useEffect,useRef,useState,type FormEvent} from 'react';
import {Link} from 'react-router-dom';
import {projectApi,type Project} from '../lib/projectApi';
import {chatApi} from '../lib/chatApi';
import {citationLocation,selectChatProvider,validateChatInput} from '../lib/chatState';
import type {ProviderCatalog,RagAnswer,RagQuery} from '../types/chat';
import './meeting-search-chat.css';

export function MeetingSearchChat(){
  const [projects,setProjects]=useState<Project[]>([]);
  const [catalog,setCatalog]=useState<ProviderCatalog>();
  const [input,setInput]=useState<RagQuery>({question:'',k:3,chat_provider_id:'',chat_model_id:'',embedding_profile_id:''});
  const [answer,setAnswer]=useState<RagAnswer>();
  const [error,setError]=useState('');
  const [isLoading,setIsLoading]=useState(false);
  const active=useRef<AbortController | undefined>(undefined);
  const question=useRef<HTMLTextAreaElement>(null);
  useEffect(()=>{
    const controller=new AbortController();
    void projectApi.list().then(data=>{if(!controller.signal.aborted)setProjects(data);}).catch(cause=>{if(!controller.signal.aborted)setError(cause.message);});
    void chatApi.providers(controller.signal).then(data=>{
      if(controller.signal.aborted)return;
      setCatalog(data);
      const provider=data.providers.find(p=>p.chat_models.length);
      setInput(value=>({...value,k:Math.min(value.k,data.max_k),chat_provider_id:provider?.id??'',chat_model_id:provider?.chat_models[0]??'',embedding_profile_id:data.embedding_profiles[0]?.id??''}));
    }).catch(cause=>{if(!controller.signal.aborted)setError(cause instanceof Error?cause.message:'Dịch vụ hỏi đáp chưa sẵn sàng. Kiểm tra cấu hình và worker RAG.');});
    return ()=>{controller.abort();active.current?.abort();};
  },[]);
  async function submit(event:FormEvent){
    event.preventDefault();
    if(!catalog||isLoading)return;
    const problem=validateChatInput(input,catalog);
    if(problem){setError(problem);question.current?.focus();return;}
    active.current?.abort();
    const controller=new AbortController();
    active.current=controller;
    setIsLoading(true);setError('');setAnswer(undefined);
    try{
      const result=await chatApi.query({...input,question:input.question.trim()},controller.signal);
      if(!controller.signal.aborted&&active.current===controller)setAnswer(result);
    }catch(cause){
      if(!controller.signal.aborted)setError(cause instanceof Error?cause.message:'Không thể gửi câu hỏi.');
    }finally{if(!controller.signal.aborted)setIsLoading(false);}
  }
  const embeddingProvider=catalog?.providers.find(p=>p.id===catalog.embedding_profiles.find(p=>p.id===input.embedding_profile_id)?.provider_id);
  return <div className="page rag-page">
    <section className="title"><div><small>MEETING LIBRARY</small><h2>Tìm kiếm thông tin</h2><p>Tìm bằng chứng trong tối đa K cuộc họp, kèm nguồn và thời gian.</p></div></section>
    <form className="panel rag-form" onSubmit={event=>void submit(event)} aria-busy={isLoading}>
      <fieldset disabled={isLoading}><legend>Câu hỏi và nguồn tìm kiếm</legend>
        <label htmlFor="rag-question">Câu hỏi</label><textarea ref={question} id="rag-question" value={input.question} maxLength={2000} rows={4} onChange={e=>setInput({...input,question:e.target.value})} required/>
        <label>Dự án<select value={input.project_id??""} onChange={event=>{setAnswer(undefined);setInput({...input,project_id:event.target.value||undefined});}}><option value="">Tất cả dự án có quyền truy cập</option>{projects.map(project=><option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
        <div className="rag-controls">
          <label>Số cuộc họp tối đa (K)<input type="number" min={1} max={catalog?.max_k??10} value={input.k} onChange={e=>setInput({...input,k:e.target.valueAsNumber})} required/></label>
          <label>Provider trả lời<select value={input.chat_provider_id} onChange={e=>catalog&&setInput(selectChatProvider(input,e.target.value,catalog))} required><option value="">Chọn provider</option>{catalog?.providers.filter(p=>p.chat_models.length).map(p=><option key={p.id} value={p.id}>{p.name} · {p.is_external?'Bên ngoài':'Local'}</option>)}</select></label>
          <label>Model trả lời<select value={input.chat_model_id} onChange={e=>setInput({...input,chat_model_id:e.target.value})} required><option value="">Chọn model</option>{catalog?.providers.find(p=>p.id===input.chat_provider_id)?.chat_models.map(m=><option key={m}>{m}</option>)}</select></label>
          <label>Embedding profile<select value={input.embedding_profile_id} onChange={e=>setInput({...input,embedding_profile_id:e.target.value})} required><option value="">Chọn profile</option>{catalog?.embedding_profiles.map(p=><option key={p.id} value={p.id}>{p.id}</option>)}</select></label>
        </div>
        {embeddingProvider&&<p>Embedding: {embeddingProvider.name} · {embeddingProvider.is_external?'Bên ngoài: câu hỏi và transcript được gửi để lập index/tìm kiếm.':'Local trên máy này.'}</p>}
        <button className="primary" type="submit" disabled={!catalog?.embedding_profiles.length||!input.chat_model_id}>{isLoading?'Đang tìm và trả lời…':'Tìm câu trả lời'}</button>
      </fieldset>
      {catalog&&!catalog.embedding_profiles.length&&<p role="status">Chưa có index sẵn sàng. Chạy worker và hoàn tất backfill trước khi hỏi.</p>}
      {error&&<p className="message" role="alert">{error}</p>}
      <span className="rag-status" role="status">{isLoading?'Đang xử lý câu hỏi.':''}</span>
    </form>
    {answer&&<section className="panel rag-results" aria-label="Kết quả hỏi đáp">
      <h3>{answer.retrieval_status==='answered'?'Câu trả lời':'Chưa đủ bằng chứng'}</h3><p className="rag-answer">{answer.answer}</p>
      {!!answer.selected_meetings.length&&<><h3>Cuộc họp liên quan</h3><ul>{answer.selected_meetings.map(m=><li key={m.meeting_id}><Link to={`/workspace/${encodeURIComponent(m.meeting_id)}`}>{m.title}</Link> · Độ liên quan {m.relevance_score.toFixed(2)}</li>)}</ul></>}
      {!!answer.citations.length&&<><h3>Nguồn trích dẫn</h3><ol>{answer.citations.map(c=><li key={c.source_id}><Link to={citationLocation(c)}>{c.meeting_title} · {c.speaker} · {c.start_seconds.toFixed(1)}–{c.end_seconds.toFixed(1)} giây</Link><p>{c.excerpt}</p><small>[{c.source_id}]</small></li>)}</ol></>}
      <p>Trích dẫn hợp lệ xác nhận nguồn được sử dụng; hãy đối chiếu nội dung trước khi ra quyết định.</p>
    </section>}
  </div>;
}
