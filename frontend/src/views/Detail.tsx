/** Landing detail view: GCA scopes, top-down track, charts, data sheet (FR-5/FR-6). */

import { useEffect, useState } from "react";
import { getLanding } from "../api/client";
import { GcaScope } from "../components/GcaScope";
import { GradeSummary } from "../components/GradeSummary";
import { TimeSeriesChart } from "../components/TimeSeriesChart";
import { TopDownTrack } from "../components/TopDownTrack";
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

      {/* ===== Data sheet (print target, FR-6) ===== */}
      <section className="print-sheet" aria-label="評価データシート">
        <h1 className="sheet-title">DCS Landing Teacher — 着陸評価データシート</h1>

        <table className="info-table">
          <tbody>
            <tr>
              <th>記録 ID</th>
              <td>{detail.id}</td>
              <th>日時</th>
              <td>{formatEpoch(detail.touchdown_epoch ?? detail.touchdown_time)}</td>
            </tr>
            <tr>
              <th>プレイヤー</th>
              <td>{detail.pilot ?? "-"}</td>
              <th>機体</th>
              <td>{detail.airframe ?? "-"}</td>
            </tr>
            <tr>
              <th>空港 / 空母</th>
              <td>{detail.venue_name ?? (detail.kind === "land" ? "空港" : "-")}</td>
              <th>種別</th>
              <td>{kindLabel(detail.kind)}</td>
            </tr>
            <tr>
              <th>ソース</th>
              <td>{detail.source_name ?? detail.source_id ?? "-"}</td>
              <th>グレード / 評点</th>
              <td>
                <span className={`grade-badge ${gradeClass(detail.grade)}`}>
                  {detail.grade ?? "-"}
                </span>
                {detail.score !== null && ` （評点 ${detail.score.toFixed(1)}）`}
              </td>
            </tr>
            <tr>
              <th>アウトカム</th>
              <td>{detail.outcome ?? "-"}</td>
            </tr>
          </tbody>
        </table>

        {track && track.samples.length > 0 ? (
          <GcaScope samples={track.samples} />
        ) : (
          <p className="empty-message">進入軌跡データが保存されていません。</p>
        )}

        <GradeSummary
          kind={detail.kind}
          outcome={detail.outcome}
          grade={detail.grade}
          score={detail.score}
          comment={detail.comment}
          factors={detail.factors}
          metrics={detail.metrics}
          gradingVersion={detail.grading_version}
        />

        <p className="sheet-footer">
          評価時刻: {formatIso(detail.graded_at)} ／ DCS Landing Teacher による自動評価
        </p>
      </section>

      {/* ===== Screen-only sections ===== */}
      {track && track.samples.length > 0 && (
        <>
          <section className="no-print">
            <h2>トップダウン軌跡</h2>
            <TopDownTrack samples={track.samples} />
          </section>

          <section className="no-print">
            <h2>時系列チャート</h2>
            <TimeSeriesChart track={track} />
          </section>
        </>
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
