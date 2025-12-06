"""
Модуль для работы с базой данных SQLite.

Содержит:
- init_db(): инициализация БД и создание таблиц
"""

import sqlite3
import json
from pathlib import Path
from typing import Optional, List


# Путь к файлу БД (в корне проекта)
PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "bot.db"


def get_connection():
    """Создает и возвращает соединение с БД"""
    return sqlite3.connect(DB_PATH)


def init_db():
    """
    Инициализирует базу данных и создает таблицу user_state, если её нет.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            chat_id INTEGER PRIMARY KEY,
            business_connection_id TEXT,
            asked_for_time INTEGER,
            waiting_for_time INTEGER,
            time TEXT,
            checklist_message_id INTEGER,
            date TEXT,
            tasks TEXT,
            service_message_ids TEXT,
            pending_task_text TEXT,
            pending_task_message_id INTEGER,
            pending_service_message_ids TEXT,
            awaiting_tag INTEGER,
            tags_history TEXT,
            tags_page_index INTEGER,
            pending_confirm_job_id TEXT,
            tag_checklists TEXT,
            last_closed_date TEXT,
            last_opened_date TEXT
        )
    """)
    
    # Добавляем колонки, если их нет (для существующих БД)
    for column in ["tag_checklists", "last_closed_date", "last_opened_date", "timezone_offset_minutes", "next_rollover_job_name", "day_end_time"]:
        try:
            if column == "timezone_offset_minutes":
                cursor.execute(f"ALTER TABLE user_state ADD COLUMN {column} INTEGER DEFAULT 0")
            else:
                cursor.execute(f"ALTER TABLE user_state ADD COLUMN {column} TEXT")
        except sqlite3.OperationalError:
            # Колонка уже существует - это нормально
            pass
    
    conn.commit()
    conn.close()


def get_all_chat_ids() -> List[int]:
    """
    Возвращает список всех chat_id из базы данных.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT chat_id FROM user_state")
    rows = cursor.fetchall()
    conn.close()
    
    return [row[0] for row in rows]


def delete_user_state(chat_id: int) -> bool:
    """
    Удаляет состояние пользователя из базы данных.
    Возвращает True, если запись была удалена, False если не найдена.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("DELETE FROM user_state WHERE chat_id = ?", (chat_id,))
    deleted = cursor.rowcount > 0
    
    conn.commit()
    conn.close()
    
    return deleted

