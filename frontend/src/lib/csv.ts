/** CSV generation (frontend-side; the API has no CSV endpoint yet). */

function escapeCell(value: unknown): string {
  if (value === null || value === undefined) return "";
  const s = String(value);
  if (/[",\r\n]/.test(s)) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

export function toCsv(headers: string[], rows: unknown[][]): string {
  const lines = [headers.map(escapeCell).join(",")];
  for (const row of rows) {
    lines.push(row.map(escapeCell).join(","));
  }
  return lines.join("\r\n");
}

/** Prepend a UTF-8 BOM so Excel opens Japanese text correctly. */
export function withBom(csv: string): string {
  return `\uFEFF${csv}`;
}

type SummaryRow = {
  id: number;
  kind: string | null;
  outcome: string | null;
  venue_name: string | null;
  pilot: string | null;
  airframe: string | null;
  touchdown_time: number | null;
  grade: string | null;
  score: number | null;
};

const SUMMARY_HEADERS = [
  "id",
  "kind",
  "outcome",
  "venue_name",
  "pilot",
  "airframe",
  "touchdown_time_epoch_s",
  "grade",
  "score",
];

export function landingsToCsv(items: SummaryRow[]): string {
  return toCsv(
    SUMMARY_HEADERS,
    items.map((it) => [
      it.id,
      it.kind ?? "",
      it.outcome ?? "",
      it.venue_name ?? "",
      it.pilot ?? "",
      it.airframe ?? "",
      it.touchdown_time ?? "",
      it.grade ?? "",
      it.score ?? "",
    ]),
  );
}

type SampleRow = {
  time: number;
  distance_to_go: number;
  glideslope_deviation?: number | null;
  centerline_deviation?: number | null;
  speed?: number | null;
  aoa?: number | null;
  agl?: number | null;
};

const SAMPLE_HEADERS = [
  "time_s",
  "distance_to_go_m",
  "glideslope_deviation_m",
  "centerline_deviation_m",
  "speed_ms",
  "aoa_deg",
  "agl_m",
];

export function samplesToCsv(samples: SampleRow[]): string {
  return toCsv(
    SAMPLE_HEADERS,
    samples.map((s) => [
      s.time,
      s.distance_to_go,
      s.glideslope_deviation ?? "",
      s.centerline_deviation ?? "",
      s.speed ?? "",
      s.aoa ?? "",
      s.agl ?? "",
    ]),
  );
}

/** Trigger a browser download of CSV text. */
export function downloadCsv(filename: string, csv: string): void {
  const blob = new Blob([withBom(csv)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
