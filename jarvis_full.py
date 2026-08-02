import os
import sys
import subprocess
import webbrowser
import urllib.parse
import wikipedia
import speech_recognition as sr
import pyttsx3
import sounddevice as sd
import numpy as np
from datetime import datetime
import requests
import ctypes
import threading
import re
import difflib
import time
from collections import deque

#  ОПЦИОНАЛЬНЫЕ ИМПОРТЫ 
try:
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from comtypes import CLSCTX_ALL
    from ctypes import cast, POINTER
    HAS_PYCAW = True
except ImportError:
    HAS_PYCAW = False

try:
    import screen_brightness_control as sbc
    HAS_SBC = True
except ImportError:
    HAS_SBC = False

try:
    from duckduckgo_search import DDGS
    HAS_DDGS = True
except ImportError:
    HAS_DDGS = False

#  НАСТРОЙКИ

wikipedia.set_lang("ru")
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2"  # <-- Укажите свою модель: ollama list

SAMPLE_RATE = 16000
WAKE_DURATION = 4
COMMAND_DURATION = 6
NOISE_FLOOR = None

#  ГЛОБАЛЬНЫЕ ОБЪЕКТЫ

engine = pyttsx3.init()
voices = engine.getProperty('voices')
for voice in voices:
    if "russian" in voice.name.lower() or "ru" in voice.id.lower():
        engine.setProperty('voice', voice.id)
        break
engine.setProperty('rate', 180)

context_memory = deque(maxlen=6)
reminders = {}

COMMAND_PATTERNS = {
    "time":       ["время", "который час", "часы", "сколько времени", "сколько время", "текущее время", "час", "времени"],
    "weather":    ["погода", "погоду", "прогноз погоды", "какая погода", "погодка"],
    "greeting":   ["привет", "здравствуй", "добрый день", "доброе утро", "хай", "приветствую"],
    "browser":    ["браузер", "открой браузер", "интернет", "гугл", "google"],
    "youtube":    ["ютуб", "youtube", "видео", "открой ютуб", "you tube"],
    "steam":      ["стим", "steam", "игры", "играть"],
    "vscode":     ["код", "vscode", "вскод", "редактор", "коде", "visual studio code"],
    "pycharm":    ["пайчарм", "pycharm", "пай чарм"],
    "terminal":   ["терминал", "консоль", "командная строка", "cmd", "командную строку"],
    "volume":     ["громкость", "звук", "тише", "громче", "выключи звук", "включи звук", "без звука", "звука"],
    "brightness": ["яркость", "ярче", "тусклее", "экран", "подсветка", "светлее", "темнее"],
    "lock":       ["блокировка", "заблокируй", "лок экрана", "заблокируй экран", "блок"],
    "shutdown":   ["выключи компьютер", "выключение", "завершение работы", "выключи", "выключить пк"],
    "reboot":     ["перезагрузи", "перезагрузка", "рестарт", "перезагрузить", "ребут"],
    "reminder":   ["напомни", "напоминание", "будильник", "напомни мне", "напомни через", "напомни мне через"],
    "search":     ["найди", "поиск", "загугли", "поищи", "погугли"],
    "exit":       ["выключись", "пока", "завершить", "стоп", "до свидания", "закройся"],
}

#  БАЗОВЫЕ ФУНКЦИИ (AUDIO + TTS)

def _normalize_audio(audio_array):
    """Усиливает тихий сигнал до 90% от максимума int16."""
    audio_f = audio_array.astype(np.float32)
    peak = np.max(np.abs(audio_f))
    if peak == 0:
        return audio_array
    target = 0.9 * 32767
    gain = target / peak
    gain = min(gain, 10.0)
    return (audio_f * gain).astype(np.int16)


def _measure_rms(audio_array):
    """Среднеквадратичная амплитуда сигнала (0..1)."""
    audio_f = audio_array.astype(np.float32)
    return np.sqrt(np.mean(audio_f ** 2)) / 32767.0


