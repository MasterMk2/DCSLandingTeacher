/**
 * TypeScript types mirroring backend/app/api/schemas.py.
 * Keep in sync with the Pydantic response models.
 */

export type LandingKind = "carrier" | "land";
export type LandingOutcome = "full_stop" | "touch_and_go" | "bolter";
/**
 * Two-phase confirmation (Issue #5): "provisional" while the outcome is
 * still under observation (bolter / touch-and-go dwell), "final" once set.
 */
export type OutcomeStatus = "provisional" | "final";

/** LandingSummary (GET /api/landings rows, WS notification payload). */
export interface LandingSummary {
  id: number;
  flight_id: number;
  kind: string | null;
  outcome: string | null;
  outcome_status?: OutcomeStatus;
  venue_name: string | null;
  pilot: string | null;
  airframe: string | null;
  /** Epoch seconds of touchdown. */
  touchdown_time: number | null;
  grade: string | null;
  score: number | null;
  /** ISO-8601 datetime string. */
  created_at: string | null;
}

/** FactorOut. */
export interface Factor {
  name: string;
  severity?: string | null;
  evidence?: Record<string, unknown> | null;
}

/** DeviationSampleOut. Units: meters, m/s, degrees. */
export interface DeviationSample {
  time: number;
  distance_to_go: number;
  glideslope_deviation?: number | null;
  centerline_deviation?: number | null;
  speed?: number | null;
  aoa?: number | null;
  agl?: number | null;
}

/** ApproachTrackOut. */
export interface ApproachTrack {
  kind?: string | null;
  outcome?: string | null;
  glideslope_deg?: number | null;
  course_deg?: number | null;
  touchdown_time?: number | null;
  samples: DeviationSample[];
}

/** TouchdownState. */
export interface TouchdownState {
  latitude?: number | null;
  longitude?: number | null;
  altitude?: number | null;
  heading?: number | null;
  speed_ms?: number | null;
  descent_rate_ms?: number | null;
}

/** LandingDetail (GET /api/landings/{id}). */
export interface LandingDetail extends LandingSummary {
  carrier_object_id?: number | null;
  comment?: string | null;
  factors: Factor[];
  metrics?: Record<string, unknown> | null;
  grading_version?: string | null;
  graded_at?: string | null;
  touchdown?: TouchdownState | null;
  approach_track?: ApproachTrack | null;
}

/** LandingListResponse. */
export interface LandingListResponse {
  items: LandingSummary[];
  total: number;
  limit: number;
  offset: number;
}

/** Query filters accepted by GET /api/landings. */
export interface LandingFilters {
  player?: string;
  airframe?: string;
  venue?: string;
  kind?: string;
  grade?: string;
  outcome?: string;
  date_from?: string;
  date_to?: string;
}

/**
 * WS messages pushed by /api/ws/landings:
 * - {"type":"landing","landing":{...}}        new landing (may be provisional)
 * - {"type":"landing_update","landing":{...}} provisional landing confirmed
 * - {"type":"import","import":{...}}          ACMI import job state change
 */
export interface WsLandingMessage {
  type: "landing" | "landing_update";
  landing: Partial<LandingSummary>;
}

/** ImportJobOut (GET /api/imports, GET /api/imports/{id}, WS "import"). */
export interface ImportJob {
  id: string;
  filename: string;
  /** "pending" | "processing" | "completed" | "failed" */
  status: string;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  frames_processed: number;
  landings_detected: number;
  duplicates_skipped: number;
  error?: string | null;
}

/** Acknowledgement returned by POST /api/import. */
export interface ImportStartResponse {
  id: string;
  filename: string;
  status: string;
}
