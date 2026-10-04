/**
 * @vitest-environment jsdom
 *
 * An uploaded recording is usually from another server or another day, so it
 * is deliberately kept out of the shared history. That left it with nowhere
 * to appear at all -- the source dropdown does not list import sources
 * either -- so the panel shows its own results in their own block.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

const listLandings = vi.fn();
const importAcmiFile = vi.fn();
const getImport = vi.fn();
const discardImport = vi.fn();
const discardImportOnUnload = vi.fn();

vi.mock("../api/client", () => ({
  listLandings: (...a: unknown[]) => listLandings(...a),
  importAcmiFile: (...a: unknown[]) => importAcmiFile(...a),
  getImport: (...a: unknown[]) => getImport(...a),
  discardImport: (...a: unknown[]) => discardImport(...a),
  discardImportOnUnload: (...a: unknown[]) => discardImportOnUnload(...a),
}));

const { ImportPanel } = await import("./ImportPanel");

function landing(id: number) {
  return {
    id,
    flight_id: 1,
    kind: "land",
    outcome: "full_stop",
    outcome_status: "final",
    venue_name: null,
    pilot: "someone",
    airframe: "F-16C_50",
    touchdown_time: 100,
    touchdown_epoch: 1781184699,
    grade: "B",
    score: 86.4,
    created_at: "2026-08-27T16:04:37",
    source_id: "import:job-1",
    source_name: "import:job-1",
  };
}

describe("ImportPanel results", () => {
  beforeEach(() => {
    listLandings.mockReset();
    importAcmiFile.mockReset();
    getImport.mockReset();
    discardImport.mockReset();
    discardImportOnUnload.mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("lists what an import found, scoped to that import's source", async () => {
    importAcmiFile.mockResolvedValue({
      id: "job-1",
      filename: "a.acmi",
      status: "pending",
    });
    getImport.mockResolvedValue({
      id: "job-1",
      filename: "a.acmi",
      status: "completed",
      created_at: "2026-08-27T00:00:00Z",
      frames_processed: 10,
      total_frames: 10,
      progress_percent: 100,
      landings_detected: 2,
      duplicates_skipped: 0,
    });
    listLandings.mockResolvedValue({
      items: [landing(1), landing(2)],
      total: 2,
      limit: 200,
      offset: 0,
    });

    const { container } = render(<ImportPanel />);
    // fireEvent, not .click(): a raw DOM click does not flush React state,
    // so the collapsed panel never opens and the dropzone is not rendered.
    fireEvent.click(screen.getByRole("button", { name: /インポート/ }));
    // Driven through drop rather than the file input: jsdom will not let a
    // FileList be assigned to <input type=file>.
    const dropzone = container.querySelector(".import-dropzone") as HTMLElement;
    fireEvent.drop(dropzone, {
      dataTransfer: { files: [new File(["x"], "a.acmi")] },
    });

    await waitFor(
      () => expect(container.querySelector(".import-results")).not.toBeNull(),
      { timeout: 4000 },
    );
    expect(listLandings).toHaveBeenCalledWith({ source: "import:job-1" }, 200, 0);
    expect(container.querySelectorAll(".import-results tbody tr")).toHaveLength(2);
  });

  it.each(["pending", "processing", "completed", "failed"])(
    "discards the current %s job on pagehide and removes the listener on unmount",
    async (status) => {
      vi.useFakeTimers();
      importAcmiFile.mockResolvedValue({
        id: "job-1", filename: "a.acmi", status: "pending",
      });
      getImport.mockResolvedValue({
        id: "job-1", filename: "a.acmi", status,
        created_at: "2026-08-27T00:00:00Z",
        frames_processed: 10, total_frames: 20, progress_percent: 50,
        landings_detected: 0, duplicates_skipped: 0,
      });
      listLandings.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
      const { container, unmount } = render(<ImportPanel />);
      fireEvent(window, new Event("pagehide"));
      expect(discardImportOnUnload).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: /インポート/ }));
      await act(async () => {
        fireEvent.drop(container.querySelector(".import-dropzone") as HTMLElement, {
          dataTransfer: { files: [new File(["x"], "a.acmi")] },
        });
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      expect(container.querySelector(`.status-${status}`)).not.toBeNull();
      fireEvent(window, new Event("pagehide"));
      expect(discardImportOnUnload).toHaveBeenCalledTimes(1);
      expect(discardImportOnUnload).toHaveBeenCalledWith("job-1");
      expect(discardImport).not.toHaveBeenCalled();
      unmount();
      fireEvent(window, new Event("pagehide"));
      expect(discardImportOnUnload).toHaveBeenCalledTimes(1);
    },
  );
});
