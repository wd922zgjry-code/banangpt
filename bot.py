import base64
import io
import mimetypes
import os
import re
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import matplotlib
matplotlib.use('Agg')  # Для работы без графического интерфейса
import matplotlib.pyplot as plt
import numpy as np
import requests
import telebot
from telebot.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# Фоновый мини-сервер, чтобы Render держал сервис активным
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()

# Чтение ключей из переменных окружения
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Реальные рабочие модели Google API и их таймауты
# Реальные рабочие модели Google API и их таймауты
MODEL_TIMEOUTS = {
    'gemini-2.5-flash-lite': 20,
    'gemini-2.5-flash': 40,
    'gemini-2.0-flash': 40,
    'gemini-2.5-pro': 120,
}

# Пользовательские названия для меню (в точности по вашему списку)
MODEL_INFO = {
    'auto': '🔄 Авто (Умный перебор)',
    'gemini-2.5-flash-lite': '🚀 3.5 Flash-Lite (Самые быстрые ответы)',
    'gemini-2.5-flash': '⚡ 3.8 Flash (All-around help)',
    'gemini-2.0-flash': '🔬 3.1 Pro (Расширенные возможности)',
    'gemini-2.5-pro': '🧠 4 Argon (Сложные задачи)',
}

FALLBACK_ORDER = [
    'gemini-2.5-flash',
    'gemini-2.0-flash',
    'gemini-2.5-flash-lite',
    'gemini-2.5-pro',
]

user_models = {}

def get_user_model(chat_id):
    return user_models.get(chat_id, 'auto')

# ----------------- ХРАНИЛИЩЕ КОНТЕКСТА (ПАМЯТЬ) -----------------
user_context = {}
MAX_HISTORY = 10  # Хранить последние 10 сообщений диалога

def add_to_context(chat_id, role, text):
    """Добавляет сообщение пользователя или ассистента в историю"""
    if chat_id not in user_context:
        user_context[chat_id] = []
    
    user_context[chat_id].append({"role": role, "text": text})
    if len(user_context[chat_id]) > MAX_HISTORY:
        user_context[chat_id] = user_context[chat_id][-MAX_HISTORY:]

def get_context_as_prompt(chat_id):
    """Преобразует историю переписки в диалоговый текст"""
    if chat_id not in user_context or not user_context[chat_id]:
        return ""
    
    formatted = []
    for item in user_context[chat_id]:
        role_label = "Пользователь" if item["role"] == "user" else "Ассистент"
        formatted.append(f"{role_label}: {item['text']}")
    return "\n".join(formatted)

def clear_context(chat_id):
    """Очищает память беседы"""
    user_context[chat_id] = []

# ----------------- КЛАВИАТУРЫ И МЕНЮ -----------------

def get_main_keyboard(chat_id):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    current_model_key = get_user_model(chat_id)
    
    # Короткое название для кнопки
    short_names = {
        'auto': 'Авто',
        'gemini-2.5-flash-lite': '3.5 Flash-Lite',
        'gemini-2.5-flash': '3.8 Flash',
        'gemini-2.0-flash': '3.1 Pro',
        'gemini-2.5-pro': '4 Argon',
    }
    model_name_short = short_names.get(current_model_key, '3.8 Flash')
    
    btn_model = KeyboardButton(f"📝 Модель: {model_name_short}")
    btn_new = KeyboardButton("🔄 Новый диалог")
    btn_settings = KeyboardButton("⚙️ Настройки")
    
    markup.row(btn_model)
    markup.row(btn_new, btn_settings)
    return markup

def send_settings_menu(chat_id):
    current_model_key = get_user_model(chat_id)
    model_label = MODEL_INFO.get(current_model_key, 'Авто')

    settings_text = (
        "⚙️ *Панель настроек*\n\n"
        f"🤖 Активная модель: *{model_label}*\n"
        f"🧠 Память контекста: *Включена* (до {MAX_HISTORY} реплик)\n\n"
        "Выберите необходимое действие:"
    )
    markup = InlineKeyboardMarkup()
    btn_model = InlineKeyboardButton("🤖 Выбрать модель", callback_data="open_models")
    btn_reset = InlineKeyboardButton("🔄 Очистить контекст", callback_data="reset_context_inline")
    markup.row(btn_model)
    markup.row(btn_reset)
    
    btn_help = InlineKeyboardButton("❓ Помощь", callback_data="show_help")
    btn_close = InlineKeyboardButton("❌ Закрыть", callback_data="close_settings")
    markup.row(btn_help, btn_close)
    
    bot.send_message(chat_id, settings_text, parse_mode='Markdown', reply_markup=markup)

