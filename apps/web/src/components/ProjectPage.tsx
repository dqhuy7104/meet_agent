import { useEffect, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { MeetBotPanel } from "./MeetBotPanel";
import { meetingApi } from "../lib/api";
import { projectApi, type Project, type Membership } from "../lib/projectApi";
import type { ApiMeeting } from "../types/meeting";
import { roleLabels } from "../lib/roleLabels";
export function ProjectPage() {
  const { id = "" } = useParams();
  return <ProjectDetail key={id} id={id} />;
}
function ProjectDetail({ id }: { id: string }) {
  const [project, setProject] = useState<Project>();
  const [meetings, setMeetings] = useState<ApiMeeting[] | null>(null);
  const [members, setMembers] = useState<Membership[]>([]);
  const [error, setError] = useState("");
  const canManage =
    project?.role === "system_admin" || project?.role === "project_manager";
  const canUpload =
    !!project && !project.is_archived && project.role !== "viewer";
  const visibleMeetings = meetings?.filter(
    (meeting) => project?.role === "system_admin" || meeting.status !== "cancelled",
  );
  useEffect(() => {
    const controller = new AbortController();
    let isActive = true;
    setProject(undefined);
    setMeetings(null);
    setMembers([]);
    setError("");
    void projectApi
      .get(id, controller.signal)
      .then((value) => {
        if (!isActive) return;
        setProject(value);
        if (value.role === "system_admin" || value.role === "project_manager")
          void projectApi
            .members(id, controller.signal)
            .then((value) => {
              if (isActive) setMembers(value);
            })
            .catch(fail);
      })
      .catch(fail);
    void meetingApi
      .list(controller.signal, id)
      .then((value) => {
        if (isActive) setMeetings(value.items);
      })
      .catch(fail);
    function fail(cause: Error) {
      if (isActive) setError(cause.message);
    }
    return () => {
      isActive = false;
      controller.abort();
    };
  }, [id]);
  async function act(action: () => Promise<unknown>) {
    setError("");
    try {
      await action();
    } catch (cause) {
      setError((cause as Error).message);
    }
  }
  async function addMember(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canManage) return;
    const data = new FormData(event.currentTarget);
    await act(async () => {
      const target = await projectApi.lookup(id, String(data.get("email")));
      await projectApi.setMember(
        id,
        target.id,
        String(data.get("role")) as Membership["role"],
      );
      setMembers(await projectApi.members(id));
    });
  }
  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canUpload) return;
    const data = new FormData(event.currentTarget);
    await act(async () => {
      await meetingApi.upload(
        data.getAll("files") as File[],
        String(data.get("title")),
        id,
      );
      setMeetings((await meetingApi.list(undefined, id)).items);
    });
  }
  if (!project)
    return (
      <div className="page access-page">
        {error ? (
          <p role="alert" className="access-error">
            {error}
          </p>
        ) : (
          <p role="status">Đang tải dự án…</p>
        )}
      </div>
    );
  return (
    <div className="page access-page">
      <Link className="access-back" to="/projects">
        ← Dự án
      </Link>
      <section className="title">
        <div>
          <h1>{project.name}</h1>
          <p>
            Vai trò: {roleLabels[project.role]}
            {project.is_archived ? " · Đã lưu trữ" : ""}
          </p>
        </div>
      </section>
      {error && (
        <p role="alert" className="access-error">
          {error}
        </p>
      )}
      <div
        className={`access-detail ${canManage ? "access-detail-managed" : ""}`}
      >
        <div className="access-stack">
          <section
            className="access-panel"
            aria-labelledby="project-meetings-title"
          >
            <h2 id="project-meetings-title">Cuộc họp</h2>
            <ul className="access-list">
              {visibleMeetings?.map((meeting) => (
                <li key={meeting.id}>
                  <Link
                    className="access-meeting-link"
                    to={`/workspace/${meeting.id}`}
                  >
                    {meeting.title ?? meeting.original_filename}
                    <span aria-hidden="true">↗</span>
                  </Link>
                </li>
              ))}
            </ul>
            {meetings === null && !error && (
              <p role="status" className="access-state">Đang tải cuộc họp…</p>
            )}
            {visibleMeetings?.length === 0 && (
              <p className="access-state">Chưa có cuộc họp trong dự án.</p>
            )}
          </section>
          {canUpload && (
            <section
              className="access-panel"
              aria-labelledby="project-upload-title"
            >
              <h2 id="project-upload-title">Tải bản ghi</h2>
              <form
                className="access-form access-upload"
                onSubmit={(event) => void upload(event)}
              >
                <label>
                  Tiêu đề
                  <input name="title" />
                </label>
                <label>
                  Audio
                  <input
                    name="files"
                    type="file"
                    accept="audio/*"
                    multiple
                    required
                  />
                </label>
                <button className="primary">Tải audio</button>
              </form>
            </section>
          )}
          {canUpload && <MeetBotPanel key={id} projectId={id} />}
        </div>
        {canManage && (
          <div className="access-stack">
            <section
              className="access-panel"
              aria-labelledby="project-members-title"
            >
              <h2 id="project-members-title">Thành viên</h2>
              <p className="access-note" role="note">
                Email ownership is unverified. Verify identity outside this app
                before granting access.
              </p>
              <form
                className="access-form access-member-form"
                onSubmit={(event) => void addMember(event)}
              >
                <label>
                  Email tài khoản
                  <input type="email" name="email" required />
                </label>
                <label>
                  Vai trò
                  <select name="role">
                    <option value="member">Member</option>
                    <option value="viewer">Viewer</option>
                    {project.role === "system_admin" && (
                      <option value="project_manager">{roleLabels.project_manager}</option>
                    )}
                  </select>
                </label>
                <button>Thêm thành viên</button>
              </form>
              <ul className="access-list access-members">
                {members.map((member) => (
                  <li key={member.account_id}>
                    <div className="access-identity">
                      <b>{member.account_id}</b>
                      <small>{roleLabels[member.role]}</small>
                    </div>
                    {(project.role === "system_admin" ||
                      member.role !== "project_manager") && (
                      <button
                        className="access-remove"
                        aria-label={`Xóa thành viên ${member.account_id}`}
                        onClick={() =>
                          void act(async () => {
                            await projectApi.removeMember(
                              id,
                              member.account_id,
                            );
                            setMembers(await projectApi.members(id));
                          })
                        }
                      >
                        Xóa thành viên
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            </section>
            {project.role === "system_admin" && (
              <section
                className="access-panel"
                aria-labelledby="project-settings-title"
              >
                <h2 id="project-settings-title">Cài đặt dự án</h2>
                <form
                  className="access-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    const name = String(
                      new FormData(event.currentTarget).get("name"),
                    );
                    void act(async () =>
                      setProject(await projectApi.rename(id, name)),
                    );
                  }}
                >
                  <label>
                    Tên dự án
                    <input
                      name="name"
                      defaultValue={project.name}
                      required
                      maxLength={200}
                    />
                  </label>
                  <button>Đổi tên</button>
                </form>
                <div className="access-archive">
                  <button
                    onClick={() =>
                      void act(async () =>
                        setProject(
                          await projectApi.archive(id, !project.is_archived),
                        ),
                      )
                    }
                  >
                    {project.is_archived ? "Khôi phục dự án" : "Lưu trữ dự án"}
                  </button>
                </div>
              </section>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
