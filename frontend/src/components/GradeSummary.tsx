/** Evaluation summary: grade/score, factors with evidence, comment. */

import { factorDescription, gradeClass, outcomeLabel } from "../lib/format";
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
      const value = typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(2)) : String(v);
      return `${k}: ${value}`;
    })
    .join(" / ");
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
  return (
    <div className="grade-summary">
      <div className="grade-headline">
        <span className={`grade-badge grade-large ${gradeClass(grade)}`}>
          {kind === "carrier" ? (grade ?? "-") : score !== null ? `評点 ${score.toFixed(1)}` : "-"}
        </span>
        <span className="grade-sub">
          {kind === "carrier" ? "LSO グレード" : "陸上簡易評価"}
          {score !== null && kind === "carrier" ? "" : ""}
          {" ／ "}
          {outcomeLabel(outcome)}
          {gradingVersion ? ` ／ 評価 v${gradingVersion}` : ""}
        </span>
      </div>

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
              <th>重要度</th>
              <th>根拠データ</th>
            </tr>
          </thead>
          <tbody>
            {factors.map((f, i) => (
              <tr key={`${f.name}-${i}`}>
                <td className="factor-name">{f.name}</td>
                <td>{factorDescription(f.name) || "-"}</td>
                <td>{f.severity ?? "-"}</td>
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
            {Object.entries(metrics).map(([k, v]) => (
              <div key={k} className="metrics-row">
                <dt>{k}</dt>
                <dd>
                  {typeof v === "number"
                    ? Number.isInteger(v)
                      ? v
                      : v.toFixed(2)
                    : JSON.stringify(v)}
                </dd>
              </div>
            ))}
          </dl>
        </>
      )}
    </div>
  );
}
