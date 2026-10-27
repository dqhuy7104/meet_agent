import { AuthProvider, useAuth } from "./lib/AuthProvider";
import { LoginPage } from "./components/LoginPage";
import { ProjectsPage } from "./components/ProjectsPage";
import { ProjectPage } from "./components/ProjectPage";
import { AccountsPage } from "./components/AccountsPage";
import {
  createElement,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ComponentType,
  type ReactNode,
} from "react";
import {
  NavLink,
  Route,
  Routes,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  ChevronLeft,
  Clock3,
  FileAudio,
  LayoutDashboard,
  Menu,
  Moon,
  PanelLeftOpen,
  Plus,
  Search,
  Settings,
  Sparkles,
  Trash2,
  Upload as UploadIcon,
  X,
} from "lucide-react";
import { ApiError, meetingApi, type BackendMeeting } from "./lib/api";
import { VoiceProfilesPage } from "./components/VoiceProfilesPage";
import { MeetingSpeakerIdentities } from "./components/MeetingSpeakerIdentities";
import { speakerDisplay } from "./lib/voiceDisplay";
import { MeetBotPanel } from "./components/MeetBotPanel";
import { MeetingSearchChat } from "./components/MeetingSearchChat";
import { findAudioPartAtTime } from "./lib/timeline";
import { speakerLabels } from "./lib/speakers";
import type { ApiMeeting, MeetingStatus } from "./types/meeting";

