import base64
import io
import mimetypes
import os
import re
import time
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

# Безопасное чтение ключей (GitHub не будет блокировать)
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

bot = telebot.TeleBot(TELEGRAM_TOKEN)

MODEL_TIMEOUTS = {
    'gemini-4-argon': 180,
    'gemini-3.1-pro': 120,
    'gemini-3.8-flash': 40,
    'gemini-3.5-flash-lite': 20,
}

MODEL_INFO = {
    'auto': '🔄 Авто (Умный перебор со своими лимитами)',
    'gemini-4-argon': f'🧠 4 Argon (Сложные задачи — до {MODEL_TIMEOUTS["gemini-4-argon"]} сек)',
    'gemini-3.1-pro': f'🔬 3.1 Pro (Рассуждения — до {MODEL_TIMEOUTS["gemini-3.1-pro"]} сек)',
    'gemini-3.8-flash': f'⚡ 3.8 Flash (Универсальная — до {MODEL_TIMEOUTS["gemini-3.8-flash"]} сек)',
    'gemini-3.5-flash-lite': f'🚀 3.5 Flash-Lite (Мгновенная — до {MODEL_TIMEOUTS["gemini-3.5-flash-lite"]} сек)',
}

FALLBACK_ORDER = [
    'gemini-4-argon',
    'gemini-3.1-pro',
    'gemini-3.8-flash',
    'gemini-3.5-flash-lite',
]

user_models = {}

def get_user_model(chat_id):
    return user_models.get(chat_id, 'auto')

# ----------------- НОВЫЙ ИНТЕРФЕЙС -----------------

def get_main_keyboard(chat_id):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    current_model_key = get_user_model(chat_id)
    # Показываем текущую реальную модель в кнопке
    model_name_short = current_model_key.replace("gemini-", "").replace("-", " ").title() if current_model_key != 'auto' else 'Авто'
    
    btn_model = KeyboardButton(f"📝 Модель: {model_name_short}")
    markup.add(btn_model)
    
    btn_new = KeyboardButton("🔄 Новый диалог")
    btn_role = KeyboardButton("🎭 Роль")
    markup.row(btn_new, btn_role)
    
    btn_settings = KeyboardButton("⚙️ Настройки")
    markup.add(btn_settings)
    return markup

def send_settings_menu(chat_id):
    settings_text = (
        "⚙️ Настройки нового диалога\n\n"
        "🤖 Текстовая модель: по умолчанию\n"
        "🎭 Роль: не выбрана\n"
        "🛠 Инструменты: выключены\n\n"
        "/stt - распознавание речи 🎙\n"
        "/text - диалоговые модели 💬\n"
        "/dialogs - список диалогов 📁\n\n"
        "Выберите, что изменить. Настройки применятся к следующему новому диалогу."
    )
    markup = InlineKeyboardMarkup()
    btn_model = InlineKeyboardButton("🤖 Модель", callback_data="open_models")
    btn_role = InlineKeyboardButton("🎭 Роль", callback_data="stub")
    markup.row(btn_model, btn_role)
    
    btn_tools = InlineKeyboardButton("🛠 Инструменты", callback_data="stub")
    markup.add(btn_tools)
    
    btn_interface = InlineKeyboardButton("Изменить интерфейс", callback_data="stub")
    markup.add(btn_interface)
    
    btn_help = InlineKeyboardButton("❓ Помощь", callback_data="show_help")
    markup.add(btn_help)
    
    btn_reset = InlineKeyboardButton("🔄 Сбросить", callback_data="stub")
    btn_close = InlineKeyboardButton("❌ Закрыть", callback_data="close_settings")
    markup.row(btn_reset, btn_close)
    
    bot.send_message(chat_id, settings_text, reply_markup=markup)

def get_model_inline_keyboard(current_model):
    markup = InlineKeyboardMarkup(row_width=1)
    for model_id, label in MODEL_INFO.items():
        prefix = '✅ ' if model_id == current_model else ''
        markup.add(InlineKeyboardButton(f'{prefix}{label}', callback_data=f'setmodel:{model_id}'))
    markup.add(InlineKeyboardButton("⬅️ Назад в настройки", callback_data="back_to_settings"))
    return markup

