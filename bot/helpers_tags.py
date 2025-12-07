"""
Модуль для работы с тегами: клавиатура, пагинация.
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from state import UserState, save_user_state
from helpers_text import get_user_local_date
from helpers_checklist import get_checklist_title_from_date

logger = logging.getLogger(__name__)

# Константа
TAGS_PER_PAGE = 3  # Количество тегов на странице


def build_tags_keyboard(user_state: UserState) -> InlineKeyboardMarkup:
    """
    Создаёт клавиатуру с тегами для текущей страницы.
    До 3 тегов на странице, с кнопками навигации.
    В начале добавляет кнопку с названием дневного отчета (например, "#7дек_вс").
    Всегда включает кнопку "Удалить" внизу.
    
    Показывает:
    1. Все активные теги из tag_checklists (чеклисты, которые существуют)
    2. Теги из tags_history, которых нет в tag_checklists (для истории)
    """
    buttons = []
    
    # Добавляем кнопку с названием дневного отчета в начало
    try:
        current_date = get_user_local_date(user_state)
        daily_title = get_checklist_title_from_date(current_date)
        buttons.append([InlineKeyboardButton(daily_title, callback_data="TAG_SELECT:" + daily_title)])
    except Exception as e:
        logger.error(f"❌ Ошибка при создании кнопки дневного отчета: {e}", exc_info=True)
    
    # Формируем объединенный список тегов:
    # 1. Сначала все активные теги из tag_checklists (чеклисты, которые существуют)
    # 2. Затем теги из tags_history, которых нет в tag_checklists
    active_tags = list(user_state.tag_checklists.keys())  # Активные чеклисты
    history_tags = [tag for tag in user_state.tags_history if tag not in active_tags]  # История без активных
    
    # Объединяем: сначала активные, потом история
    all_tags = active_tags + history_tags
    
    # Если есть теги - показываем их с пагинацией
    if all_tags:
        page = user_state.tags_page_index
        total_pages = (len(all_tags) + TAGS_PER_PAGE - 1) // TAGS_PER_PAGE if all_tags else 0
        
        # Кнопки с тегами для текущей страницы
        start_idx = page * TAGS_PER_PAGE
        end_idx = min(start_idx + TAGS_PER_PAGE, len(all_tags))
        
        for tag in all_tags[start_idx:end_idx]:
            buttons.append([InlineKeyboardButton(tag, callback_data=f"TAG_SELECT:{tag}")])
        
        # Кнопки навигации
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️", callback_data="TAGS_PAGE_PREV"))
        if end_idx < len(all_tags):
            nav_row.append(InlineKeyboardButton("➡️", callback_data="TAGS_PAGE_NEXT"))
        
        if nav_row:
            buttons.append(nav_row)
    
    # Строка с действиями: только Удалить (всегда в конце)
    action_row = [
        InlineKeyboardButton("❌ Удалить", callback_data="TASK_DELETE"),
    ]
    buttons.append(action_row)
    
    return InlineKeyboardMarkup(buttons)


async def on_tags_page_next(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState, chat_id: int) -> None:
    """Обработка кнопки 'Вперёд' в пагинации тегов"""
    # Формируем объединенный список тегов (как в build_tags_keyboard)
    active_tags = list(user_state.tag_checklists.keys())
    history_tags = [tag for tag in user_state.tags_history if tag not in active_tags]
    all_tags = active_tags + history_tags
    
    total_pages = (len(all_tags) + TAGS_PER_PAGE - 1) // TAGS_PER_PAGE if all_tags else 0
    if user_state.tags_page_index + 1 < total_pages:
        user_state.tags_page_index += 1
        save_user_state(chat_id, user_state)
        
        # Обновляем клавиатуру
        if update.callback_query:
            try:
                await update.callback_query.edit_message_reply_markup(
                    reply_markup=build_tags_keyboard(user_state)
                )
            except Exception as e:
                logger.error(f"❌ Ошибка при обновлении клавиатуры тегов: {e}", exc_info=True)


async def on_tags_page_prev(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState, chat_id: int) -> None:
    """Обработка кнопки 'Назад' в пагинации тегов"""
    if user_state.tags_page_index > 0:
        user_state.tags_page_index -= 1
        save_user_state(chat_id, user_state)
        
        # Обновляем клавиатуру
        if update.callback_query:
            try:
                await update.callback_query.edit_message_reply_markup(
                    reply_markup=build_tags_keyboard(user_state)
                )
            except Exception as e:
                logger.error(f"❌ Ошибка при обновлении клавиатуры тегов: {e}", exc_info=True)

