/** Landing detail view: pattern plan view, glideslope profile, data sheet
 * (FR-5/FR-6). A4 printable layout.
 *
 * The GCA azimuth/elevation scopes used to sit here too. They were dropped
 * once the pattern view (equal scale, whole circuit, unclamped along-course
 * axis) and the glideslope profile (height and deviation against range)
 * between them covered the same ground: two more dark boxes plotting the
 * same final approach was noise, not information. */

import { useEffect, useState } from "react";
import { getLanding } from "../api/client";
import { PatternTrack } from "../components/PatternTrack";
import { TimeSeriesChart } from "../components/TimeSeriesChart";
import { GlideslopeProfileChart } from "../components/GlideslopeProfileChart";
import {
  factorDescription,
  factorLabel,
  formatEpoch,
  formatIso,
  formatMetric,
  gradeClass,
  isInternalMetricKey,
  kindLabel,
  mToFt,
  msToKnots,
  msToFpm,
  patternClass,
  patternLabel,
} from "../lib/format";
import { downloadCsv, samplesToCsv } from "../lib/csv";
import type { Factor, LandingDetail } from "../types/api";

/** 採点しなかった理由 (backend: land_grader.UNSCORED_*) の日本語。 */
const UNSCORED_REASON_JA: Record<string, string> = {
  "not-measured": "この記録からは測れませんでした",
  "not-applicable-to-airframe-class": "この機体クラスには基準が無く採点対象外です",
};

/** 素点を持たない陸上コンポーネントの表示。
 *
 * 素点が無いのは「測ったうえで 0 点」ではなく「採点していない」で、
 * 重みごと合成から外れている。空欄や "-" だと最低点と読まれるので、
 * 理由と、本来なら占めていた重みを出す。 */
function unscoredText(f: Factor): string | null {
  const reason = f.evidence?.["unscored_reason"];
  if (typeof reason !== "string") return null;
  const weight =
    typeof f.weight === "number" ? `（本来の重み ${f.weight.toFixed(2)}）` : "";
  return `未評価${weight}: ${UNSCORED_REASON_JA[reason] ?? reason}`;
}

/** 「この点数が何割の項目から出ているか」の一行。全項目を採点できた
 *  着陸では出さない (常に 100% と書いてあると読み飛ばされる)。 */
