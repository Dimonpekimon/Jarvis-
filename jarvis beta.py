import os
import sys
import subprocess
import webbrowser
import urllib.parse
import wikipedia

# Настраиваем Википедию на русский язык
wikipedia.set_lang("ru")

def speak(text):
    """Функция вывода текста Джарвиса в консоль"""
    print(f"Джарвис: {text}", flush=True)

def listen_command():
    """Ввод команд через клавиатуру для проверки работы программы"""
    print("\n[Введите команду в консоль и нажмите Enter]: ", end="", flush=True)
    command = input()
    return command.lower()

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
    """Запуск VS Code напрямую через системную команду Windows"""
    try:
        # Windows сама найдет и запустит VS Code по ключевому слову code
        os.system("start code")
        speak("Открываю вижуал студио код, сэр.")
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
    """Безопасный запуск Steam в Windows"""
    paths = [
        r"C:\Program Files (x86)\Steam\Steam.exe", 
        r"C:\Program Files\Steam\Steam.exe"
    ]
    # Сначала пытаемся найти файл в стандартных папках
    if not open_windows_app(paths, "стим"):
        # Если не нашли, принудительно запускаем через системный протокол Windows
        speak("Запускаю Стим через системный протокол, сэр.")
        webbrowser.open("steam://open/main")

def execute_command(command):
    """Логика обработки команд"""
    command = command.lower().strip()
    
    if "привет" in command or "джарвис" in command:
        speak("Приветствую, сэр. Чем могу помочь?")
        
    elif "открой браузер" in command:
        speak("Открываю браузер.")
        webbrowser.open("https://google.com")
        
    elif "ютуб" in command or "youtube" in command:
        speak("Открываю Ютуб, сэр.")
        webbrowser.open("https://youtube.com")
        
    elif "стим" in command or "steam" in command:
        open_steam()
        
    elif "код" in command or "vscode" in command or "вскод" in command or "vsc" in command:
        open_vscode()
        
    elif "пайчарм" in command or "pycharm" in command:
        open_pycharm()
        
    elif "терминал" in command or "консоль" in command or "командная строка" in command:
        speak("Запускаю командную строку.")
        os.system("start cmd")
        
    elif "выключись" in command or "пока" in command:
        speak("Отключаю интерфейсы. До встречи, сэр.")
        sys.exit()
        
    # --- УМНЫЙ ПОИСК В ВИКИПЕДИИ НА СВОБОДНЫЕ ВОПРОСЫ ---
    else:
        print(f"Джарвис ищет в Википедии: {command}...", flush=True)
        try:
            # Шаг 1: Ищем похожие статьи по нашему запросу
            search_results = wikipedia.search(command)
            
            if search_results:
                # Если статьи найдены, берем самый первый (точный) результат
                exact_page = search_results[0]
                summary = wikipedia.summary(exact_page, sentences=2)
                speak(summary)
            else:
                speak("Сэр, мне не удалось найти информацию об этом даже в поиске Википедии.")
                
        except wikipedia.exceptions.DisambiguationError as e:
            # Если нашлось много вариантов, берем первый предложенный вариант из списка альтернатив
            try:
                summary = wikipedia.summary(e.options[0], sentences=2)
                speak(summary)
            except Exception:
                speak("Сэр, по этому запросу слишком много совпадений. Уточните, пожалуйста.")
        except wikipedia.exceptions.PageError:
            speak("Сэр, страница на Википедии пустая или заблокирована.")
        except Exception:
            speak("Произошла ошибка при поиске информации.")

if __name__ == "__main__":
    speak("Системы Джарвиса успешно запущены.")
    while True:
        voice_input = listen_command()
        if voice_input.strip():
            execute_command(voice_input)