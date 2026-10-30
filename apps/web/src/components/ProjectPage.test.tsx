// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom';
import { ProjectPage } from './ProjectPage';
afterEach(()=>{cleanup();vi.unstubAllGlobals();});
function deferred<T>() {let resolve!:(value:T)=>void;const promise=new Promise<T>(accept=>{resolve=accept;});return {promise,resolve};}
const response=(body:unknown)=>({ok:true,status:200,json:async()=>body});
it.each(['system_admin','project_manager','member','viewer'])('shows cancelled meetings only to system admin (role=%s)',async(role)=>{
 vi.stubGlobal('fetch',vi.fn(async(url:string)=>response(
  url.includes('/meetings')?{items:[{id:'active',title:'Active meeting',status:'completed'},{id:'cancelled',title:'Cancelled meeting',status:'cancelled'}]}:
  url.endsWith('/members')?[]:{id:'p',name:'Project',role,is_archived:false}
 )));
 render(<MemoryRouter initialEntries={['/projects/p']}><Routes><Route path="/projects/:id" element={<ProjectPage/>}/></Routes></MemoryRouter>);
 await screen.findByRole('link',{name:'Active meeting'});
 expect(!!screen.queryByRole('link',{name:'Cancelled meeting'})).toBe(role==='system_admin');
});
it('shows the empty state when all meetings are cancelled for a non-admin',async()=>{
 vi.stubGlobal('fetch',vi.fn(async(url:string)=>response(url.includes('/meetings')?{items:[{id:'cancelled',title:'Cancelled meeting',status:'cancelled'}]}:{id:'p',name:'Project',role:'viewer',is_archived:false})));
 render(<MemoryRouter initialEntries={['/projects/p']}><Routes><Route path="/projects/:id" element={<ProjectPage/>}/></Routes></MemoryRouter>);
 await screen.findByText('Chưa có cuộc họp trong dự án.');
 expect(screen.queryByRole('link',{name:'Cancelled meeting'})).toBeNull();
});
it.each([false,true])('does not report empty meetings before a successful response (failure=%s)',async(isFailure)=>{
 const pending=deferred<ReturnType<typeof response>>();
 vi.stubGlobal('fetch',vi.fn((url:string)=>url.includes('/meetings')?pending.promise:Promise.resolve(response({id:'p',name:'Project',role:'viewer',is_archived:false}))));
 render(<MemoryRouter initialEntries={['/projects/p']}><Routes><Route path="/projects/:id" element={<ProjectPage/>}/></Routes></MemoryRouter>);
 await screen.findByRole('heading',{name:'Project'});
 expect(screen.queryByText('Chưa có cuộc họp trong dự án.')).toBeNull();
 expect(screen.getByRole('status').textContent).toBe('Đang tải cuộc họp…');
 await act(async()=>pending.resolve(isFailure?{...response({detail:'Không tải được cuộc họp.'}),ok:false,status:500}:response({items:[]})));
 if(isFailure){expect(screen.getByRole('alert').textContent).toBe('Không tải được cuộc họp.');expect(screen.queryByText('Chưa có cuộc họp trong dự án.')).toBeNull();}
 else expect(screen.getByText('Chưa có cuộc họp trong dự án.')).toBeTruthy();
 expect(screen.queryByRole('status')).toBeNull();
});
it('discards delayed project, meeting and membership responses across route changes',async()=>{
 const oldMembers=deferred<ReturnType<typeof response>>();
 const oldMeetings=deferred<ReturnType<typeof response>>();
 const lateProject=deferred<ReturnType<typeof response>>();
 const signals:AbortSignal[]=[];
 vi.stubGlobal('fetch',vi.fn((url:string,options:RequestInit)=>{
  if(url==='/api/projects/a/members'){signals.push(options.signal as AbortSignal);return oldMembers.promise;}
  if(url.includes('project_id=a'))return oldMeetings.promise;
  if(url==='/api/projects/slow')return lateProject.promise;
  if(url.includes('/meetings'))return Promise.resolve(response({items:[]}));
  if(url.endsWith('/members'))return Promise.resolve(response([]));
  return Promise.resolve(response({id:url.split('/').pop(),name:url.endsWith('/a')?'Project A':'Project B',role:'project_manager',is_archived:false}));
 }));
 render(<MemoryRouter initialEntries={['/projects/a']}><Link to="/projects/b">Open B</Link><Link to="/projects/slow">Open slow</Link><Routes><Route path="/projects/:id" element={<ProjectPage/>}/></Routes></MemoryRouter>);
 await screen.findByText('Project A');
 expect(screen.getByRole('note').textContent).toContain('Email ownership is unverified');
 expect(screen.getByRole('note').textContent).toContain('Verify identity outside this app');
 fireEvent.click(screen.getByRole('link',{name:'Open B'}));
 await screen.findByText('Project B');
 await act(async()=>{oldMembers.resolve(response([{account_id:'old-account',project_id:'a',role:'member'}]));oldMeetings.resolve(response({items:[{id:'old-meeting',title:'Old meeting'}]}));});
 expect(screen.queryByText(/old-account/)).toBeNull();expect(screen.queryByText('Old meeting')).toBeNull();expect(signals[0].aborted).toBe(true);
 fireEvent.click(screen.getByRole('link',{name:'Open slow'}));
 fireEvent.click(screen.getByRole('link',{name:'Open B'}));
 await screen.findByText('Project B');
 await act(async()=>{lateProject.resolve(response({id:'slow',name:'Late project',role:'system_admin',is_archived:false}));});
 expect(screen.queryByText('Late project')).toBeNull();expect(screen.queryByRole('button',{name:'Lưu trữ dự án'})).toBeNull();
});
