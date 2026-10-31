// @vitest-environment jsdom
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import App from '../App';
import {MeetingSearchChat} from './MeetingSearchChat';
import {AuthProvider} from '../lib/AuthProvider';
beforeEach(()=>{HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');this.dispatchEvent(new Event('close'));};});
afterEach(()=>{cleanup();vi.unstubAllGlobals();});
const account={id:'me',email:'me@example.test',is_system_admin:false,is_disabled:false};
const project={id:'p',name:'Visible project',role:'member',is_archived:false};
function backend(meetingSource='upload',archived=false){
 return vi.fn(async(url:string,options?:RequestInit)=>{
 let body:unknown={};let status=200;
 if(url==='/api/auth/session')body={account,csrf_token:'token'};
 else if(url==='/api/projects')body=[project];
 else if(url==='/api/projects/p')body={...project,is_archived:archived};
 else if(url.includes('/api/meetings?'))body={items:[],total:0};
 else if(url==='/api/meetings/private'){body={detail:'Meeting not found'};status=404;}
 else if(url==='/api/meetings/m')body={id:'m',project_id:'p',uploaded_by_account_id:'me',source:meetingSource,title:'Meeting title',status:'completed',audio_files:[],transcript_segments:[],diarization_segments:[],speaker_identities:[]};
 else if(url==='/api/bot-sessions/active'||url==='/api/bot-sessions/recent')return {ok:true,status:204};
 else if(url==='/api/voice-profiles')body=[];
 else if(url.startsWith('/api/chat/providers'))body={max_k:5,providers:[{id:'local',name:'Local',is_external:false,chat_models:['model']}],embedding_profiles:[{id:'embed',provider_id:'local'}]};
 else if(url.startsWith('/api/chat/index-status'))body={profiles:[{profile_id:'embed',meetings:[{meeting_id:'m',title:'Pending meeting',state:'pending',attempts:0,failure_code:null}]}]};
 else if(url==='/api/chat/conversations'&&options?.method==='POST')body={id:'c',title:'Hội thoại mới',project_id:JSON.parse(options.body as string).project_id,last_ordinal:0,config:null,read_only:false};
 else if(url.startsWith('/api/chat/conversations?'))body={items:[],next_cursor:null};
 else if(url==='/api/chat/conversations/c/turns')body={id:'turn',ordinal:1,status:'completed',question:'What happened?',answer:{answer:'Answer',retrieval_status:'answered',selected_meetings:[],citations:[]}};
 else if(url==='/api/chat/conversations/c')body={conversation:{id:'c',title:'Saved',project_id:'p',last_ordinal:1,config:{k:3,chat_provider_id:'local',chat_model_id:'model',embedding_profile_id:'embed',context_weight:.8},read_only:false},turns:[{id:'turn',ordinal:1,status:'completed',question:'What happened?',answer:{answer:'Answer',retrieval_status:'answered',selected_meetings:[],citations:[]}}],next_before_ordinal:null};
 return {ok:status<400,status,json:async()=>body};
 });
}
it.each([['upload',true],['google_meet_bot',false]])('member delete visibility for %s',async(source,canDelete)=>{
 vi.stubGlobal('fetch',backend(source));render(<MemoryRouter initialEntries={['/workspace/m']}><App/></MemoryRouter>);await screen.findByText('Meeting title');await waitFor(()=>expect(!!screen.queryByRole('button',{name:'Xóa'})).toBe(canDelete));
});
it('deletes a completed meeting after confirmation and returns to the project library',async()=>{
 const base=backend();
 const fetcher=vi.fn(async(url:string,options?:RequestInit)=>{
  if(url==='/api/meetings/m'&&options?.method==='DELETE')return {ok:true,status:204};
  return base(url,options);
 });
 vi.stubGlobal('fetch',fetcher);
 vi.spyOn(window,'confirm').mockReturnValueOnce(true);
 render(<MemoryRouter initialEntries={['/workspace/m']}><App/></MemoryRouter>);
 await screen.findByText('Meeting title');
 fireEvent.click(await screen.findByRole('button',{name:'Xóa'}));
 await screen.findByRole('link',{name:/Visible project/});
 const deletion=fetcher.mock.calls.find(([url,options])=>url==='/api/meetings/m'&&options?.method==='DELETE');
 expect(deletion).toBeTruthy();
 expect(new Headers(deletion?.[1]?.headers).get('X-CSRF-Token')).toBe('token');
 vi.mocked(window.confirm).mockRestore();
});
it('does not expose create controls on archived projects',async()=>{vi.stubGlobal('fetch',backend('upload',true));render(<MemoryRouter initialEntries={['/projects/p']}><App/></MemoryRouter>);await screen.findByText('Visible project');expect(screen.queryByRole('button',{name:'Tải audio'})).toBeNull();expect(screen.queryByRole('button',{name:'Thêm Bot'})).toBeNull();});
it('handles inaccessible direct meeting links safely',async()=>{vi.stubGlobal('fetch',backend());render(<MemoryRouter initialEntries={['/workspace/private']}><App/></MemoryRouter>);await screen.findByText('Meeting not found');expect(screen.queryByRole('audio')).toBeNull();expect(screen.queryByText('Transcript')).toBeNull();});
it('defaults chat to all visible projects and sends a selected project',async()=>{
 const fetcher=backend();vi.stubGlobal('fetch',fetcher);render(<MemoryRouter><AuthProvider><MeetingSearchChat/></AuthProvider></MemoryRouter>);fireEvent.click(await screen.findByRole('button',{name:'Cấu hình'}));const selector=await screen.findByLabelText('Dự án');await screen.findByText('Visible project');expect((selector as HTMLSelectElement).value).toBe('');expect(screen.getAllByRole('option').some(option=>option.textContent==='Invisible project')).toBe(false);
 fireEvent.change(selector,{target:{value:'p'}});fireEvent.change(screen.getByLabelText('Câu hỏi'),{target:{value:'What happened?'}});
 await waitFor(()=>expect((screen.getByRole('button',{name:'Gửi'}) as HTMLButtonElement).disabled).toBe(false));
 fireEvent.click(screen.getByRole('button',{name:'Gửi'}));await screen.findByText('Answer');const call=fetcher.mock.calls.find(([url])=>url==='/api/chat/conversations');expect(JSON.parse(call?.[1]?.body as string).project_id).toBe('p');
 expect(fetcher.mock.calls.some(([url])=>url==='/api/chat/providers?project_id=p')).toBe(true);
 expect(fetcher.mock.calls.some(([url])=>url==='/api/chat/index-status?project_id=p')).toBe(true);
 expect(screen.getByRole('link',{name:'Pending meeting'})).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'Làm mới trạng thái'}));
 await waitFor(()=>expect(fetcher.mock.calls.filter(([url])=>url==='/api/chat/index-status?project_id=p').length).toBeGreaterThanOrEqual(2));
});
it('submits uploads and bot creation in the selected active project',async()=>{
 const base=backend();const fetcher=vi.fn(async(url:string,options?:RequestInit)=>{if(url==='/api/meetings'||url==='/api/bot-sessions')return {ok:true,status:200,json:async()=>({meeting_id:'m',id:'bot',status:'queued'})};return base(url,options);});vi.stubGlobal('fetch',fetcher);render(<MemoryRouter initialEntries={['/projects/p']}><App/></MemoryRouter>);await screen.findByText('Visible project');
 fireEvent.change(screen.getByLabelText('Audio'),{target:{files:[new File(['audio'],'recording.wav',{type:'audio/wav'})]}});fireEvent.submit(screen.getByRole('button',{name:'Tải audio'}).closest('form')!);await waitFor(()=>expect(fetcher.mock.calls.some(([url])=>url==='/api/meetings')).toBe(true));const upload=fetcher.mock.calls.find(([url])=>url==='/api/meetings');expect((upload?.[1]?.body as FormData).get('project_id')).toBe('p');
 fireEvent.change(screen.getByLabelText('Link Google Meet'),{target:{value:'https://meet.google.com/abc-defg-hij'}});fireEvent.click(screen.getByRole('button',{name:'Thêm Bot'}));await waitFor(()=>expect(fetcher.mock.calls.some(([url])=>url==='/api/bot-sessions')).toBe(true));const bot=fetcher.mock.calls.find(([url])=>url==='/api/bot-sessions');expect(JSON.parse(bot?.[1]?.body as string).project_id).toBe('p');expect(new Headers(bot?.[1]?.headers).get('X-CSRF-Token')).toBe('token');
});
it('warns administrators to verify identity before granting account access',async()=>{
 const base=backend();vi.stubGlobal('fetch',vi.fn(async(url:string,options?:RequestInit)=>{
  if(url==='/api/auth/session')return {ok:true,status:200,json:async()=>({account:{...account,is_system_admin:true},csrf_token:'token'})};
  if(url==='/api/admin/accounts')return {ok:true,status:200,json:async()=>[account]};
  return base(url,options);
 }));render(<MemoryRouter initialEntries={['/accounts']}><App/></MemoryRouter>);
 expect((await screen.findByRole('note')).textContent).toContain('Email ownership is unverified');
 expect(screen.getByRole('note').textContent).toContain('Verify identity outside this app');
 expect(await screen.findByRole('checkbox',{name:'Admin'})).toBeTruthy();
});

