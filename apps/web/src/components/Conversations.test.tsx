// @vitest-environment jsdom
import {afterEach,beforeEach,it,expect,vi} from 'vitest';
import {cleanup,render,screen,fireEvent,waitFor} from '@testing-library/react';
import {MemoryRouter,useNavigate} from 'react-router-dom';
import {MeetingSearchChat} from './MeetingSearchChat';
vi.mock('../lib/AuthProvider',()=>({useAuth:()=>({account:{id:'me',is_system_admin:true}})}));
afterEach(()=>{cleanup();vi.unstubAllGlobals();});
beforeEach(()=>{
 HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};
 HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');this.dispatchEvent(new Event('close'));};
});
it('uses an icon-only gear and a dismissible config drawer without the old title',async()=>{
 setup(false,false);await screen.findByText('Saved answer');
 expect(screen.queryByText('MEETING LIBRARY')).toBeNull();
 expect(screen.queryByRole('heading',{name:'Chatbot'})).toBeNull();
 const trigger=screen.getByRole('button',{name:'Cấu hình'});
 expect(trigger.textContent).toBe('');
 expect(screen.queryByRole('dialog')).toBeNull();
 fireEvent.click(trigger);expect(screen.getByRole('dialog',{name:'Cấu hình'})).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'Đóng cấu hình'}));
 expect(screen.queryByRole('dialog')).toBeNull();expect(document.activeElement).toBe(trigger);
});
const config={k:3,chat_provider_id:'local',chat_model_id:'model',embedding_profile_id:'embed',context_weight:.8};
it('defaults outside knowledge off and saves explicit opt in',async()=>{
 const fetcher=setup();await screen.findByText('Saved answer');
 const checkbox=screen.getByRole('checkbox',{name:'Cho phép kiến thức ngoài cuộc họp'}) as HTMLInputElement;
 expect(checkbox.checked).toBe(false);fireEvent.click(checkbox);
 fireEvent.change(screen.getByLabelText('Câu hỏi'),{target:{value:'Advice'}});
 fireEvent.click(screen.getByRole('button',{name:'Gửi'}));
 await waitFor(()=>expect(fetcher.mock.calls.some(([url])=>url.endsWith('/turns'))).toBe(true));
 const call=fetcher.mock.calls.find(([url])=>url.endsWith('/turns'))!;
 expect(JSON.parse(call[1]!.body as string).config.allow_model_knowledge).toBe(true);
});
const conversation={id:'saved',title:'Saved question',creator_account_id:'me',creator_email:null,project_id:null,config,created_at:'now',updated_at:'now',last_ordinal:1,read_only:false};
const answer={answer:'Saved answer',retrieval_status:'answered',selected_meetings:[],citations:[{source_id:'secret-id',meeting_id:'m',speaker:'Alice',start_seconds:2,end_seconds:4,excerpt:'Evidence'}]};
function setup(readOnly=false,showConfig=true){const fetcher=vi.fn(async(url:string,options?:RequestInit)=>{
 let body:unknown={};
 if(url==='/api/projects')body=[];
 else if(url.includes('/providers'))body={max_k:5,providers:[{id:'local',name:'Local',chat_models:['model']}],embedding_profiles:[{id:'embed',provider_id:'local'}]};
 else if(url.includes('/index-status'))body={profiles:[]};
 else if(url.includes('/turns'))body={id:'t2',ordinal:2,question:'Follow up',config,status:'completed',answer};
 else if(url.startsWith('/api/chat/conversations?'))body={items:[{...conversation,read_only:readOnly}],next_cursor:null};
 else if(url.startsWith('/api/chat/conversations/'))body={conversation:{...conversation,read_only:readOnly},turns:[{id:'t',ordinal:1,question:'Saved question',status:'completed',config,answer}],next_before_ordinal:null};
 return {ok:true,status:200,json:async()=>body};
 });vi.stubGlobal('fetch',fetcher);render(<MemoryRouter initialEntries={['/?conversation=saved']}><MeetingSearchChat/></MemoryRouter>);if(showConfig)fireEvent.click(screen.getByRole('button',{name:'Cấu hình'}));return fetcher;}
