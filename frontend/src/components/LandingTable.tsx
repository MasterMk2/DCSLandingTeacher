/** Landing history table (FR-5 dashboard). */

import {
  formatEpoch,
  gradeClass,
  kindLabel,
  outcomeLabel,
} from "../lib/format";
import { isProvisional } from "../lib/landings";
import type { LandingSummary } from "../types/api";

export interface LandingTableProps {
  items: LandingSummary[];
  onSelect: (id: number) => void;
}

export function LandingTable({ items, onSelect }: LandingTableProps) {
  if (items.length === 0) {
    return <p className="empty-message">着陸記録がありません。</p>;
  }
  return (
    <table className="landing-table">
      <thead>
        <tr>
          <th>#</th>
          <th>日時</th>
          <th>プレイヤー</th>
          <th>機体</th>
          <th>空港 / 空母</th>
          <th>ソース</th>
          <th>種別</th>
          <th>グレード</th>
          <th>評点</th>
          <th>アウトカム</th>
        </tr>
      </thead>
      <tbody>
        {items.map((it) => (
          <tr
            key={it.id}
            className="landing-row"
            onClick={() => onSelect(it.id)}
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter") onSelect(it.id);
            }}
          >
            <td>{it.id}</td>
            <td>{formatEpoch(it.touchdown_epoch ?? it.touchdown_time)}</td>
            <td>{it.pilot ?? "-"}</td>
            <td>{it.airframe ?? "-"}</td>
            <td>{it.venue_name ?? (it.kind === "land" ? "空港" : "-")}</td>
            <td>{it.source_name ?? it.source_id ?? "-"}</td>
            <td>{kindLabel(it.kind)}</td>
            <td>
              {isProvisional(it) ? (
                <span className="grade-badge grade-provisional" title="アウトカム確定中">
                  評価中
                </span>
              ) : (
                <span className={`grade-badge ${gradeClass(it.grade)}`}>
                  {it.grade ?? "-"}
                </span>
              )}
            </td>
            <td>{it.score !== null ? it.score.toFixed(1) : "-"}</td>
            <td>{outcomeLabel(it.outcome)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
