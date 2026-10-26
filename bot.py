import asyncio
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import telebot
from telebot.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from gemini_webapi import GeminiClient

# ----------------- ФОНОВЫЙ СЕРВЕР ДЛЯ RENDER -----------------
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()

# ----------------- НАСТРОЙКИ И АВТОРИЗАЦИЯ -----------------
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '').strip()
SECURE_1PSID = os.getenv('GEMINI_SECURE_1PSID', '').strip()
SECURE_1PSIDTS = os.getenv('GEMINI_SECURE_1PSIDTS', '').strip() or None

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задана переменная окружения TELEGRAM_TOKEN!")

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Выделенный фоновый цикл asyncio и потокобезопасная инициализация лока
bot_loop = asyncio.new_event_loop()
threading.Thread(target=bot_loop.run_forever, daemon=True).start()

_gemini_client = None
_plot_lock = threading.Lock()

async def _create_lock():
    return asyncio.Lock()

_client_lock = asyncio.run_coroutine_threadsafe(_create_lock(), bot_loop).result()

async def _get_or_init_client(force_reinit=False):
    global _gemini_client
    async with _client_lock:
        if not SECURE_1PSID:
            raise ValueError("Не задана переменная GEMINI_SECURE_1PSID на Render!")

        if _gemini_client is None or force_reinit:
            if _gemini_client is not None:
                try:
                    await _gemini_client.close()
                except Exception:
                    pass
            _gemini_client = GeminiClient(SECURE_1PSID, SECURE_1PSIDTS, proxies=None)
            await _gemini_client.init(timeout=60, auto_close=False, auto_refresh=True)
        return _gemini_client

# ----------------- УПРАВЛЕНИЕ МОДЕЛЯМИ -----------------
AVAILABLE_MODELS = {
    "argon": "gemini-4-argon",
    "3.8": "gemini-3.8-flash",
    "3.5": "gemini-3.5-flash",
    "lite": "gemini-3.5-flash-lite",
    "pro": "gemini-3.1-pro"
}
user_selected_model = {}

def get_user_model(chat_id):
    return user_selected_model.get(chat_id, "gemini-4-argon")

def get_user_model_short_name(chat_id):
    model_id = get_user_model(chat_id)
    mapping = {
        "gemini-4-argon": "4 Argon",
        "gemini-3.8-flash": "3.8 Flash",
        "gemini-3.5-flash": "3.5 Flash",
        "gemini-3.5-flash-lite": "3.5 Lite",
        "gemini-3.1-pro": "3.1 Pro"
    }
    return mapping.get(model_id, "4 Argon")

async def _ask_gemini_web_async(prompt, file_path=None, unique_prefix="0", chat_id=None):
    client = await _get_or_init_client()
    model_name = get_user_model(chat_id) if chat_id else "gemini-4-argon"

    try:
        if file_path and os.path.exists(file_path):
            response = await client.generate_content(prompt, files=[file_path], model=model_name)
        else:
            response = await client.generate_content(prompt, model=model_name)
    except Exception as first_err:
        print(f"⚠️ Переподключение сессии Gemini после ошибки: {first_err}")
        client = await _get_or_init_client(force_reinit=True)
        if file_path and os.path.exists(file_path):
            response = await client.generate_content(prompt, files=[file_path], model=model_name)
        else:
            response = await client.generate_content(prompt, model=model_name)

    saved_images = []
    if getattr(response, 'images', None):
        for idx, img in enumerate(response.images):
            img_name = f"gen_img_{unique_prefix}_{idx}.png"
            try:
                saved_path = await img.save(path=".", filename=img_name)
                actual_path = saved_path if (isinstance(saved_path, str) and os.path.exists(saved_path)) else img_name
                if os.path.exists(actual_path):
                    saved_images.append(actual_path)
            except Exception as img_err:
                print(f"⚠️ Не удалось сохранить картинку {idx}: {img_err}")

    return (response.text or ""), saved_images

