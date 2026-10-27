import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError } from "../lib/api";
import { botApi, type BotSession, type BotStatus } from "../lib/botApi";

const activeStatuses: BotStatus[] = ["queued", "joining", "waiting_admission", "recording", "finalizing"];
const labels: Record<BotStatus, string> = {
  queued: "Đang chờ bot", joining: "Bot đang vào Meet",
  waiting_admission: "Đang chờ host duyệt", recording: "Đang ghi âm",
  finalizing: "Đang chốt bản ghi", completed: "Đã tạo cuộc họp",
  interrupted: "Ghi âm bị gián đoạn", cancelled: "Đã hủy",
};

export function MeetBotPanel({projectId}: {projectId?: string}) {
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [session, setSession] = useState<BotSession | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const navigate = useNavigate();

  useEffect(() => {
    const controller = new AbortController();
    void botApi.active(controller.signal).then(async (active) => {
      if (active) return active;
      return botApi.recent(controller.signal);
    }).then((found) => {
      if (!controller.signal.aborted && found && (!projectId || found.project_id === projectId)) setSession(found);
    }).catch((cause) => {
      if (!controller.signal.aborted) setError(cause instanceof ApiError ? cause.message : "Không tải được phiên bot local.");
    });
    return () => controller.abort();
  }, [projectId]);

  useEffect(() => {
    if (!session || !activeStatuses.includes(session.status)) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      void botApi.get(session.id, controller.signal).then(setSession).catch((cause) => {
        if (!controller.signal.aborted) setError(cause instanceof ApiError ? cause.message : "Không cập nhật được trạng thái bot.");
      });
    }, 2000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [session?.id, session?.status]);

  useEffect(() => {
    if (session?.status === "completed" && session.meeting_id) {
      navigate(`/workspace/${session.meeting_id}`);
    }
  }, [navigate, session?.status, session?.meeting_id]);

  const start = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      setSession(await botApi.create(url, title, projectId));
      setUrl("");
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Không kết nối được API local.");
    } finally {
      setBusy(false);
    }
  };

  const action = async (kind: "stop" | "process" | "discard") => {
    if (!session) return;
    setBusy(true);
    setError("");
    try {
      if (kind === "stop") setSession(await botApi.stop(session.id));
      if (kind === "process") setSession(await botApi.processParts(session.id));
      if (kind === "discard") {
        await botApi.discard(session.id);
        setSession(null);
      }
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Không thể cập nhật phiên bot.");
    } finally {
      setBusy(false);
    }
  };

  const isActive = session && activeStatuses.includes(session.status);
  return (
    <section className="panel meet-bot" aria-labelledby="meet-bot-title">
      <div className="panel-head">
        <div><h3 id="meet-bot-title">Bot Google Meet</h3><p>Bot tham gia như một người dự họp và ghi audio trên máy này.</p></div>
      </div>
      {!isActive && (
        <form onSubmit={(event) => void start(event)}>
          <label>Link Google Meet
            <input type="url" required value={url} onChange={(event) => setUrl(event.target.value)}
              placeholder="https://meet.google.com/abc-defg-hij" />
          </label>
          <label>Tên cuộc họp (tùy chọn)
            <input maxLength={200} value={title} onChange={(event) => setTitle(event.target.value)} />
          </label>
          <p>Hãy thông báo cho người tham gia rằng bot sẽ ghi âm. Host có thể cần duyệt bot vào phòng.</p>
          <button className="primary" type="submit" disabled={busy}>{busy ? "Đang gửi..." : "Thêm Bot"}</button>
        </form>
      )}
      {session && (
        <div className="bot-state" role="status">
          <strong>{labels[session.status]}</strong>
          <span>{session.finalized_part_count} phần audio đã chốt</span>
          {session.started_at && <span>Đã chạy: {Math.max(0, Math.floor((Date.now() - Date.parse(session.started_at)) / 60000))} phút</span>}
          {session.stop_requested && <span>Đã yêu cầu dừng bot.</span>}
          {session.failure_code && <span>Mã lỗi: {session.failure_code}</span>}
          {session.failure_code === "meeting_access_denied" &&
            <span>Google Meet đã từ chối bot. Kiểm tra quyền truy cập cuộc họp và tài khoản Google trong hồ sơ Chrome của bot.</span>}
          {session.status === "queued" && Date.now() - Date.parse(session.created_at) > 10000 &&
            <span>Bot chưa nhận yêu cầu. Hãy kiểm tra tiến trình bot runner đang chạy, sau đó kiểm tra Chrome và pactl trên máy chạy bot.</span>}
          <div className="bot-actions">
            {isActive && session.status !== "finalizing" && <button type="button" disabled={busy || session.stop_requested}
              onClick={() => void action("stop")}>Dừng bot</button>}
            {session.status === "interrupted" && session.finalized_part_count > 0 && <button className="primary" type="button"
              disabled={busy} onClick={() => void action("process")}>Xử lý phần đã ghi</button>}
            {session.status === "interrupted" && <button type="button" disabled={busy}
              onClick={() => void action("discard")}>Xóa bản ghi</button>}
          </div>
        </div>
      )}
      {error && <p className="message" role="alert">{error}</p>}
    </section>
  );
}
