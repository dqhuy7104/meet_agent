// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AuthProvider } from "../lib/AuthProvider";
import { AuthPages, authReturnPath } from "./AuthPages";

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
});

function mount(path: string) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: false, status: 401, json: async () => ({}) })),
  );
  render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <AuthPages />
      </AuthProvider>
    </MemoryRouter>,
  );
}

it("opens signup directly and links back to login", async () => {
  mount("/signup");
  expect(
    await screen.findByRole("heading", { name: "Tạo tài khoản" }),
  ).toBeTruthy();
  expect(
    (screen.getByLabelText("Mật khẩu") as HTMLInputElement).minLength,
  ).toBe(12);
  expect(
    screen
      .getByRole("link", { name: "Quay lại đăng nhập" })
      .getAttribute("href"),
  ).toBe("/login");
  fireEvent.click(screen.getByRole("link", { name: "Quay lại đăng nhập" }));
  expect(
    await screen.findByRole("heading", { name: "Đăng nhập" }),
  ).toBeTruthy();
});

it("preserves the theme and return destination when changing auth pages", async () => {
  mount("/login?returnTo=%2Fapp%2Fprojects%2Fp");
  await screen.findByRole("heading", { name: "Đăng nhập" });
  fireEvent.click(
    screen.getByRole("button", { name: "Chuyển sang giao diện sáng" }),
  );
  expect(screen.getByRole("main").getAttribute("data-theme")).toBe("light");
  fireEvent.click(screen.getByRole("link", { name: "Đăng ký" }));
  expect(
    await screen.findByRole("heading", { name: "Tạo tài khoản" }),
  ).toBeTruthy();
  expect(screen.getByRole("main").getAttribute("data-theme")).toBe("light");
  expect(
    screen
      .getByRole("link", { name: "Quay lại đăng nhập" })
      .getAttribute("href"),
  ).toBe("/login?returnTo=%2Fapp%2Fprojects%2Fp");
  fireEvent.click(
    screen.getByRole("button", { name: "Chuyển sang giao diện tối" }),
  );
  expect(screen.getByRole("main").getAttribute("data-theme")).toBe("dark");
});

it.each([
  ["", "/app/"],
  [
    "?returnTo=%2Fapp%2Fworkspace%2Fm%3Fq%3Dnotes%23audio",
    "/app/workspace/m?q=notes#audio",
  ],
  ["?returnTo=https%3A%2F%2Fexample.com", "/app/"],
  ["?returnTo=%2F%2Fexample.com", "/app/"],
  ["?returnTo=%2Fsignup", "/app/"],
  ["?returnTo=%2Fapp%2F..%2Flogin", "/app/"],
])("restricts auth redirects to the app (%s)", (search, expected) => {
  expect(authReturnPath(search)).toBe(expected);
});