def ask_gemini_sync(prompt, file_path=None, unique_prefix="0", chat_id=None):
    future = asyncio.run_coroutine_threadsafe(
        _ask_gemini_web_async(prompt, file_path, unique_prefix, chat_id), bot_loop
    )
    return future.result(timeout=150)

# ----------------- ОЧИСТКА ТЕКСТА -----------------
def clean_formatting(text):
    if not text:
        return ""
    text = re.sub(r'\*{2,}', '', text)
    text = re.sub(r'(?m)^\s*\*\s+', '• ', text)
    text = re.sub(r'(?<![\w*])\*([^\s*][^*]*?[^\s*])\*(?![\w*])', r'\1', text)
    text = re.sub(r'(?<![\w*])\*([^\s*])\*(?![\w*])', r'\1', text)
    text = re.sub(r'(?m)^\s*#+\s*', '', text)
    text = text.replace('$', '').replace('`', '')
    return text.strip()

def strip_service_markers(text):
    if not text:
        return ""
    text = re.sub(r'\[SEND_AS_FILE:[^\]]+\]', '', text)
    text = re.sub(r'\[GENERATE_IMAGE:.*?\]', '', text, flags=re.DOTALL)
    text = re.sub(r'```python.*?```', '[Построен график]', text, flags=re.DOTALL | re.IGNORECASE)
    return clean_formatting(text)

# ----------------- ХРАНИЛИЩЕ КОНТЕКСТА -----------------
user_context = {}
MAX_HISTORY = 10

def add_to_context(chat_id, role, text):
    if chat_id not in user_context:
        user_context[chat_id] = []
    user_context[chat_id].append({"role": role, "text": text})
    if len(user_context[chat_id]) > MAX_HISTORY:
        user_context[chat_id] = user_context[chat_id][-MAX_HISTORY:]

def get_context_as_prompt(chat_id):
    if chat_id not in user_context or not user_context[chat_id]:
        return ""
    formatted = []
    for item in user_context[chat_id]:
        role_label = "Пользователь" if item["role"] == "user" else "Ассистент"
        formatted.append(f"{role_label}: {item['text']}")
    return "\n".join(formatted)

def clear_context(chat_id):
    user_context[chat_id] = []
    user_selected_model.pop(chat_id, None)

# ----------------- КЛАВИАТУРЫ И МЕНЮ -----------------
def get_main_keyboard(chat_id):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    model_name = get_user_model_short_name(chat_id)
    
    btn_model = KeyboardButton(f"📝 Модель: {model_name}")
    btn_new = KeyboardButton("🔄 Новый диалог")
    btn_settings = KeyboardButton("⚙️ Настройки")
    
    markup.row(btn_model)
    markup.row(btn_new, btn_settings)
    return markup

def send_settings_menu(chat_id):
    current = get_user_model(chat_id)
    settings_text = (
        "⚙️ *Панель настроек*\n\n"
        f"🤖 Активная модель: `{current}`\n"
        f"🧠 Память контекста: *Включена* (до {MAX_HISTORY} реплик)\n\n"
        "Выберите необходимое действие:"
    )
    markup = InlineKeyboardMarkup(row_width=1)
    btn_choose_model = InlineKeyboardButton("🤖 Выбрать модель", callback_data="open_model_selector")
    btn_reset = InlineKeyboardButton("🔄 Очистить контекст", callback_data="reset_context_inline")
    markup.add(btn_choose_model, btn_reset)
    
    markup_row2 = InlineKeyboardMarkup(row_width=2)
    markup_row2.add(
        InlineKeyboardButton("❓ Помощь", callback_data="show_help"),
        InlineKeyboardButton("❌ Закрыть", callback_data="close_settings")
    )
    
    bot.send_message(chat_id, settings_text, parse_mode='Markdown', reply_markup=markup)

