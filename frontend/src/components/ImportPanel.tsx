/** ACMI file import panel: drag & drop / file picker + progress + summary. */

import { useCallback, useRef, useState } from "react";
import { getImport, importAcmiFile } from "../api/client";
import {
  IMPORT_ACCEPT,
  formatImportSummary,
  importStatusLabel,
  isActiveImportStatus,
} from "../lib/importStatus";
import type { ImportJob } from "../types/api";

export interface ImportPanelProps {
  /** Called when a job reaches a terminal state so the list can refresh. */
  onImported?: () => void;
}

const POLL_INTERVAL_MS = 1000;

export function ImportPanel({ onImported }: ImportPanelProps) {
  const [open, setOpen] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<ImportJob | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

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
      setUploading(true);
      try {
        const started = await importAcmiFile(file);
        setJob({
          id: started.id,
          filename: started.filename,
          status: started.status,
          created_at: new Date().toISOString(),
          frames_processed: 0,
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
            </p>
          )}
          {error && <p className="error-message">インポートエラー: {error}</p>}
        </div>
      )}
    </div>
  );
}