def calibrate_mic(duration=2):
    """Измеряет фоновый шум при старте."""
    global NOISE_FLOOR
    print("[Калибровка микрофона: не говорите 2 секунды...]")
    try:
        audio = sd.rec(int(duration * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                       channels=1, dtype='int16')
        sd.wait()
        NOISE_FLOOR = _measure_rms(audio)
        print(f"[Фоновый шум: {NOISE_FLOOR:.4f}]")
    except Exception as e:
        print(f"[Ошибка калибровки: {e}]")
        NOISE_FLOOR = 0.02


def speak(text):
    """Все ответы Джарвиса идут через эту функцию: вывод + TTS."""
    if not text:
        return
    print(f"Джарвис: {text}", flush=True)
    try:
        engine.say(text)
        engine.runAndWait()
    except Exception as e:
        print(f"[Ошибка синтеза речи]: {e}", flush=True)


def listen_command(duration=COMMAND_DURATION):
    """Слушает команду с усилением, калибровкой и fallback на консоль."""
    recognizer = sr.Recognizer()
    try:
        print(f"\n[Слушаю {duration} сек...] ", end="", flush=True)

        try:
            audio_data = sd.rec(int(duration * SAMPLE_RATE),
                                samplerate=SAMPLE_RATE, channels=1, dtype='int16')
            sd.wait()
        except Exception as mic_err:
            print(f"\n[Микрофон недоступен: {mic_err}]")
            print(f"\n[Консольный режим ввода]: ", end="", flush=True)
            return input().lower().strip()

        audio_data = _normalize_audio(audio_data)
        rms = _measure_rms(audio_data)
        print(f"[Уровень: {rms:.3f}]", end="")

        if NOISE_FLOOR and rms < NOISE_FLOOR * 1.5:
            speak("Сэр, я вас почти не слышу. Говорите громче или проверьте микрофон.")
            return ""

        byte_data = audio_data.tobytes()
        audio = sr.AudioData(byte_data, SAMPLE_RATE, 2)

        try:
            text = recognizer.recognize_google(audio, language="ru-RU").lower().strip()
            print(f"  -> \"{text}\"")
            return text
        except sr.UnknownValueError:
            speak("Сэр, не расслышал. Повторите, пожалуйста.")
            return ""
        except sr.RequestError as e:
            print(f"\n[Ошибка сервиса распознавания: {e}]")
            return ""

    except Exception as e:
        print(f"\n[Критическая ошибка ввода: {e}]")
        print(f"\n[Консольный режим]: ", end="", flush=True)
        return input().lower().strip()


def listen_wake_word():
    """Фоновое прослушивание wake word с усилением."""
    recognizer = sr.Recognizer()
    try:
        print("[Ожидаю обращения...]      ", end="\r", flush=True)
        audio_data = sd.rec(int(WAKE_DURATION * SAMPLE_RATE),
                            samplerate=SAMPLE_RATE, channels=1, dtype='int16')
        sd.wait()

        audio_data = _normalize_audio(audio_data)
        rms = _measure_rms(audio_data)
        if NOISE_FLOOR and rms < NOISE_FLOOR * 2:
            return False

        byte_data = audio_data.tobytes()
        audio = sr.AudioData(byte_data, SAMPLE_RATE, 2)
        text = recognizer.recognize_google(audio, language="ru-RU").lower()

        triggered = any(w in text for w in ["джарвис", "джарвиз", "jarvis",
                                             "привет джарвис", "эй джарвис"])
        if triggered:
            print(f"[Wake word detected: {text}]")
        return triggered
    except sr.UnknownValueError:
        return False
    except Exception:
        return False

#  FUZZY MATCHING

def fuzzy_match_command(text):
    """Определяет намерение: точное вхождение -> difflib fallback."""
    text = text.lower().strip()

    for cmd, aliases in COMMAND_PATTERNS.items():
        for alias in aliases:
            if alias in text:
                return cmd, alias

    all_aliases = []
    alias_to_cmd = {}
    for cmd, aliases in COMMAND_PATTERNS.items():
        for alias in aliases:
            all_aliases.append(alias)
            alias_to_cmd[alias] = cmd

    matches = difflib.get_close_matches(text, all_aliases, n=1, cutoff=0.5)
    if matches:
        best = matches[0]
        return alias_to_cmd[best], best

    return None, None

#  ПОГОДА / ПОИСК / LLM / НАПОМИНАНИЯ / СИСТЕМА

def get_weather_stable(city_name):
    try:
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(city_name)}&count=1&language=ru"
        geo_resp = requests.get(geo_url, timeout=5).json()

        if not geo_resp.get('results'):
            return f"Сэр, мне не удалось найти город {city_name} на карте."

        loc = geo_resp['results'][0]
        lat, lon = loc['latitude'], loc['longitude']
        name = loc.get('name', city_name)

        weather_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,weather_code"
        weather_data = requests.get(weather_url, timeout=5).json()

        current = weather_data['current']
        temp = round(current['temperature_2m'])
        code = current['weather_code']
        status = "ясно"
        if code in [1, 2, 3]:   status = "переменная облачность"
        elif code in [45, 48]:  status = "туманно"
        elif code in [51, 53, 55, 61, 63, 65, 80, 81, 82]: status = "идет дождь"
        elif code in [71, 73, 75, 77, 85, 86]:            status = "идет снег"
        elif code in [95, 96, 99]:                         status = "гроза"

        return f"В городе {name} сейчас {status}, температура воздуха {temp} градусов Цельсия."
    except Exception:
        return "Сэр, возникла ошибка при обращении к метеослужбе."


