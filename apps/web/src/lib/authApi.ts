import { request } from './api';
export type Account = {id:string; email:string; is_system_admin:boolean; is_disabled:boolean};
export type Session = {account:Account; csrf_token:string};
const credentials = (path:string,email:string,password:string) => request<Session>(`/api/auth/${path}`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})});
export const authApi = {
  session: () => request<Session>('/api/auth/session'),
  login: (email:string,password:string) => credentials('login',email,password),
  register: (email:string,password:string) => credentials('register',email,password),
  logout: () => request<void>('/api/auth/logout',{method:'POST'}),
};