def send_model_selection_menu(chat_id):
    current = get_user_model(chat_id)
    text = f"🤖 *Выбор модели ИИ*\n\nТекущая модель: `{current}`\nВыберите нужную версию:"
    
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("⚡ 4 Argon", callback_data="set_model_argon"),
        InlineKeyboardButton("🚀 3.8 Flash", callback_data="set_model_3.8"),
        InlineKeyboardButton("💡 3.5 Flash", callback_data="set_model_3.5"),
        InlineKeyboardButton("⚡ 3.5 Lite", callback_data="set_model_lite"),
        InlineKeyboardButton("🧠 3.1 Pro", callback_data="set_model_pro")
    )
    markup.add(InlineKeyboardButton("⬅️ Назад в настройки", callback_data="back_to_settings"))
    bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)

def safe_reply(message, text):
    try:
        return bot.reply_to(message, text, reply_markup=get_main_keyboard(message.chat.id))
    except Exception:
        return bot.send_message(message.chat.id, text, reply_markup=get_main_keyboard(message.chat.id))

def send_long_message(message, text):
    cleaned = clean_formatting(text)
    if not cleaned:
        cleaned = 'Пустой ответ от модели.'
    for i in range(0, len(cleaned), 4000):
        safe_reply(message, cleaned[i:i + 4000])

