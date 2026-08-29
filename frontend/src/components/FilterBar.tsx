/** Filter controls for the landing dashboard (FR-5). */

import type { LandingFilters, SourceInfo } from "../types/api";
import { SourceSelector } from "./SourceSelector";

export interface FilterBarProps {
  filters: LandingFilters;
  onChange: (filters: LandingFilters) => void;
  sources?: SourceInfo[];
}

const KIND_OPTIONS = [
  { value: "", label: "すべて" },
  { value: "carrier", label: "空母着艦" },
  { value: "land", label: "陸上着陸" },
];

const OUTCOME_OPTIONS = [
  { value: "", label: "すべて" },
  { value: "full_stop", label: "フルストップ" },
  { value: "touch_and_go", label: "タッチアンドゴー" },
  { value: "bolter", label: "ボルター" },
];

// The server matches `grade` with equality against whatever the grader
// stored, and the two graders use different scales: land landings get the
// letters A-E from grading.yaml, carrier traps get the US Navy LSO grades.
// Listing only the LSO grades made the control unusable on this deployment,
// which records land landings almost exclusively -- every option matched zero
// rows.
const LAND_GRADE_OPTIONS = ["A", "B", "C", "D", "E"].map((g) => ({
  value: g,
  label: g,
}));

const CARRIER_GRADE_OPTIONS = [
  { value: "OK", label: "OK" },
  { value: "OK-", label: "OK-" },
  { value: "(OK)", label: "(OK)" },
  { value: "_NO_GRADE_", label: "_NO_GRADE_" },
  { value: "CUT", label: "CUT" },
];

export function FilterBar({ filters, onChange, sources }: FilterBarProps) {
  const set = (patch: Partial<LandingFilters>) => onChange({ ...filters, ...patch });

  return (
    <div className="filter-bar no-print">
      <label>
        プレイヤー
        <input
          type="text"
          placeholder="部分一致"
          value={filters.player ?? ""}
          onChange={(e) => set({ player: e.target.value || undefined })}
        />
      </label>
      <label>
        機体
        <input
          type="text"
          placeholder="部分一致"
          value={filters.airframe ?? ""}
          onChange={(e) => set({ airframe: e.target.value || undefined })}
        />
      </label>
      <label>
        空港 / 空母
        <input
          type="text"
          placeholder="部分一致"
          value={filters.venue ?? ""}
          onChange={(e) => set({ venue: e.target.value || undefined })}
        />
      </label>
      {sources && sources.length > 0 && (
        <label>
          ソース
          <SourceSelector
            sources={sources}
            currentSource={filters.source ?? null}
            onChange={(sourceId) => set({ source: sourceId || undefined })}
          />
        </label>
      )}
      <label>
        進入方式
        <select
          value={filters.pattern ?? ""}
          onChange={(e) => set({ pattern: e.target.value || undefined })}
        >
          <option value="">すべて</option>
          <option value="overhead">オーバーヘッド</option>
          <option value="straight_in">ストレートイン</option>
          <option value="unknown">不明</option>
        </select>
      </label>
      <label>
        種別
        <select
          value={filters.kind ?? ""}
          onChange={(e) => set({ kind: e.target.value || undefined })}
        >
          {KIND_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        グレード
        <select
          value={filters.grade ?? ""}
          onChange={(e) => set({ grade: e.target.value || undefined })}
        >
          <option value="">すべて</option>
          <optgroup label="陸上">
            {LAND_GRADE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </optgroup>
          <optgroup label="空母 (LSO)">
            {CARRIER_GRADE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </optgroup>
        </select>
      </label>
      <label>
        アウトカム
        <select
          value={filters.outcome ?? ""}
          onChange={(e) => set({ outcome: e.target.value || undefined })}
        >
          {OUTCOME_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        登録日時（開始）
        <input
          type="datetime-local"
          value={filters.date_from ?? ""}
          onChange={(e) => set({ date_from: e.target.value || undefined })}
        />
      </label>
      <label>
        登録日時（終了）
        <input
          type="datetime-local"
          value={filters.date_to ?? ""}
          onChange={(e) => set({ date_to: e.target.value || undefined })}
        />
      </label>
    </div>
  );
}
