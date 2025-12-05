"""
Модуль для безопасного удаления сообщений.
"""

import logging

logger = logging.getLogger(__name__)


async def safe_delete(bot, business_connection_id: str, chat_id: int, message_id: int) -> None:
    """Безопасно удаляет business сообщение, игнорируя ошибки"""
    try:
        # Используем delete_business_messages - НЕ требует chat_id, только business_connection_id и message_ids
        await bot.delete_business_messages(
            business_connection_id=business_connection_id,
            message_ids=[message_id],
        )
    except Exception as e:
        logger.warning(f"⚠️ Не удалось удалить message_id={message_id}: {e}")