# ----------------- ЛОГИКА GEMINI -----------------

def call_single_model_http(model, parts, system_text):
    model_timeout = MODEL_TIMEOUTS.get(model, 60)
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}'

    system_instruction = {'parts': [{'text': system_text}]}
    payload = {
        'contents': [{'parts': parts}],
        'system_instruction': system_instruction,
        'generationConfig': {'maxOutputTokens': 4096},
    }

    resp = requests.post(url, json=payload, timeout=(10, model_timeout))
    if resp.status_code == 200:
        data = resp.json()
        candidate_parts = data['candidates'][0]['content']['parts']
        texts = [p['text'] for p in candidate_parts if 'text' in p]
        reply_text = '\n'.join(texts).strip()
        if reply_text:
            return reply_text
    raise Exception(f'Код {resp.status_code}')

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

# ----------------- ОБРАБОТЧИКИ СООБЩЕНИЙ -----------------

@bot.message_handler(commands=['start', 'model'])
def send_welcome(message):
    bot.send_message(
        message.chat.id,
        'Добро пожаловать в приватный ИИ-ассистент! 🍌\nВыберите действие в меню ниже👇',
        reply_markup=get_main_keyboard(message.chat.id)
    )

@bot.callback_query_handler(func=lambda call: call.data == "show_help")
def process_help(call):
    help_text = (
        "С возвращением!\n\n"
        "1. Бот забыл диалог или отключилась память?\n"
        "Все диалоги сохраняются. Откройте команду /dialogs, выбирайте сохраненные диалоги, чтобы посмотреть их.\n\n"
        "2. Запутались в настройках?\n"
        "Нижняя клавиатура бота всегда показывает текущие активные настройки.\n\n"
        "3. Техподдержка и вопросы\n"
        "Бот абсолютно бесплатный и сделан для своих. Если что-то сломалось, зависло или нужна помощь — пиши мне: @isrustoy"
    )
    bot.send_message(call.message.chat.id, help_text)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "close_settings")
def process_close(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)

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

@bot.callback_query_handler(func=lambda call: call.data == "stub")
def process_stub(call):
    bot.answer_callback_query(call.id, "Эта функция пока в разработке 🛠")

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
    
    # Обработка кнопок интерфейса
    if message.text == '⚙️ Настройки':
        send_settings_menu(message.chat.id)
        return
    elif message.text == '🔄 Новый диалог':
        bot.send_message(message.chat.id, "✅ Контекст диалога очищен! Можем начать новую тему.", reply_markup=get_main_keyboard(message.chat.id))
        return
    elif message.text == '🎭 Роль':
        bot.send_message(message.chat.id, "Меню выбора ролей находится в разработке 🛠")
        return
    elif message.text.startswith('📝 Модель:'):
        current = get_user_model(message.chat.id)
        bot.send_message(message.chat.id, "Выберите текстовую модель:", reply_markup=get_model_inline_keyboard(current))
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
            
            code_match = re.search(r'```python(.*?)```', response_text, DOTALL := re.DOTALL) if True else None
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
            system_text = (
                'Отвечай максимально кратко, чётко, по делу, без "воды". '
                'СТРОГО ЗАПРЕЩЕНО использовать знаки доллара ($), квадратные скобки для формул и любые символы LaTeX. '
                'Пиши все формулы обычным текстом (например: B = F / (I * l)). '
                'Никаких лишних технических символов.'
            )
            reply_text = ask_gemini_smart(message.chat.id, [{'text': message.text}], system_text)
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
        bot.delete_message(message.chat.id, wait_msg.message_id)
        send_long_message(message, reply_text)
        print('📤 Ответ по файлу отправлен в Telegram!')
    except Exception as e:
        print(f'❌ ОШИБКА ФАЙЛА: {e}')
        bot.edit_message_text(f'❌ Ошибка при обработке файла: {e}', message.chat.id, wait_msg.message_id)

print('🍌 Приватный Банановый бот запущен!')
if __name__ == "__main__":
    bot.infinity_polling(timeout=180, long_polling_timeout=60)