function coverageText(metrics?: Record<string, unknown> | null): string | null {
  if (!metrics) return null;
  const weight = metrics["measured_weight"];
  const unmeasured = metrics["unmeasured_components"];
  if (typeof weight !== "number" || !Array.isArray(unmeasured) || unmeasured.length === 0) {
    return null;
  }
  const names = unmeasured.map((n) => factorLabel(String(n))).join("・");
  const scored = metrics["graded"] === false ? "採点に使えた項目" : "評価できた項目";
  return `${scored}は全体の ${Math.round(weight * 100)}%（未評価: ${names}）`;
}

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
  const coverage = coverageText(detail.metrics);
  // A plan view only earns its space when there is a circuit to look at.
  // "overhead" is the classifier's answer; the lateral test catches the
  // approaches it left as "unknown" that were in fact flown as patterns.
  const showPattern =
    detail.kind === "land" &&
    !!track &&
    (detail.approach_pattern === "overhead" ||
      track.samples.some(
        (s) => Math.abs(s.centerline_deviation ?? 0) > 500,
      ));
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
            <span>日時: {formatIso(detail.created_at)}</span>
            <span>ミッション時刻: {formatEpoch(detail.touchdown_epoch)}</span>
            <span>評価時刻: {formatIso(detail.graded_at)}</span>
          </div>
        </header>

        <div className="sheet-grid">
          {/* Left column: Basic info + GCA Scopes */}
          <div className="sheet-left">
            <div className="table-scroll">
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
                      <span className={patternClass(detail.approach_pattern)}>
                        {patternLabel(detail.approach_pattern)}
                      </span>
                    )}
                  </td>
                </tr>
              </tbody>
            </table>
            </div>

            {track && track.samples.length > 0 ? (
              <>
                {showPattern && (
                  <>
                    <h4 className="pattern-heading">パターン軌跡</h4>
                    <PatternTrack track={track} metrics={detail.metrics} />
                  </>
                )}
              </>
            ) : (
              <p className="empty-message">進入軌跡データが保存されていません。</p>
            )}
          </div>

          {/* Right column: Grade + Glideslope Profile + Factors + Metrics */}
          <div className="sheet-right">
            <div className="grade-panel">
              <div className="grade-headline">
                <span className={`grade-badge grade-large ${gradeClass(detail.grade)}`}>
                  {detail.kind === "carrier"
                    ? (detail.grade ?? "-")
                    : detail.score !== null
                      ? `評点 ${detail.score.toFixed(1)}`
                      : "記録不足"}
                </span>
                <span className="grade-sub">
                  {detail.kind === "carrier" ? "LSO グレード" : "陸上簡易評価"}
                  {" ／ "}
                  {kindLabel(detail.outcome)}
                  {detail.grading_version ? ` ／ 評価 v${detail.grading_version}` : ""}
                </span>
              </div>

              {/* 点数の射程。測れなかった項目は重みごと合成から外れるので、
                  半分しか見られていない着陸の点数が「全項目を見たうえでの
                  点数」と読まれないよう、評点のすぐ下に必ず出す。 */}
              {coverage && <p className="grade-coverage">{coverage}</p>}

              {detail.comment && <p className="grade-comment">{detail.comment}</p>}

              {/* Glideslope Profile Chart (horizontal, A4-friendly) */}
              {track && track.samples.length > 0 && (
                <GlideslopeProfileChart track={track} metrics={detail.metrics} />
              )}

              {/* Factors Table */}
              <div className="factors-section">
                <h4>検出ファクター（{detail.factors.length} 件）</h4>
                {detail.factors.length === 0 ? (
                  <p className="empty-message">顕著なファクターは検出されませんでした。</p>
                ) : (
                  <div className="table-scroll">
                  <table className="factor-table">
                    <thead>
                      <tr>
                        <th>ファクター</th>
                        <th>素点 / 重要度</th>
                        <th>説明</th>
                        <th>根拠データ</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.factors.map((f, i) => {
                        const ev = f.evidence ?? {};
                        // 空母の LSO ファクターは重大度と説明文を evidence に
                        // 持つが、陸上の採点コンポーネントは素点と重みを持つ。
                        // 片方の形だけを描くと、もう片方は列が全部 "-" になる。
                        const scored = typeof f.score === "number";
                        const weight =
                          typeof f.weight === "number" ? `（重み ${f.weight.toFixed(2)}）` : "";
                        const description =
                          ev.description !== undefined && ev.description !== null
                            ? String(ev.description)
                            : factorDescription(f.name) || "-";
                        const details =
                          ev.details !== undefined && ev.details !== null
                            ? String(ev.details)
                            : null;
                        return (
                          <tr key={`${f.name}-${i}`}>
                            <td className="factor-name">{f.name}</td>
                            <td className={`factor-severity ${f.severity ?? ""}`}>
                              {scored
                                ? `${(f.score as number).toFixed(0)} 点${weight}`
                                : (unscoredText(f) ?? f.severity ?? "-")}
                            </td>
                            <td className="factor-description">
                              {description}
                              {details && <span className="factor-details">{details}</span>}
                            </td>
                            <td className="factor-evidence">{formatFactorEvidence(ev)}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  </div>
                )}
              </div>

              {/* Metrics */}
              {detail.metrics && Object.keys(detail.metrics).length > 0 && (
                <div className="metrics-section">
                  <h4>評価メトリクス</h4>
                  <dl className="metrics-list">
                    {Object.entries(detail.metrics)
                      .filter(([k]) => !isInternalMetricKey(k))
                      .map(([k, v]) => {
                      const { label, text } = formatMetric(k, v);
                      return (
                        <div key={k} className="metrics-row">
                          <dt>{label}</dt>
                          <dd>{text}</dd>
                        </div>
                      );
                      })}
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
  // formatMetric supplies the Japanese label and the unit conversion, and --
  // the reason this is no longer a bare template string -- a readable form
  // for nested values: `${v}` printed "bands_fpm: [object Object]".
  return Object.entries(evidence)
    .filter(([k]) => k !== "description" && k !== "details")
    .filter(([k]) => !isInternalMetricKey(k))
    .map(([k, v]) => {
      const { label, text } = formatMetric(k, v);
      return `${label}: ${text}`;
    })
    .join(" / ");
}
