import { useEffect, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { useAuth } from '../lib/AuthProvider';
import { projectApi, type Project } from '../lib/projectApi';
export function ProjectsPage() {
 const {account}=useAuth(); const [projects,setProjects]=useState<Project[]>();const [error,setError]=useState('');
 useEffect(()=>{void projectApi.list().then(setProjects).catch(cause=>setError(String(cause.message)));},[]);
 async function create(event:FormEvent<HTMLFormElement>) {event.preventDefault();if(!account?.is_system_admin)return;const form=event.currentTarget;try{const project=await projectApi.create(String(new FormData(form).get('name')));setProjects(current=>[...(current??[]),project]);form.reset();}catch(cause){setError((cause as Error).message);}}
 return <main className="page"><section className="title"><h2>Dự án</h2></section>{error&&<p role="alert">{error}</p>}{account?.is_system_admin&&<form onSubmit={event=>void create(event)}><label>Tên dự án<input name="name" required maxLength={200}/></label><button className="primary">Tạo dự án</button></form>}{!projects?<p role="status">Đang tải dự án…</p>:projects.length?<ul className="library">{projects.map(project=><li key={project.id}><Link to={`/projects/${project.id}`}>{project.name}</Link> · {project.role}{project.is_archived&&<span className="status">Đã lưu trữ</span>}</li>)}</ul>:<p>Bạn chưa được thêm vào dự án nào.</p>}</main>;
}