# ----------------- УМНАЯ ОБРАБОТКА ОТВЕТА -----------------
def process_smart_response(message, reply_text, saved_images, chart_path, unique_prefix, force_file=False):
    chat_id = message.chat.id

    img_gen_match = re.search(r'\[GENERATE_IMAGE:(.*?)\]', reply_text, re.DOTALL)
    if img_gen_match and not saved_images:
        img_description = img_gen_match.group(1).strip() or (message.text or "image")
        img_prompt = f"Generate a high-quality photo or image of: {img_description}. Return the generated image directly."
        _, gen_images = ask_gemini_sync(img_prompt, unique_prefix=f"{unique_prefix}_art", chat_id=chat_id)
        if gen_images:
            saved_images = gen_images
            reply_text = re.sub(r'\[GENERATE_IMAGE:.*?\]', '', reply_text, flags=re.DOTALL).strip()

    if saved_images:
        clean_cap = clean_formatting(re.sub(r'\[GENERATE_IMAGE:.*?\]', '', reply_text, flags=re.DOTALL))
        for idx, img_path in enumerate(saved_images):
            if os.path.exists(img_path):
                try:
                    with open(img_path, 'rb') as photo:
                        cap = clean_cap[:1024] if idx == 0 and clean_cap else "🎨 Сгенерировано по вашему запросу"
                        bot.send_photo(chat_id, photo, caption=cap, reply_markup=get_main_keyboard(chat_id))
                finally:
                    if os.path.exists(img_path):
                        os.remove(img_path)
        if len(clean_cap) > 1024:
            send_long_message(message, clean_cap[1024:])
        return

    file_marker = re.search(r'\[SEND_AS_FILE:([^\]]+)\]', reply_text)
    if file_marker or force_file:
        ext_map = {
            'python': 'py', 'py': 'py', 'json': 'json', 'html': 'html',
            'js': 'js', 'javascript': 'js', 'cpp': 'cpp', 'c': 'c',
            'csv': 'csv', 'sql': 'sql', 'xml': 'xml', 'md': 'md', 'txt': 'txt'
        }
        clean_reply = re.sub(r'\[SEND_AS_FILE:[^\]]+\]', '', reply_text).strip()
        code_block = re.search(r'```([a-zA-Z0-9_-]+)?\s*\n(.*?)```', clean_reply, re.DOTALL)
        extra_explanation = ""

        if file_marker:
            raw_name = os.path.basename(file_marker.group(1).strip())
            safe_name = re.sub(r'[^a-zA-Zа-яА-ЯёЁ0-9_.-]', '_', raw_name).strip('._')
            if not safe_name:
                safe_name = f"document_{unique_prefix}.txt"
            elif '.' not in safe_name:
                safe_name += ".txt"
            file_name = f"{unique_prefix}_{safe_name}"
            if code_block:
                file_content = code_block.group(2).strip()
                extra_explanation = clean_formatting(re.sub(r'```.*?```', '', clean_reply, flags=re.DOTALL))
            else:
                file_content = clean_formatting(clean_reply)
        elif code_block:
            lang = (code_block.group(1) or "txt").strip().lower()
            file_ext = ext_map.get(lang, 'txt')
            file_name = f"result_{unique_prefix}.{file_ext}"
            file_content = code_block.group(2).strip()
            extra_explanation = clean_formatting(re.sub(r'```.*?```', '', clean_reply, flags=re.DOTALL))
        else:
            file_name = f"document_{unique_prefix}.txt"
            file_content = clean_formatting(clean_reply)

        doc_caption = extra_explanation[:1024] if extra_explanation else "📂 Готовый файл по вашему запросу:"

        try:
            with open(file_name, "w", encoding="utf-8") as f:
                f.write(file_content)

            with open(file_name, "rb") as f:
                bot.send_document(
                    chat_id,
                    f,
                    caption=doc_caption,
                    reply_markup=get_main_keyboard(chat_id)
                )
        finally:
            if os.path.exists(file_name):
                os.remove(file_name)

        if len(extra_explanation) > 1024:
            send_long_message(message, extra_explanation[1024:])
        return

    code_match = re.search(r'```python\s*\n(.*?)```', reply_text, re.DOTALL | re.IGNORECASE)
    if code_match and (chart_path in code_match.group(1) or 'plt.savefig' in code_match.group(1) or 'plt.plot' in code_match.group(1)):
        python_code = code_match.group(1).strip()
        python_code = re.sub(r'plt\.show\s*\(\s*\)', '', python_code)
        python_code = re.sub(
            r'plt\.savefig\s*\([^)]*\)',
            f"plt.savefig('{chart_path}', bbox_inches='tight', dpi=200)",
            python_code
        )

        with _plot_lock:
            try:
                plt.close('all')
                plt.figure(figsize=(8, 5))
                
                # Защита от дублирующих импортов внутри кода графиков
                python_code = re.sub(r'^\s*(import|from)\s+matplotlib.*$', '', python_code, flags=re.MULTILINE)
                python_code = re.sub(r'^\s*(import|from)\s+numpy.*$', '', python_code, flags=re.MULTILINE)
                python_code = re.sub(r'^\s*(import|from)\s+pyplot.*$', '', python_code, flags=re.MULTILINE)

                exec_env = {"__builtins__": __builtins__, "plt": plt, "np": np, "matplotlib": matplotlib}
                exec(python_code, exec_env, exec_env)
                
                if not os.path.exists(chart_path) and plt.get_fignums():
                    plt.savefig(chart_path, bbox_inches='tight', dpi=200)
                plt.close('all')
            except Exception as ex:
                print(f"⚠️ Ошибка автопостроения графика: {ex}")
                plt.close('all')

        if os.path.exists(chart_path):
            text_without_code = re.sub(r'```python.*?```', '', reply_text, flags=re.DOTALL | re.IGNORECASE).strip()
            clean_caption = clean_formatting(text_without_code)
            try:
                with open(chart_path, 'rb') as photo:
                    bot.send_photo(
                        chat_id,
                        photo,
                        caption=clean_caption[:1024] if clean_caption else "📈 График к задаче:",
                        reply_markup=get_main_keyboard(chat_id)
                    )
            finally:
                if os.path.exists(chart_path):
                    os.remove(chart_path)

            if len(clean_caption) > 1024:
                send_long_message(message, clean_caption[1024:])
            return

    send_long_message(message, reply_text)

