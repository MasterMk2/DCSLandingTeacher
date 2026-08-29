/**
 * Pure helpers for ACMI import job states (used by ImportPanel and tests).
 */

import type { ImportJob } from "../types/api";

export type ImportStatus = ImportJob["status"];

/** Source id the backend scopes an import's records to. Must match
 *  `app.importer.import_source_id`. */
export function importSourceId(jobId: string): string {
  return `import:${jobId}`;
}

/** Accepted file extensions for the upload dialog / drag & drop. */
export const IMPORT_ACCEPT = ".acmi,.acmi.txt,.acmi.zip";

/** True once the job reached a terminal state (no more polling needed). */
export function isTerminalImportStatus(status: string): boolean {
  return status === "completed" || status === "failed";
}

/** True while the upload/parse is still in flight. */
export function isActiveImportStatus(status: string): boolean {
  return status === "pending" || status === "processing";
}

/** Human-readable Japanese label for a job status. */
export function importStatusLabel(status: string): string {
  switch (status) {
    case "pending":
      return "待機中";
    case "processing":
      return "解析中";
    case "completed":
      return "完了";
    case "failed":
      return "失敗";
    default:
      return status;
  }
}

/** One-line result summary, e.g. 「検出 3 件・重複スキップ 1 件」.
 *
 * A bare "検出 0 件" reads as "there are no landings in this file", which is
 * the wrong conclusion when every landing in it was already on record. Say
 * which of the two happened.
 */
export function formatImportSummary(job: ImportJob): string {
  const parts: string[] = [];
  if (job.landings_detected === 0 && job.status === "completed") {
    parts.push(
      job.duplicates_skipped > 0
        ? "新規なし（すべて記録済み）"
        : "着陸は検出されませんでした",
    );
  } else if (job.landings_detected > 0 || job.status === "completed") {
    parts.push(`検出 ${job.landings_detected} 件`);
  }
  if (job.duplicates_skipped > 0) {
    parts.push(`重複スキップ ${job.duplicates_skipped} 件`);
  }
  parts.push(`${job.frames_processed} フレーム`);
  return parts.join("・");
}
