/**
 * Визуализатор в стиле Winamp: столбики на прозрачном фоне.
 *
 * Данные берутся из двух источников, и это принципиально:
 *  1) AnalyserNode — когда Electron сам проигрывает WAV (тест или кэш Piper);
 *  2) levelRef     — громкость из SSE-событий Python (source: "tts").
 * Второй нужен потому, что по умолчанию фразу озвучивает Python, а не Electron:
 * так визуализация работает вообще без доступа к аудиопотоку.
 */

import { useEffect, useRef } from "react";
import type { JSX, RefObject } from "react";
import type { AssistantState } from "../../shared/ipc";
import type { AudioEngine } from "../audio";

interface Props {
  engine: AudioEngine;
  /** 0..1 — уровень громкости из событий ядра. */
  levelRef: RefObject<number>;
  state: AssistantState;
}

interface Palette {
  low: string;
  mid: string;
  high: string;
  glow: string;
}

const PALETTE: Record<AssistantState, Palette> = {
  idle: { low: "#2b3a4d", mid: "#4a6a86", high: "#7fa7c4", glow: "#4a90c2" },
  listening: { low: "#0f5f74", mid: "#1fb6d8", high: "#8ef0ff", glow: "#2ad4ff" },
  thinking: { low: "#6b4a10", mid: "#e0a028", high: "#ffe08a", glow: "#ffb545" },
  speaking: { low: "#2c2a6b", mid: "#7c5cff", high: "#c9b6ff", glow: "#8f7bff" },
  loading: { low: "#3a3a44", mid: "#6f7280", high: "#b9bcc7", glow: "#8b8f9c" },
  offline: { low: "#33262a", mid: "#6b4a52", high: "#a98d95", glow: "#8a5f6a" },
};

const GAP = 3; // расстояние между столбиками, css-пиксели
const PADDING = 10;

export default function Visualizer({ engine, levelRef, state }: Props): JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const stateRef = useRef<AssistantState>(state);
  stateRef.current = state;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d", { alpha: true });
    if (!ctx) return;

    let width = 0;
    let height = 0;
    let dpr = Math.min(window.devicePixelRatio || 1, 2);

    let values = new Float32Array(0);
    let peaks = new Float32Array(0);
    let frame = 0;
    let last = performance.now();

    const resize = (): void => {
      const rect = canvas.getBoundingClientRect();
      width = Math.max(1, rect.width);
      height = Math.max(1, rect.height);
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    resize();

    const draw = (now: number): void => {
      frame = requestAnimationFrame(draw);
      // Ограничиваем шаг: после сворачивания окна delta может быть огромной.
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;

      const palette = PALETTE[stateRef.current] ?? PALETTE.idle;
      const barCount = Math.max(14, Math.min(56, Math.floor((width - PADDING * 2) / 8)));
      if (values.length !== barCount) {
        values = new Float32Array(barCount);
        peaks = new Float32Array(barCount);
      }

      const spectrum = engine.playing ? engine.spectrum() : null;
      const level = Math.max(0, Math.min(1, levelRef.current ?? 0));
      const time = now / 1000;
      const usable = height - PADDING * 2;

      for (let i = 0; i < barCount; i += 1) {
        const t = barCount === 1 ? 0 : i / (barCount - 1);
        let target: number;

        if (spectrum) {
          // Логарифмическая раскладка: низкие частоты шире — так читается речь.
          const bins = Math.min(spectrum.length, 512);
          const bin = Math.floor(2 + Math.pow(t, 1.9) * (bins - 3));
          let peakValue = 0;
          for (let k = bin; k < Math.min(bin + 2, bins); k += 1) {
            if (spectrum[k] > peakValue) peakValue = spectrum[k];
          }
          target = Math.pow(peakValue / 255, 1.35);
        } else if (level > 0.015) {
          // Один общий уровень → синтетический «спектр» с живым дрожанием.
          const shape = 0.5 + 0.5 * Math.sin(Math.PI * (0.12 + 0.88 * t));
          const jitter = 0.62 + 0.38 * Math.sin(time * 3.1 + i * 1.7) * Math.cos(time * 1.3 + i * 0.9);
          target = level * shape * jitter;
        } else {
          // Почти невидимое «дыхание», чтобы оверлей не выглядел мёртвым.
          target = 0.01 + 0.012 * Math.sin(time * 1.5 + i * 0.45);
        }

        const attack = target > values[i] ? 0.55 : 0.16;
        values[i] += (target - values[i]) * Math.min(1, attack + dt * 4);
        if (values[i] > peaks[i]) peaks[i] = values[i];
        else peaks[i] = Math.max(values[i], peaks[i] - dt * 0.55);
      }

      ctx.clearRect(0, 0, width, height);

      const innerWidth = width - PADDING * 2;
      const barWidth = Math.max(2, (innerWidth - GAP * (barCount - 1)) / barCount);
      const gradient = ctx.createLinearGradient(0, height - PADDING, 0, PADDING);
      gradient.addColorStop(0, palette.low);
      gradient.addColorStop(0.55, palette.mid);
      gradient.addColorStop(1, palette.high);

      // Мягкое свечение под столбиками — рисуется один раз, дешевле shadowBlur.
      let average = 0;
      for (let i = 0; i < barCount; i += 1) average += values[i];
      average /= barCount;
      if (average > 0.02) {
        const glow = ctx.createLinearGradient(0, height, 0, height * 0.45);
        glow.addColorStop(0, `${palette.glow}44`);
        glow.addColorStop(1, "transparent");
        ctx.globalAlpha = Math.min(0.85, average * 1.8);
        ctx.fillStyle = glow;
        ctx.fillRect(0, height * 0.45, width, height * 0.55);
        ctx.globalAlpha = 1;
      }

      ctx.fillStyle = gradient;
      for (let i = 0; i < barCount; i += 1) {
        const barHeight = Math.max(2, values[i] * usable);
        const x = PADDING + i * (barWidth + GAP);
        const y = height - PADDING - barHeight;
        ctx.beginPath();
        ctx.roundRect(x, y, barWidth, barHeight, Math.min(barWidth / 2, 3));
        ctx.fill();
      }

      // «Шапки» пиков — классическая деталь Winamp.
      ctx.fillStyle = palette.high;
      ctx.globalAlpha = 0.9;
      for (let i = 0; i < barCount; i += 1) {
        const peakY = height - PADDING - Math.max(2, peaks[i] * usable) - 3;
        const x = PADDING + i * (barWidth + GAP);
        ctx.fillRect(x, peakY, barWidth, 1.5);
      }
      ctx.globalAlpha = 1;
    };

    frame = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [engine, levelRef]);

  return <canvas ref={canvasRef} className="visualizer" aria-hidden="true" />;
}
