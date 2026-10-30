import { useState, type FormEvent } from "react";
import { useAuth } from "../lib/AuthProvider";
import { Link } from "react-router-dom";
import { Moon, Sun } from "lucide-react";

export function LoginPage({
  isRegister = false,
  search = "",
}: {
  isRegister?: boolean;
  search?: string;
}) {
  const { login, register } = useAuth();
  const [theme, setTheme] = useState(() =>
    localStorage.getItem("meeting-auth-theme") === "light" ? "light" : "dark",
  );
  const [error, setError] = useState("");
  const [isBusy, setIsBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setIsBusy(true);
    setError("");
    try {
      await (isRegister ? register : login)(
        String(data.get("email")),
        String(data.get("password")),
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Không thể đăng nhập.");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <main className="auth-page" data-theme={theme}>
      <button
        className="auth-theme-toggle"
        type="button"
        aria-label={
          theme === "dark"
            ? "Chuyển sang giao diện sáng"
            : "Chuyển sang giao diện tối"
        }
        onClick={() => {
          const next = theme === "dark" ? "light" : "dark";
          localStorage.setItem("meeting-auth-theme", next);
          setTheme(next);
        }}
      >
        {theme === "dark" ? (
          <Sun size={18} aria-hidden="true" />
        ) : (
          <Moon size={18} aria-hidden="true" />
        )}
        <span>{theme === "dark"}</span>
      </button>
      <div className="auth-layout">
        <h1 className="auth-brand">
          Meeting <span>Agent</span>
        </h1>
        <section className="auth-panel" aria-labelledby="auth-title">
          <h2 id="auth-title">{isRegister ? "Tạo tài khoản" : "Đăng nhập"}</h2>
          <form
            onSubmit={(event) => void submit(event)}
            aria-busy={isBusy}
            aria-describedby={error ? "auth-error" : undefined}
          >
            <label htmlFor="auth-email">Email</label>
            <input
              id="auth-email"
              name="email"
              type="email"
              autoComplete="username"
              required
            />
            <label htmlFor="auth-password">Mật khẩu</label>
            <input
              id="auth-password"
              name="password"
              type="password"
              autoComplete={isRegister ? "new-password" : "current-password"}
              minLength={isRegister ? 12 : 1}
              aria-describedby={isRegister ? "auth-password-help" : undefined}
              required
            />
            {isRegister && (
              <p id="auth-password-help" className="auth-help">
                Mật khẩu ít nhất 12 ký tự. Quản trị viên sẽ thêm bạn vào dự án.
              </p>
            )}
            {error && (
              <p id="auth-error" role="alert" className="access-error">
                {error}
              </p>
            )}
            <button className="primary" disabled={isBusy}>
              {isBusy
                ? "Đang xử lý…"
                : isRegister
                  ? "Tạo tài khoản"
                  : "Đăng nhập"}
            </button>
          </form>
          <Link
            className="auth-switch"
            to={`${isRegister ? "/login" : "/signup"}${search}`}
          >
            {isRegister ? "Quay lại đăng nhập" : "Đăng ký"}
          </Link>
        </section>
      </div>
    </main>
  );
}