def get_model_inline_keyboard(current_model):
    markup = InlineKeyboardMarkup(row_width=1)
    for model_id, label in MODEL_INFO.items():
        prefix = '✅ ' if model_id == current_model else ''
        markup.add(InlineKeyboardButton(f'{prefix}{label}', callback_data=f'setmodel:{model_id}'))
    markup.add(InlineKeyboardButton("⬅️ Назад в настройки", callback_data="back_to_settings"))
    return markup

# ----------------- ВЗАИМОДЕЙСТВИЕ С GEMINI API -----------------

def call_single_model_http(model, parts, system_text):
    model_timeout = MODEL_TIMEOUTS.get(model, 60)
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}'

    payload = {
        'contents': [{'role': 'user', 'parts': parts}],
        'system_instruction': {'parts': [{'text': system_text}]},
        'generationConfig': {'maxOutputTokens': 4096},
    }

    resp = requests.post(url, json=payload, timeout=(10, model_timeout))
    if resp.status_code == 200:
        data = resp.json()
        candidates = data.get('candidates', [])
        if candidates and 'content' in candidates[0]:
            candidate_parts = candidates[0]['content'].get('parts', [])
            texts = [p['text'] for p in candidate_parts if 'text' in p]
            reply_text = '\n'.join(texts).strip()
            if reply_text:
                return reply_text
    
    raise Exception(f'Код {resp.status_code}: {resp.text}')

def ask_gemini_smart(chat_id, parts, system_text):
    chosen = get_user_model(chat_id)
    models_to_try = (
        FALLBACK_ORDER if chosen == 'auto'
        else [chosen] + [m for m in FALLBACK_ORDER if m != chosen]
    )

    last_error = ''
    for model in models_to_try:
        limit_sec = MODEL_TIMEOUTS.get(model, 60)
        try:
            return call_single_model_http(model, parts, system_text)
        except requests.exceptions.Timeout:
            last_error = f'Превышен лимит {limit_sec} сек'
        except Exception as e:
            last_error = str(e)
            if '503' in last_error:
                time.sleep(2)

    raise Exception(f'Не удалось получить ответ. Причина: {last_error}')

def generate_fallback_cooling_curve():
    plt.figure(figsize=(8, 5))
    t = [0, 2, 4, 6, 8, 10, 12, 14]
    T = [900, 750, 650, 548, 548, 548, 400, 200]
    plt.plot(t, T, color='b', linewidth=2.5, marker='o', label='Кривая охлаждения сплава №1')
    plt.axhline(y=548, color='r', linestyle='--', label='Эвтектическая горизонталь (548 C)')
    plt.title('Кривая охлаждения сплава (Температура - Время)')
    plt.xlabel('Время')
    plt.ylabel('Температура (C)')
    plt.grid(True)
    plt.legend()
    plt.savefig('chart.png', bbox_inches='tight', dpi=300)
    plt.close()

def send_long_message(message, text):
    if not text:
        text = 'Пустой ответ от модели.'
    for i in range(0, len(text), 4000):
        bot.reply_to(message, text[i:i + 4000], reply_markup=get_main_keyboard(message.chat.id))

# ----------------- ОБРАБОТЧИКИ TELEGRAM -----------------

@bot.message_handler(commands=['start', 'model'])
def send_welcome(message):
    clear_context(message.chat.id)
    bot.send_message(
        message.chat.id,
        'Добро пожаловать в BananGPT! 🍌\nКонтекст беседы сброшен. Чем могу помочь?',
        reply_markup=get_main_keyboard(message.chat.id)
    )