it.each([0,.5,1])('restores history and submits slider configuration %s to conversation turns',async(weight)=>{const fetcher=setup();await screen.findByText('Saved answer');expect(screen.getByRole('link',{name:/Nghe audio/}).getAttribute('href')).toBe('/workspace/m?t=2');expect(screen.queryByText('secret-id')).toBeNull();fireEvent.change(screen.getByLabelText('Trọng số ngữ cảnh'),{target:{value:String(weight)}});fireEvent.change(screen.getByLabelText('Câu hỏi'),{target:{value:'Follow up'}});fireEvent.click(screen.getByRole('button',{name:'Gửi'}));await waitFor(()=>expect(fetcher.mock.calls.some(([u])=>u.endsWith('/turns'))).toBe(true));const call=fetcher.mock.calls.find(([u])=>u.endsWith('/turns'))!;expect(JSON.parse(call[1]!.body as string).config.context_weight).toBe(weight);expect(fetcher.mock.calls.some(([u])=>u.endsWith('/queries'))).toBe(false);});
it('keeps other-user conversations read only',async()=>{setup(true);await screen.findByText('Saved answer');expect(screen.getByText('Chỉ đọc')).toBeTruthy();expect(screen.queryByRole('button',{name:'Gửi'})).toBeNull();expect(screen.queryByRole('button',{name:'Xóa hội thoại'})).toBeNull();expect(screen.getByRole('button',{name:'Người khác'})).toBeTruthy();});
it('loads history even when providers fail and disables changed-source audio',async()=>{
 const fetcher=setup();await screen.findByText('Saved answer');cleanup();
 vi.stubGlobal('fetch',vi.fn(async(url:string,options?:RequestInit)=>{if(url.includes('/providers'))return {ok:false,status:503};if(url.endsWith('/saved'))return {ok:true,status:200,json:async()=>({conversation,turns:[{id:'t',ordinal:1,status:'stale',question:'Old question',answer}],next_before_ordinal:null})};return fetcher(url,options);}));
 render(<MemoryRouter initialEntries={['/?conversation=saved']}><MeetingSearchChat/></MemoryRouter>);await screen.findByText('Saved answer');expect(screen.getByText('Nguồn đã thay đổi')).toBeTruthy();expect(screen.queryByRole('link',{name:/Nghe audio/})).toBeNull();
});
it('recovers the saved result after a lost response before resending',async()=>{
 const fetcher=setup();await screen.findByText('Saved answer');let lost=false;
 vi.stubGlobal('fetch',vi.fn(async(url:string,options?:RequestInit)=>{if(url.endsWith('/turns')){lost=true;throw new Error('Lost response');}if(lost&&url.endsWith('/saved'))return {ok:true,status:200,json:async()=>({conversation:{...conversation,last_ordinal:2},turns:[{id:'t2',ordinal:2,status:'completed',question:'Follow up',answer:{...answer,answer:'Recovered answer'}}],next_before_ordinal:null})};return fetcher(url,options);}));
 fireEvent.change(screen.getByLabelText('Câu hỏi'),{target:{value:'Follow up'}});fireEvent.click(screen.getByRole('button',{name:'Gửi'}));await screen.findByText('Recovered answer');expect((screen.getByLabelText('Câu hỏi') as HTMLTextAreaElement).value).toBe('');
});
it('creates an empty conversation and deletes only after confirmation',async()=>{
 const fetcher=setup();await screen.findByText('Saved answer');let isNew=false;
 const writes=vi.fn(async(url:string,options?:RequestInit)=>{if(url==='/api/chat/conversations'&&options?.method==='POST'){isNew=true;return {ok:true,status:200,json:async()=>({...conversation,id:'new',title:'Hội thoại mới',config:null,last_ordinal:0})};}if(url.endsWith('/new'))return {ok:true,status:options?.method==='DELETE'?204:200,json:async()=>({conversation:{...conversation,id:'new',title:'Hội thoại mới',config:null,last_ordinal:0},turns:[],next_before_ordinal:null})};return fetcher(url,options);});vi.stubGlobal('fetch',writes);
 fireEvent.click(screen.getByRole('button',{name:'New Chat'}));await waitFor(()=>expect(isNew).toBe(true));await screen.findByRole('heading',{name:'Hội thoại mới'});await waitFor(()=>expect((screen.getByLabelText('Provider trả lời') as HTMLSelectElement).value).toBe('local'));
 vi.spyOn(window,'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);fireEvent.click(screen.getByRole('button',{name:'Xóa hội thoại'}));expect(writes.mock.calls.some(([,o])=>o?.method==='DELETE')).toBe(false);fireEvent.click(screen.getByRole('button',{name:'Xóa hội thoại'}));await waitFor(()=>expect(writes.mock.calls.some(([,o])=>o?.method==='DELETE')).toBe(true));vi.mocked(window.confirm).mockRestore();
});
it('keeps a failed question draft and does not resend while recovery is unavailable',async()=>{
 const fetcher=setup();await screen.findByText('Saved answer');
 vi.stubGlobal('fetch',vi.fn(async(url:string,options?:RequestInit)=>{if(url.endsWith('/turns')||url.endsWith('/saved'))throw new Error('Connection lost');return fetcher(url,options);}));
 fireEvent.change(screen.getByLabelText('Câu hỏi'),{target:{value:'Keep this draft'}});fireEvent.click(screen.getByRole('button',{name:'Gửi'}));await screen.findByRole('button',{name:'Tải lại hội thoại'});expect((screen.getByLabelText('Câu hỏi') as HTMLTextAreaElement).value).toBe('Keep this draft');expect((screen.getByRole('button',{name:'Gửi'}) as HTMLButtonElement).disabled).toBe(true);
});
it('does not expose redacted question or answer content',async()=>{
 const fetcher=setup();await screen.findByText('Saved answer');cleanup();vi.stubGlobal('fetch',vi.fn(async(url:string,options?:RequestInit)=>{if(url.endsWith('/saved'))return {ok:true,status:200,json:async()=>({conversation,turns:[{id:'hidden',ordinal:1,status:'redacted',question:'Hidden question',answer}],next_before_ordinal:null})};return fetcher(url,options);}));render(<MemoryRouter initialEntries={['/?conversation=saved']}><MeetingSearchChat/></MemoryRouter>);await screen.findByText('Nội dung không còn khả dụng.');expect(screen.queryByText('Hidden question')).toBeNull();expect(screen.queryByText('Saved answer')).toBeNull();
});
it('blocks sends when a selected conversation could not be loaded',async()=>{
 const fetcher=setup();await screen.findByText('Saved answer');cleanup();vi.stubGlobal('fetch',vi.fn(async(url:string,options?:RequestInit)=>{if(url.endsWith('/missing'))return {ok:false,status:404};return fetcher(url,options);}));render(<MemoryRouter initialEntries={['/?conversation=missing']}><MeetingSearchChat/></MemoryRouter>);await screen.findByRole('alert');fireEvent.click(screen.getByRole('button',{name:'Cấu hình'}));await screen.findByRole('option',{name:/Local ·/});fireEvent.change(screen.getByLabelText('Provider trả lời'),{target:{value:'local'}});fireEvent.change(screen.getByLabelText('Embedding profile'),{target:{value:'embed'}});fireEvent.change(screen.getByLabelText('Câu hỏi'),{target:{value:'Should not send'}});expect((screen.getByRole('button',{name:'Gửi'}) as HTMLButtonElement).disabled).toBe(true);
});

function Back(){const navigate=useNavigate();return <button onClick={()=>navigate(-1)}>Back</button>;}
it('restores browser back selection and prepends earlier turns',async()=>{
 const fetcher=setup();await screen.findByText('Saved answer');cleanup();vi.stubGlobal('fetch',vi.fn(async(url:string,options?:RequestInit)=>{if(url.endsWith('/earlier?before_ordinal=2'))return {ok:true,status:200,json:async()=>({conversation,turns:[{id:'first',ordinal:1,status:'completed',question:'First question',answer:{...answer,answer:'First answer'}}],next_before_ordinal:null})};if(url.endsWith('/earlier'))return {ok:true,status:200,json:async()=>({conversation:{...conversation,id:'earlier'},turns:[{id:'second',ordinal:2,status:'completed',question:'Second question',answer:{...answer,answer:'Second answer'}}],next_before_ordinal:2})};return fetcher(url,options);}));render(<MemoryRouter initialEntries={['/?conversation=earlier','/?conversation=saved']} initialIndex={1}><Back/><MeetingSearchChat/></MemoryRouter>);await screen.findByText('Saved answer');fireEvent.click(screen.getByRole('button',{name:'Back'}));await screen.findByText('Second answer');expect(screen.queryByText('Saved answer')).toBeNull();fireEvent.click(screen.getByRole('button',{name:'Tin nhắn trước'}));await screen.findByText('First answer');expect(screen.getByText('Second answer')).toBeTruthy();expect(screen.getAllByText(/First question|Second question/).map(node=>node.textContent)).toEqual(['First question','Second question']);
});
it('clears the recovered draft once a running request completes',async()=>{
 const fetcher=setup();await screen.findByText('Saved answer');let started=false;vi.stubGlobal('fetch',vi.fn(async(url:string,options?:RequestInit)=>{if(url.endsWith('/turns')){started=true;return {ok:true,status:200,json:async()=>({id:'running',ordinal:2,question:'Follow up',config,status:'running',answer:null})};}if(started&&url.endsWith('/saved'))return {ok:true,status:200,json:async()=>({conversation:{...conversation,last_ordinal:2},turns:[{id:'running',ordinal:2,question:'Follow up',config,status:'completed',answer:{...answer,answer:'Finished answer'}}],next_before_ordinal:null})};return fetcher(url,options);}));fireEvent.change(screen.getByLabelText('Câu hỏi'),{target:{value:'Follow up'}});fireEvent.click(screen.getByRole('button',{name:'Gửi'}));await screen.findByText('Finished answer',{}, {timeout:3500});expect((screen.getByLabelText('Câu hỏi') as HTMLTextAreaElement).value).toBe('');
});
it('keeps an unsaved draft usable when creating its conversation fails',async()=>{
 const fetcher=setup();await screen.findByText('Saved answer');cleanup();vi.stubGlobal('fetch',vi.fn(async(url:string,options?:RequestInit)=>{if(url==='/api/chat/conversations'&&options?.method==='POST')throw new Error('Cannot create');return fetcher(url,options);}));render(<MemoryRouter><MeetingSearchChat/></MemoryRouter>);fireEvent.click(screen.getByRole('button',{name:'Cấu hình'}));await screen.findByRole('option',{name:'model'});fireEvent.change(screen.getByLabelText('Câu hỏi'),{target:{value:'Unsaved question'}});fireEvent.click(screen.getByRole('button',{name:'Gửi'}));await screen.findByText('Cannot create');await waitFor(()=>expect((screen.getByRole('button',{name:'Gửi'}) as HTMLButtonElement).disabled).toBe(false));expect((screen.getByLabelText('Câu hỏi') as HTMLTextAreaElement).value).toBe('Unsaved question');
});
it.each([false,true])('keeps loaded earlier turns and their cursor during polling (exhausted=%s)',async(exhausted)=>{
 const fetcher=setup();await screen.findByText('Saved answer');cleanup();let details=0;
 const latest={id:'latest',ordinal:3,question:'Latest question',config,status:'running',answer:null};
 const earlier={id:'earlier',ordinal:2,question:'Earlier question',config,status:'completed',answer:{...answer,answer:'Earlier answer'}};
 vi.stubGlobal('fetch',vi.fn(async(url:string,options?:RequestInit)=>{
  if(url.endsWith('/saved?before_ordinal=3'))return {ok:true,status:200,json:async()=>({conversation,turns:[earlier],next_before_ordinal:exhausted?null:2})};
  if(url.endsWith('/saved?before_ordinal=2'))return {ok:true,status:200,json:async()=>({conversation,turns:[{...earlier,id:'first',ordinal:1,question:'First question',answer:{...answer,answer:'First answer'}}],next_before_ordinal:null})};
  if(url.endsWith('/saved')){details++;return {ok:true,status:200,json:async()=>({conversation,turns:[details===1?latest:{...latest,status:'completed',answer:{...answer,answer:'Latest answer'}}],next_before_ordinal:3})};}
  return fetcher(url,options);
 }));render(<MemoryRouter initialEntries={['/?conversation=saved']}><MeetingSearchChat/></MemoryRouter>);
 fireEvent.click(await screen.findByRole('button',{name:'Tin nhắn trước'}));await screen.findByText('Earlier answer');await screen.findByText('Latest answer',{}, {timeout:3500});
 expect(screen.getByText('Earlier answer')).toBeTruthy();expect(screen.getAllByText('Latest question')).toHaveLength(1);
 if(exhausted)expect(screen.queryByRole('button',{name:'Tin nhắn trước'})).toBeNull();else{fireEvent.click(screen.getByRole('button',{name:'Tin nhắn trước'}));await screen.findByText('First answer');expect(screen.getByText('Earlier answer')).toBeTruthy();}
});
it('keeps detail recovery available after a lost first response and repeated detail failures',async()=>{
 const fetcher=setup();await screen.findByText('Saved answer');cleanup();let canRecover=false;
 vi.stubGlobal('fetch',vi.fn(async(url:string,options?:RequestInit)=>{
  if(url==='/api/chat/conversations'&&options?.method==='POST')return {ok:true,status:200,json:async()=>({...conversation,id:'created',config:null,last_ordinal:0})};
  if(url.endsWith('/created/turns'))throw new Error('Lost first response');
  if(url.endsWith('/created')){if(!canRecover)throw new Error('Detail unavailable');return {ok:true,status:200,json:async()=>({conversation:{...conversation,id:'created'},turns:[{id:'finished',ordinal:1,question:'Keep first question',status:'completed',config,answer:{...answer,answer:'Recovered first answer'}}],next_before_ordinal:null})};}
  return fetcher(url,options);
 }));render(<MemoryRouter><MeetingSearchChat/></MemoryRouter>);fireEvent.click(screen.getByRole('button',{name:'Cấu hình'}));await screen.findByRole('option',{name:'model'});fireEvent.change(screen.getByLabelText('Câu hỏi'),{target:{value:'Keep first question'}});fireEvent.click(screen.getByRole('button',{name:'Gửi'}));await screen.findByText('Detail unavailable');
 expect((screen.getByLabelText('Câu hỏi') as HTMLTextAreaElement).value).toBe('Keep first question');expect((screen.getByRole('button',{name:'Gửi'}) as HTMLButtonElement).disabled).toBe(true);
 canRecover=true;fireEvent.click(screen.getByRole('button',{name:'Tải lại hội thoại'}));await screen.findByText('Recovered first answer');expect((screen.getByLabelText('Câu hỏi') as HTMLTextAreaElement).value).toBe('');expect((screen.getByLabelText('Model trả lời') as HTMLSelectElement).value).toBe('model');expect((screen.getByRole('button',{name:'Gửi'}) as HTMLButtonElement).disabled).toBe(false);
});
