/**
 * Корень приложения.
 *
 * Задачи компонента:
 *  - подписаться на события ядра и разложить их по состоянию;
 *  - держать два режима: обычный/полупрозрачный и компактный/диалог;
 *  - включать и выключать click-through по положению курсора.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { JSX } from "react";
import type {
  AssistantState,
  BusEvent,
  ChatMessage,
  OverlayMode,
  SidecarStatus,
  UiMode,
} from "../shared/ipc";
import { audioEngine } from "./audio";
import DialogPanel from "./components/DialogPanel";
import Visualizer from "./components/Visualizer";

const MAX_MESSAGES = 80;

/** Новый id для реплики: ключ React + порядок в списке. */
function newId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
}

/**
 * Click-through.
 *
 * Окно прозрачное и «сквозное», поэтому клики уходят на рабочий стол. Но
 * mousemove приходит даже в сквозном режиме (setIgnoreMouseEvents с
 * forward: true) — по нему мы определяем, попал ли курсор в интерактивную
 * зону (атрибут data-hit), и только тогда возвращаем окну способность
 * принимать мышь.
 */
function useClickThrough(): void {
  useEffect(() => {
    let ignoring: boolean | null = null;

    const apply = (value: boolean): void => {
      if (value === ignoring) return; // не спамим IPC на каждое движение
      ignoring = value;
      window.pyatnitsa.setIgnoreMouse(value);
    };

    const onMove = (event: MouseEvent): void => {
      const element = document.elementFromPoint(event.clientX, event.clientY);
      const interactive = element instanceof Element && element.closest("[data-hit]") !== null;
      apply(!interactive);
    };

    const onLeave = (): void => apply(true);

    window.addEventListener("mousemove", onMove, { passive: true });
    window.addEventListener("mouseleave", onLeave);
    ignoring = true; // main создаёт окно уже сквозным

    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseleave", onLeave);
    };
  }, []);
}

export default function App(): JSX.Element {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [state, setState] = useState<AssistantState>("offline");
  const [sidecar, setSidecar] = useState<SidecarStatus>({ state: "starting" });
  const [overlayMode, setOverlayMode] = useState<OverlayMode>("normal");
  const [uiMode, setUiMode] = useState<UiMode>("compact");
  const [audible, setAudible] = useState<boolean>(
    () => window.localStorage.getItem("pyatnitsa.audible") === "1",
  );

  const levelRef = useRef(0);
  const stateRef = useRef<AssistantState>(state);
  stateRef.current = state;
  const audibleRef = useRef(audible);
  audibleRef.current = audible;

  useClickThrough();

  /** Просит у main настоящий WAV из кэша Piper и гоняет его через анализатор. */
  const playAssistantAudio = useCallback(async (text: string): Promise<void> => {
    const payload = await window.pyatnitsa.playTts(text);
    if (!payload) return; // не нашлось — визуализатор останется на уровнях из SSE
    try {
      await audioEngine.playBase64(payload.base64, { silent: !audibleRef.current });
    } catch {
      /* битый файл не должен ломать интерфейс */
    }
  }, []);

  const handleEvent = useCallback(
    (event: BusEvent): void => {
      switch (event.type) {
        case "message": {
          const text = (event.text ?? "").trim();
          if (!text) break;
          const role = (event.role ?? "assistant") as ChatMessage["role"];
          setMessages((prev) => [...prev, { id: newId(), role, text, at: Date.now() }].slice(-MAX_MESSAGES));
          if (role === "assistant") void playAssistantAudio(text);
          break;
        }
        case "state": {
          if (typeof event.value === "string") setState(event.value as AssistantState);
          break;
        }
        case "level": {
          // mic-уровни нам не нужны: микрофон рисует сам Python в своём UI.
          if (event.source === "tts" && typeof event.value === "number") {
            levelRef.current = event.value;
          }
          break;
        }
        case "hello": {
          if (event.state) setState(event.state);
          break;
        }
        default:
          break;
      }
    },
    [playAssistantAudio],
  );

  useEffect(() => {
    void window.pyatnitsa.getConfig().then((config) => {
      setOverlayMode(config.overlayMode);
      setUiMode(config.uiMode);
    });

    const offEvent = window.pyatnitsa.onEvent(handleEvent);
    const offSidecar = window.pyatnitsa.onSidecar(setSidecar);
    const offUi = window.pyatnitsa.onUiMode(setUiMode);
    const offOverlay = window.pyatnitsa.onOverlayMode(setOverlayMode);

    return () => {
      offEvent();
      offSidecar();
      offUi();
      offOverlay();
    };
  }, [handleEvent]);

  // Клавиатура: Esc — прервать речь или свернуть диалог.
  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key !== "Escape") return;
      const current = stateRef.current;
      if (current === "speaking" || current === "thinking") {
        window.pyatnitsa.interrupt();
      } else if (uiMode === "dialog") {
        window.pyatnitsa.setUiMode("compact");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [uiMode]);

  const submit = useCallback(async (text: string): Promise<void> => {
    // Реплику пользователя пришлёт само ядро событием message — не дублируем.
    const result = await window.pyatnitsa.sendMessage(text);
    if (!result.ok) {
      setMessages((prev) =>
        [...prev, { id: newId(), role: "system" as const, text: `Не отправлено: ${result.error ?? "ошибка"}`, at: Date.now() }].slice(-MAX_MESSAGES),
      );
    }
  }, []);

  const toggleAudible = useCallback((): void => {
    setAudible((prev) => {
      const next = !prev;
      window.localStorage.setItem("pyatnitsa.audible", next ? "1" : "0");
      return next;
    });
  }, []);

  const openDialog = useCallback((): void => {
    window.pyatnitsa.setUiMode("dialog");
  }, []);

  return (
    <div className={`app app--${uiMode} app--${overlayMode}`}>
      {/* Карточка кликабельна только в режиме диалога; в компактном — нет. */}
      <div className="card" data-hit={uiMode === "dialog" ? "1" : undefined}>
        <div className="card__viz">
          <Visualizer engine={audioEngine} levelRef={levelRef} state={state} />
        </div>

        {uiMode === "dialog" ? (
          <DialogPanel
            messages={messages}
            state={state}
            sidecar={sidecar}
            overlayMode={overlayMode}
            audible={audible}
            onSubmit={(text) => void submit(text)}
            onInterrupt={() => window.pyatnitsa.interrupt()}
            onReset={() => void window.pyatnitsa.reset()}
            onTest={() => {
              void window.pyatnitsa.playStub().then((payload) => {
                if (payload) void audioEngine.playBase64(payload.base64, { silent: !audible });
              });
            }}
            onToggleAudible={toggleAudible}
            onToggleDim={() => window.pyatnitsa.setOverlayMode(overlayMode === "dim" ? "normal" : "dim")}
            onCollapse={() => window.pyatnitsa.setUiMode("compact")}
          />
        ) : (
          <button type="button" className="expand" data-hit="1" title="Открыть диалог (Ctrl+Alt+U)" onClick={openDialog}>
            ⌃
          </button>
        )}
      </div>
    </div>
  );
}