# ----------------- ОБРАБОТЧИКИ TELEGRAM -----------------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    clear_context(message.chat.id)
    bot.send_message(
        message.chat.id,
        'Добро пожаловать в BananGPT 🍌\n'
        'Пишите любой запрос или скидывайте задачу — я сам пойму, нужен ли график, фото или файл!',
        reply_markup=get_main_keyboard(message.chat.id)
    )

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    chat_id = call.message.chat.id
    data = call.data

    if data == "show_help":
        help_text = (
            "💡 *Справка по боту*\n\n"
            "1. *Контекст диалога:*\n"
            "Бот помнит суть текущего разговора. Чтобы начать новую тему без старых хвостов — нажмите «🔄 Новый диалог».\n\n"
            "2. *Выбор модели:*\n"
            "Вы можете переключаться между моделями линейки Gemini через меню настроек или кнопку на клавиатуре.\n\n"
            "3. *Поддержка:* @isrustoy"
        )
        try:
            bot.edit_message_text(help_text, chat_id, call.message.message_id, parse_mode='Markdown')
        except Exception:
            bot.send_message(chat_id, help_text, parse_mode='Markdown')
        bot.answer_callback_query(call.id)

    elif data == "close_settings":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(call.id)

    elif data == "reset_context_inline":
        clear_context(chat_id)
        bot.answer_callback_query(call.id, "Контекст диалога очищен!")
        bot.send_message(chat_id, "🔄 Контекст диалога очищен. Можем начать новую тему!", reply_markup=get_main_keyboard(chat_id))

    elif data == "open_model_selector":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        send_model_selection_menu(chat_id)
        bot.answer_callback_query(call.id)

    elif data == "back_to_settings":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        send_settings_menu(chat_id)
        bot.answer_callback_query(call.id)

    elif data.startswith("set_model_"):
        key = data.replace("set_model_", "")
        if key in AVAILABLE_MODELS:
            user_selected_model[chat_id] = AVAILABLE_MODELS[key]
            model_display = get_user_model_short_name(chat_id)
            bot.answer_callback_query(call.id, f"Модель изменена на {model_display}!")
            try:
                bot.delete_message(chat_id, call.message.message_id)
            except Exception:
                pass
            bot.send_message(
                chat_id, 
                f"✅ Активная модель успешно изменена на: `{AVAILABLE_MODELS[key]}`", 
                parse_mode='Markdown', 
                reply_markup=get_main_keyboard(chat_id)
            )

