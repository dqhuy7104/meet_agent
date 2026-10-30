import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronRight, LogOut, Users } from "lucide-react";
import { useAuth } from "../lib/AuthProvider";
import { projectApi, type Project } from "../lib/projectApi";
import { roleLabels } from "../lib/roleLabels";
import { SystemPromptSettings } from "./SystemPromptSettings";

function ProjectAccess() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let isActive = true;
    setProjects(null);
    setError("");
    void projectApi
      .list()
      .then((items) => {
        if (isActive) setProjects(items);
      })
      .catch((cause) => {
        if (isActive) setError((cause as Error).message);
      });
    return () => {
      isActive = false;
    };
  }, [attempt]);

  return (
    <section
      className="access-panel settings-projects"
      aria-labelledby="settings-projects-title"
    >
      <h2 id="settings-projects-title">Dự án của bạn</h2>
      {error ? (
        <div>
          <p className="access-error" role="alert">
            {error}
          </p>
          <button onClick={() => setAttempt((value) => value + 1)}>
            Thử lại
          </button>
        </div>
      ) : projects === null ? (
        <p className="access-state" role="status">
          Đang tải dự án…
        </p>
      ) : projects.length ? (
        <ul className="access-list access-projects">
          {projects.map((project) => (
            <li key={project.id}>
              <Link
                className="access-project-link"
                to={`/projects/${encodeURIComponent(project.id)}`}
              >
                <div>
                  <b>{project.name}</b>
                  <span className="settings-project-role">
                    Vai trò: {roleLabels[project.role]}
                  </span>
                </div>
                {project.is_archived && (
                  <span className="access-badge">Đã lưu trữ</span>
                )}
                <ChevronRight size={18} aria-hidden="true" />
              </Link>
            </li>
          ))}
        </ul>
      ) : (
        <p className="access-state">Bạn chưa được thêm vào dự án nào.</p>
      )}
    </section>
  );
}

export function SettingsPage() {
  const { account, logout } = useAuth();
  const [isLoggingOut, setIsLoggingOut] = useState(false);
  const [error, setError] = useState("");

  async function signOut() {
    setIsLoggingOut(true);
    setError("");
    try {
      await logout();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setIsLoggingOut(false);
    }
  }

  return (
    <div className="page access-page settings-page">
      <section className="title">
        <h1>Cài đặt workspace</h1>
      </section>
      <section
        className="access-panel"
        aria-labelledby="account-settings-title"
      >
        <h2 id="account-settings-title">Tài khoản</h2>
        <p className="access-note settings-email">{account?.email}</p>
        <div className="settings-actions">
          {account?.is_system_admin && (
            <Link className="settings-account-link" to="/settings/accounts">
              <Users size={18} aria-hidden="true" />
              <span>Quản lý tài khoản</span>
              <ChevronRight size={18} aria-hidden="true" />
            </Link>
          )}
          <button
            className="settings-logout"
            disabled={isLoggingOut}
            onClick={() => void signOut()}
          >
            <LogOut size={18} aria-hidden="true" />
            {isLoggingOut ? "Đang đăng xuất…" : "Đăng xuất"}
          </button>
        </div>
        {error && (
          <p className="access-error" role="alert">
            {error}
          </p>
        )}
      </section>
      {account?.is_system_admin && <SystemPromptSettings key={`prompts-${account.id}`} />}
      {account && <ProjectAccess key={account.id} />}
    </div>
  );
}
