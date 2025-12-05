"""
Скрипт для очистки всех пользователей: удаляет все сообщения бота из чатов и очищает БД.
"""

import logging
import sqlite3
from pathlib import Path
from db import get_connection, get_all_chat_ids, DB_PATH
from state import load_user_state
from helpers_delete import safe_delete
from telegram import Bot
import asyncio
import os
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Загружаем переменные окружения
PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN")


async def cleanup_user_messages(bot: Bot, chat_id: int, user_state) -> None:
    """Удаляет все сообщения бота для конкретного пользователя"""
    try:
        logger.info(f"🧹 Очистка сообщений для chat_id={chat_id}")
        
        # Удаляем дневной чеклист
        if user_state.checklist_message_id:
            try:
                await safe_delete(
                    bot,
                    user_state.business_connection_id,
                    chat_id,
                    user_state.checklist_message_id,
                )
                logger.info(f"  ✅ Удален дневной чеклист: message_id={user_state.checklist_message_id}")
            except Exception as e:
                logger.warning(f"  ⚠️ Не удалось удалить дневной чеклист: {e}")
        
        # Удаляем теговые чеклисты
        for tag, tag_state in user_state.tag_checklists.items():
            if tag_state.checklist_message_id:
                try:
                    await safe_delete(
                        bot,
                        user_state.business_connection_id,
                        chat_id,
                        tag_state.checklist_message_id,
                    )
                    logger.info(f"  ✅ Удален теговый чеклист '{tag}': message_id={tag_state.checklist_message_id}")
                except Exception as e:
                    logger.warning(f"  ⚠️ Не удалось удалить теговый чеклист '{tag}': {e}")
        
        # Удаляем служебные сообщения
        all_service_messages = []
        all_service_messages.extend(user_state.service_message_ids)
        all_service_messages.extend(user_state.pending_service_message_ids)
        
        if user_state.pending_task_message_id:
            all_service_messages.append(user_state.pending_task_message_id)
        
        for msg_id in all_service_messages:
            try:
                await safe_delete(
                    bot,
                    user_state.business_connection_id,
                    chat_id,
                    msg_id,
                )
            except Exception as e:
                logger.debug(f"  ⚠️ Не удалось удалить служебное сообщение {msg_id}: {e}")
        
        logger.info(f"  ✅ Очищено сообщений для chat_id={chat_id}")
        
    except Exception as e:
        logger.error(f"  ❌ Ошибка при очистке сообщений для chat_id={chat_id}: {e}", exc_info=True)


async def cleanup_all_users_and_messages() -> None:
    """Удаляет все сообщения бота из чатов и очищает БД"""
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не найден в .env")
        return
    
    bot = Bot(BOT_TOKEN)
    
    # Получаем всех пользователей из БД
    chat_ids = get_all_chat_ids()
    logger.info(f"📋 Найдено пользователей в БД: {len(chat_ids)}")
    
    if not chat_ids:
        logger.info("ℹ️ Пользователей не найдено, нечего очищать")
        return
    
    # Удаляем сообщения для каждого пользователя
    for chat_id in chat_ids:
        try:
            user_state = load_user_state(chat_id)
            if user_state:
                await cleanup_user_messages(bot, chat_id, user_state)
            else:
                logger.warning(f"⚠️ Не удалось загрузить user_state для chat_id={chat_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка при обработке chat_id={chat_id}: {e}", exc_info=True)
    
    # Удаляем все записи из БД
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM user_state")
    deleted_count = cursor.rowcount
    conn.commit()
    conn.close()
    
    logger.info(f"✅ Удалено записей из БД: {deleted_count}")
    logger.info("✅ Очистка завершена")


if __name__ == "__main__":
    asyncio.run(cleanup_all_users_and_messages())


