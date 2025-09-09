import json
import os
from datetime import datetime, date
from typing import Dict, Any

class BotAnalytics:
    """
    Простая система аналитики для Telegram бота.
    Сохраняет статистику в JSON файл.
    """
    
    def __init__(self, stats_file: str = "bot_stats.json"):
        self.stats_file = stats_file
        self.stats = self._load_stats()
    
    def _load_stats(self) -> Dict[str, Any]:
        """Загружает статистику из файла"""
        if os.path.exists(self.stats_file):
            try:
                with open(self.stats_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                pass
        
        # Инициализируем пустую статистику
        return {
            "total_users": 0,
            "total_messages": 0,
            "total_voice_messages": 0,
            "total_searches": 0,
            "users": {},
            "daily_stats": {},
            "start_date": datetime.now().isoformat()
        }
    
    def _save_stats(self):
        """Сохраняет статистику в файл"""
        try:
            with open(self.stats_file, 'w', encoding='utf-8') as f:
                json.dump(self.stats, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Ошибка сохранения статистики: {e}")
    
    def track_user_message(self, user_id: int, username: str = "Unknown", is_voice: bool = False):
        """Отслеживает сообщение от пользователя"""
        today = date.today().isoformat()
        user_key = str(user_id)
        
        # Общая статистика
        self.stats["total_messages"] += 1
        if is_voice:
            self.stats["total_voice_messages"] += 1
        
        # Статистика пользователя
        if user_key not in self.stats["users"]:
            self.stats["total_users"] += 1
            self.stats["users"][user_key] = {
                "username": username,
                "first_seen": datetime.now().isoformat(),
                "messages_count": 0,
                "voice_messages_count": 0,
                "searches_triggered": 0
            }
        
        # Обновляем данные пользователя
        user_stats = self.stats["users"][user_key]
        user_stats["messages_count"] += 1
        user_stats["last_seen"] = datetime.now().isoformat()
        if username:
            user_stats["username"] = username
        if is_voice:
            user_stats["voice_messages_count"] += 1
        
        # Ежедневная статистика
        if today not in self.stats["daily_stats"]:
            self.stats["daily_stats"][today] = {
                "messages": 0,
                "voice_messages": 0,
                "unique_users": [],
                "searches": 0
            }
        
        daily = self.stats["daily_stats"][today]
        daily["messages"] += 1
        if is_voice:
            daily["voice_messages"] += 1
        
        # Добавляем пользователя в список, если его там еще нет
        if user_id not in daily["unique_users"]:
            daily["unique_users"].append(user_id)
        
        daily["unique_users_count"] = len(daily["unique_users"])
        
        self._save_stats()
    
    def track_search(self, user_id: int, search_query: str):
        """Отслеживает использование поиска"""
        today = date.today().isoformat()
        user_key = str(user_id)
        
        self.stats["total_searches"] += 1
        
        if user_key in self.stats["users"]:
            self.stats["users"][user_key]["searches_triggered"] += 1
        
        if today in self.stats["daily_stats"]:
            self.stats["daily_stats"][today]["searches"] += 1
        
        self._save_stats()
    
    def get_summary(self) -> str:
        """Возвращает краткую сводку статистики"""
        today = date.today().isoformat()
        daily_today = self.stats["daily_stats"].get(today, {})
        
        summary = f"""📊 Статистика бота Равшан:

👥 Всего пользователей: {self.stats['total_users']}
💬 Всего сообщений: {self.stats['total_messages']}
🎤 Голосовых сообщений: {self.stats['total_voice_messages']}
🔍 Поисковых запросов: {self.stats['total_searches']}

📅 Сегодня ({today}):
💬 Сообщений: {daily_today.get('messages', 0)}
👥 Уникальных пользователей: {daily_today.get('unique_users_count', 0)}
🔍 Поисков: {daily_today.get('searches', 0)}
"""
        return summary
    
    def get_top_users(self, limit: int = 10) -> str:
        """Возвращает топ активных пользователей"""
        users = []
        for user_id, data in self.stats["users"].items():
            users.append({
                "username": data.get("username", "Unknown"),
                "messages": data["messages_count"],
                "voice": data["voice_messages_count"],
                "searches": data["searches_triggered"]
            })
        
        # Сортируем по количеству сообщений
        users.sort(key=lambda x: x["messages"], reverse=True)
        
        result = f"🏆 Топ {min(limit, len(users))} активных пользователей:\n\n"
        for i, user in enumerate(users[:limit], 1):
            result += f"{i}. @{user['username']}: {user['messages']} сообщений ({user['voice']} голосовых, {user['searches']} поисков)\n"
        
        return result