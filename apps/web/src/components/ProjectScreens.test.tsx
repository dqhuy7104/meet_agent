// @vitest-environment jsdom
import {afterEach,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import App from '../App';
import {MeetingSearchChat} from './MeetingSearchChat';
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
 else if(url==='/api/chat/providers')body={max_k:5,providers:[{id:'local',name:'Local',is_external:false,chat_models:['model']}],embedding_profiles:[{id:'embed',provider_id:'local'}]};
 else if(url==='/api/chat/queries')body={answer:'Answer',retrieval_status:'answered',selected_meetings:[],citations:[]};
 return {ok:status<400,status,json:async()=>body};
 });
}
it.each([['upload',true],['google_meet_bot',false]])('member delete visibility for %s',async(source,canDelete)=>{
 vi.stubGlobal('fetch',backend(source));render(<MemoryRouter initialEntries={['/workspace/m']}><App/></MemoryRouter>);await screen.findByText('Meeting title');await waitFor(()=>expect(!!screen.queryByRole('button',{name:'Xóa'})).toBe(canDelete));
});
it('does not expose create controls on archived projects',async()=>{vi.stubGlobal('fetch',backend('upload',true));render(<MemoryRouter initialEntries={['/projects/p']}><App/></MemoryRouter>);await screen.findByText('Visible project');expect(screen.queryByRole('button',{name:'Tải audio'})).toBeNull();expect(screen.queryByRole('button',{name:'Thêm Bot'})).toBeNull();});
it('handles inaccessible direct meeting links safely',async()=>{vi.stubGlobal('fetch',backend());render(<MemoryRouter initialEntries={['/workspace/private']}><App/></MemoryRouter>);await screen.findByText('Meeting not found');expect(screen.queryByRole('audio')).toBeNull();expect(screen.queryByText('Transcript')).toBeNull();});
it('defaults chat to all visible projects and sends a selected project',async()=>{
 const fetcher=backend();vi.stubGlobal('fetch',fetcher);render(<MemoryRouter><MeetingSearchChat/></MemoryRouter>);const selector=await screen.findByLabelText('Dự án');await screen.findByText('Visible project');expect((selector as HTMLSelectElement).value).toBe('');expect(screen.getAllByRole('option').some(option=>option.textContent==='Invisible project')).toBe(false);
 fireEvent.change(selector,{target:{value:'p'}});fireEvent.change(screen.getByLabelText('Câu hỏi'),{target:{value:'What happened?'}});fireEvent.click(screen.getByRole('button',{name:'Tìm câu trả lời'}));await screen.findByText('Answer');const call=fetcher.mock.calls.find(([url])=>url==='/api/chat/queries');expect(JSON.parse(call?.[1]?.body as string).project_id).toBe('p');
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
});
