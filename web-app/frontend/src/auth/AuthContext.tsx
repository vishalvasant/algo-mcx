import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { fetchMe, login as apiLogin, logout as apiLogout } from "../api/client";
import { getStoredToken, setStoredToken } from "./storage";

interface AuthState {
  username: string | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [username, setUsername] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const bootstrap = useCallback(async () => {
    const token = getStoredToken();
    if (!token) {
      setUsername(null);
      setLoading(false);
      return;
    }
    try {
      const me = await fetchMe();
      setUsername(me.username);
    } catch {
      setStoredToken(null);
      setUsername(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    bootstrap();
  }, [bootstrap]);

  const login = useCallback(async (user: string, password: string) => {
    const res = await apiLogin(user, password);
    setStoredToken(res.access_token);
    setUsername(res.username);
  }, []);

  const logout = useCallback(async () => {
    try {
      await apiLogout();
    } catch {
      /* ignore */
    }
    setStoredToken(null);
    setUsername(null);
  }, []);

  const value = useMemo(
    () => ({ username, loading, login, logout }),
    [username, loading, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
