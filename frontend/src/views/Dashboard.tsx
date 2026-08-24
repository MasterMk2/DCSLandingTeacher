/** Landing history dashboard (FR-5). */

import { useState } from "react";
import { listAllLandings } from "../api/client";
import { FilterBar } from "../components/FilterBar";
import { ImportPanel } from "../components/ImportPanel";
import { LandingTable } from "../components/LandingTable";
import { LANDING_PAGE_SIZE, useLandings } from "../hooks/useLandings";
import { downloadCsv, landingsToCsv } from "../lib/csv";
import type { LandingFilters } from "../types/api";

export interface DashboardProps {
  onSelectLanding: (id: number) => void;
}

export function Dashboard({ onSelectLanding }: DashboardProps) {
  const [filters, setFilters] = useState<LandingFilters>({});
  const [offset, setOffset] = useState(0);
  const { data, loading, error, liveCount, refresh } = useLandings(filters, offset);

  const total = data?.total ?? 0;
  const page = Math.floor(offset / LANDING_PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(total / LANDING_PAGE_SIZE));

  const handleExportCsv = async () => {
    try {
      const all = await listAllLandings(filters);
      downloadCsv("landings.csv", landingsToCsv(all));
    } catch (err) {
      alert(`CSV エクスポートに失敗しました: ${err instanceof Error ? err.message : err}`);
    }
  };

  return (
    <div className="dashboard">
      <header className="view-header no-print">
        <h1>着陸履歴</h1>
        <div className="header-actions">
          {liveCount > 0 && (
            <button className="btn btn-live" onClick={refresh} title="新規着陸を反映">
              ● 新着 {liveCount} 件
            </button>
          )}
          <button className="btn" onClick={handleExportCsv}>
            CSV エクスポート
          </button>
        </div>
      </header>

      <FilterBar
        filters={filters}
        onChange={(f) => {
          setFilters(f);
          setOffset(0);
        }}
      />

      <ImportPanel onImported={refresh} />

      {error && <p className="error-message">読み込みエラー: {error}</p>}
      {loading && <p className="loading-message">読み込み中...</p>}

      {!loading && data && (
        <>
          <LandingTable items={data.items} onSelect={onSelectLanding} />
          <div className="pagination no-print">
            <span>
              {total} 件中 {offset + 1}–{Math.min(offset + LANDING_PAGE_SIZE, total)} 件
              （ページ {page} / {pageCount}）
            </span>
            <button
              className="btn"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - LANDING_PAGE_SIZE))}
            >
              前へ
            </button>
            <button
              className="btn"
              disabled={offset + LANDING_PAGE_SIZE >= total}
              onClick={() => setOffset(offset + LANDING_PAGE_SIZE)}
            >
              次へ
            </button>
          </div>
        </>
      )}
    </div>
  );
}
