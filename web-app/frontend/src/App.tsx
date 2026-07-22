import { Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./auth/AuthContext";
import { Layout } from "./components/Layout";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { DashboardPage } from "./pages/DashboardPage";
import { DecisionLogsPage } from "./pages/DecisionLogsPage";
import { HoldingsPage } from "./pages/HoldingsPage";
import { LoginPage } from "./pages/LoginPage";
import { NotificationsPage } from "./pages/NotificationsPage";
import { OrderBookPage } from "./pages/OrderBookPage";
import { TradesPage } from "./pages/TradesPage";

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<ProtectedRoute />}>
          <Route element={<Layout />}>
            <Route index element={<DashboardPage />} />
            <Route path="holdings" element={<HoldingsPage />} />
            <Route path="notifications" element={<NotificationsPage />} />
            <Route path="trades" element={<TradesPage />} />
            <Route path="order-book" element={<OrderBookPage />} />
            <Route path="logs" element={<DecisionLogsPage />} />
          </Route>
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AuthProvider>
  );
}
