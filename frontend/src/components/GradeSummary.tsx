/** Evaluation summary: grade/score, factors with evidence, comment. */

import {
  factorDescription,
  factorLabel,
  formatMetric,
  gradeClass,
  outcomeLabel,
} from "../lib/format";
import type { Factor } from "../types/api";

export interface GradeSummaryProps {
  kind: string | null;
  outcome: string | null;
  grade: string | null;
  score: number | null;
  comment?: string | null;
  factors: Factor[];
  metrics?: Record<string, unknown> | null;
  gradingVersion?: string | null;
}

function formatEvidence(evidence: Record<string, unknown> | null | undefined): string {
  if (!evidence || Object.keys(evidence).length === 0) return "";
  return Object.entries(evidence)
    .map(([k, v]) => {
      const { label, text } = formatMetric(k, v);
      return `${label}: ${text}`;
    })
    .join(" / ");
}

/** 採点しなかった理由 (backend: land_grader.UNSCORED_*) の日本語。 */
const UNSCORED_REASON_JA: Record<string, string> = {
  "not-measured": "この記録からは測れませんでした",
  "not-applicable-to-airframe-class": "この機体クラスには基準が無く採点対象外です",
};

/** 陸上は素点と重み、空母 (LSO) は重大度。どちらも無ければ "-"。 */
function factorWeightText(f: Factor): string {
  if (typeof f.score === "number") {
    const weight = typeof f.weight === "number" ? `（重み ${f.weight.toFixed(2)}）` : "";
    return `${f.score.toFixed(0)} 点${weight}`;
  }
  // 陸上の採点コンポーネントは、素点が無い = 採点していない。空欄や
  // 0 点に見えると「測ったうえで最低点」と読まれるので、理由を出す。
  const reason = f.evidence?.["unscored_reason"];
  if (typeof reason === "string") {
    const weight = typeof f.weight === "number" ? `（本来の重み ${f.weight.toFixed(2)}）` : "";
    return `未評価${weight}: ${UNSCORED_REASON_JA[reason] ?? reason}`;
  }
  return f.severity ?? "-";
}

/** 採点に使えた重みの内訳。評点の隣に出して、点数の射程を示す。 */
function coverageText(metrics?: Record<string, unknown> | null): string | null {
  if (!metrics) return null;
  const weight = metrics["measured_weight"];
  const unmeasured = metrics["unmeasured_components"];
  if (typeof weight !== "number" || !Array.isArray(unmeasured) || unmeasured.length === 0) {
    return null;
  }
  const names = unmeasured.map((n) => factorLabel(String(n))).join("・");
  return `評価できた項目は全体の ${Math.round(weight * 100)}%（未評価: ${names}）`;
}

export function GradeSummary({
  kind,
  outcome,
  grade,
  score,
  comment,
  factors,
  metrics,
  gradingVersion,
}: GradeSummaryProps) {
  const coverage = coverageText(metrics);
  return (
    <div className="grade-summary">
      <div className="grade-headline">
        <span className={`grade-badge grade-large ${gradeClass(grade)}`}>
          {kind === "carrier"
            ? (grade ?? "-")
            : score !== null
              ? `評点 ${score.toFixed(1)}`
              : "記録不足"}
        </span>
        <span className="grade-sub">
          {kind === "carrier" ? "LSO グレード" : "陸上簡易評価"}
          {score !== null && kind === "carrier" ? "" : ""}
          {" ／ "}
          {outcomeLabel(outcome)}
          {gradingVersion ? ` ／ 評価 v${gradingVersion}` : ""}
        </span>
      </div>

      {/* 点数の射程。半分しか見られていない着陸の点数を、全項目を見た
          うえでの点数として読ませないための一行。 */}
      {coverage && <p className="grade-coverage">{coverage}</p>}

      {comment && <p className="grade-comment">{comment}</p>}

      <h4>検出ファクター（{factors.length} 件）</h4>
      {factors.length === 0 ? (
        <p className="empty-message">顕著なファクターは検出されませんでした。</p>
      ) : (
        <table className="factor-table">
          <thead>
            <tr>
              <th>ファクター</th>
              <th>説明</th>
              <th>素点 / 重要度</th>
              <th>根拠データ</th>
            </tr>
          </thead>
          <tbody>
            {factors.map((f, i) => (
              <tr key={`${f.name}-${i}`}>
                <td className="factor-name">{f.name}</td>
                <td>{factorDescription(f.name) || "-"}</td>
                <td>{factorWeightText(f)}</td>
                <td className="factor-evidence">{formatEvidence(f.evidence)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {metrics && Object.keys(metrics).length > 0 && (
        <>
          <h4>評価メトリクス</h4>
          <dl className="metrics-list">
            {Object.entries(metrics).map(([k, v]) => {
              const { label, text } = formatMetric(k, v);
              return (
                <div key={k} className="metrics-row">
                  <dt>{label}</dt>
                  <dd>{typeof v === "object" && v !== null ? JSON.stringify(v) : text}</dd>
                </div>
              );
            })}
          </dl>
        </>
      )}
    </div>
  );
}
