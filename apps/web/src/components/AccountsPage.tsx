import { useEffect, useState } from "react";
import { useAuth } from "../lib/AuthProvider";
import { projectApi } from "../lib/projectApi";
import type { Account } from "../lib/authApi";
import { Link } from "react-router-dom";
import { roleLabels } from "../lib/roleLabels";
export function AccountsPage() {
  const { account, refresh } = useAuth();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    if (account?.is_system_admin)
      void projectApi
        .accounts()
        .then(setAccounts)
        .catch((cause) => setError(cause.message));
  }, [account?.is_system_admin]);
  async function update(target: Account, changes: Partial<Account>) {
    if (!account?.is_system_admin) return;
    try {
      await projectApi.updateAccount(target.id, changes);
      setAccounts(await projectApi.accounts());
      await refresh();
    } catch (cause) {
      setError((cause as Error).message);
    }
  }
  if (!account?.is_system_admin)
    return (
      <div className="page access-page">
        <p>Chỉ Admin mới có quyền truy cập.</p>
      </div>
    );
  return (
    <div className="page access-page">
      <Link className="access-back" to="/settings">← Cài đặt</Link>
      <section className="title">
        <h1>Tài khoản</h1>
      </section>
      <p className="access-note" role="note">
        Email ownership is unverified. Verify identity outside this app before
        granting access.
      </p>
      {error && (
        <p role="alert" className="access-error">
          {error}
        </p>
      )}
      <section className="access-panel" aria-label="Danh sách tài khoản">
        <ul className="access-list">
          {accounts.map((target) => (
            <li className="access-account" key={target.id}>
              <b className="access-identity">{target.email}</b>
              <div
                className="access-checkboxes"
                role="group"
                aria-label={`Quyền của ${target.email}`}
              >
                <label>
                  <input
                    type="checkbox"
                    checked={target.is_system_admin}
                    onChange={() =>
                      void update(target, {
                        is_system_admin: !target.is_system_admin,
                      })
                    }
                  />
                  {roleLabels.system_admin}
                </label>
                <label>
                  <input
                    type="checkbox"
                    checked={target.is_disabled}
                    onChange={() =>
                      void update(target, { is_disabled: !target.is_disabled })
                    }
                  />
                  Vô hiệu hóa
                </label>
              </div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
