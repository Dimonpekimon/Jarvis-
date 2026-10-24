# -*- coding: utf-8 -*-
"""Управление голосом Пятницы из командной строки.

    python voice.py                 # какие голоса есть и какой выбран
    python voice.py use irina       # скачать (если нужно) и выбрать голосом
    python voice.py use dmitri --keep-speed
    python voice.py tone confident  # характер: off | soft | confident
    python voice.py test            # синтез пробной фразы + замеры
    python voice.py test irina --play    # и сразу послушать
    python voice.py samples         # образцы всех скачанных голосов в файлы

Голос хранится в ``pyatnitsa_settings.json`` (ключ ``PIPER_VOICE``), поэтому
выбор переживает перезапуск. Уже запущенная Пятница подхватит смену голоса
при следующей фразе: загрузчик моделей сверяет путь.

Тембр (``PIPER_TONE``) — отдельный слой: те же голоса, но с обработкой
(``tone.py``). Пустое значение означает «как задумано для голоса» — у «Мари»
это ``brisk``, у остальных обработки нет.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import config
import tone
import voices

#: Фразы для сравнения голосов. Разные по характеру: приветствие, ответ с
#: числами, длинная фраза — на ней слышна разница в темпе и интонации.
SAMPLES = {
    "01-приветствие": "Добрый вечер, сэр. Системы запущены, слушаю вас.",
    "02-ответ": "В Москве плюс восемь, облачно. Завтра небольшой дождь.",
    "03-длинная": ("Курс доллара вырос на двадцать копеек и составляет "
                   "девяносто два рубля сорок копеек, сэр."),
}


def cmd_list() -> int:
    active = voices.active_name()
    print(f"Активный голос: {active}")
    print(f"Модель:         {voices.active_path()}")
    print(f"Темп:           {config.PIPER_SPEED}")
    print(f"Тембр:          {voices.tone_name()} — "
          f"{tone.describe(voices.tone_profile())}")
    print()
    print("Доступные русские голоса Piper:")
    for key in voices.VOICE_CATALOG:
        print("  " + voices.describe(key))
    print("\n  → — выбран сейчас")
    print("\nСменить голос:  python voice.py use irina")
    print("Сменить тембр:  python voice.py tone soft|confident|off")
    print("Проверить:      python voice.py test")
    return 0


def cmd_use(name: str, keep_speed: bool) -> int:
    key = str(name).strip().lower()
    if key not in voices.VOICE_CATALOG:
        print(f"Нет такого голоса: {name}")
        print("Доступны: " + ", ".join(sorted(voices.VOICE_CATALOG)))
        return 1

    if not voices.is_installed(key):
        print(f"Голос «{key}» ещё не скачан — загружаю...")
        try:
            voices.ensure(key, on_log=lambda message: print("  " + message))
        except Exception as exc:  # noqa: BLE001
            print(f"Не удалось скачать: {exc}")
            return 1

    changes = voices.set_active(key, keep_speed=keep_speed)
    info = voices.VOICE_CATALOG[key]
    print(f"Голос переключён: {info['title']} ({key}), "
          f"{'женский' if info['gender'] == 'female' else 'мужской'}")
    print(f"  PIPER_VOICE = {changes['PIPER_VOICE']}")
    print("  PIPER_MODEL = \"\" (модель берётся по имени голоса)")
    if "PIPER_SPEED" in changes:
        print(f"  PIPER_SPEED = {changes['PIPER_SPEED']} (рекомендуемый темп)")
    else:
        print(f"  PIPER_SPEED = {config.PIPER_SPEED} (оставлен прежним)")
    print(f"  Тембр: {voices.tone_name(key)} — "
          f"{tone.describe(voices.tone_profile(key))}")
    print(f"\nЗаписано в {config.SETTINGS_FILE.name}. "
          "Запущенная Пятница применит голос к следующей фразе.")
    return 0


def cmd_tone(name: str) -> int:
    """Выбор характера: off | soft | confident | auto."""
    try:
        key = voices.set_tone(name)
    except KeyError as exc:
        print(exc.args[0])
        return 1

    label = key or "auto"
    print(f"Тембр: {label} — {tone.describe(voices.tone_profile())}")
    print(f"  PIPER_TONE = \"{key}\"")
    if not key:
        print(f"  Для «{voices.active_name()}» это значит: "
              f"{voices.tone_name()} (по каталогу).")
    print("\nСтарый кэш озвучки будет очищен: голос соберётся заново.")
    try:
        import tts
        tts.ensure_tone_cache()
    except Exception as exc:  # noqa: BLE001
        print(f"  Кэш не очищен ({exc}) — удалите data/tts_cache/*.wav вручную.")
    return 0


def cmd_test(name: str | None, play: bool) -> int:
    try:
        result = voices.probe(name)
    except Exception as exc:  # noqa: BLE001
        print(f"Не удалось синтезировать: {exc}")
        return 1

    print(f"Голос:        {result['voice']}")
    print(f"Модель:       {result['model']}")
    print(f"Частота:      {result['rate']} Гц")
    print(f"Длительность: {result['seconds']:.2f} c "
          f"({result['words_per_minute']:.0f} слов/мин, темп {result['speed']:.1f})")
    print(f"Тембр:        {voices.tone_name(result['voice'])} — "
          f"{tone.describe(voices.tone_profile(result['voice']))}")
    print(f"Громкость:    {voices.effective_volume(result['voice']):.2f} "
          f"(поправка голоса {voices.gain(result['voice']):.2f})")
    print(f"Пик:          {result['peak']:.2f}   RMS: {result['rms']:.3f}")
    if result["peak"] < 0.2:
        print("  Внимание: сигнал очень тихий — проверьте PIPER_VOLUME.")
    # Пик 1.0 — штатная нормализация Piper. Перегруз видно по доле сэмплов,
    # которые стоят на пределе: у нормального голоса это доли процента.
    if result.get("loud_ratio", 0.0) > 0.02:
        print(f"  Внимание: {result['loud_ratio'] * 100:.1f}% сэмплов на пределе — "
              "возможен клиппинг, уменьшите PIPER_VOLUME.")

    if play:
        import tempfile

        import tts
        from piper import PiperVoice
        from piper.config import SynthesisConfig
        import wave

        voice = PiperVoice.load(str(voices.ensure(result["voice"])))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "probe.wav"
            with wave.open(str(out), "wb") as handle:
                voice.synthesize_wav(voices.PROBE_PHRASE, handle,
                                     syn_config=SynthesisConfig(
                                         length_scale=voices.length_scale(result["voice"], result["speed"]),
                                         volume=voices.effective_volume(result["voice"])))
            # Проигрываем ровно то, что услышим в работе — с обработкой тембра.
            tone.apply_to_wav(out, voices.tone_profile(result["voice"]))
            print("\nИграю пробную фразу...")
            tts.play_audio_file(out)
    return 0


def cmd_samples(out_dir: Path, only: str | None) -> int:
    """Синтезирует одинаковые фразы разными голосами — для сравнения на слух."""
    import wave

    from piper import PiperVoice
    from piper.config import SynthesisConfig

    installed = [only] if only else voices.installed_voices()
    if not installed:
        print("Ни один голос не скачан. Начните с: python voice.py use irina")
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    made = 0
    for key in installed:
        model = voices.ensure(key)
        voice = PiperVoice.load(str(model))
        speed = voices.recommended_speed(key)
        profile = voices.tone_profile(key)
        for title, phrase in SAMPLES.items():
            path = out_dir / f"{key}-{title}.wav"
            with wave.open(str(path), "wb") as handle:
                voice.synthesize_wav(phrase, handle, syn_config=SynthesisConfig(
                    length_scale=voices.length_scale(key, speed),
                    volume=voices.effective_volume(key)))
            # Образцы должны звучать как в работе, иначе сравнение врёт.
            tone.apply_to_wav(path, profile)
            made += 1
        print(f"  {key}: {len(SAMPLES)} файла, темп {speed:.2f}, "
              f"тембр {voices.tone_name(key)}")
    print(f"\nГотово: {made} файлов в {out_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Голос Пятницы: список, выбор, проверка")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="показать голоса и текущий выбор")

    use = sub.add_parser("use", help="выбрать голос")
    use.add_argument("name", help="irina | dmitri | denis | ruslan")
    use.add_argument("--keep-speed", action="store_true",
                     help="не менять PIPER_SPEED на рекомендуемый для голоса")

    tone_cmd = sub.add_parser("tone", help="выбрать характер звучания")
    tone_cmd.add_argument("name", help="off | soft | confident | auto")

    test = sub.add_parser("test", help="синтезировать пробную фразу и замерить")
    test.add_argument("name", nargs="?", help="голос (по умолчанию активный)")
    test.add_argument("--play", action="store_true", help="сразу проиграть")

    samples = sub.add_parser("samples", help="образцы голосов в файлы")
    samples.add_argument("--out", default=str(Path(config.BASE_DIR) / "outputs"
                                              / "voice-samples"),
                         help="куда сохранить")
    samples.add_argument("--voice", help="только один голос")

    args = parser.parse_args()
    if args.command in (None, "list"):
        return cmd_list()
    if args.command == "use":
        return cmd_use(args.name, args.keep_speed)
    if args.command == "tone":
        return cmd_tone(args.name)
    if args.command == "test":
        return cmd_test(args.name, args.play)
    if args.command == "samples":
        return cmd_samples(Path(args.out), args.voice)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
