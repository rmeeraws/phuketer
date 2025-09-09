import asyncio
import logging
import os
import re
from inspect import iscoroutinefunction
import contextlib
import aiohttp  # <-- добавлено для heartbeat

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters.command import CommandStart
from aiogram.enums.chat_action import ChatAction
from aiogram.exceptions import TelegramBadRequest
from datetime import datetime
try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:
    ZoneInfo = None

# Импортируем наш менеджер для работы с LLM
from llm_manager import LLMManager

# Импортируем конфиг с ключами
from config import TELEGRAM_BOT_TOKEN

# Импортируем аналитику
from analytics import BotAnalytics

# ---- НОВОЕ: читаем переменные окружения для heartbeat ----
HEALTHCHECKS_PING_URL = os.getenv("HEALTHCHECKS_PING_URL", "").strip()
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "300"))

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Инициализируем бота и диспетчер
if not TELEGRAM_BOT_TOKEN:
    logging.error("TELEGRAM_BOT_TOKEN не найден. Убедитесь, что он установлен в .env файле.")
    exit()

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# Создаем экземпляр нашего LLM менеджера
llm_manager = LLMManager()

# Создаем экземпляр аналитики
analytics = BotAnalytics()

# Словарь для хранения истории сообщений каждого пользователя + чата
user_history = {}

# --- helpers для send_long_message ---

MD_HEADER_RE = re.compile(r'^\s*#{1,6}\s*', flags=re.MULTILINE)
ANCHOR_RE = re.compile(r'<a\s+href="([^"]+)">(.*?)</a>', flags=re.IGNORECASE | re.DOTALL)

def strip_markdown_headers(text: str) -> str:
    """Убираем markdown-заголовки вида '# Заголовок' в начале строк."""
    return MD_HEADER_RE.sub('', text)

def html_to_plain(text: str) -> str:
    """
    Простой и безопасный фолбэк:
    - <a href="URL">label</a> -> 'label (URL)'
    - <br> -> '\n'
    - прочие теги убираем
    """
    text = ANCHOR_RE.sub(lambda m: f"{m.group(2)} ({m.group(1)})", text)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p\s*>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<p\s*>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'</?(?:b|i|u|em|strong|span|code|pre|blockquote|tt)>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'</?[^>]+>', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text

def split_safely(text: str, max_length: int) -> list[str]:
    """
    Делим текст на куски <= max_length, стараясь резать по абзацам/предложениям.
    """
    if len(text) <= max_length:
        return [text]

    parts = []
    current = ""

    paragraphs = text.split('\n')
    for paragraph in paragraphs:
        if len(paragraph) > max_length:
            sentences = re.split(r'(?<=[\.\!\?])\s+', paragraph)
            for sent in sentences:
                if not sent:
                    continue
                while len(sent) > max_length:
                    if current:
                        parts.append(current)
                        current = ""
                    parts.append(sent[:max_length])
                    sent = sent[max_length:]
                if len(current) + len(sent) + 1 > max_length:
                    if current:
                        parts.append(current)
                    current = sent
                else:
                    current = (current + ' ' + sent).strip()
        else:
            addition = ('\n' + paragraph) if current else paragraph
            if len(current) + len(addition) > max_length:
                if current:
                    parts.append(current)
                current = paragraph
            else:
                current += addition

    if current:
        parts.append(current)

    return parts

# --- обновленный send_long_message ---

async def send_long_message(message: types.Message, text: str, max_length: int = 4000):
    """
    Отправляет длинное сообщение, разбивая его на части и сохраняя HTML-форматирование.
    Фолбэк: если Telegram ругается на HTML, преобразуем в plain text с сохранением ссылок.
    """
    text = strip_markdown_headers(text)
    parts = split_safely(text, max_length)

    for i, part in enumerate(parts):
        out = part
        if i > 0:
            out = f"...продолжение:\n{out}"
        if i < len(parts) - 1:
            out += "\n\n📄 <i>Продолжение следует...</i>"

        try:
            await message.reply(out, parse_mode='HTML')
        except TelegramBadRequest:
            plain = html_to_plain(out)
            await message.reply(plain)

# --- универсальный хелпер для LLM-вызова ---

async def fetch_llm_response(messages, model_name: str, user_id: int) -> str | None:
    fn = llm_manager.get_response
    if iscoroutinefunction(fn):
        return await fn(messages, model_name=model_name, user_id=user_id)
    else:
        return await asyncio.to_thread(fn, messages, model_name=model_name, user_id=user_id)

