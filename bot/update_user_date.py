"""
Скрипт для обновления даты пользователя на актуальную
"""
import sys
import os
from pathlib import Path

# Добавляем путь к модулям
sys.path.insert(0, str(Path(__file__).parent))

from db import get_connection
from state import load_user_state, save_user_state
from helpers_text import get_user_local_date

def update_user_date(chat_id: int):
    """Обновляет дату пользователя на актуальную"""
    user_state = load_user_state(chat_id)
    if not user_state:
        print(f"❌ Пользователь chat_id={chat_id} не найден")
        return False
    
    old_date = user_state.date
    current_date = get_user_local_date(user_state)
    
    print(f"chat_id={chat_id}")
    print(f"Старая дата: {old_date}")
    print(f"Актуальная дата: {current_date}")
    
    if old_date != current_date:
        user_state.date = current_date
        save_user_state(chat_id, user_state)
        print(f"✅ Дата обновлена: {old_date} → {current_date}")
        return True
    else:
        print(f"ℹ️ Дата уже актуальна")
        return False

if __name__ == "__main__":
    if len(sys.argv) > 1:
        chat_id = int(sys.argv[1])
        update_user_date(chat_id)
    else:
        # Обновляем всех пользователей
        from db import get_all_chat_ids
        chat_ids = get_all_chat_ids()
        print(f"Найдено пользователей: {len(chat_ids)}")
        for chat_id in chat_ids:
            update_user_date(chat_id)
            print()

