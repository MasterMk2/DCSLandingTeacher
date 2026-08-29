/** ACMI file import panel: drag & drop / file picker + progress + summary. */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  discardImport,
  discardImportOnUnload,
  getImport,
  importAcmiFile,
  listLandings,
} from "../api/client";
import { LandingTable } from "./LandingTable";
import { importSourceId } from "../lib/importStatus";
import {
  IMPORT_ACCEPT,
  formatImportSummary,
  importStatusLabel,
  isActiveImportStatus,
} from "../lib/importStatus";
import type { ImportJob, LandingSummary } from "../types/api";

export interface ImportPanelProps {
  /** Called when a job reaches a terminal state so the list can refresh. */
  onImported?: () => void;
  /** Opens a landing's detail sheet; enables the results table below. */
  onSelectLanding?: (id: number) => void;
}

const POLL_INTERVAL_MS = 1000;

export function ImportPanel({ onImported, onSelectLanding }: ImportPanelProps) {
  const [open, setOpen] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<ImportJob | null>(null);
  // Results of THIS import, kept out of the shared history on purpose: the
  // recording is usually from another server or another day, and mixing it
  // into the server's own list makes both unreadable.
  const [results, setResults] = useState<LandingSummary[] | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // Kept in a ref as well so the unload handler below always sees the current
  // job without being torn down and re-registered on every poll tick.
  const jobRef = useRef<ImportJob | null>(null);
  jobRef.current = job;

  // An upload is scratch data scoped to its own source. Throw it away when
  // the page goes away, so an abandoned tab does not leave another server's
  // recording sitting in the database until the retention sweep runs.
  useEffect(() => {
    const drop = () => {
      const current = jobRef.current;
      if (current) discardImportOnUnload(current.id);
    };
    window.addEventListener("pagehide", drop);
    return () => window.removeEventListener("pagehide", drop);
  }, []);

  const handleDiscard = useCallback(async () => {
    const current = jobRef.current;
    if (!current) return;
    try {
      await discardImport(current.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return;
    }
    setJob(null);
    setResults(null);
    setError(null);
    onImported?.();
  }, [onImported]);

  const pollJob = useCallback(
    (jobId: string) => {
      const timer: ReturnType<typeof setTimeout> = setTimeout(async () => {
        try {
          const current = await getImport(jobId);
          setJob(current);
          if (isActiveImportStatus(current.status)) {
            pollJob(jobId);
          } else {
            setUploading(false);
            onImported?.();
            if (current.status === "completed") {
              try {
                const page = await listLandings(
                  { source: importSourceId(jobId) },
                  200,
                  0,
                );
                setResults(page.items);
              } catch {
                // The summary line already says how many were found; a
                // failed listing must not look like a failed import.
                setResults([]);
              }
            }
          }
        } catch (err) {
          setError(err instanceof Error ? err.message : String(err));
          setUploading(false);
        }
      }, POLL_INTERVAL_MS);
      return timer;
    },
    [onImported],
  );

  const startUpload = useCallback(
    async (file: File) => {
      setError(null);
      setJob(null);
      setResults(null);
      setUploading(true);
      try {
        const started = await importAcmiFile(file);
        setJob({
          id: started.id,
          filename: started.filename,
          status: started.status,
          created_at: new Date().toISOString(),
          frames_processed: 0,
          total_frames: 0,
          progress_percent: null,
          landings_detected: 0,
          duplicates_skipped: 0,
        });
        pollJob(started.id);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        setUploading(false);
      }
    },
    [pollJob],
  );

  const handleDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      setDragOver(false);
      const file = event.dataTransfer.files?.[0];
      if (file) void startUpload(file);
    },
    [startUpload],
  );

  const handleFileChange = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const file = event.target.files?.[0];
      if (file) void startUpload(file);
      event.target.value = "";
    },
    [startUpload],
  );

  return (
    <div className="import-panel no-print">
      <button
        type="button"
        className="btn"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        ACMI ファイルをインポート
      </button>

      {open && (
        <div className="import-dropzone-wrap">
          <div
            className={`import-dropzone${dragOver ? " drag-over" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={handleDrop}
            onClick={() => inputRef.current?.click()}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") inputRef.current?.click();
            }}
          >
            {uploading
              ? "アップロード中・解析中..."
              : "ここにドロップ、またはクリックしてファイルを選択（.acmi / .acmi.txt / .acmi.zip）"}
          </div>
          <input
            ref={inputRef}
            type="file"
            accept={IMPORT_ACCEPT}
            onChange={handleFileChange}
            hidden
          />

          {job && (
            <>
              <p className="import-status">
                <span className={`import-badge status-${job.status}`}>
                  {importStatusLabel(job.status)}
                </span>{" "}
                {job.filename}
                {!isActiveImportStatus(job.status) && (
                  <> — {formatImportSummary(job)}</>
                )}
                {job.status === "failed" && job.error && (
                  <span className="error-message"> {job.error}</span>
                )}
                {!isActiveImportStatus(job.status) && (
                  <button
                    type="button"
                    className="btn btn-small"
                    onClick={handleDiscard}
                  >
                    破棄
                  </button>
                )}
              </p>
              {!isActiveImportStatus(job.status) && (
                <p className="import-note">
                  ※ 取り込んだ内容は一時データです。サーバの記録一覧には出ず、
                  破棄するかタブを閉じた時点で削除されます。
                </p>
              )}
              {isActiveImportStatus(job.status) && job.total_frames > 0 && (
                <div className="import-progress">
                  <div
                    className="import-progress-bar"
                    style={{ width: `${job.progress_percent ?? 0}%` }}
                    role="progressbar"
                    aria-valuenow={job.progress_percent ?? 0}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-label={`解析進捗 ${job.progress_percent ?? 0}%`}
                  />
                  <span className="import-progress-text">
                    {job.frames_processed} / {job.total_frames} フレーム
                    ({job.progress_percent ?? 0}%)
                  </span>
                </div>
              )}
            </>
          )}
          {error && <p className="error-message">インポートエラー: {error}</p>}

          {results !== null && onSelectLanding && (
            <div className="import-results">
              <h4>インポート結果（{results.length} 件・一時データ）</h4>
              {results.length === 0 ? (
                <p className="empty-message">
                  このファイルから新しい着陸は取り込まれませんでした。
                </p>
              ) : (
                <LandingTable items={results} onSelect={onSelectLanding} />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
