/**
 * Минимальный SSE-клиент (Server-Sent Events) на голом node:http.
 *
 * Python отдаёт поток событий в формате:
 *     data: {"type":"state","value":"speaking"}\n\n
 *     : ping\n\n              ← keep-alive, его надо игнорировать
 *
 * Библиотеки не нужны: протокол умещается в 40 строк, а лишняя зависимость
 * в main-процессе — лишний риск при сборке.
 */

import http from "node:http";
import { log } from "./log";

const MIN_DELAY = 400;
const MAX_DELAY = 5000;

export class SseClient {
  private req: http.ClientRequest | null = null;
  private stopped = true;
  private delay = MIN_DELAY;
  private retryTimer: NodeJS.Timeout | null = null;
  private buffer = "";
  private connected = false;

  constructor(
    private readonly url: string,
    private readonly onEvent: (event: unknown) => void,
    private readonly onOpenChange?: (open: boolean) => void,
  ) {}

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.setConnected(false);
    this.req?.destroy();
    this.req = null;
  }

  private setConnected(value: boolean): void {
    if (this.connected === value) return;
    this.connected = value;
    this.onOpenChange?.(value);
  }

  private connect(): void {
    if (this.stopped) return;
    this.buffer = "";

    const req = http.get(
      this.url,
      {
        agent: false, // без keep-alive: переподключение должно быть чистым
        headers: { Accept: "text/event-stream", "Cache-Control": "no-cache" },
      },
      (res) => {
        if (res.statusCode !== 200) {
          log("warn", `SSE: неожиданный статус ${res.statusCode}`);
          res.resume();
          this.scheduleRetry();
          return;
        }
        res.setEncoding("utf8");
        this.delay = MIN_DELAY;
        this.setConnected(true);
        res.on("data", (chunk: string) => this.feed(chunk));
        res.on("end", () => this.scheduleRetry());
        res.on("error", () => this.scheduleRetry());
      },
    );

    req.on("error", (error) => {
      log("debug", `SSE: ${error.message}`);
      this.scheduleRetry();
    });
    // Поток живёт, пока жив Python: свой таймаут только мешает.
    req.setTimeout(0);
    this.req = req;
  }

  private scheduleRetry(): void {
    if (this.stopped || this.retryTimer) return;
    this.setConnected(false);
    this.req?.destroy();
    this.req = null;
    const wait = this.delay;
    this.delay = Math.min(MAX_DELAY, Math.round(this.delay * 1.7));
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, wait);
  }

  /** Склеивает чанки и разбирает готовые блоки, разделённые пустой строкой. */
  private feed(chunk: string): void {
    this.buffer += chunk;
    let index = this.buffer.indexOf("\n\n");
    while (index !== -1) {
      const block = this.buffer.slice(0, index);
      this.buffer = this.buffer.slice(index + 2);
      this.parseBlock(block);
      index = this.buffer.indexOf("\n\n");
    }
    // Защита от мусора: если Python замолчал на середине строки — не копим вечно.
    if (this.buffer.length > 1_000_000) this.buffer = "";
  }

  private parseBlock(block: string): void {
    for (const rawLine of block.split("\n")) {
      const line = rawLine.trimEnd();
      if (!line || line.startsWith(":")) continue; // комментарий/keep-alive
      if (!line.startsWith("data:")) continue;
      const payload = line.slice(5).trim();
      if (!payload) continue;
      try {
        this.onEvent(JSON.parse(payload));
      } catch {
        log("debug", `SSE: не разобрал JSON: ${payload.slice(0, 120)}`);
      }
    }
  }
}
