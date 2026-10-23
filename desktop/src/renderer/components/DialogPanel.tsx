/**
 * Диалоговая панель: история реплик + поле ввода.
 *
 * Появляется только в режиме «диалог» — в компактном окне остаются лишь
 * столбики визуализатора.
 */

import { useEffect, useRef, useState } from "react";
import type { JSX } from "react";
import type { AssistantState, ChatMessage, OverlayMode, SidecarStatus } from "../../shared/ipc";
import { ROLE_LABEL, STATE_COLOR, STATE_LABEL } from "../status";

interface Props {
  messages: ChatMessage[];
  state: AssistantState;
  sidecar: SidecarStatus;
  overlayMode: OverlayMode;
  audible: boolean;
  onSubmit: (text: string) => void;
  onInterrupt: () => void;
  onReset: () => void;
  onTest: () => void;
  onToggleAudible: () => void;
  onToggleDim: () => void;
  onCollapse: () => void;
}

export default function DialogPanel(props: Props): JSX.Element {
  const {
    messages,
    state,
    sidecar,
    overlayMode,
    audible,
    onSubmit,
    onInterrupt,
    onReset,
    onTest,
    onToggleAudible,
    onToggleDim,
    onCollapse,
  } = props;

  const [text, setText] = useState("");
  const listRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Автоскролл к последней реплике.
  useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [messages.length]);

  const send = (): void => {
    const value = text.trim();
    if (!value) return;
    setText("");
    onSubmit(value);
    inputRef.current?.focus();
  };

  const busy = state === "thinking" || state === "speaking";

  return (
    <div className="dialog">
      {/* Шапка одновременно служит ручкой для перетаскивания окна */}
      <header className="dialog__head drag" data-hit="1">
        <span className="dot" style={{ background: STATE_COLOR[state], boxShadow: `0 0 8px ${STATE_COLOR[state]}` }} />
        <span className="dialog__state">{STATE_LABEL[state]}</span>
        {sidecar.state === "error" && <span className="badge badge--warn">ядро: ошибка</span>}
        {sidecar.state === "offline" && <span className="badge">ядро: offline</span>}

        <div className="dialog__tools">
          <button
            type="button"
            className={`tool ${audible ? "tool--on" : ""}`}
            title={audible ? "Звук в Electron включён" : "Только анализ (звучит Python)"}
            onClick={onToggleAudible}
          >
            {audible ? "🔊" : "🔇"}
          </button>
          <button
            type="button"
            className={`tool ${overlayMode === "dim" ? "tool--on" : ""}`}
            title="Полупрозрачный режим (Ctrl+Alt+J)"
            onClick={onToggleDim}
          >
            ◐
          </button>
          <button type="button" className="tool" title="Свернуть в компактный вид (Ctrl+Alt+U)" onClick={onCollapse}>
            ▾
          </button>
        </div>
      </header>

      <div className="messages" ref={listRef} data-hit="1">
        {messages.length === 0 && (
          <p className="hint">
            Скажите «Джарвис…» или напишите здесь. Ответ озвучит Python, а столбики наверху покажут его спектр.
          </p>
        )}
        {messages.map((message) => (
          <div key={message.id} className={`msg msg--${message.role}`}>
            <span className="msg__role">{ROLE_LABEL[message.role] ?? message.role}</span>
            <span className="msg__text">{message.text}</span>
          </div>
        ))}
      </div>

      <footer className="dialog__foot" data-hit="1">
        <input
          ref={inputRef}
          className="input"
          value={text}
          placeholder="Команда или вопрос…"
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              send();
            }
          }}
        />
        <button type="button" className="btn btn--primary" onClick={send} title="Отправить (Enter)">
          ➤
        </button>
        <button
          type="button"
          className="btn"
          onClick={onInterrupt}
          disabled={!busy}
          title="Прервать речь (Esc)"
        >
          ■
        </button>
        <button type="button" className="btn" onClick={onTest} title="Проиграть тестовый WAV">
          ♪
        </button>
        <button type="button" className="btn" onClick={onReset} title="Сбросить контекст диалога">
          ↺
        </button>
      </footer>
    </div>
  );
}
