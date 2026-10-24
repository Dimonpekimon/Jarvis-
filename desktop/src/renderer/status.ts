/** Подписи и цвета состояний — в одном месте, чтобы UI не расходился. */

import type { AssistantState } from "../shared/ipc";

export const STATE_LABEL: Record<AssistantState, string> = {
  idle: "Готов",
  listening: "Слушаю",
  thinking: "Думаю",
  speaking: "Говорю",
  loading: "Загрузка",
  offline: "Ядро не на связи",
};

export const STATE_COLOR: Record<AssistantState, string> = {
  idle: "#5b7391",
  listening: "#2ad4ff",
  thinking: "#ffb545",
  speaking: "#8f7bff",
  loading: "#8b8f9c",
  offline: "#b06a78",
};

export const ROLE_LABEL: Record<string, string> = {
  user: "Вы",
  assistant: "Пятница",
  system: "Система",
};
