/** Minimal hash-based router: #/ (dashboard), #/landings/:id (detail). */

import { useCallback, useEffect, useState } from "react";
import { AUTH_INVALID_EVENT, clearToken, getToken, saveToken } from "./auth/token";
import { TokenPrompt } from "./components/TokenPrompt";
import { Dashboard } from "./views/Dashboard";
import { Detail } from "./views/Detail";

function parseHash(): { view: "dashboard" } | { view: "detail"; id: number } {
  const hash = location.hash.replace(/^#/, "");
  const match = /^\/landings\/(\d+)$/.exec(hash);
  if (match) return { view: "detail", id: Number(match[1]) };
  return { view: "dashboard" };
}

export default function App() {
  const [route, setRoute] = useState(parseHash);
  // Bumped whenever the stored token changes so data views remount and
  // refetch with the new credentials (REST + WebSocket).
  const [authVersion, setAuthVersion] = useState(0);
  const [promptOpen, setPromptOpen] = useState(false);

  useEffect(() => {
    const onHashChange = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  // The REST client dispatches this event on 401/403 (Issue #8): clear the
  // rejected token and ask for a new one.
  useEffect(() => {
    const onAuthInvalid = () => {
      clearToken();
      setPromptOpen(true);
      setAuthVersion((v) => v + 1);
    };
    window.addEventListener(AUTH_INVALID_EVENT, onAuthInvalid);
    return () => window.removeEventListener(AUTH_INVALID_EVENT, onAuthInvalid);
  }, []);

  const goDashboard = useCallback(() => {
    location.hash = "#/";
  }, []);

  const selectLanding = useCallback((id: number) => {
    location.hash = `#/landings/${id}`;
  }, []);

  const handleTokenSubmit = useCallback((token: string) => {
    saveToken(token);
    setPromptOpen(false);
    setAuthVersion((v) => v + 1);
  }, []);

  const handleTokenCancel = useCallback(() => {
    setPromptOpen(false);
    setAuthVersion((v) => v + 1);
  }, []);

  return (
    <div className="app">
      <nav className="app-nav no-print">
        <a href="#/" className="app-title">
          DCS Landing Teacher
        </a>
        <span className="app-subtitle">着陸・着艦レビューシステム</span>
      </nav>
      {/* key remounts the views when the token changes (refetch + WS reconnect) */}
      <main key={authVersion}>
        {route.view === "dashboard" ? (
          <Dashboard onSelectLanding={selectLanding} />
        ) : (
          <Detail id={route.id} onBack={goDashboard} />
        )}
      </main>
      {promptOpen && (
        <TokenPrompt
          onSubmit={handleTokenSubmit}
          onCancel={getToken() ? handleTokenCancel : undefined}
        />
      )}
    </div>
  );
}
