// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom';
import { ProjectPage } from './ProjectPage';
afterEach(()=>{cleanup();vi.unstubAllGlobals();});
function deferred<T>() {let resolve!:(value:T)=>void;const promise=new Promise<T>(accept=>{resolve=accept;});return {promise,resolve};}
const response=(body:unknown)=>({ok:true,status:200,json:async()=>body});
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
