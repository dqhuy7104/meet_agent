import { request } from './api';
import type { Account } from './authApi';
export type Role = 'system_admin'|'project_manager'|'member'|'viewer';
export type Project = {id:string;name:string;is_archived:boolean;role:Role};
export type Membership = {project_id:string;account_id:string;role:Exclude<Role,'system_admin'>};
const path = (id:string) => `/api/projects/${encodeURIComponent(id)}`;
const json = (method:string,body:unknown):RequestInit => ({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
export const projectApi = {
 list:()=>request<Project[]>('/api/projects'), get:(id:string,signal?:AbortSignal)=>request<Project>(path(id),{signal}),
 create:(name:string)=>request<Project>('/api/projects',json('POST',{name})),
 rename:(id:string,name:string)=>request<Project>(path(id),json('PATCH',{name})),
 archive:(id:string,archived:boolean)=>request<Project>(`${path(id)}/${archived?'archive':'restore'}`,{method:'POST'}),
 members:(id:string,signal?:AbortSignal)=>request<Membership[]>(`${path(id)}/members`,{signal}),
 lookup:(id:string,email:string)=>request<Account>(`${path(id)}/accounts/lookup?email=${encodeURIComponent(email)}`),
 setMember:(id:string,accountId:string,role:Membership['role'])=>request<Membership>(`${path(id)}/members/${encodeURIComponent(accountId)}`,json('PUT',{role})),
 removeMember:(id:string,accountId:string)=>request<void>(`${path(id)}/members/${encodeURIComponent(accountId)}`,{method:'DELETE'}),
 accounts:()=>request<Account[]>('/api/admin/accounts'),
 updateAccount:(id:string,changes:Partial<Pick<Account,'is_system_admin'|'is_disabled'>>)=>request<Account>(`/api/admin/accounts/${encodeURIComponent(id)}`,json('PATCH',changes)),
};