@bot.callback_query_handler(func=lambda call: call.data == "show_help")
def process_help(call):
    help_text = (
        "💡 *Справка по боту*\n\n"
        "1. *Контекст диалога:*\n"
        "Бот помнит суть текущего разговора. Чтобы начать новую тему без старых хвостов — нажмите «🔄 Новый диалог».\n\n"
        "2. *Выбор модели:*\n"
        "Вы можете переключаться между моделями линейки Gemini через меню настроек или кнопку на клавиатуре.\n\n"
        "3. *Поддержка:* @isrustoy"
    )
    bot.send_message(call.message.chat.id, help_text, parse_mode='Markdown')
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "close_settings")
def process_close(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "reset_context_inline")
def process_reset_context(call):
    clear_context(call.message.chat.id)
    bot.answer_callback_query(call.id, "Контекст диалога очищен!")
    bot.send_message(call.message.chat.id, "🔄 Контекст диалога очищен. Можем начать новую тему!")

@bot.callback_query_handler(func=lambda call: call.data == "open_models")
def process_open_models(call):
    current = get_user_model(call.message.chat.id)
    bot.edit_message_text(
        "Выберите текстовую модель:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=get_model_inline_keyboard(current)
    )

@bot.callback_query_handler(func=lambda call: call.data == "back_to_settings")
def process_back_settings(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)
    send_settings_menu(call.message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('setmodel:'))
def handle_model_selection(call):
    model_id = call.data.split(':', 1)[1]
    if model_id in MODEL_INFO:
        user_models[call.message.chat.id] = model_id
        bot.answer_callback_query(call.id, f'Выбрано: {MODEL_INFO[model_id]}')
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(
            call.message.chat.id,
            f'✅ Установлена модель: *{MODEL_INFO[model_id]}*',
            parse_mode='Markdown',
            reply_markup=get_main_keyboard(call.message.chat.id)
        )

@bot.message_handler(content_types=['text'])
def handle_text(message):
    text_lower = message.text.lower()
    
    # Кнопки клавиатуры
    if message.text == '⚙️ Настройки':
        send_settings_menu(message.chat.id)
        return
    elif message.text == '🔄 Новый диалог':
        clear_context(message.chat.id)
        bot.send_message(
            message.chat.id, 
            "✅ Контекст диалога очищен! Можем начать новую тему.", 
            reply_markup=get_main_keyboard(message.chat.id)
        )
        return
    elif message.text.startswith('📝 Модель:'):
        current = get_user_model(message.chat.id)
        bot.send_message(
            message.chat.id, 
            "Выберите текстовую модель:", 
            reply_markup=get_model_inline_keyboard(current)
        )
        return

    print(f'\n📥 Сообщение: {message.text}')
    
    is_graph_request = any(kw in text_lower for kw in ['нарисуй график', 'построй график', 'график', 'диаграмма', 'визуализируй', 'кривая охлаждения', 'кривая'])
    is_file_request = any(kw in text_lower for kw in ['сохрани в файл', 'сделай файл', 'выгрузи в документе', 'отправь файлом'])

    wait_msg = bot.reply_to(message, '⏳ Думаю...')
    bot.send_chat_action(message.chat.id, 'typing')

    try:
        if is_graph_request:
            system_text = (
                "Ты опытный инженер. Пользователь просит построить график или кривую охлаждения. "
                "Дай краткое текстовое пояснение к этому графику (без знаков $ и LaTeX), а также напиши Python-код для построения, "
                "обернув его в ```python ... ```, который сохраняет график в 'chart.png'."
            )
            response_text = ask_gemini_smart(message.chat.id, [{'text': message.text}], system_text)
            
            code_match = re.search(r'```python(.*?)```', response_text, re.DOTALL)
            code_success = False
            
            if code_match:
                python_code = code_match.group(1).strip()
                try:
                    local_vars = {'plt': plt, 'np': np}
                    plt.figure(figsize=(8, 5))
                    exec(python_code, {}, local_vars)
                    if os.path.exists('chart.png'):
                        code_success = True
                except Exception as ex:
                    print(f"⚠️ Ошибка выполнения кода: {ex}")

            if not code_success:
                generate_fallback_cooling_curve()

            bot.delete_message(message.chat.id, wait_msg.message_id)
            
            clean_caption = re.sub(r'```python.*?```', '', response_text, flags=re.DOTALL).strip()
            if not clean_caption:
                clean_caption = "📈 График кривой охлаждения сплава:"

            with open('chart.png', 'rb') as photo:
                bot.send_photo(message.chat.id, photo, caption=clean_caption[:1024], reply_markup=get_main_keyboard(message.chat.id))
            if os.path.exists('chart.png'):
                os.remove('chart.png')

        elif is_file_request:
            system_text = "Отвечай подробно и структурированно, данные будут сохранены в текстовый файл. Без знаков $ и LaTeX."
            reply_text = ask_gemini_smart(message.chat.id, [{'text': message.text}], system_text)
            bot.delete_message(message.chat.id, wait_msg.message_id)
            
            file_name = "response.txt"
            with open(file_name, "w", encoding="utf-8") as f:
                f.write(reply_text)
            with open(file_name, "rb") as f:
                bot.send_document(message.chat.id, f, caption="📂 Вот ваш файл:", reply_markup=get_main_keyboard(message.chat.id))
            os.remove(file_name)

        else:
            # 1. Запоминаем вопрос пользователя
            add_to_context(message.chat.id, "user", message.text)

            # 2. Формируем запрос с учетом памяти
            history_str = get_context_as_prompt(message.chat.id)
            full_prompt = (
                "Ты умный и полезный ассистент BananGPT. "
                "Ниже приведена история текущего диалога с пользователем. "
                "Используй ее для понимания темы, местоимений и уточняющих вопросов.\n\n"
                f"--- ИСТОРИЯ ДИАЛОГА ---\n{history_str}\n--- КОНЕЦ ИСТОРИИ ---\n\n"
                "Ответь на последнюю реплику пользователя максимально кратко, чётко, по делу, без 'воды'. "
                "СТРОГО ЗАПРЕЩЕНО использовать знаки доллара ($), квадратные скобки для формул и любые символы LaTeX. "
                "Пиши все формулы обычным текстом (например: B = F / (I * l))."
            )

            reply_text = ask_gemini_smart(message.chat.id, [{'text': full_prompt}], "Отвечай строго по сути, соблюдая контекст беседы.")
            
            # 3. Сохраняем ответ модели в контекст
            add_to_context(message.chat.id, "model", reply_text)

            bot.delete_message(message.chat.id, wait_msg.message_id)
            send_long_message(message, reply_text)

        print('📤 Ответ отправлен в Telegram!')
    except Exception as e:
        print(f'❌ ОШИБКА: {e}')
        try:
            bot.edit_message_text(f'❌ Ошибка: {e}', message.chat.id, wait_msg.message_id)
        except:
            bot.send_message(message.chat.id, f'❌ Ошибка: {e}')

