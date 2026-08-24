import { describe, expect, it } from "vitest";
import type { ImportJob } from "../types/api";
import {
  formatImportSummary,
  importStatusLabel,
  isActiveImportStatus,
  isTerminalImportStatus,
} from "./importStatus";

function makeJob(overrides: Partial<ImportJob> = {}): ImportJob {
  return {
    id: "job-1",
    filename: "session.acmi",
    status: "processing",
    created_at: "2026-01-01T00:00:00+00:00",
    frames_processed: 1200,
    landings_detected: 3,
    duplicates_skipped: 1,
    ...overrides,
  };
}

describe("isTerminalImportStatus", () => {
  it("treats completed and failed as terminal", () => {
    expect(isTerminalImportStatus("completed")).toBe(true);
    expect(isTerminalImportStatus("failed")).toBe(true);
  });

  it("keeps pending and processing non-terminal", () => {
    expect(isTerminalImportStatus("pending")).toBe(false);
    expect(isTerminalImportStatus("processing")).toBe(false);
  });
});

describe("isActiveImportStatus", () => {
  it("marks pending and processing as active", () => {
    expect(isActiveImportStatus("pending")).toBe(true);
    expect(isActiveImportStatus("processing")).toBe(true);
    expect(isActiveImportStatus("completed")).toBe(false);
    expect(isActiveImportStatus("failed")).toBe(false);
  });
});

describe("importStatusLabel", () => {
  it("maps known statuses to Japanese labels", () => {
    expect(importStatusLabel("pending")).toBe("待機中");
    expect(importStatusLabel("processing")).toBe("解析中");
    expect(importStatusLabel("completed")).toBe("完了");
    expect(importStatusLabel("failed")).toBe("失敗");
  });

  it("falls back to the raw value for unknown statuses", () => {
    expect(importStatusLabel("unknown")).toBe("unknown");
  });
});

describe("formatImportSummary", () => {
  it("reports detections and duplicate skips", () => {
    const text = formatImportSummary(makeJob());
    expect(text).toContain("検出 3 件");
    expect(text).toContain("重複スキップ 1 件");
    expect(text).toContain("1200 フレーム");
  });

  it("mentions zero detections for a completed job without landings", () => {
    const text = formatImportSummary(
      makeJob({
        status: "completed",
        landings_detected: 0,
        duplicates_skipped: 0,
      }),
    );
    expect(text).toContain("検出 0 件");
    expect(text).not.toContain("重複");
  });
});
