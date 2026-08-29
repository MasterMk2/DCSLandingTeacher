/** Landing history table (FR-5 dashboard). */

import {
  formatIso,
  formatMissionTime,
  gradeClass,
  kindLabel,
  outcomeLabel,
  patternClass,
  patternLabel,
} from "../lib/format";
import { isProvisional } from "../lib/landings";
import type { LandingSortKey, LandingSummary } from "../types/api";

export interface LandingTableProps {
  items: LandingSummary[];
  onSelect: (id: number) => void;
  sort?: LandingSortKey;
  order?: "asc" | "desc";
  /** Omit to render plain, non-clickable headers. */
  onSort?: (key: LandingSortKey) => void;
}

const COLUMNS: { key: LandingSortKey | null; label: string }[] = [
  { key: null, label: "#" },
  { key: "time", label: "日時" },
  { key: "pilot", label: "プレイヤー" },
  { key: "airframe", label: "機体" },
  { key: "venue", label: "空港 / 空母" },
  { key: "source", label: "ソース" },
  { key: "kind", label: "種別" },
  { key: "pattern", label: "進入方式" },
  { key: "grade", label: "グレード" },
  { key: "score", label: "評点" },
  { key: "outcome", label: "アウトカム" },
];

export function LandingTable({
  items,
  onSelect,
  sort,
  order,
  onSort,
}: LandingTableProps) {
  if (items.length === 0) {
    return <p className="empty-message">着陸記録がありません。</p>;
  }
  return (
    // Wrapped rather than making the <table> itself `display: block` and
    // scrollable: that drops table layout, so the columns stop lining up
    // with the header. The scroll belongs to a box around it.
    <div className="table-scroll">
    <table className="landing-table">
      <thead>
        <tr>
          {COLUMNS.map((col) => {
            const sortable = col.key !== null && onSort !== undefined;
            const active = sortable && sort === col.key;
            return (
              <th
                key={col.label}
                // Column class so the narrow-screen rules can drop specific
                // columns by meaning rather than by nth-child, which silently
                // hides the wrong one the moment the order changes.
                className={`col-${col.key ?? "id"}${sortable ? " sortable" : ""}`}
                aria-sort={
                  active ? (order === "asc" ? "ascending" : "descending") : undefined
                }
                onClick={sortable ? () => onSort(col.key as LandingSortKey) : undefined}
                title={sortable ? "クリックで並べ替え" : undefined}
              >
                {col.label}
                {active && <span className="sort-arrow">{order === "asc" ? "▲" : "▼"}</span>}
              </th>
            );
          })}
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
            <td className="col-id">{it.id}</td>
            {/* Real recording time. The mission clock goes in the tooltip:
                it is the date inside the .miz (June 2026 for the Caucasus
                mission), which reads as wrong to whoever just flew it. */}
            <td
              className="col-time"
              title={`ミッション時刻: ${formatMissionTime(it.touchdown_epoch)}`}
            >
              {formatIso(it.created_at)}
            </td>
            <td className="col-pilot">{it.pilot ?? "-"}</td>
            <td className="col-airframe">{it.airframe ?? "-"}</td>
            <td className="col-venue">
              {it.venue_name ?? (it.kind === "land" ? "空港" : "-")}
            </td>
            <td className="col-source">{it.source_name ?? it.source_id ?? "-"}</td>
            <td className="col-kind">{kindLabel(it.kind)}</td>
            <td className="col-pattern">
              {it.approach_pattern ? (
                <span className={patternClass(it.approach_pattern)}>
                  {patternLabel(it.approach_pattern)}
                </span>
              ) : (
                "-"
              )}
            </td>
            <td className="col-grade">
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
            {/* Rows can arrive from the WebSocket as partial payloads, so a
                missing score must render as "-" rather than throwing: an
                exception here unmounts the whole tree and blanks the page. */}
            <td className="col-score">
              {typeof it.score === "number" ? it.score.toFixed(1) : "-"}
            </td>
            <td className="col-outcome">{outcomeLabel(it.outcome)}</td>
          </tr>
        ))}
      </tbody>
    </table>
    </div>
  );
}