@bot.message_handler(content_types=['text'])
def handle_text(message):
    chat_id = message.chat.id
    unique_prefix = f"{chat_id}_{message.message_id}"
    text_stripped = message.text.strip()

    if text_stripped.lower().startswith('📝 модель:'):
        send_model_selection_menu(chat_id)
        return

    if text_stripped.lower().startswith('/model'):
        parts = text_stripped.split()
        if len(parts) > 1 and parts[1].lower() in AVAILABLE_MODELS:
            chosen_key = parts[1].lower()
            user_selected_model[chat_id] = AVAILABLE_MODELS[chosen_key]
            bot.reply_to(message, f"✅ Активная модель успешно изменена на: `{AVAILABLE_MODELS[chosen_key]}`", parse_mode='Markdown', reply_markup=get_main_keyboard(chat_id))
        else:
            available_list = ", ".join(AVAILABLE_MODELS.keys())
            bot.reply_to(message, f"⚠️ Неверный формат. Доступные ключи: {available_list}\nПример: `/model 3.5`", parse_mode='Markdown', reply_markup=get_main_keyboard(chat_id))
        return

    text_lower = text_stripped.lower()

    if message.text == '⚙️ Настройки':
        send_settings_menu(chat_id)
        return
    elif message.text == '🔄 Новый диалог':
        clear_context(chat_id)
        bot.send_message(
            chat_id,
            "✅ Контекст диалога очищен! Можем начать новую тему.",
            reply_markup=get_main_keyboard(chat_id)
        )
        return

    print(f'\n📥 Сообщение: {message.text} [Модель: {get_user_model(chat_id)}]')

    is_direct_image = any(kw in text_lower for kw in [
        'нарисуй фото', 'сгенерируй фото', 'создай фото', 'сгенерируй картинку',
        'нарисуй картинку', 'сделай фото', 'сделай картинку', 'нарисуй арт'
    ])
    force_file = any(kw in text_lower for kw in [
        'сохрани в файл', 'сделай файл', 'выгрузи в документе', 'отправь файлом', 'создай файл', 'скинь файлом'
    ])

    wait_msg = safe_reply(message, '⏳ Думаю...')
    bot.send_chat_action(chat_id, 'upload_photo' if is_direct_image else 'typing')

    try:
        if is_direct_image:
            img_prompt = (
                f"Generate a high-quality image based on this description: {message.text}. "
                "Please generate and return an image directly."
            )
            reply_text, saved_images = ask_gemini_sync(img_prompt, unique_prefix=unique_prefix, chat_id=chat_id)
            try:
                bot.delete_message(chat_id, wait_msg.message_id)
            except Exception:
                pass

            if saved_images:
                for idx, img_path in enumerate(saved_images):
                    if os.path.exists(img_path):
                        try:
                            with open(img_path, 'rb') as photo:
                                bot.send_photo(
                                    chat_id,
                                    photo,
                                    caption=f"🎨 Сгенерировано по запросу: {message.text[:200]}" if idx == 0 else None,
                                    reply_markup=get_main_keyboard(chat_id)
                                )
                        finally:
                            if os.path.exists(img_path):
                                os.remove(img_path)
            else:
                send_long_message(message, reply_text)
            return

        chart_path = f"chart_{unique_prefix}.png"
        add_to_context(chat_id, "user", message.text)
        history_str = get_context_as_prompt(chat_id)

        smart_prompt = (
            "Ты умный универсальный ассистент BananGPT.\n"
            f"--- ИСТОРИЯ ДИАЛОГА ---\n{history_str}\n--- КОНЕЦ ИСТОРИИ ---\n\n"
            f"Запрос или задача пользователя: {message.text}\n\n"
            "СТРОГИЕ ПРАВИЛА ОФОРМЛЕНИЯ И ДЕЙСТВИЙ:\n"
            "1. ЧИСТЫЙ ТЕКСТ: КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать жирный шрифт и звездочки (** или ***), решетки (#), знаки доллара ($) и разметку Markdown/LaTeX. Пиши чистым текстом, а формулы — обычными символами (например: B = F / (I * l)).\n"
            "2. АВТО-ГРАФИК: Прочитай условие задачи. Если по смыслу задачи требуется построить математический или инженерный график, диаграмму, кривую охлаждения, эпюру (или пользователь сам попросил график) — дай текстовое решение И напиши рабочий код matplotlib в блоке ```python ... ```, который сохраняет график строго в файл "
            f"'{chart_path}' с помощью plt.savefig('{chart_path}', bbox_inches='tight'). Не используй plt.show(). Если график не требуется — код matplotlib не пиши.\n"
            "3. АВТО-ФАЙЛ: Если пользователь просит выдать ответ файлом, документом, скриптом или таблицей — напиши в самом начале ответа метку [SEND_AS_FILE:имя_файла.расширение] и ниже приведи содержимое файла.\n"
            "4. АВТО-ФОТО: Если пользователь просит нарисовать художественную картинку, иллюстрацию или фото (а не математический график) — напиши в ответе метку [GENERATE_IMAGE:подробное описание картинки на английском языке]."
        )

        reply_text, saved_images = ask_gemini_sync(smart_prompt, unique_prefix=unique_prefix, chat_id=chat_id)
        add_to_context(chat_id, "model", strip_service_markers(reply_text))

        try:
            bot.delete_message(chat_id, wait_msg.message_id)
        except Exception:
            pass

        process_smart_response(message, reply_text, saved_images, chart_path, unique_prefix, force_file=force_file)
        print('📤 Ответ отправлен в Telegram!')

    except Exception as e:
        print(f'❌ ОШИБКА: {e}')
        try:
            bot.edit_message_text(f'❌ Ошибка: {e}', chat_id, wait_msg.message_id)
        except Exception:
            bot.send_message(chat_id, f'❌ Ошибка: {e}')

