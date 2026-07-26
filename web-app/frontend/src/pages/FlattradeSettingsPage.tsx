import { useCallback, useEffect, useState } from "react";
import { KeyRound, RefreshCw, Save, Shield } from "lucide-react";
import {
  fetchFlattradeCredentials,
  fetchHealth,
  reauthenticate,
  saveFlattradeCredentials,
} from "../api/client";
import type { FlattradeCredentialsStatus } from "../types";
import { AppPageShell } from "../components/AppPageShell";
import { StatusBadge } from "../components/StatusBadge";

const EMPTY_FORM = {
  user_id: "",
  api_key: "",
  api_secret: "",
  password: "",
  totp_secret: "",
  redirect_url: "http://127.0.0.1:8000/callback",
};

export function FlattradeSettingsPage() {
  const [status, setStatus] = useState<FlattradeCredentialsStatus | null>(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [busy, setBusy] = useState(false);
  const [reauthBusy, setReauthBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sessionValid, setSessionValid] = useState<boolean | null>(null);

  const load = useCallback(() => {
    setBusy(true);
    Promise.all([fetchFlattradeCredentials(), fetchHealth()])
      .then(([creds, health]) => {
        setStatus(creds);
        setForm((prev) => ({
          ...prev,
          user_id: creds.user_id ?? "",
          redirect_url: creds.redirect_url || EMPTY_FORM.redirect_url,
        }));
        setSessionValid(health.flattrade_session?.valid ?? false);
        setError(null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setBusy(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onChange = (field: keyof typeof EMPTY_FORM, value: string) => {
    setForm((prev) => ({ ...prev, [field]: value }));
    setMessage(null);
  };

  const onSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const payload: Record<string, string> = {};
      if (form.user_id.trim()) payload.user_id = form.user_id.trim();
      if (form.api_key.trim()) payload.api_key = form.api_key.trim();
      if (form.api_secret.trim()) payload.api_secret = form.api_secret.trim();
      if (form.password.trim()) payload.password = form.password.trim();
      if (form.totp_secret.trim()) payload.totp_secret = form.totp_secret.trim();
      if (form.redirect_url.trim()) payload.redirect_url = form.redirect_url.trim();

      const saved = await saveFlattradeCredentials(payload);
      setStatus(saved);
      setForm((prev) => ({
        ...prev,
        api_key: "",
        api_secret: "",
        password: "",
        totp_secret: "",
        user_id: saved.user_id ?? prev.user_id,
        redirect_url: saved.redirect_url,
      }));
      setMessage(
        "Credentials saved to database. Use Re-authenticate to refresh the broker session.",
      );
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  const onReauth = async () => {
    setReauthBusy(true);
    setError(null);
    setMessage(null);
    try {
      await reauthenticate(true);
      setMessage("Broker session refreshed.");
      const health = await fetchHealth();
      setSessionValid(health.flattrade_session?.valid ?? false);
    } catch (err) {
      setError(String(err));
    } finally {
      setReauthBusy(false);
    }
  };

  const sourceLabel =
    status?.source === "database"
      ? "Database"
      : status?.source === "environment"
        ? "Environment (.env)"
        : status?.source === "mixed"
          ? "Database + .env"
          : "Not configured";

  return (
    <AppPageShell
      title="Flattrade"
      icon={KeyRound}
      description="Broker API credentials stored in the database — used by the trading engine for login, quotes, and orders."
      actions={
        <button
          type="button"
          className="btn btn-ghost btn-sm logs-refresh-btn"
          onClick={load}
          disabled={busy}
        >
          <RefreshCw size={13} />
          {busy ? "Loading…" : "Refresh"}
        </button>
      }
    >
      <div className="logs-page-full settings-page">
        <section className="cockpit-panel logs-stats-panel">
          <header className="cockpit-panel-head logs-stats-head">
            <Shield size={14} strokeWidth={2} />
            <h3>Connection status</h3>
          </header>
          <div className="cockpit-command-metrics logs-command-metrics">
            <div className="cmd-metric">
              <span>Credential source</span>
              <strong>{sourceLabel}</strong>
            </div>
            <div className="cmd-metric">
              <span>API configured</span>
              <strong>{status?.has_api_credentials ? "Yes" : "No"}</strong>
            </div>
            <div className="cmd-metric">
              <span>Auto login (TOTP)</span>
              <strong>{status?.has_auto_login ? "Ready" : "Incomplete"}</strong>
            </div>
            <div className="cmd-metric">
              <span>Session</span>
              <strong>
                {sessionValid === null ? "—" : sessionValid ? "Valid" : "Expired / missing"}
              </strong>
            </div>
          </div>
          {status?.api_key_masked ? (
            <p className="settings-hint mono">
              Current API key: <span>{status.api_key_masked}</span>
            </p>
          ) : null}
        </section>

        {error ? <div className="error-banner">{error}</div> : null}
        {message ? <div className="settings-success-banner">{message}</div> : null}

        <section className="cockpit-panel settings-form-panel">
          <header className="cockpit-panel-head">
            <KeyRound size={14} strokeWidth={2} />
            <h3>Credentials</h3>
            <StatusBadge
              label={status?.has_api_credentials ? "Configured" : "Required"}
              severity={status?.has_api_credentials ? "success" : "warning"}
            />
          </header>

          <form className="settings-form" onSubmit={onSave}>
            <label>
              <span>User ID</span>
              <input
                type="text"
                value={form.user_id}
                onChange={(e) => onChange("user_id", e.target.value)}
                placeholder="e.g. FZ49363"
                autoComplete="off"
              />
            </label>

            <label>
              <span>API key</span>
              <input
                type="password"
                value={form.api_key}
                onChange={(e) => onChange("api_key", e.target.value)}
                placeholder={
                  status?.api_key_masked ? "Leave blank to keep current" : "From Flattrade Wall → Pi"
                }
                autoComplete="off"
              />
            </label>

            <label>
              <span>API secret</span>
              <input
                type="password"
                value={form.api_secret}
                onChange={(e) => onChange("api_secret", e.target.value)}
                placeholder={status?.api_secret_set ? "Leave blank to keep current" : "API secret"}
                autoComplete="off"
              />
            </label>

            <label>
              <span>Password</span>
              <input
                type="password"
                value={form.password}
                onChange={(e) => onChange("password", e.target.value)}
                placeholder={
                  status?.password_set ? "Leave blank to keep current" : "Flattrade login password"
                }
                autoComplete="off"
              />
            </label>

            <label>
              <span>TOTP secret</span>
              <input
                type="password"
                value={form.totp_secret}
                onChange={(e) => onChange("totp_secret", e.target.value)}
                placeholder={
                  status?.totp_secret_set
                    ? "Leave blank to keep current"
                    : "Profile → Security → TOTP secret"
                }
                autoComplete="off"
              />
            </label>

            <label>
              <span>OAuth redirect URL</span>
              <input
                type="url"
                value={form.redirect_url}
                onChange={(e) => onChange("redirect_url", e.target.value)}
                placeholder="http://127.0.0.1:8000/callback"
                autoComplete="off"
              />
            </label>

            <p className="settings-form-note">
              Secrets are stored in Postgres. Leave password fields empty when updating other fields
              to keep existing values. Re-authenticate uses the saved row — one click, no re-entry.
            </p>

            <div className="settings-form-actions">
              <button type="submit" className="btn btn-primary" disabled={busy}>
                <Save size={14} />
                {busy ? "Saving…" : "Save credentials"}
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                onClick={onReauth}
                disabled={reauthBusy || !status?.has_auto_login}
              >
                <RefreshCw size={14} />
                {reauthBusy ? "Connecting…" : "Re-authenticate"}
              </button>
            </div>
          </form>
        </section>
      </div>
    </AppPageShell>
  );
}