def search_web(query):
    if not query or query.strip() == "":
        return "Сэр, уточните запрос для поиска."

    if HAS_DDGS:
        try:
            with DDGS() as ddgs:
                results = ddgs.text(query, max_results=3)
                if results:
                    texts = [f"{r['title']}. {r['body']}" for r in results]
                    return "Вот что я нашёл, сэр: " + " ".join(texts)
                return "Ничего не найдено, сэр."
        except Exception as e:
            return f"Ошибка поиска: {e}"

    try:
        res = wikipedia.search(query)
        if res:
            summary = wikipedia.summary(res[0], sentences=2)
            return f"По вашему запросу в Википедии: {summary}"
        return "Сэр, мне не удалось найти информацию."
    except Exception:
        return "Сэр, поиск временно недоступен."


def ask_ollama(prompt, context=None):
    """Локальная LLM через Ollama. Если недоступна — возвращает None."""
    system = (
        "Ты — голосовой ассистент Джарвис. Отвечай кратко (1-3 предложения), по-русски, вежливо. "
        "Обращайся к пользователю 'сэр'. Если не знаешь ответ — честно скажи об этом."
    )

    full_prompt = ""
    if context:
        for user_msg, assistant_msg in context:
            full_prompt += f"Пользователь: {user_msg}\nДжарвис: {assistant_msg}\n"
    full_prompt += f"Пользователь: {prompt}\nДжарвис:"

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": full_prompt,
        "system": system,
        "stream": False,
        "options": {"temperature": 0.7, "num_predict": 150}
    }

    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=60).json()
        answer = resp.get("response", "").strip()
        answer = answer.replace(f"Пользователь: {prompt}", "").strip()
        answer = answer.replace("Джарвис:", "").strip()
        return answer if answer else "Сэр, модель вернула пустой ответ."
    except requests.exceptions.ConnectionError:
        return None
    except Exception as e:
        return f"Сэр, ошибка связи с моделью: {e}"


def set_reminder(text, minutes):
    def fire():
        speak(f"Сэр, напоминаю: {text}")
        for k in list(reminders.keys()):
            if reminders[k].get("text") == text:
                reminders.pop(k, None)

    sec = max(1, int(minutes * 60))
    t = threading.Timer(sec, fire)
    tid = f"reminder_{datetime.now().strftime('%H%M%S')}_{len(reminders)}"
    reminders[tid] = {"timer": t, "text": text}
    t.start()
    return f"Напоминание установлено через {minutes} минут: {text}"