# --- индикатор прогресса ---

async def progress_notifier(bot: Bot, chat_id: int, message_id: int, stop_event: asyncio.Event):
    """
    Анимация: 'Окей-кап⏳', 'Окей-кап..⌛️', 'Окей-кап...⏳'
    """
    frames = ["Окей-кап⏳", "Окей-кап..⌛️", "Окей-кап...⏳"]
    idx = 0
    try:
        while not stop_event.is_set():
            try:
                await bot.edit_message_text(frames[idx % len(frames)], chat_id, message_id, parse_mode='HTML')
            except Exception:
                pass
            idx += 1
            await asyncio.sleep(1)  # шаг анимации
    except Exception as e:
        logging.error(f"progress_notifier error: {e}")

# --- Healthcheck heartbeat (НОВОЕ) ---

async def heartbeat_task():
    """
    Периодически пингует Healthchecks.
    Если процесс упадёт или Replit остановит workflow — пинги прекратятся,
    и Healthchecks пришлёт алерт в Telegram.
    """
    if not HEALTHCHECKS_PING_URL:
        logging.info("heartbeat: HEALTHCHECKS_PING_URL is empty, skipping.")
        return

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Однократный старт-пинг (не обязателен, но полезен для диагностики)
        with contextlib.suppress(Exception):
            await session.get(f"{HEALTHCHECKS_PING_URL}/start")

        while True:
            try:
                await session.get(HEALTHCHECKS_PING_URL)
                logging.debug("heartbeat: ping ok")
            except Exception as e:
                # Отсутствие пингов — и есть сигнал для алерта, поэтому просто логируем
                logging.warning(f"heartbeat: ping failed: {e}")
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)

# --- обработчики ---

def phuket_now_str() -> str:
    """Текущее время на Пхукете (UTC+7) в удобном формате."""
    if ZoneInfo:
        now = datetime.now(ZoneInfo("Asia/Bangkok"))  # Пхукет = Азия/Бангкок
    else:
        # Фолбэк: берём системное UTC и прибавляем 7 часов
        now = datetime.utcnow() + timedelta(hours=7)
    return now.strftime("%d.%m.%Y • %H:%M (UTC+7)")

@dp.message(lambda m: m.text and m.text.strip().lower() in {"/time", "time", "время"})
async def time_command(message: types.Message):
    await message.reply(f"Сейчас на Пхукете: <b>{phuket_now_str()}</b>", parse_mode="HTML")

# Перехват естественных вопросов: "сколько времени", "какое сейчас время", "time in Phuket" и т.п.
TIME_PAT = re.compile(
    r"(скол(ь|ъ)ко.*врем|како(е|й).*врем|сейчас.*врем|time.*phuket|current.*time.*phuket|время.*пхукет)",
    re.IGNORECASE
)

@dp.message(lambda m: m.text and TIME_PAT.search(m.text))
async def handle_time_question(message: types.Message):
    # Отвечаем детерминированно и не зовём модель
    await message.reply(f"Сейчас на Пхукете: <b>{phuket_now_str()}</b>", parse_mode="HTML")

@dp.message(CommandStart())
async def send_welcome(message: types.Message):
    if message.from_user and message.chat:
        chat_key = f"{message.from_user.id}_{message.chat.id}"
        user_history[chat_key] = []
    await message.reply(
        "Привет! 👋\n"
        "Я твой персональный нейро-эксперт по Пхукету! 🏝️\n\n"
        "Я могу помочь тебе с:\n"
        "📍 Поиском интересных мест (пляжи, храмы, кафе)\n"
        "💎 Рекомендациями по ресторанам и развлечениям\n"
        "🚗 Практическими советами (транспорт, аренда, обмен валюты)\n"
        "🗺️ И многим другим!\n\n"
        "Просто спроси меня о чём-нибудь, связанным с Пхукетом! 😉\n\n"
        "<i>Команды для админа:</i>\n"
        "/stats - статистика использования\n"
        "/topusers - топ активных пользователей"
    )

@dp.message(lambda message: message.text and message.text.startswith('/stats'))
async def send_stats(message: types.Message):
    if message.from_user:
        stats_summary = analytics.get_summary()
        await message.reply(stats_summary, parse_mode='HTML')

@dp.message(lambda message: message.text and message.text.startswith('/topusers'))
async def send_top_users(message: types.Message):
    if message.from_user:
        top_users = analytics.get_top_users(10)
        await message.reply(top_users, parse_mode='HTML')

