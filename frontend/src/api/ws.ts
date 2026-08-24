/**
 * WebSocket client for /api/ws/landings with automatic reconnection
 * and keep-alive ping (the backend answers {"type":"pong"} to "ping").
 */
import type { WsLandingMessage } from "../types/api";

const PING_INTERVAL_MS = 25_000;
const INITIAL_RETRY_MS = 1_000;
const MAX_RETRY_MS = 30_000;

export type LandingListener = (msg: WsLandingMessage) => void;

export class LandingSocket {
  private ws: WebSocket | null = null;
  private retryMs = INITIAL_RETRY_MS;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private closedByUser = false;

  constructor(private readonly listener: LandingListener) {}

  connect(): void {
    if (this.ws || this.closedByUser) return;
    // NOTE: the backend router has the global "/api" prefix, so the actual
    // WebSocket path is /api/ws/landings.
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/api/ws/landings`;
    try {
      this.ws = new WebSocket(url);
    } catch {
      this.scheduleRetry();
      return;
    }
    this.ws.onopen = () => {
      this.retryMs = INITIAL_RETRY_MS;
      this.startPing();
    };
    this.ws.onmessage = (ev: MessageEvent) => {
      try {
        const data = JSON.parse(ev.data as string) as
          | WsLandingMessage
          | { type: "pong" };
        if (data.type === "landing") this.listener(data);
      } catch {
        // Ignore malformed frames.
      }
    };
    this.ws.onclose = () => {
      this.stopPing();
      this.ws = null;
      if (!this.closedByUser) this.scheduleRetry();
    };
    this.ws.onerror = () => {
      // onclose follows; nothing else to do here.
    };
  }

  close(): void {
    this.closedByUser = true;
    this.stopPing();
    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
  }

  private startPing(): void {
    this.stopPing();
    this.pingTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send("ping");
      }
    }, PING_INTERVAL_MS);
  }

  private stopPing(): void {
    if (this.pingTimer !== null) clearInterval(this.pingTimer);
    this.pingTimer = null;
  }

  private scheduleRetry(): void {
    if (this.retryTimer !== null || this.closedByUser) return;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, this.retryMs);
    this.retryMs = Math.min(this.retryMs * 2, MAX_RETRY_MS);
  }
}