@bot.message_handler(content_types=['photo', 'document'])
def handle_files(message):
    chat_id = message.chat.id
    unique_prefix = f"{chat_id}_{message.message_id}"
    print('\n📎 Получен файл/фото от пользователя')
    wait_msg = safe_reply(message, '⏳ Анализирую задачу и файл...')
    bot.send_chat_action(chat_id, 'upload_photo')

    temp_path = None
    chart_path = f"chart_{unique_prefix}.png"
    try:
        if message.photo:
            file_info = bot.get_file(message.photo[-1].file_id)
            temp_path = f"upload_{unique_prefix}.jpg"
        elif message.document:
            file_info = bot.get_file(message.document.file_id)
            orig_name = message.document.file_name or "document.pdf"
            safe_ext = os.path.splitext(orig_name)[1] or ".bin"
            temp_path = f"upload_{unique_prefix}{safe_ext}"
        else:
            bot.edit_message_text("❌ Не удалось распознать файл.", chat_id, wait_msg.message_id)
            return

        downloaded_bytes = bot.download_file(file_info.file_path)
        with open(temp_path, "wb") as f:
            f.write(downloaded_bytes)

        user_caption = message.caption if message.caption else "Реши задачу или проанализируй этот файл подробно и по делу."
        force_file = any(kw in user_caption.lower() for kw in ['в файл', 'файлом', 'документом'])

        smart_file_prompt = (
            f"Запрос пользователя к прикрепленному файлу: {user_caption}\n\n"
            "СТРОГИЕ ПРАВИЛА:\n"
            "1. ЧИСТЫЙ ТЕКСТ: ЗАПРЕЩЕНО использовать звездочки (** или ***), решетки (#), знаки доллара ($) и LaTeX. Пиши чистым текстом.\n"
            "2. АВТО-ГРАФИК: Если в задаче на фото/в файле требуется построить график, диаграмму или кривую (или об этом попросил пользователь) — реши задачу И напиши код matplotlib в блоке ```python ... ```, который сохраняет график строго в файл "
            f"'{chart_path}' через plt.savefig('{chart_path}', bbox_inches='tight'). Не используй plt.show(). Если график не нужен — код не пиши.\n"
            "3. АВТО-ФАЙЛ: Если пользователь просит ответ файлом — добавь в начале метку [SEND_AS_FILE:имя_файла.расширение]."
        )

        reply_text, saved_images = ask_gemini_sync(smart_file_prompt, file_path=temp_path, unique_prefix=unique_prefix, chat_id=chat_id)
        add_to_context(chat_id, "user", f"[Отправлен файл]: {user_caption}")
        add_to_context(chat_id, "model", strip_service_markers(reply_text))

        try:
            bot.delete_message(chat_id, wait_msg.message_id)
        except Exception:
            pass

        process_smart_response(message, reply_text, saved_images, chart_path, unique_prefix, force_file=force_file)
        print('📤 Ответ по файлу отправлен в Telegram!')
    except Exception as e:
        print(f'❌ ОШИБКА ФАЙЛА: {e}')
        try:
            bot.edit_message_text(f'❌ Ошибка при обработке файла: {e}', chat_id, wait_msg.message_id)
        except Exception:
            bot.send_message(chat_id, f'❌ Ошибка при обработке файла: {e}')
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

print('🍌 Приватный Банановый веб-бот запущен!')
if __name__ == "__main__":
    while True:
        try:
            bot.infinity_polling(timeout=180, long_polling_timeout=60)
        except Exception as crash_err:
            print(f"⚠️ Перезапуск polling после сбоя: {crash_err}")
            time.sleep(5)