@dp.message(F.voice)
async def handle_voice_message(message: types.Message):
    if not message.from_user or not message.voice:
        return

    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    chat_key = f"{user_id}_{message.chat.id}"

    logging.info(f"Получено голосовое сообщение от {username} в чате {message.chat.id}")

    analytics.track_user_message(user_id, username, is_voice=True)

    # Индикатор
    progress_msg = await bot.send_message(message.chat.id, "Окей-кап⏳", parse_mode='HTML')
    stop_event = asyncio.Event()
    progress_task = asyncio.create_task(progress_notifier(bot, message.chat.id, progress_msg.message_id, stop_event))

    try:
        if chat_key not in user_history:
            user_history[chat_key] = []

        voice_file = await bot.get_file(message.voice.file_id)
        if not voice_file.file_path:
            await message.reply("Не удалось получить голосовое сообщение.")
            return

        audio_filename = f"voice_{message.from_user.id}.ogg"
        await bot.download_file(voice_file.file_path, audio_filename)

        recognized_text = llm_manager.transcribe_audio(audio_filename)
        os.remove(audio_filename)

        if not recognized_text:
            await message.reply("Извини, не удалось распознать твою речь. Попробуй, пожалуйста, еще раз.")
            return

        user_history[chat_key].append({"role": "user", "content": recognized_text})

        response_text = await fetch_llm_response(
            user_history[chat_key],
            model_name="deepseek",
            user_id=user_id
        )

        if response_text:
            user_history[chat_key].append({"role": "assistant", "content": response_text})
            await send_long_message(message, response_text)
        else:
            await progress_msg.edit_text("🙈 Не смог получить ответ, попробуй ещё раз.")

        logging.info(f"Отправлен ответ на голосовое сообщение: {response_text}")

    except Exception as e:
        logging.error(f"Произошла ошибка при обработке голосового сообщения: {e}", exc_info=True)
        await message.reply("Извини, произошла какая-то ошибка. Попробуй ещё раз позже.")
    finally:
        stop_event.set()
        await progress_task
        try:
            await bot.delete_message(message.chat.id, progress_msg.message_id)
        except Exception:
            try:
                await progress_msg.edit_text("✅ Готово")
            except Exception:
                pass

@dp.message()
async def handle_text_message(message: types.Message):
    if not message.from_user or not message.text:
        return

    user_id = message.from_user.id
    user_text = message.text
    username = message.from_user.username or "Unknown"
    chat_key = f"{user_id}_{message.chat.id}"

    logging.info(f"Получено сообщение от {username} в чате {message.chat.id}: {user_text}")

    analytics.track_user_message(user_id, username, is_voice=False)

    # Индикатор
    progress_msg = await bot.send_message(message.chat.id, "Окей-кап⏳", parse_mode='HTML')
    stop_event = asyncio.Event()
    progress_task = asyncio.create_task(progress_notifier(bot, message.chat.id, progress_msg.message_id, stop_event))

    try:
        if chat_key not in user_history:
            user_history[chat_key] = []

        user_history[chat_key].append({"role": "user", "content": user_text})

        response_text = await fetch_llm_response(
            user_history[chat_key],
            model_name="deepseek",
            user_id=user_id
        )

        if response_text:
            user_history[chat_key].append({"role": "assistant", "content": response_text})
            await send_long_message(message, response_text)
        else:
            await progress_msg.edit_text("🙈 Не смог получить ответ, попробуй ещё раз.")

        logging.info(f"Отправлен ответ: {response_text}")

    except Exception as e:
        logging.error(f"Произошла ошибка при обработке сообщения: {e}", exc_info=True)
        await message.reply("Извини, произошла какая-то ошибка. Попробуй ещё раз позже.")
    finally:
        stop_event.set()
        await progress_task
        try:
            await bot.delete_message(message.chat.id, progress_msg.message_id)
        except Exception:
            try:
                await progress_msg.edit_text("✅ Готово")
            except Exception:
                pass

async def main():
    # Безопасный запуск heartbeat (если URL задан)
    hb_task = None
    try:
        if HEALTHCHECKS_PING_URL:
            hb_task = asyncio.create_task(heartbeat_task())
            logging.info("heartbeat: started (%ss)", HEARTBEAT_INTERVAL_SEC)

        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("Бот запущен!")
        await dp.start_polling(bot)
    finally:
        if hb_task:
            hb_task.cancel()
            with contextlib.suppress(Exception):
                await hb_task
        logging.info("Bot shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())