def system_control(action, value=None):
    if action == "volume":
        if not HAS_PYCAW:
            return "Для управления громкостью установите: pip install pycaw comtypes"
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = cast(interface, POINTER(IAudioEndpointVolume))

        if value == "up":
            cur = vol.GetMasterVolumeLevelScalar()
            new = min(1.0, cur + 0.1)
            vol.SetMasterVolumeLevelScalar(new, None)
            return f"Громкость увеличена до {int(new*100)}%, сэр."
        elif value == "down":
            cur = vol.GetMasterVolumeLevelScalar()
            new = max(0.0, cur - 0.1)
            vol.SetMasterVolumeLevelScalar(new, None)
            return f"Громкость уменьшена до {int(new*100)}%, сэр."
        elif value == "mute":
            vol.SetMute(1, None)
            return "Звук отключён, сэр."
        elif value == "unmute":
            vol.SetMute(0, None)
            return "Звук включён, сэр."
        return "Уточните действие со звуком, сэр."

    elif action == "brightness":
        if not HAS_SBC:
            return "Для управления яркостью установите: pip install screen-brightness-control"
        if value == "up":
            cur = sbc.get_brightness()[0]
            new = min(100, cur + 10)
            sbc.set_brightness(new)
            return f"Яркость увеличена до {new}%, сэр."
        elif value == "down":
            cur = sbc.get_brightness()[0]
            new = max(0, cur - 10)
            sbc.set_brightness(new)
            return f"Яркость уменьшена до {new}%, сэр."
        return "Уточните действие с яркостью, сэр."

    elif action == "lock":
        ctypes.windll.user32.LockWorkStation()
        return "Экран заблокирован, сэр."

    elif action == "shutdown":
        speak("Выключаю систему. До свидания, сэр.")
        os.system("shutdown /s /t 5")
        return ""

    elif action == "reboot":
        speak("Перезагружаю систему, сэр.")
        os.system("shutdown /r /t 5")
        return ""

    return "Сэр, системная команда не распознана."

#  ЗАПУСК ПРИЛОЖЕНИЙ

def open_windows_app(possible_paths, app_name):
    for path in possible_paths:
        expanded_path = os.path.expandvars(path)
        if os.path.exists(expanded_path):
            os.startfile(expanded_path)
            speak(f"Открываю {app_name}, сэр.")
            return True
    return False


def open_vscode():
    try:
        os.system("start code")
        speak("Открываю зарезервированную среду разработки, сэр.")
    except Exception:
        speak("Сэр, не удалось запустить среду разработки напрямую.")


