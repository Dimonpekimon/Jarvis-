/**
 * Сайдкар: Python-ядро ассистента как дочерний процесс.
 *
 * Electron не переписывает Python — он его запускает и общается по HTTP.
 * Если Python не установлен, приложение не падает: остаётся оверлей-заглушка.
 */

import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { log } from "./log";
import { pythonCandidates, pythonRoot, sidecarEnabled, type PythonCommand } from "./config";
import type { SidecarStatus } from "../shared/ipc";

/** Модуль, по которому проверяем, что у интерпретатора есть зависимости проекта. */
const PROBE_MODULE = "requests";

export class Sidecar {
  private child: ChildProcess | null = null;
  private stopping = false;
  private candidateIndex = 0;
  private failures = 0;
  private restartTimer: NodeJS.Timeout | null = null;

  constructor(private readonly onStatus: (status: SidecarStatus) => void) {}

  get running(): boolean {
    return this.child !== null;
  }

  start(): void {
    if (!sidecarEnabled()) {
      this.onStatus({ state: "offline", detail: "сайдкар отключён (PYATNITSA_NO_SIDECAR=1)" });
      return;
    }
    if (this.child) return;
    this.stopping = false;
    this.spawnCandidate();
  }

  /**
   * Проверяет, что интерпретатор видит зависимости проекта.
   *
   * Без этой проверки в PATH легко поймать «чужой» python: сайдкар падает на
   * `import requests`, Electron перезапускает его по кругу, и в интерфейсе
   * просто «ядро не на связи» без внятной причины.
   */
  private probe(candidate: PythonCommand): boolean {
    const result = spawnSync(
      candidate.command,
      [...candidate.prefix, "-c", `import ${PROBE_MODULE}`],
      { cwd: pythonRoot(), timeout: 8000, windowsHide: true, encoding: "utf8" },
    );
    if (result.error) {
      log("debug", `Проба «${candidate.command}»: ${result.error.message}`);
      return false;
    }
    if (result.status !== 0) {
      log("debug", `«${candidate.command}» не видит ${PROBE_MODULE}: ${(result.stderr || "").trim().split("\n").pop()}`);
      return false;
    }
    return true;
  }

  /** Пробует кандидатов по очереди: явный путь → venv → python → py -3. */
  private spawnCandidate(): void {
    const candidates = pythonCandidates();
    const total = candidates.length;

    let chosen: PythonCommand | null = null;
    while (this.candidateIndex < total) {
      const candidate = candidates[this.candidateIndex];
      if (this.probe(candidate)) {
        chosen = candidate;
        break;
      }
      this.candidateIndex += 1;
    }

    if (!chosen) {
      // Ни один не подошёл — запускаем последнего, чтобы в логе был настоящий
      // traceback Python, а не только наше «не нашёл».
      chosen = candidates[candidates.length - 1];
      log("error", `Ни один интерпретатор не видит «${PROBE_MODULE}». Поставьте зависимости: pip install -r requirements.txt`);
    }

    const command = chosen.command;
    const args = [...chosen.prefix, ...chosen.args];
    const cwd = pythonRoot();
    log("info", `Запускаю сайдкар: ${command} ${args.join(" ")} (cwd=${cwd})`);
    this.onStatus({ state: "starting", detail: `${command} ${args[0]}` });

    let settled = false;
    let child: ChildProcess;
    try {
      child = spawn(command, args, {
        cwd,
        windowsHide: true,
        // stdin — открытый канал, а не /dev/null: в режиме --mode text ядро
        // читает input(), и на закрытом stdin получало бы EOF в цикле,
        // крутя процессор на 100 %.
        stdio: ["pipe", "pipe", "pipe"],
        env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
      });
    } catch (error) {
      log("error", `spawn упал: ${String(error)}`);
      this.candidateIndex += 1;
      this.spawnCandidate();
      return;
    }

    this.child = child;

    child.on("spawn", () => {
      settled = true;
      this.failures = 0;
      log("info", `Сайдкар запущен, pid=${child.pid}`);
      // «ready» объявит main после успешного /api/health.
    });

    child.on("error", (error) => {
      if (settled) {
        log("error", `Ошибка сайдкара: ${error.message}`);
        return;
      }
      // ENOENT — этого интерпретатора нет, пробуем следующего кандидата.
      log("warn", `Не удалось запустить «${command}»: ${error.message}`);
      this.child = null;
      this.candidateIndex += 1;
      this.spawnCandidate();
    });

    child.stdout?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => this.forward(chunk, "out"));
    child.stderr?.setEncoding("utf8");
    child.stderr?.on("data", (chunk: string) => this.forward(chunk, "err"));

    child.on("exit", (code, signal) => {
      this.child = null;
      if (this.stopping) {
        log("info", "Сайдкар остановлен");
        return;
      }
      log("warn", `Сайдкар завершился: code=${code} signal=${signal}`);
      this.failures += 1;
      if (this.failures > 3) {
        this.onStatus({ state: "error", detail: `Python падает (код ${code})` });
        return;
      }
      this.onStatus({ state: "offline", detail: "перезапуск…" });
      this.restartTimer = setTimeout(() => {
        this.restartTimer = null;
        if (!this.stopping) this.spawnCandidate();
      }, 3000);
    });
  }

  private forward(chunk: string, stream: "out" | "err"): void {
    for (const line of chunk.split(/\r?\n/)) {
      const text = line.trimEnd();
      if (!text) continue;
      if (stream === "err" && /traceback|error|ошибк/i.test(text)) log("warn", `py: ${text}`);
      else log("debug", `py: ${text}`);
    }
  }

  stop(): void {
    this.stopping = true;
    if (this.restartTimer) {
      clearTimeout(this.restartTimer);
      this.restartTimer = null;
    }
    const child = this.child;
    if (!child?.pid) return;
    this.child = null;
    log("info", `Останавливаю сайдкар pid=${child.pid}`);
    if (process.platform === "win32") {
      // Python мог запустить потоки/подпроцессы — валим всё дерево.
      spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], { windowsHide: true });
    } else {
      child.kill("SIGTERM");
    }
  }
}
