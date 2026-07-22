import { useState } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { AlertTriangle, Lock, LogIn } from "lucide-react";
import { useAuth } from "../auth/AuthContext";

export function LoginPage() {
  const { login, username } = useAuth();
  const location = useLocation();
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const from = (location.state as { from?: string } | null)?.from ?? "/";

  if (username) {
    return <Navigate to={from} replace />;
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(user.trim(), password);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-shell">
      <div className="login-grid-bg" />
      <div className="login-card">
        <div className="login-brand">
          <div className="brand-icon lg">AM</div>
          <div>
            <h1>Algo-MCX</h1>
            <p>MCX Gold · Silver · Natural Gas</p>
          </div>
        </div>

        <form className="login-form" onSubmit={submit}>
          <label>
            <span>Username</span>
            <input
              autoComplete="username"
              value={user}
              onChange={(e) => setUser(e.target.value)}
              placeholder="admin"
              required
            />
          </label>
          <label>
            <span>Password</span>
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              required
            />
          </label>

          {error && (
            <div className="error-banner compact">
              <AlertTriangle size={14} />
              {error}
            </div>
          )}

          <button type="submit" className="btn btn-primary btn-block" disabled={busy}>
            <LogIn size={16} />
            {busy ? "Signing in…" : "Sign in to terminal"}
          </button>
        </form>

        <div className="login-footer">
          <Lock size={12} />
          <span>Paper-first · Flattrade Pi · JWT secured</span>
        </div>
      </div>
    </div>
  );
}
