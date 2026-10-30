// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import App from "../App";
import { AuthProvider, useAuth } from "../lib/AuthProvider";
import { SettingsPage } from "./SettingsPage";
import type { Role } from "../lib/projectApi";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function backend(isAdmin: boolean, logoutFails = false) {
  return vi.fn(async (url: string, _options?: RequestInit) => {
    if (url === "/api/auth/logout")
      return {
        ok: !logoutFails,
        status: logoutFails ? 500 : 204,
        json: async () => ({
          detail: "Không thể đăng xuất. Vui lòng thử lại.",
        }),
      };
    return {
      ok: true,
      status: 200,
      json: async () =>
        url === "/api/admin/system-prompts"
          ? { summary_system_prompt: "", rag_system_prompt: "" }
          : url === "/api/auth/session"
          ? {
              account: {
                id: "a",
                email: "admin@example.test",
                is_system_admin: isAdmin,
                is_disabled: false,
              },
              csrf_token: "token",
            }
          : [],
    };
  });
}

it.each([true, false])(
  "puts logout and admin-only account management in settings (admin=%s)",
  async (isAdmin) => {
    vi.stubGlobal("fetch", backend(isAdmin));
    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <App />
      </MemoryRouter>,
    );
    await screen.findByRole("heading", { name: "Cài đặt workspace" });
    const nav = within(screen.getByRole("navigation"));
    expect(nav.queryByRole("button", { name: "Đăng xuất" })).toBeNull();
    expect(
      nav.queryByRole("link", { name: /Tài khoản|Quản lý tài khoản/ }),
    ).toBeNull();
    expect(screen.getByRole("button", { name: "Đăng xuất" })).toBeTruthy();
    expect(!!screen.queryByRole("heading", { name: "System prompt" })).toBe(isAdmin);
    expect(!!screen.queryByRole("link", { name: "Quản lý tài khoản" })).toBe(
      isAdmin,
    );
    if (isAdmin) {
      expect(
        screen
          .getByRole("link", { name: "Quản lý tài khoản" })
          .getAttribute("href"),
      ).toBe("/settings/accounts");
      fireEvent.click(screen.getByRole("link", { name: "Quản lý tài khoản" }));
      await screen.findByRole("heading", { name: "Tài khoản" });
      expect(
        nav.getByRole("link", { name: "Cài đặt" }).getAttribute("aria-current"),
      ).toBe("page");
      fireEvent.click(screen.getByRole("link", { name: "← Cài đặt" }));
      await screen.findByRole("heading", { name: "Cài đặt workspace" });
    }
  },
);

it("logs out through the session provider with CSRF", async () => {
  const fetcher = backend(false);
  vi.stubGlobal("fetch", fetcher);
  function SessionState() {
    const { account } = useAuth();
    return <p>{account ? "Signed in" : "Signed out"}</p>;
  }
  render(
    <MemoryRouter>
      <AuthProvider>
        <SettingsPage />
        <SessionState />
      </AuthProvider>
    </MemoryRouter>,
  );
  await screen.findByText("Signed in");
  fireEvent.click(screen.getByRole("button", { name: "Đăng xuất" }));
  await screen.findByText("Signed out");
  const call = fetcher.mock.calls.find(([url]) => url === "/api/auth/logout");
  expect(call?.[1]?.method).toBe("POST");
  expect(new Headers(call?.[1]?.headers).get("X-CSRF-Token")).toBe("token");
});

it("keeps settings available and shows logout failures", async () => {
  vi.stubGlobal("fetch", backend(false, true));
  render(
    <MemoryRouter initialEntries={["/settings"]}>
      <App />
    </MemoryRouter>,
  );
  await screen.findByRole("heading", { name: "Cài đặt workspace" });
  fireEvent.click(screen.getByRole("button", { name: "Đăng xuất" }));
  expect((await screen.findByRole("alert")).textContent).toBe(
    "Không thể đăng xuất. Vui lòng thử lại.",
  );
  expect(
    (screen.getByRole("button", { name: "Đăng xuất" }) as HTMLButtonElement)
      .disabled,
  ).toBe(false);
});

it.each<[Role, string]>([
  ["system_admin", "Admin"],
  ["project_manager", "Project Manager"],
  ["member", "Thành viên"],
  ["viewer", "Chỉ xem"],
])(
  "shows the server-provided %s role beside its project",
  async (role, label) => {
    const base = backend(role === "system_admin");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, options?: RequestInit) =>
        url === "/api/projects"
          ? {
              ok: true,
              status: 200,
              json: async () => [
                {
                  id: "p",
                  name: "Nghiên cứu sản phẩm",
                  role,
                  is_archived: true,
                },
              ],
            }
          : base(url, options),
      ),
    );
    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <App />
      </MemoryRouter>,
    );
    const link = await screen.findByRole("link", {
      name: new RegExp(`Nghiên cứu sản phẩm.*${label}`),
    });
    expect(link.getAttribute("href")).toBe("/projects/p");
    expect(within(link).getByText("Đã lưu trữ")).toBeTruthy();
  },
);

it("shows loading until an empty project response arrives", async () => {
  let finish!: (response: {
    ok: boolean;
    status: number;
    json: () => Promise<unknown>;
  }) => void;
  const pending = new Promise<{
    ok: boolean;
    status: number;
    json: () => Promise<unknown>;
  }>((resolve) => {
    finish = resolve;
  });
  const base = backend(false);
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, options?: RequestInit) =>
      url === "/api/projects" ? pending : base(url, options),
    ),
  );
  render(
    <MemoryRouter initialEntries={["/settings"]}>
      <App />
    </MemoryRouter>,
  );
  await screen.findByText("Đang tải dự án…");
  expect(screen.queryByText("Bạn chưa được thêm vào dự án nào.")).toBeNull();
  finish({ ok: true, status: 200, json: async () => [] });
  await screen.findByText("Bạn chưa được thêm vào dự án nào.");
  expect(screen.queryByText("Đang tải dự án…")).toBeNull();
});

it("retries failed project loading without reporting an empty list", async () => {
  const base = backend(false);
  let attempts = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url !== "/api/projects") return base(url, options);
      attempts++;
      return attempts === 1
        ? {
            ok: false,
            status: 503,
            json: async () => ({ detail: "Không tải được dự án." }),
          }
        : {
            ok: true,
            status: 200,
            json: async () => [
              {
                id: "p",
                name: "Dự án đã tải lại",
                role: "member",
                is_archived: false,
              },
            ],
          };
    }),
  );
  render(
    <MemoryRouter initialEntries={["/settings"]}>
      <App />
    </MemoryRouter>,
  );
  expect((await screen.findByRole("alert")).textContent).toBe(
    "Không tải được dự án.",
  );
  expect(screen.queryByText("Bạn chưa được thêm vào dự án nào.")).toBeNull();
  expect(screen.getByRole("button", { name: "Đăng xuất" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Thử lại" }));
  await screen.findByRole("link", { name: /Dự án đã tải lại/ });
  expect(screen.queryByRole("alert")).toBeNull();
});