const labels: Record<MeetingStatus, string> = {
  completed: "Hoàn tất",
  processing: "Đang xử lý",
  queued: "Đang chờ",
  failed: "Cần thử lại",
  cancelled: "Đã xoá",
};
const time = (seconds: number) =>
  `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
const duration = (seconds: number | null) =>
  seconds === null
    ? "Chưa có thời lượng"
    : seconds < 60
      ? `${Math.round(seconds)} giây`
      : `${Math.floor(seconds / 60)} phút`;
const title = (meeting: ApiMeeting) =>
  meeting.title ?? meeting.original_filename;
const date = (meeting: ApiMeeting) =>
  new Date(meeting.created_at).toLocaleDateString("vi-VN");
function Status({ status }: { status: MeetingStatus }) {
  return (
    <span className={`status ${status}`}>
      <i />
      {labels[status]}
    </span>
  );
}
function Shell({ children }: { children: ReactNode }) {
  const { account, logout } = useAuth();
  const [isSidebarOpen, setIsSidebarOpen] = useState(true);
  const [dark, setDark] = useState(false);
  const navigation = [
    ["/projects", "Dự án", LayoutDashboard],

    ["/chat", "Tìm kiếm thông tin", Search],
    ["/voice-profiles", "Hồ sơ giọng nói", FileAudio],
    ["/settings", "Cài đặt", Settings],
  ] as const;
  return (
    <div
      className={`app ${isSidebarOpen ? "" : "sidebar-hidden"}`}
      data-theme={dark ? "dark" : "light"}
    >
      <aside>
        <div className="brand">
          <b>
            <span>
              Meeting<i> Agent</i>
            </span>
          </b>
          <button
            onClick={() => setIsSidebarOpen(false)}
            aria-label="Ẩn điều hướng"
          >
            <Menu />
          </button>
        </div>
        <nav>
          {account?.is_system_admin && <NavLink to="/accounts">Accounts</NavLink>}
          <button onClick={() => void logout().catch(() => undefined)}>Đăng xuất</button>
          {navigation.map(([to, name, Icon]) => (
            <NavLink key={to} to={to} end={to === "/projects"} aria-label={name} title={name}>
              {createElement(Icon as ComponentType<{ size?: number }>, {
                size: 19,
              })}
              <span>{name}</span>
            </NavLink>
          ))}
        </nav>
        <div className="mode">
          <i /> <span>Thư viện local</span>
        </div>
      </aside>
      <main>
        {!isSidebarOpen && (
          <button
            className="sidebar-open-button"
            onClick={() => setIsSidebarOpen(true)}
            aria-label="Mở điều hướng"
          >
            <PanelLeftOpen />
          </button>
        )}
        <header>
          <div>
            <small>WORKSPACE</small>
          </div>
          <div className="header-actions">
            <button onClick={() => setDark(!dark)} aria-label="Đổi giao diện">
              <Moon />
            </button>
          </div>
        </header>
        {children}
      </main>
    </div>
  );
}
function useMeetingLibrary() {
  const [items, setItems] = useState<ApiMeeting[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState("");
  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const page = await meetingApi.list(signal);
      const visibleItems = page.items.filter((meeting) => meeting.status !== "cancelled");
      setItems(visibleItems);
      setTotal(visibleItems.length);
      setError("");
    } catch (cause) {
      if (!signal?.aborted)
        setError(
          cause instanceof ApiError
            ? cause.message
            : "Không thể tải thư viện meeting local.",
        );
    }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);
  const removeFromLibrary = useCallback((meetingId: string) => {
    setItems((current) => current?.filter((meeting) => meeting.id !== meetingId) ?? null);
    setTotal((current) => Math.max(0, current - 1));
  }, []);
  return { items, total, error, reload: () => void load(), removeFromLibrary };
}
function MeetingRow({
  meeting,
  onRemoved,
}: {
  meeting: ApiMeeting;
  onRemoved?: (meetingId: string) => void;
}) {
  const [isDeleting, setIsDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const remove = async () => {
    if (!window.confirm(`Xóa cuộc họp “${title(meeting)}”? Audio và transcript sẽ bị xóa.`)) return;
    setIsDeleting(true);
    setDeleteError("");
    try {
      await meetingApi.remove(meeting.id);
      onRemoved?.(meeting.id);
    } catch (cause) {
      setDeleteError(cause instanceof ApiError ? cause.message : "Không thể xóa cuộc họp local.");
    } finally {
      setIsDeleting(false);
    }
  };
  return (
    <div className="meeting-row">
      <NavLink to={`/workspace/${meeting.id}`} className="meeting-link">
        <span className="file"><FileAudio size={18} /></span>
        <div><b>{title(meeting)}</b><small>{date(meeting)} · {duration(meeting.duration_seconds)}</small></div>
        <p>{meeting.original_filename}</p><span /><Status status={meeting.status} />
      </NavLink>
      <button className="meeting-delete" onClick={() => void remove()} disabled={isDeleting} aria-label={`Xóa ${title(meeting)}`} title={deleteError || "Xóa cuộc họp"}>
        <Trash2 size={16} /> {isDeleting ? "Đang xóa..." : "Xóa"}
      </button>
    </div>
  );
}
function LibraryError({
  message,
  retry,
}: {
  message: string;
  retry: () => void;
}) {
  return (
    <div className="empty">
      <FileAudio />
      <h3>{message}</h3>
      <button className="primary" onClick={retry}>
        Thử lại
      </button>
    </div>
  );
}
function Stat({
  icon: Icon,
  label,
  value,
  detail,
}: {
  icon: ComponentType<{ size?: number }>;
  label: string;
  value: string;
  detail: string;
}) {
  return (
    <article className="stat">
      <span>
        <Icon size={19} />
      </span>
      <small>{label}</small>
      <b>{value}</b>
      <em>{detail}</em>
    </article>
  );
}
function Dashboard() {
  const { items, total, error, reload, removeFromLibrary } = useMeetingLibrary();
  if (error)
    return (
      <div className="page">
        <LibraryError message={error} retry={reload} />
      </div>
    );
  if (!items)
    return (
      <div className="page">
        <div className="empty">
          <Clock3 />
          <h3>Đang tải thư viện cuộc họp…</h3>
        </div>
      </div>
    );
  const completed = items.filter(({ status }) => status === "completed").length;
  const waiting = items.filter(
    ({ status }) => status === "queued" || status === "processing",
  ).length;
  return (
    <div className="page">
      <section className="hero">
        <div>
          <mark>MEETING LIBRARY LOCAL</mark>
          <h2>
            Quản lý và xem lại
            <br />
            <em>bản ghi cuộc họp.</em>
          </h2>
          <p>
            Tải audio lên máy local, theo dõi xử lý và xem transcript theo thời
            gian.
          </p>
        </div>
        <NavLink className="primary" to="/meetings">
          <UploadIcon size={17} />Tải bản ghi
        </NavLink>
      </section>
      <section className="stats">
        <Stat
          icon={FileAudio}
          label="Tổng cuộc họp"
          value={String(total)}
          detail="Theo dữ liệu từ API local"
        />
        <Stat
          icon={Clock3}
          label="Hoàn tất"
          value={String(completed)}
          detail="Trong các cuộc họp đang hiển thị"
        />
        <Stat
          icon={Clock3}
          label="Đang xử lý"
          value={String(waiting)}
          detail="Trong các cuộc họp đang hiển thị"
        />
      </section>
      <section className="panel recent">
        <div className="panel-head">
          <div>
            <h3>Cuộc họp gần đây</h3>
            <p>Dữ liệu từ meeting library local</p>
          </div>
          <NavLink to="/meetings">Xem tất cả</NavLink>
        </div>
        {items.length ? (
          items
            .slice(0, 4)
            .map((meeting) => <MeetingRow meeting={meeting} key={meeting.id} onRemoved={removeFromLibrary} />)
        ) : (
          <div className="empty">
            <FileAudio />
            <h3>Chưa có cuộc họp nào</h3>
            <p>Tải bản ghi audio đầu tiên để bắt đầu.</p>
          </div>
        )}
      </section>
    </div>
  );
}
function UploadModal({
  close,
  onUploaded,
}: {
  close: () => void;
  onUploaded: (id: string) => void;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [meetingTitle, setMeetingTitle] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const select = (selected: FileList | null) => {
    const next = Array.from(selected ?? []);
    if (!next.length) return;
    if (next.length > 10 || next.some((file) => !file.type.startsWith("audio/") || !file.size || file.size > 25 * 1024 * 1024) || next.reduce((sum, file) => sum + file.size, 0) > 100 * 1024 * 1024) {
      setFiles([]);
      setMessage("Chọn tối đa 10 file audio, mỗi file tối đa 25 MiB và tổng tối đa 100 MiB.");
      return;
    }
    setFiles(next);
    setMessage("");
  };
  const send = async () => {
    if (!files.length) {
      setMessage("Chọn ít nhất một file audio để tiếp tục.");
      return;
    }
    setBusy(true);
    try {
      const meeting = await meetingApi.upload(files, meetingTitle);
      onUploaded(meeting.meeting_id);
    } catch (cause) {
      setMessage(
        cause instanceof ApiError
          ? cause.message
          : "Không kết nối được API local.",
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <div
      className="overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="upload-title"
    >
      <div className="upload">
        <button className="close" onClick={close} aria-label="Đóng">
          <X />
        </button>
        <mark>LOCAL UPLOAD</mark>
        <h2 id="upload-title">Tải bản ghi lên</h2>
        <p>
          Chọn tối đa 10 phần ghi âm theo thứ tự phát. Mỗi file tối đa 25 MiB, tổng tối đa 100 MiB.
        </p>
        <label
          className="drop"
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            event.preventDefault();
            select(event.dataTransfer.files);
          }}
        >
          <input
            ref={input}
            type="file"
            accept="audio/*"
            multiple
            onChange={(event) => select(event.target.files)}
          />
          <UploadIcon size={26} />
          <b>{files.length ? `${files.length} file đã chọn` : "Kéo thả audio vào đây"}</b>
          <small>
            {files.length
              ? files.map((file, index) => `${index + 1}. ${file.name}`).join(" · ")
              : "hoặc chọn nhiều file từ máy tính"}
          </small>
          <button
            type="button"
            onClick={(event) => {
              event.preventDefault();
              input.current?.click();
            }}
          >
            Chọn file audio
          </button>
        </label>
        <label className="input">
          Tên cuộc họp
          <input
            maxLength={200}
            value={meetingTitle}
            onChange={(event) => setMeetingTitle(event.target.value)}
            placeholder="Ví dụ: Sprint planning tuần 42"
          />
        </label>
        {message && (
          <p className="message" role="alert">
            {message}
          </p>
        )}
        <button className="primary full" onClick={send} disabled={busy}>
          {busy ? "Đang gửi..." : "Bắt đầu xử lý"}
        </button>
      </div>
    </div>
  );
}
function Meetings() {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<MeetingStatus | "all">("all");
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const { items, error, reload, removeFromLibrary } = useMeetingLibrary();
  const matching =
    items?.filter(
      (meeting) =>
        (filter === "all" || meeting.status === filter) &&
        `${title(meeting)} ${meeting.original_filename}`
          .toLowerCase()
          .includes(query.toLowerCase()),
    ) ?? [];
  return (
    <div className="page">
      <section className="title">
        <div>
          <small>THƯ VIỆN</small>
          <h2>Cuộc họp của bạn</h2>
        </div>
        <button className="primary" onClick={() => setOpen(true)}>
          <Plus />
          Cuộc họp mới
        </button>
      </section>
      <MeetBotPanel />
      <div className="toolbar">
        <label>
          <Search size={18} />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Tìm theo tiêu đề hoặc tên file..."
          />
        </label>
        <select
          value={filter}
          onChange={(event) =>
            setFilter(event.target.value as MeetingStatus | "all")
          }
        >
          <option value="all">Mọi trạng thái</option>
          {Object.entries(labels).map(([status, label]) => (
            <option key={status} value={status}>
              {label}
            </option>
          ))}
        </select>
      </div>
      {error ? (
        <LibraryError message={error} retry={reload} />
      ) : !items ? (
        <div className="empty">
          <Clock3 />
          <h3>Đang tải thư viện cuộc họp…</h3>
        </div>
      ) : matching.length ? (
        <section className="library">
          {matching.map((meeting) => (
            <MeetingRow meeting={meeting} key={meeting.id} onRemoved={removeFromLibrary} />
          ))}
        </section>
      ) : (
        <div className="empty">
          <Search />
          <h3>
            {items.length ? "Không tìm thấy cuộc họp" : "Chưa có cuộc họp nào"}
          </h3>
          <p>
            {items.length
              ? "Thử một từ khóa hoặc bộ lọc khác."
              : "Tải bản ghi audio đầu tiên để bắt đầu."}
          </p>
        </div>
      )}
      {open && (
        <UploadModal
          close={() => setOpen(false)}
          onUploaded={(id) => {
            setOpen(false);
            navigate(`/workspace/${id}`);
          }}
        />
      )}
    </div>
  );
}
function Workspace() {
  const { id = "" } = useParams();
  return <LocalWorkspace id={id} />;
}
function LocalWorkspace({ id }: { id: string }) {
  const [searchParams] = useSearchParams();
  const requestedTime = searchParams.get("t");
  const locationSeek = useRef("");
  const [meeting, setMeeting] = useState<BackendMeeting>();
  const [error, setError] = useState("");
  const [deleteError, setDeleteError] = useState("");
  const [isDeleting, setIsDeleting] = useState(false);
  const [query, setQuery] = useState("");
  const [summary, setSummary] = useState("");
  const [summaryError, setSummaryError] = useState("");
  const [isSummaryLoading, setIsSummaryLoading] = useState(false);
  const [summaryAttempt, setSummaryAttempt] = useState(0);
  const navigate = useNavigate();
  const audios = useRef<(HTMLAudioElement | null)[]>([]);
  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const result = await meetingApi.get(id, signal);
        setMeeting(result);
        setError("");
        return result;
      } catch (cause) {
        if (!signal?.aborted)
          setError(
            cause instanceof ApiError
              ? cause.message
              : "Không tải được cuộc họp local.",
          );
        return undefined;
      }
    },
    [id],
  );
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    void load(controller.signal).then((result) => {
      if (
        result &&
        (result.status === "queued" || result.status === "processing")
      )
        timer = setTimeout(() => void load(controller.signal), 3000);
    });
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [load]);
  useEffect(() => {
    if (meeting?.id !== id || meeting.status !== "completed") return;
    if (!meeting.transcript_segments.some((segment) => segment.text.trim())) {
      setSummary("");
      setSummaryError("");
      return;
    }
    const controller = new AbortController();
    setSummary("");
    setSummaryError("");
    setIsSummaryLoading(true);
    void meetingApi.summary(meeting.id, controller.signal).then((result) => {
      if (!controller.signal.aborted) setSummary(result.summary);
    }).catch((cause) => {
      if (controller.signal.aborted) return;
      const code = cause instanceof ApiError ? cause.message : "";
      setSummaryError(code === "rag_disabled" || code === "chat_provider_not_configured"
        ? "Chưa cấu hình chat model cho RAG."
        : "Không tạo được tóm tắt. Vui lòng thử lại.");
    }).finally(() => {
      if (!controller.signal.aborted) setIsSummaryLoading(false);
    });
    return () => controller.abort();
  }, [id, meeting?.id, meeting?.status, summaryAttempt]);
  const seek = useCallback((seconds: number, shouldPlay = true) => {
    const parts = meeting?.audio_files ?? [];
    let uploadOffset = 0;
    const timing = parts.map((part) => {
      const start_seconds = part.start_seconds ?? uploadOffset;
      const duration_seconds = part.duration_seconds ?? 0;
      uploadOffset += duration_seconds;
      return { start_seconds, duration_seconds };
    });
    const index = findAudioPartAtTime(timing, seconds);
    if (index === null) return;
    const player = audios.current[parts[index].ordinal];
    if (player && player.readyState >= 1) {
      player.currentTime = seconds - timing[index].start_seconds;
      if (shouldPlay) void player.play().catch(() => undefined);
      return true;
    }
    return false;
  }, [meeting]);
  const seekToSource = useCallback(() => {
    const key = `${id}:${requestedTime}`;
    if (requestedTime !== null && locationSeek.current !== key && meeting?.id === id) {
      const seconds = Number(requestedTime);
      if (Number.isFinite(seconds) && seconds >= 0 && seek(seconds, false)) locationSeek.current = key;
    }
  }, [id, requestedTime, meeting, seek]);
  useEffect(() => { seekToSource(); }, [seekToSource]);
  const remove = async () => {
    if (!meeting || !window.confirm(`Xóa cuộc họp “${title(meeting)}”? Audio và transcript sẽ bị xóa.`)) return;
    setIsDeleting(true);
    setDeleteError("");
    try {
      await meetingApi.remove(id);
      navigate("/meetings");
    } catch (cause) {
      setDeleteError(cause instanceof ApiError ? cause.message : "Không thể xóa cuộc họp local.");
    } finally {
      setIsDeleting(false);
    }
  };
  if (error)
    return (
      <div className="page">
        <button className="back" onClick={() => navigate("/meetings")}>
          <ChevronLeft />
          Thư viện
        </button>
        <LibraryError message={error} retry={() => void load()} />
      </div>
    );
  if (!meeting || meeting.id !== id)
    return (
      <div className="page">
        <div className="empty">
          <Clock3 />
          <h3>Đang tải cuộc họp local…</h3>
        </div>
      </div>
    );
  const segments = meeting.transcript_segments.filter(
    (segment) =>
      !query || segment.text.toLowerCase().includes(query.toLowerCase()),
  );
  const speakers = speakerLabels(meeting.diarization_segments);
  return (
    <div className="page workspace">
      <section className="work-title">
        <button className="back" onClick={() => navigate("/meetings")}>
          <ChevronLeft />
          Cuộc họp
        </button>
        <div>
          <small>MEETING LIBRARY LOCAL</small>
          <h2>{title(meeting)}</h2>
        </div>
        <Status status={meeting.status} />
        <button className="meeting-delete" onClick={() => void remove()} disabled={isDeleting}>
          <Trash2 size={16} />
          {isDeleting ? "Đang xóa..." : "Xóa"}
        </button>
      </section>
      {deleteError && <p className="message" role="alert">{deleteError}</p>}
      {meeting.status !== "completed" ? (
        <div className="empty">
          <Clock3 />
          <h3>
            {meeting.status === "failed"
              ? "Xử lý chưa thành công"
              : "Bản ghi đang được xử lý"}
          </h3>
          <p>
            {meeting.status === "failed"
              ? "Bạn có thể thử lại từ API local."
              : "Trang sẽ tự cập nhật khi worker hoàn thành."}
          </p>
        </div>
      ) : (
        <div className="work-grid">
          <section>
            <div className="audio">
              <div className="audio-top">
                <div>
                  <small>AUDIO</small>
                  <h3>Bản ghi từ meeting library</h3>
                </div>
              </div>
              {meeting.audio_files.map((part) => (
                <div key={part.ordinal}>
                  {meeting.audio_files.length > 1 && <p>{part.ordinal + 1}. {part.original_filename} · từ {time(part.start_seconds ?? 0)}</p>}
                  <audio
                    ref={(node) => { audios.current[part.ordinal] = node; }}
                    controls
                    onLoadedMetadata={seekToSource}
                    aria-label={`Audio phần ${part.ordinal + 1}: ${part.original_filename}`}
                    src={meetingApi.audioUrl(id, part.ordinal)}
                    onPlay={() => audios.current.forEach((other, index) => {
                      if (index !== part.ordinal) other?.pause();
                    })}
                  />
                </div>
              ))}
            </div>
            <div className="transcript-title">
              <div>
                <h3>Transcript</h3>
                <p>
                  {meeting.transcript_segments.length} đoạn · dữ liệu backend
                </p>
              </div>
              <label>
                <Search size={16} />
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Tìm trong transcript"
                />
              </label>
            </div>
            <div className="transcript">
              {segments.map((segment, index) => (
                <article
                  key={`${segment.start}-${index}`}
                  onClick={() => seek(segment.start)}
                >
                  <i>{segment.speaker}</i>
                  <div>
                    <header>
                      <b>{speakerDisplay(segment.speaker, meeting.speaker_identities ?? []).name} · {segment.speaker}</b>
                      <button
                        onClick={(event) => {
                          event.stopPropagation();
                          seek(segment.start);
                        }}
                      >
                        {time(segment.start)}
                      </button>
                    </header>
                    <p>{segment.text}</p>
                  </div>
                </article>
              ))}
              {!segments.length && (
                <div className="empty">
                  <Search />
                  <h3>Không tìm thấy đoạn transcript</h3>
                </div>
              )}
            </div>
          </section>
          <aside className="insight">
            <div className="ai">
              <div>
                <h1>Thông tin tóm tắt</h1>
              </div>
            </div>
            <div className="insight-body">
              <MeetingSpeakerIdentities
                meetingId={id}
                speakers={[...new Set(meeting.diarization_segments.map(turn => turn.speaker))]}
                identities={meeting.speaker_identities ?? []}
                status={meeting.voice_identification_status ?? "not_run"}
                onCorrected={identity => setMeeting(current => current?.id === id ? {...current, speaker_identities: [...(current.speaker_identities ?? []).filter(item => item.speaker !== identity.speaker), identity]} : current)}
              />
              <h4>Diarization</h4>
              <p>
                {speakers.length} người nói{speakers.length ? `: ${speakers.join(", ")}` : " được phát hiện"}.
              </p>
            </div>
            <div className="insight-body">
              <h3>Nội dung cuộc họp</h3>
              <p aria-live="polite">{isSummaryLoading ? "Đang tạo tóm tắt…" : summary || summaryError || "Không có nội dung lời nói để tóm tắt."}</p>
              {summaryError && <button className="primary" type="button" onClick={() => setSummaryAttempt((attempt) => attempt + 1)}>Thử lại</button>}
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}
function SettingsPage() {
  return (
    <div className="page">
      <section className="title">
        <div>
          <h2>Cài đặt workspace</h2>
        </div>
      </section>
    </div>
  );
}
function AuthenticatedApp() {
  const { account, loading } = useAuth();
  if (loading) return <main className="page" role="status">Đang tải phiên đăng nhập…</main>;
  if (!account) return <LoginPage />;
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<ProjectsPage />} />
        <Route path="/projects" element={<ProjectsPage />} />
        <Route path="/projects/:id" element={<ProjectPage />} />
        <Route path="/accounts" element={<AccountsPage />} />
        <Route path="/meetings" element={<ProjectsPage />} />
        <Route path="/chat" element={<MeetingSearchChat />} />
        <Route path="/workspace/:id" element={<Workspace />} />
        <Route path="/voice-profiles" element={<VoiceProfilesPage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Routes>
    </Shell>
  );
}

export default function App() { return <AuthProvider><AuthenticatedApp /></AuthProvider>; }
