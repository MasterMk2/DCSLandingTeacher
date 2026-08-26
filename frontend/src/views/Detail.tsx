/** Landing detail view: GCA scopes, glideslope chart, data sheet (FR-5/FR-6).
 * A4 printable layout. Top-down track removed (overlaps with GCA azimuth scope). */

import { useEffect, useState } from "react";
import { getLanding } from "../api/client";
import { GcaScope } from "../components/GcaScope";
import { TimeSeriesChart } from "../components/TimeSeriesChart";
import { GlideslopeProfileChart } from "../components/GlideslopeProfileChart";
import {
  formatEpoch,
  formatIso,
  gradeClass,
  kindLabel,
  mToFt,
  msToKnots,
  msToFpm,
} from "../lib/format";
import { downloadCsv, samplesToCsv } from "../lib/csv";
import type { LandingDetail } from "../types/api";

export interface DetailProps {
  id: number;
  onBack: () => void;
}

export function Detail({ id, onBack }: DetailProps) {
  const [detail, setDetail] = useState<LandingDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    getLanding(id)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  if (error) {
    return (
      <div className="detail">
        <button className="btn no-print" onClick={onBack}>
          ← 一覧へ戻る
        </button>
        <p className="error-message">読み込みエラー: {error}</p>
      </div>
    );
  }

  if (!detail) {
    return <p className="loading-message">読み込み中...</p>;
  }

  const track = detail.approach_track ?? null;
  const td = detail.touchdown ?? null;

  const handleExportCsv = () => {
    if (!track || track.samples.length === 0) {
      alert("この着陸には進入軌跡データがありません。");
      return;
    }
    downloadCsv(`landing_${detail.id}_samples.csv`, samplesToCsv(track.samples));
  };

  return (
    <div className="detail">
      <header className="view-header no-print">
        <button className="btn" onClick={onBack}>
          ← 一覧へ戻る
        </button>
        <div className="header-actions">
          <button className="btn" onClick={() => window.print()}>
            データシート印刷 / PDF
          </button>
          <button className="btn" onClick={handleExportCsv}>
            軌跡 CSV エクスポート
          </button>
        </div>
      </header>

      {/* ===== Data sheet (print target, FR-6) - A4 layout ===== */}
      <section className="print-sheet" aria-label="評価データシート">
        <header className="sheet-header">
          <h1 className="sheet-title">DCS Landing Teacher — 着陸評価データシート</h1>
          <div className="sheet-meta no-print">
            <span>記録 ID: {detail.id}</span>
            <span>日時: {formatEpoch(detail.touchdown_epoch ?? detail.touchdown_time)}</span>
            <span>評価時刻: {formatIso(detail.graded_at)}</span>
          </div>
        </header>

        <div className="sheet-grid">
          {/* Left column: Basic info + GCA Scopes */}
          <div className="sheet-left">
            <table className="info-table">
              <tbody>
                <tr>
                  <th>プレイヤー</th>
                  <td>{detail.pilot ?? "-"}</td>
                </tr>
                <tr>
                  <th>機体</th>
                  <td>{detail.airframe ?? "-"}</td>
                </tr>
                <tr>
                  <th>空港 / 空母</th>
                  <td>{detail.venue_name ?? (detail.kind === "land" ? "空港" : "-")}</td>
                </tr>
                <tr>
                  <th>種別</th>
                  <td>{kindLabel(detail.kind)}</td>
                </tr>
                <tr>
                  <th>ソース</th>
                  <td>{detail.source_name ?? detail.source_id ?? "-"}</td>
                </tr>
                <tr>
                  <th>アウトカム</th>
                  <td>{detail.outcome ?? "-"}</td>
                </tr>
                <tr>
                  <th>進入パターン</th>
                  <td>
                    {detail.approach_pattern && (
                      <span className={`pattern-badge ${detail.approach_pattern}`}>
                        {detail.approach_pattern === "overhead" && "オーバーヘッド"}
                        {detail.approach_pattern === "straight_in" && "ストレートイン"}
                        {detail.approach_pattern === "unknown" && "不明"}
                      </span>
                    )}
                  </td>
                </tr>
              </tbody>
            </table>

            {track && track.samples.length > 0 ? (
              <GcaScope samples={track.samples} />
            ) : (
              <p className="empty-message">進入軌跡データが保存されていません。</p>
            )}
          </div>

          {/* Right column: Grade + Glideslope Profile + Factors + Metrics */}
          <div className="sheet-right">
            <div className="grade-panel">
              <div className="grade-headline">
                <span className={`grade-badge grade-large ${gradeClass(detail.grade)}`}>
                  {detail.kind === "carrier" ? (detail.grade ?? "-") : detail.score !== null ? `評点 ${detail.score.toFixed(1)}` : "-"}
                </span>
                <span className="grade-sub">
                  {detail.kind === "carrier" ? "LSO グレード" : "陸上簡易評価"}
                  {" ／ "}
                  {kindLabel(detail.outcome)}
                  {detail.grading_version ? ` ／ 評価 v${detail.grading_version}` : ""}
                </span>
              </div>

              {detail.comment && <p className="grade-comment">{detail.comment}</p>}

              {/* Glideslope Profile Chart (horizontal, A4-friendly) */}
              {track && track.samples.length > 0 && (
                <GlideslopeProfileChart track={track} />
              )}

              {/* Factors Table */}
              <div className="factors-section">
                <h4>検出ファクター（{detail.factors.length} 件）</h4>
                {detail.factors.length === 0 ? (
                  <p className="empty-message">顕著なファクターは検出されませんでした。</p>
                ) : (
                  <table className="factor-table">
                    <thead>
                      <tr>
                        <th>ファクター</th>
                        <th>重要度</th>
                        <th>説明</th>
                        <th>詳細</th>
                        <th>根拠データ</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.factors.map((f, i) => {
                        const ev = f.evidence ?? {};
                        return (
                          <tr key={`${f.name}-${i}`}>
                            <td className="factor-name">{f.name}</td>
                            <td className={`factor-severity ${f.severity}`}>{f.severity ?? "-"}</td>
                            <td className="factor-description">{String(ev.description ?? "-")}</td>
                            <td className="factor-details">{String(ev.details ?? "-")}</td>
                            <td className="factor-evidence">{formatFactorEvidence(ev)}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                )}
              </div>

              {/* Metrics */}
              {detail.metrics && Object.keys(detail.metrics).length > 0 && (
                <div className="metrics-section">
                  <h4>評価メトリクス</h4>
                  <dl className="metrics-list">
                    {Object.entries(detail.metrics).map(([k, v]) => (
                      <div key={k} className="metrics-row">
                        <dt>{k}</dt>
                        <dd>{typeof v === "object" && v !== null ? JSON.stringify(v) : String(v)}</dd>
                      </div>
                    ))}
                  </dl>
                </div>
              )}

              <p className="sheet-footer">
                DCS Landing Teacher による自動評価
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* ===== Screen-only sections ===== */}
      {track && track.samples.length > 0 && (
        <section className="no-print">
          <h2>時系列チャート</h2>
          <TimeSeriesChart track={track} />
        </section>
      )}

      {td && (
        <section className="no-print">
          <h2>接地点情報</h2>
          <dl className="metrics-list">
            <div className="metrics-row">
              <dt>速度</dt>
              <dd>
                {td.speed_ms !== null && td.speed_ms !== undefined
                  ? `${td.speed_ms.toFixed(1)} m/s（${msToKnots(td.speed_ms).toFixed(0)} kt）`
                  : "-"}
              </dd>
            </div>
            <div className="metrics-row">
              <dt>降下率</dt>
              <dd>
                {td.descent_rate_ms !== null && td.descent_rate_ms !== undefined
                  ? `${td.descent_rate_ms.toFixed(2)} m/s（${msToFpm(td.descent_rate_ms).toFixed(0)} fpm）`
                  : "-"}
              </dd>
            </div>
            <div className="metrics-row">
              <dt>機首方位</dt>
              <dd>{td.heading !== null && td.heading !== undefined ? `${td.heading.toFixed(1)}°` : "-"}</dd>
            </div>
            <div className="metrics-row">
              <dt>高度</dt>
              <dd>
                {td.altitude !== null && td.altitude !== undefined
                  ? `${mToFt(td.altitude).toFixed(0)} ft（${td.altitude.toFixed(0)} m）`
                  : "-"}
              </dd>
            </div>
            <div className="metrics-row">
              <dt>座標</dt>
              <dd>
                {td.latitude !== null && td.latitude !== undefined && td.longitude !== null && td.longitude !== undefined
                  ? `${td.latitude.toFixed(5)}, ${td.longitude.toFixed(5)}`
                  : "-"}
              </dd>
            </div>
          </dl>
        </section>
      )}
    </div>
  );
}

// Helper to format factor evidence excluding description/details
function formatFactorEvidence(evidence: Record<string, unknown> | null | undefined): string {
  if (!evidence || Object.keys(evidence).length === 0) return "";
  return Object.entries(evidence)
    .filter(([k]) => k !== "description" && k !== "details")
    .map(([k, v]) => `${k}: ${v}`)
    .join(" / ");
}