it('shows friendly role names in the project library',async()=>{
 const base=backend();vi.stubGlobal('fetch',vi.fn(async(url:string,options?:RequestInit)=>{
  if(url==='/api/projects')return {ok:true,status:200,json:async()=>[
   {...project,id:'admin-project',name:'Admin project',role:'system_admin'},
   {...project,id:'managed-project',name:'Managed project',role:'project_manager'},
  ]};
  return base(url,options);
 }));render(<MemoryRouter initialEntries={['/projects']}><App/></MemoryRouter>);
 expect(await screen.findByRole('link',{name:/Admin project.*Vai trò: Admin/})).toBeTruthy();
 expect(screen.getByRole('link',{name:/Managed project.*Vai trò: Project Manager/})).toBeTruthy();
});

it('labels project managers while preserving the membership API role',async()=>{
 const base=backend();const fetcher=vi.fn(async(url:string,options?:RequestInit)=>{
  if(url==='/api/projects/p')return {ok:true,status:200,json:async()=>({...project,role:'system_admin'})};
  if(url==='/api/projects/p/members')return {ok:true,status:200,json:async()=>[{account_id:'manager-account',project_id:'p',role:'project_manager'}]};
  if(url.startsWith('/api/projects/p/accounts/lookup'))return {ok:true,status:200,json:async()=>({...account,id:'new-manager'})};
  return base(url,options);
 });vi.stubGlobal('fetch',fetcher);render(<MemoryRouter initialEntries={['/projects/p']}><App/></MemoryRouter>);
 await screen.findByText('Vai trò: Admin');
 await screen.findByText('Project Manager',{selector:'small'});
 expect((screen.getByRole('option',{name:'Project Manager'}) as HTMLOptionElement).value).toBe('project_manager');
 fireEvent.change(screen.getByLabelText('Email tài khoản'),{target:{value:'manager@example.test'}});
 fireEvent.change(screen.getByLabelText('Vai trò'),{target:{value:'project_manager'}});
 fireEvent.submit(screen.getByRole('button',{name:'Thêm thành viên'}).closest('form')!);
 await waitFor(()=>expect(fetcher.mock.calls.some(([url])=>url==='/api/projects/p/members/new-manager')).toBe(true));
 const call=fetcher.mock.calls.find(([url])=>url==='/api/projects/p/members/new-manager');
 expect(JSON.parse(call?.[1]?.body as string).role).toBe('project_manager');
});
