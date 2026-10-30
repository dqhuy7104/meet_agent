import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { ArrowUpRight } from "lucide-react";
import { useAuth } from "../lib/AuthProvider";
import { projectApi, type Project } from "../lib/projectApi";
import { roleLabels } from "../lib/roleLabels";
export function ProjectsPage() {
  const { account } = useAuth();
  const [projects, setProjects] = useState<Project[]>();
  const [error, setError] = useState("");
  useEffect(() => {
    void projectApi
      .list()
      .then(setProjects)
      .catch((cause) => setError(String(cause.message)));
  }, []);
  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!account?.is_system_admin) return;
    const form = event.currentTarget;
    try {
      const project = await projectApi.create(
        String(new FormData(form).get("name")),
      );
      setProjects((current) => [...(current ?? []), project]);
      form.reset();
    } catch (cause) {
      setError((cause as Error).message);
    }
  }
  return (
    <div className="page access-page">
      <section className="title">
        <h1>Dự án</h1>
      </section>
      {error && (
        <p role="alert" className="access-error">
          {error}
        </p>
      )}
      {account?.is_system_admin && (
        <section
          className="access-panel access-create"
          aria-labelledby="create-project-title"
        >
          <h2 id="create-project-title">Tạo dự án</h2>
          <form
            className="access-form"
            onSubmit={(event) => void create(event)}
          >
            <label>
              Tên dự án
              <input name="name" required maxLength={200} />
            </label>
            <button className="primary">Tạo dự án</button>
          </form>
        </section>
      )}
      <section className="access-panel" aria-label="Danh sách dự án">
        {!projects ? (
          <p role="status" className="access-state">
            Đang tải dự án…
          </p>
        ) : projects.length ? (
          <ul className="access-list access-projects">
            {projects.map((project) => (
              <li key={project.id}>
                <Link
                  className="access-project-link"
                  to={`/projects/${project.id}`}
                >
                  <div>
                    <b>{project.name}</b>
                    <small>Vai trò: {roleLabels[project.role]}</small>
                  </div>
                  {project.is_archived && (
                    <span className="access-badge">Đã lưu trữ</span>
                  )}
                  <ArrowUpRight size={18} aria-hidden="true" />
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="access-state">Bạn chưa được thêm vào dự án nào.</p>
        )}
      </section>
    </div>
  );
}