def open_pycharm():
    try:
        subprocess.Popen(["pycharm"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        speak("Открываю пайчарм, сэр.")
    except FileNotFoundError:
        paths = [
            r"%LOCALAPPDATA%\Programs\PyCharm Professional\bin\pycharm64.exe",
            r"%LOCALAPPDATA%\Programs\PyCharm Community\bin\pycharm64.exe",
            r"C:\Program Files\JetBrains\PyCharm Community Edition\bin\pycharm64.exe"
        ]
        if not open_windows_app(paths, "пайчарм"):
            speak("Пайчарм не найден в стандартных директориях.")


def open_steam():
    paths = [
        r"C:\Program Files (x86)\Steam\Steam.exe",
        r"C:\Program Files\Steam\Steam.exe"
    ]
    if not open_windows_app(paths, "стим"):
        speak("Запускаю Стим через системный протокол, сэр.")
        webbrowser.open("steam://open/main")

#  ОБРАБОТКА КОМАНД

def parse_minutes(text):
    m = re.search(r'(\d+)\s*(минут|минуты|минуту|мин)', text)
    if m:
        return int(m.group(1))
    if "час" in text:
        m = re.search(r'(\d+)\s*час', text)
        if m:
            return int(m.group(1)) * 60
    if "секунд" in text or "сек" in text:
        m = re.search(r'(\d+)\s*сек', text)
        if m:
            return max(1, int(m.group(1)) / 60)
    return 1


def execute_command(raw_command):
    """Главная логика: fuzzy matching -> команда / LLM fallback."""
    command = raw_command.lower().strip()
    intent, _ = fuzzy_match_command(command)

    # Время 
    if intent == "time":
        current_time = datetime.now().strftime("%H:%M")
        speak(f"Сейчас {current_time}, сэр.")

    #  Погода 
    elif intent == "weather":
        words = command.split()
        city = ""
        if "в" in words:
            idx = words.index("в")
            if idx + 1 < len(words):
                city = words[idx + 1]
        else:
            city = words[-1]
        if city and city not in ("погода", "погоду"):
            speak(get_weather_stable(city))
        else:
            speak("Сэр, укажите, пожалуйста, конкретный город.")

    # Приветствие
    elif intent == "greeting":
        speak("Приветствую, сэр. Чем могу помочь?")

    # --- Приложения ---
    elif intent == "browser":
        speak("Открываю браузер.")
        webbrowser.open("https://google.com")
    elif intent == "youtube":
        speak("Открываю Ютуб, сэр.")
        webbrowser.open("https://youtube.com")
    elif intent == "steam":
        open_steam()
    elif intent == "vscode":
        open_vscode()
    elif intent == "pycharm":
        open_pycharm()
    elif intent == "terminal":
        speak("Запускаю командную строку.")
        os.system("start cmd")

    # Система
    elif intent == "volume":
        val = None
        if any(w in command for w in ["громче", "больше", "увеличь", "up"]):
            val = "up"
        elif any(w in command for w in ["тише", "меньше", "уменьши", "down"]):
            val = "down"
        elif any(w in command for w in ["выключи", "mute", "без звука", "выключи звук"]):
            val = "mute"
        elif any(w in command for w in ["включи звук", "unmute", "включи"]):
            val = "unmute"
        speak(system_control("volume", val))

    elif intent == "brightness":
        val = None
        if any(w in command for w in ["ярче", "больше", "увеличь", "up", "светлее"]):
            val = "up"
        elif any(w in command for w in ["тусклее", "меньше", "уменьши", "down", "темнее"]):
            val = "down"
        speak(system_control("brightness", val))

    elif intent == "lock":
        speak(system_control("lock"))
    elif intent == "shutdown":
        system_control("shutdown")
    elif intent == "reboot":
        system_control("reboot")

    # Напоминания
    elif intent == "reminder":
        minutes = parse_minutes(command)
        reminder_text = command
        for alias in COMMAND_PATTERNS["reminder"]:
            reminder_text = reminder_text.replace(alias, "").strip()
        reminder_text = re.sub(r'\d+\s*(минут|минуты|минуту|мин|час|секунд|сек)', '', reminder_text).strip()
        reminder_text = re.sub(r'через\s*', '', reminder_text).strip()
        if not reminder_text:
            reminder_text = "напоминание"
        speak(set_reminder(reminder_text, minutes))

    # Поиск 
    elif intent == "search":
        query = command
        for alias in COMMAND_PATTERNS["search"]:
            query = query.replace(alias, "").strip()
        speak(search_web(query))

    # Выход 
    elif intent == "exit":
        speak("Отключаю интерфейсы. До встречи, сэр.")
        sys.exit()

    # Fallback: Ollama LLM 
    else:
        speak("Обращаюсь к нейросети, сэр...")
        answer = ask_ollama(command, context=list(context_memory))

        if answer is None:
            speak("Нейросеть недоступна. Ищу в интернете.")
            answer = search_web(command)

        speak(answer)
        context_memory.append((command, answer))

#  ГЛАВНЫЙ ЦИКЛ

if __name__ == "__main__":
    speak("Системы Джарвиса успешно запущены. Ожидаю ваших указаний, сэр.")
    calibrate_mic()

    while True:
        if not listen_wake_word():
            continue

        # Небольшая пауза, чтобы TTS не попал в запись (если включите speak ниже)
        time.sleep(0.3)

        voice_input = listen_command(duration=COMMAND_DURATION)

        if not voice_input:
            continue

        print(f"[Распознано]: {voice_input}")
        execute_command(voice_input) 