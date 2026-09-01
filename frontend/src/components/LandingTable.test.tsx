/**
 * @vitest-environment jsdom
 *
 * A row can arrive straight off the WebSocket as a partial payload. If any
 * cell throws while rendering it, React unmounts the whole tree and the
 * dashboard goes blank -- which is what an ACMI import did, because the
 * broadcast payload carried no `score` and the cell called `.toFixed()` on
 * `undefined`.
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LandingTable } from "./LandingTable";
import type { LandingSummary } from "../types/api";

function row(overrides: Partial<LandingSummary> = {}): LandingSummary {
  return {
    id: 1,
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
    created_at: "2026-08-26T16:04:37.812390",
    source_id: "default",
    source_name: "default",
    ...overrides,
  } as LandingSummary;
}

describe("LandingTable", () => {
  it("links each row to its detail sheet in a new tab", () => {
    // Navigating this tab away would throw away what is on screen (e.g. an
    // import's temporary results), so the detail must open in a new tab.
    const { container } = render(<LandingTable items={[row({ id: 7 })]} />);
    const link = container.querySelector("tbody .col-id a.row-link");
    expect(link).not.toBeNull();
    expect(link?.getAttribute("href")).toBe("#/landings/7");
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(link?.getAttribute("rel")).toContain("noopener");
  });

  it("renders a row whose score never arrived", () => {
    const partial = row();
    delete (partial as { score?: number | null }).score;
    const { container } = render(
      <LandingTable items={[partial]} />,
    );
    expect(container.querySelectorAll("tbody tr")).toHaveLength(1);
  });

  it("shows the approach pattern with the class the stylesheet keys on", () => {
    // `pattern-badge ${value}` produced "overhead", which matches no rule and
    // renders an unstyled badge -- the detail page shipped that way.
    const { container } = render(
      <LandingTable items={[row({ approach_pattern: "overhead" })]} />,
    );
    const badge = container.querySelector(".pattern-badge");
    expect(badge?.className).toContain("pattern-overhead");
    expect(badge?.textContent).toBe("オーバーヘッド");
  });

  it("sorts on a header click and toggles direction on the second", () => {
    const seen: string[] = [];
    const { container, rerender } = render(
      <LandingTable
        items={[row()]}
        sort="time"
        order="desc"
        onSort={(k) => seen.push(k)}
      />,
    );
    const headers = Array.from(container.querySelectorAll("th"));
    const scoreHeader = headers.find((h) => h.textContent?.startsWith("評点"));
    (scoreHeader as HTMLElement).click();
    expect(seen).toEqual(["score"]);
    // The active column shows which way it is pointing.
    expect(headers[1].getAttribute("aria-sort")).toBe("descending");
    rerender(
      <LandingTable
        items={[row()]}
        sort="time"
        order="asc"
        onSort={() => {}}
      />,
    );
    expect(
      Array.from(container.querySelectorAll("th"))[1].getAttribute("aria-sort"),
    ).toBe("ascending");
  });

  it("leaves headers inert when no sort handler is given", () => {
    const { container } = render(<LandingTable items={[row()]} />);
    expect(container.querySelector("th.sortable")).toBeNull();
  });

  it("shows the real recording time, not the mission clock", () => {
    // created_at is naive UTC; the mission is set in June 2026 inside the
    // .miz, so showing touchdown_epoch here reads as a wrong date.
    const { container } = render(
      <LandingTable items={[row()]} />,
    );
    const cell = container.querySelectorAll("tbody td")[1];
    expect(cell.textContent).toContain("2026/08/2");
    expect(cell.getAttribute("title")).toContain("ミッション時刻");
  });
});

describe("LandingTable columns", () => {
  it("tags every cell with its column so narrow screens can drop the right one", () => {
    // The narrow-screen rules hide 'source' and 'kind'. Keyed on nth-child
    // they would hide whatever happened to sit in that position after the
    // next column is added.
    const { container } = render(
      <LandingTable items={[row()]} />,
    );
    for (const key of ["id", "time", "pilot", "grade", "score", "source", "kind"]) {
      expect(
        container.querySelector(`tbody .col-${key}`),
        `missing .col-${key}`,
      ).not.toBeNull();
      expect(container.querySelector(`thead .col-${key}`)).not.toBeNull();
    }
  });

  it("keeps the table inside a scroller rather than making it a block", () => {
    // `display: block` on a <table> drops table layout and the body columns
    // stop lining up with the header.
    const { container } = render(
      <LandingTable items={[row()]} />,
    );
    expect(container.querySelector(".table-scroll > table.landing-table")).not.toBeNull();
  });
});
