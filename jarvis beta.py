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

# Настраиваем Википедию на русский язык
wikipedia.set_lang("ru")

def speak(text):
    """Функция вывода текста Джарвиса в консоль и озвучки голосом с перезапуском движка"""
    print(f"Джарвис: {text}", flush=True)
    try:
        engine = pyttsx3.init()
        voices = engine.getProperty('voices')
        for voice in voices:
            if "russian" in voice.name.lower() or "ru" in voice.id.lower():
                engine.setProperty('voice', voice.id)
                break
        engine.setProperty('rate', 180)
        engine.say(text)
        engine.runAndWait()
        engine.stop()
        del engine
    except Exception as e:
        print(f"[Ошибка синтеза речи]: {e}", flush=True)

def listen_command():
    """Распознавание команд через микрофон без использования PyAudio"""
    recognizer = sr.Recognizer()
    sample_rate = 16000  
    duration = 5         
    try:
        print("\n[Джарвис на связи и слушает...] ", end="", flush=True)
        audio_data = sd.rec(int(duration * sample_rate), samplerate=sample_rate, channels=1, dtype='int16')
        sd.wait()  
        byte_data = audio_data.tobytes()
        audio_to_recognize = sr.AudioData(byte_data, sample_rate, 2)
        command = recognizer.recognize_google(audio_to_recognize, language="ru-RU")
        return command.lower().strip()
    except sr.UnknownValueError:
        return ""
    except Exception:
        print(f"\n[Консольный режим ввода]: ", end="", flush=True)
        command = input()
        return command.lower()
def get_weather_stable(city_name):
    """Стабильное получение погоды через Open-Meteo API без ключей"""
    try:
        # Шаг 1: Находим координаты города через геокодер
        geo_url = f"https://open-meteo.com{urllib.parse.quote(city_name)}&count=1&language=ru"
        geo_resp = requests.get(geo_url, timeout=5).json()
        
        if not geo_resp.get('results'):
            return f"Сэр, мне не удалось найти город {city_name} на карте."
            
        location = geo_resp['results'][0]
        lat, lon = location['latitude'], location['longitude']
        correct_name = location.get('name', city_name)
        
        # Шаг 2: Запрашиваем погоду по координатам
        weather_url = f"https://open-meteo.com{lat}&longitude={lon}&current=temperature_2m,weather_code"
        weather_data = requests.get(weather_url, timeout=5).json()
        
        current = weather_data['current']
        temp = round(current['temperature_2m'])
        
                # Полная расшифровка основных кодов погоды WMO
        code = current['weather_code']
        status = "ясно"
        if code in [1, 2, 3]: status = "переменная облачность"
        elif code in [45, 48]: status = "туманно"
        elif code in [51, 53, 55, 61, 63, 65, 80, 81, 82]: status = "идет дождь"
        elif code in [71, 73, 75, 77, 85, 86]: status = "идет снег"
        elif code in [95, 96, 99]: status = "гроза"

        return f"В городе {correct_name} сейчас {status}, температура воздуха {temp} градусов Цельсия."
    except Exception:
        return "Сэр, возникла ошибка при обращении к метеослужбе."



def open_windows_app(possible_paths, app_name):
    """Вспомогательная функция для безопасного запуска .exe в Windows"""
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
        subprocess.Popen(["pycharm"], shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        speak("Открываю пайчарм, сэр.")
    except FileNotFoundError:
        paths = [r"%LOCALAPPDATA%\Programs\PyCharm Professional\bin\pycharm64.exe",
                 r"%LOCALAPPDATA%\Programs\PyCharm Community\bin\pycharm64.exe",
                 r"C:\Program Files\JetBrains\PyCharm Community Edition\bin\pycharm64.exe"]
        if not open_windows_app(paths, "пайчарм"):
            speak("Пайчарм не найден в стандартных директориях.")

def open_steam():
    paths = [r"C:\Program Files (x86)\Steam\Steam.exe", r"C:\Program Files\Steam\Steam.exe"]
    if not open_windows_app(paths, "стим"):
        speak("Запускаю Стим через системный протокол, сэр.")
        webbrowser.open("steam://open/main")

def execute_command(command):
    """Логика обработки команд"""
    
    # Продвинутый поиск ключевых слов во фразе
    if "время" in command or "час" in command:
        current_time = datetime.now().strftime("%H:%M")
        speak(f"Сейчас {current_time}, сэр.")
        
    elif "погода" in command or "погоду" in command:
        # Ищем, в каком именно городе просят погоду
        words = command.split()
        city = ""
        # Если в команде есть предлог "в", берем слово после него
        if "в" in words:
            idx = words.index("в")
            if idx + 1 < len(words):
                city = words[idx + 1]
        # Если предлога "в" нет, берем просто последнее слово команды
        else:
            city = words[-1]
            
        if city and city != "погода" and city != "погоду":
            weather_report = get_weather_stable(city)
            speak(weather_report)
        else:
            speak("Сэр, укажите, пожалуйста, конкретный город.")
            
    elif "привет" in command:
        speak("Приветствую, сэр. Чем могу помочь?")
        
    elif "браузер" in command:
        speak("Открываю браузер.")
        webbrowser.open("https://google.com")
        
    elif "ютуб" in command or "youtube" in command:
        speak("Открываю Ютуб, сэр.")
        webbrowser.open("https://youtube.com")
        
    elif "стим" in command or "steam" in command:
        open_steam()
        
    elif "код" in command or "vscode" in command or "вскод" in command:
        open_vscode()
        
    elif "пайчарм" in command or "pycharm" in command:
        open_pycharm()
        
    elif "терминал" in command or "консоль" in command:
        speak("Запускаю командную строку.")
        os.system("start cmd")
        
    elif "выключись" in command or "пока" in command:
        speak("Отключаю интерфейсы. До встречи, сэр.")
        sys.exit()
        
    else:
        print(f"Джарвис ищет в Википедии: {command}...", flush=True)
        try:
            search_results = wikipedia.search(command)
            if search_results:
                # Берем первую найденную страницу
                summary = wikipedia.summary(search_results[0], sentences=2)
                speak(summary)
            else:
                speak("Сэр, мне не удалось найти информацию об этом.")
        except Exception:
            speak("Произошла ошибка при поиске информации.")

if __name__ == "__main__":
    speak("Системы Джарвиса успешно запущены.")
    
    while True:
        voice_input = listen_command()
        
        if "джарвис" in voice_input:
            print(f"[Распознано обращение]: {voice_input}")
            clean_command = voice_input.replace("джарвис", "").strip()
            
            if not clean_command:
                speak("Да, сэр?")
            else:
                execute_command(clean_command)
