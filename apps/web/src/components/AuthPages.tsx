import { useEffect } from "react";
import { useLocation } from "react-router-dom";
import { useAuth } from "../lib/AuthProvider";
import { LoginPage } from "./LoginPage";

export function authReturnPath(search: string): string {
  const path = new URLSearchParams(search).get("returnTo");
  if (!path?.startsWith("/app/")) return "/app/";
  const destination = new URL(path, window.location.origin);
  return destination.pathname.startsWith("/app/")
    ? destination.pathname + destination.search + destination.hash
    : "/app/";
}

export function LoginRedirect() {
  useEffect(() => {
    const path =
      window.location.pathname + window.location.search + window.location.hash;
    window.location.replace(`/login?returnTo=${encodeURIComponent(path)}`);
  }, []);
  return (
    <main className="page" role="status">
      Đang chuyển đến đăng nhập…
    </main>
  );
}

export function AuthPages() {
  const { account, loading } = useAuth();
  const { pathname, search } = useLocation();
  const returnPath = authReturnPath(search);
  const isRegister = pathname === "/signup";

  useEffect(() => {
    if (account) window.location.replace(returnPath);
  }, [account, returnPath]);

  if (loading || account)
    return (
      <main className="page" role="status">
        Đang tải phiên đăng nhập…
      </main>
    );
  return <LoginPage key={pathname} isRegister={isRegister} search={search} />;
}
