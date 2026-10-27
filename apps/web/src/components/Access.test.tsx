// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from '../lib/AuthProvider';
import { LoginPage } from './LoginPage';
import { ProjectsPage } from './ProjectsPage';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const account = {id:'a',email:'user@example.com',is_system_admin:false,is_disabled:false};
function mount(admin = false) {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ok:true,status:200,json:async()=>url.includes('/session') ? {account:{...account,is_system_admin:admin},csrf_token:'csrf'} : []})));
  render(<MemoryRouter><AuthProvider><ProjectsPage /></AuthProvider></MemoryRouter>);
}
it('offers login and registration to guests', () => { render(<AuthProvider><LoginPage /></AuthProvider>); expect(screen.getByRole('button',{name:'Đăng nhập'})).toBeTruthy(); expect(screen.getByRole('button',{name:'Đăng ký'})).toBeTruthy(); });
it('shows unassigned users an empty project state', async () => { mount(); expect(await screen.findByText('Bạn chưa được thêm vào dự án nào.')).toBeTruthy(); expect(screen.queryByRole('button',{name:'Tạo dự án'})).toBeNull(); });
it('lets administrators create projects', async () => { mount(true); expect(await screen.findByRole('button',{name:'Tạo dự án'})).toBeTruthy(); });
it('expires an authenticated session back to login',async()=>{
 vi.stubGlobal('fetch',vi.fn(async()=>({ok:true,status:200,json:async()=>({account,csrf_token:'csrf'})})));
 const {useAuth}=await import('../lib/AuthProvider');
 function Gate(){const {account:current}=useAuth();return current?<p>Signed in</p>:<LoginPage/>;}
 render(<AuthProvider><Gate/></AuthProvider>);await screen.findByText('Signed in');
 const {act}=await import('@testing-library/react');act(()=>window.dispatchEvent(new Event('session-expired')));
 expect(screen.getByRole('button',{name:'Đăng nhập'})).toBeTruthy();
});
it.each(['project_manager','member','viewer'] as const)('shows controls for %s from server role',async(role)=>{
 vi.stubGlobal('fetch',vi.fn(async(url:string)=>({ok:true,status:200,json:async()=>url.includes('/session')?{account,csrf_token:'csrf'}:url.includes('/members')?[]:url.includes('/meetings')?{items:[]}: {id:'p',name:'Research',is_archived:false,role}})));
 const {ProjectPage}=await import('./ProjectPage');const {Routes,Route}=await import('react-router-dom');
 render(<MemoryRouter initialEntries={['/projects/p']}><AuthProvider><Routes><Route path="/projects/:id" element={<ProjectPage/>}/></Routes></AuthProvider></MemoryRouter>);
 await screen.findByText('Research');
 expect(!!screen.queryByRole('button',{name:'Thêm thành viên'})).toBe(role==='project_manager');
 expect(!!screen.queryByRole('button',{name:'Tải audio'})).toBe(role!=='viewer');
 expect(screen.queryByRole('option',{name:'Manager'})).toBeNull();
 expect(screen.queryByText('System Admin')).toBeNull();
});
