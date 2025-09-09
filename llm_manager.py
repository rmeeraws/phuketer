import os
import asyncio
from openai import OpenAI  # Библиотека openai теперь используется для работы с DeepSeek
from googleapiclient.discovery import build
import logging
import re

# Импортируем ключи через модуль config, который заранее загружает переменные окружения
from config import (
    DEEPSEEK_API_KEY,
    OPENAI_API_KEY,
    GOOGLE_SEARCH_API_KEY,
    CUSTOM_SEARCH_ENGINE_ID,
)

# Класс для работы с поисковиком Google
class GoogleSearch:
    def __init__(self, api_key: str, cse_id: str):
        self.api_key = api_key
        self.cse_id = cse_id
        self.service = build("customsearch", "v1", developerKey=self.api_key)

    async def search(self, query: str, num: int = 5):
        try:
            loop = asyncio.get_event_loop()
            res = await loop.run_in_executor(
                None,
                lambda: self.service.cse().list(
                    q=query,
                    cx=self.cse_id,
                    num=num
                ).execute()
            )
            if 'items' not in res:
                logging.warning("Поиск не дал результатов.")
                return []
            return res.get('items', [])
        except Exception as e:
            logging.error(f"Ошибка при поиске Google: {e}", exc_info=True)
            return []

class LLMManager:
    """Класс для управления и выбора языковых моделей."""

    def __init__(self):
        self.deepseek_client = None

        if DEEPSEEK_API_KEY:
            self.deepseek_client = OpenAI(
                api_key=DEEPSEEK_API_KEY,
                base_url="https://api.deepseek.com/v1",
            )
        elif not OPENAI_API_KEY:
            raise RuntimeError(
                "Either DEEPSEEK_API_KEY or OPENAI_API_KEY must be set"
            )

        if GOOGLE_SEARCH_API_KEY and CUSTOM_SEARCH_ENGINE_ID:
            self.google_search_client = GoogleSearch(
                api_key=GOOGLE_SEARCH_API_KEY,
                cse_id=CUSTOM_SEARCH_ENGINE_ID,
            )
        else:
            self.google_search_client = None

    def _convert_markdown_to_html(self, text: str) -> str:
        """
        Конвертирует базовый Markdown-формат в HTML для корректного отображения в Telegram.
        """
        # Сначала преобразуем ссылки, так как они могут содержать скобки и другие символы
        text = re.sub(r'\[(.*?)\]\((.*?)\)', r'<a href="\2">\1</a>', text)
        # Затем жирный текст
        text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
        # Затем курсив
        text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
        return text

    async def get_deepseek_response(self, user_messages: list, user_id: int = 0):
        last_user_prompt = user_messages[-1]['content']
        search_needed = self.check_if_search_needed(last_user_prompt)

        search_info = ""
        if search_needed and self.google_search_client:
            if "отзывы" in last_user_prompt.lower():
                search_query = last_user_prompt.replace("отзывы", "рекомендации")
            else:
                search_query = last_user_prompt

            logging.info(f"Выполняем поиск по запросу: {search_query}")
            
            if user_id:
                try:
                    from analytics import BotAnalytics
                    analytics = BotAnalytics()
                    analytics.track_search(user_id, search_query)
                except ImportError:
                    pass
            
            search_results = await self.google_search_client.search(search_query)

            if search_results:
                for item in search_results:
                    search_info += f"Заголовок: {item.get('title')}\n"
                    search_info += f"Ссылка: {item.get('link')}\n"
                    search_info += f"Описание: {item.get('snippet')}\n\n"

        try:
            with open('bot_knowledge.md', 'r', encoding='utf-8') as f:
                system_prompt = f.read()
        except FileNotFoundError:
            system_prompt = (
                "Ты - эксперт по Пхукету, дружелюбный и знающий местный житель. "
                "Отвечай кратко, без \"воды\", всегда давай только полезные советы. Используй emojis. "
                "Используй HTML-разметку (теги <b>, <i>) для выделения важных моментов."
            )

        if search_info:
            system_prompt += f"\n\nАктуальная информация из поиска:\n{search_info}"

        messages = [{"role": "system", "content": system_prompt}] + user_messages

        try:
            if not self.deepseek_client:
                return await self.get_openai_response(user_messages, user_id)

            response = self.deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                max_tokens=4000,
                temperature=0.7,
            )
            return self._convert_markdown_to_html(response.choices[0].message.content)
        except Exception as e:
            logging.error(f"Ошибка при работе с DeepSeek API: {e}", exc_info=True)
            return None

    def check_if_search_needed(self, prompt: str) -> bool:
        """
        Определяет, нужен ли поиск для ответа на запрос.
        """
        search_keywords = [
            "актуальн", "сейчас", "сегодня", "вчера", "завтра", "недавно",
            "новости", "цены", "расписание", "работает", "открыт", "закрыт",
            "время работы", "курс", "валют", "обмен", "погода", "отзывы"
        ]
        prompt_lower = prompt.lower()
        return any(keyword in prompt_lower for keyword in search_keywords)

    async def get_response(self, user_messages: list, model_name: str = "deepseek", user_id: int = 0):
        """
        Получает ответ от указанной модели.
        """
        if model_name == "deepseek":
            if self.deepseek_client:
                return await self.get_deepseek_response(user_messages, user_id)
            return await self.get_openai_response(user_messages, user_id)
        elif model_name == "openai":
            return await self.get_openai_response(user_messages, user_id)
        else:
            if self.deepseek_client:
                return await self.get_deepseek_response(user_messages, user_id)
            return await self.get_openai_response(user_messages, user_id)

    async def get_openai_response(self, user_messages: list, user_id: int = 0):
        """
        Получает ответ от OpenAI API.
        """
        if not OPENAI_API_KEY:
            return await self.get_deepseek_response(user_messages, user_id)

        from openai import OpenAI

        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        
        last_user_prompt = user_messages[-1]['content']
        search_needed = self.check_if_search_needed(last_user_prompt)

        search_info = ""
        if search_needed and self.google_search_client:
            if "отзывы" in last_user_prompt.lower():
                search_query = last_user_prompt.replace("отзывы", "рекомендации")
            else:
                search_query = last_user_prompt

            logging.info(f"Выполняем поиск по запросу: {search_query}")
            
            if user_id:
                try:
                    from analytics import BotAnalytics
                    analytics = BotAnalytics()
                    analytics.track_search(user_id, search_query)
                except ImportError:
                    pass
            
            search_results = await self.google_search_client.search(search_query)

            if search_results:
                for item in search_results:
                    search_info += f"Заголовок: {item.get('title')}\n"
                    search_info += f"Ссылка: {item.get('link')}\n"
                    search_info += f"Описание: {item.get('snippet')}\n\n"

        try:
            with open('bot_knowledge.md', 'r', encoding='utf-8') as f:
                system_prompt = f.read()
        except FileNotFoundError:
            system_prompt = (
                "Ты - эксперт по Пхукету, дружелюбный и знающий местный житель. "
                "Отвечай кратко, без \"воды\", всегда давай только полезные советы. Используй emojis. "
                "Используй HTML-разметку (теги <b>, <i>) для выделения важных моментов."
            )

        if search_info:
            system_prompt += f"\n\nАктуальная информация из поиска:\n{search_info}"

        messages = [{"role": "system", "content": system_prompt}] + user_messages

        try:
            response = openai_client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=messages,
                max_tokens=4000,
                temperature=0.7
            )
            return self._convert_markdown_to_html(response.choices[0].message.content)
        except Exception as e:
            logging.error(f"Ошибка при работе с OpenAI API: {e}", exc_info=True)
            return await self.get_deepseek_response(user_messages, user_id)

    def transcribe_audio(self, audio_file_path: str) -> str:
        """
        Транскрибирует аудиофайл в текст с помощью OpenAI Whisper.
        """
        try:
            if OPENAI_API_KEY:
                from openai import OpenAI

                client = OpenAI(api_key=OPENAI_API_KEY)

                with open(audio_file_path, "rb") as audio_file:
                    transcript = client.audio.transcriptions.create(
                        model="whisper-1",
                        file=audio_file,
                    )
                return transcript.text
            else:
                logging.warning(
                    "OpenAI API ключ не найден, транскрипция недоступна"
                )
                return ""
        except Exception as e:
            logging.error(f"Ошибка при транскрипции аудио: {e}", exc_info=True)
            return ""