@bot.message_handler(content_types=['photo', 'document'])
def handle_files(message):
    print(f'\n📎 Получен файл/фото от пользователя')
    wait_msg = bot.reply_to(message, '⏳ Анализирую...')
    bot.send_chat_action(message.chat.id, 'typing')

    try:
        if message.photo:
            file_info = bot.get_file(message.photo[-1].file_id)
            mime_type = 'image/jpeg'
        elif message.document:
            file_info = bot.get_file(message.document.file_id)
            mime_type = message.document.mime_type or mimetypes.guess_type(message.document.file_name)[0] or 'application/pdf'
        else:
            bot.edit_message_text("❌ Не удалось распознать файл.", message.chat.id, wait_msg.message_id)
            return

        downloaded_file = bot.download_file(file_info.file_path)
        b64_data = base64.b64encode(downloaded_file).decode('utf-8')
        prompt = message.caption if message.caption else "Проанализируй этот файл кратко и по делу."

        parts = [
            {'inline_data': {'mime_type': mime_type, 'data': b64_data}},
            {'text': prompt},
        ]

        system_text = "Проанализируй файл кратко и чётко. Без знаков $ и LaTeX."
        reply_text = ask_gemini_smart(message.chat.id, parts, system_text)
        
        # Сохраняем файл в контекст
        add_to_context(message.chat.id, "user", f"[Пользователь отправил файл/фото]: {prompt}")
        add_to_context(message.chat.id, "model", reply_text)

        bot.delete_message(message.chat.id, wait_msg.message_id)
        send_long_message(message, reply_text)
        print('📤 Ответ по файлу отправлен в Telegram!')
    except Exception as e:
        print(f'❌ ОШИБКА ФАЙЛА: {e}')
        bot.edit_message_text(f'❌ Ошибка при обработке файла: {e}', message.chat.id, wait_msg.message_id)

print('🍌 Приватный Банановый бот запущен!')
if __name__ == "__main__":
    bot.infinity_polling(timeout=180, long_polling_timeout=60)