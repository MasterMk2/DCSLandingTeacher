/** Minimal hash-based router: #/ (dashboard), #/landings/:id (detail). */

import { useCallback, useEffect, useState } from "react";
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

  useEffect(() => {
    const onHashChange = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const goDashboard = useCallback(() => {
    location.hash = "#/";
  }, []);

  const selectLanding = useCallback((id: number) => {
    location.hash = `#/landings/${id}`;
  }, []);

  return (
    <div className="app">
      <nav className="app-nav no-print">
        <a href="#/" className="app-title">
          DCS Landing Teacher
        </a>
        <span className="app-subtitle">着陸・着艦レビューシステム</span>
      </nav>
      <main>
        {route.view === "dashboard" ? (
          <Dashboard onSelectLanding={selectLanding} />
        ) : (
          <Detail id={route.id} onBack={goDashboard} />
        )}
      </main>
    </div>
  );
}
