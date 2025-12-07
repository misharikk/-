"""
Модуль для работы с Telegram Checklist: создание и обновление чеклистов.
"""

import logging
from datetime import datetime
from typing import Optional, Tuple
from telegram import InputChecklist, InputChecklistTask

# Импорты из других модулей (будут добавлены после создания)
from state import UserState, TagChecklistState, TaskItem, save_user_state
from helpers_text import get_user_local_date

logger = logging.getLogger(__name__)

# Глобальный set для отслеживания обработанных событий чеклиста (защита от дубликатов)
# Ключ: (target_checklist_type, tuple(marked_as_done_ids), tuple(marked_as_undone_ids))
# Ограничиваем размер до 1000 элементов для предотвращения утечки памяти
processed_event_ids: set = set()
MAX_PROCESSED_EVENTS = 1000


def get_today_human_date() -> str:
    """
    Возвращает сегодняшнюю дату в человекочитаемом виде,
    например: '29 ноября'
    """
    MONTH_NAMES_RU = [
        "", "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря"
    ]
    now = datetime.now()
    day = now.day
    month = MONTH_NAMES_RU[now.month]
    return f"{day} {month}"


def get_human_date_from_iso(date_iso: str) -> str:
    """
    Преобразует дату из формата YYYY-MM-DD в человекочитаемый вид (например, '3 декабря').
    """
    if not date_iso:
        return "Дата не указана"
    
    try:
        date_obj = datetime.strptime(date_iso, "%Y-%m-%d")
        MONTH_NAMES_RU = [
            "", "января", "февраля", "марта", "апреля", "мая", "июня",
            "июля", "августа", "сентября", "октября", "ноября", "декабря"
        ]
        day = date_obj.day
        month = MONTH_NAMES_RU[date_obj.month]
        return f"{day} {month}"
    except Exception as e:
        logger.error(f"❌ Ошибка при преобразовании даты '{date_iso}': {e}")
        return date_iso


def get_checklist_title_from_date(date_iso: str) -> str:
    """
    Преобразует дату из формата YYYY-MM-DD в формат для title чеклиста: #4дек_чт
    """
    if not date_iso:
        return "#дата"
    
    try:
        date_obj = datetime.strptime(date_iso, "%Y-%m-%d")
        
        # Сокращенные названия месяцев
        MONTH_SHORT = [
            "", "янв", "фев", "мар", "апр", "мая", "июн",
            "июл", "авг", "сен", "окт", "ноя", "дек"
        ]
        
        # Сокращенные названия дней недели
        WEEKDAY_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        
        day = date_obj.day
        month_short = MONTH_SHORT[date_obj.month]
        weekday_short = WEEKDAY_SHORT[date_obj.weekday()]
        
        return f"#{day}{month_short}_{weekday_short}"
    except Exception as e:
        logger.error(f"❌ Ошибка при преобразовании даты '{date_iso}' для title: {e}")
        return "#дата"


async def create_checklist_for_user(
    bot,
    chat_id: int,
    user_state: UserState,
) -> None:
    """
    Создаёт нативный чеклист для данного пользователя, если он ещё не создан.
    - title = сегодняшняя дата (например, '29 ноября')
    - первая задача = 'улыбнуться себе в зеркало'
    - others_can_add_tasks = False
    - others_can_mark_tasks_as_done = True
    - сохраняет checklist_message_id, дату и список tasks в user_state
    """
    try:
        # 1. Сохраняем старую дату ДО пересчёта
        old_date = user_state.date
        
        # 2. Определяем дату для чеклиста
        # ПРАВИЛО:
        # - Если чеклиста нет (checklist_message_id is None) и дата уже установлена - используем её
        #   (например, при создании нового дня в start_new_day_for_user)
        # - Если чеклиста нет и дата отсутствует - вычисляем через get_user_local_date
        # - Если чеклист существует - проверяем, не изменилась ли дата через get_user_local_date
        if user_state.checklist_message_id is None:
            # Создаём новый чеклист
            if user_state.date:
                # Дата уже установлена (например, из start_new_day_for_user) - используем её
                current_user_date = user_state.date
                logger.debug(f"📅 Используем установленную дату для нового чеклиста: {current_user_date}")
            else:
                # Дата не установлена - вычисляем актуальную локальную дату пользователя
                current_user_date = get_user_local_date(user_state)
                logger.debug(f"📅 Вычислена дата для нового чеклиста: {current_user_date}")
        else:
            # Обновляем существующий чеклист - проверяем, не изменилась ли дата
            current_user_date = get_user_local_date(user_state)
        
        # 3. Формируем title в формате #4дек_чт
        checklist_title = get_checklist_title_from_date(current_user_date)
        
        # 4. Если чеклист уже существует — проверяем, не изменилась ли дата
        if user_state.checklist_message_id is not None:
            if old_date and old_date != current_user_date:
                logger.info(
                    f"🔄 Дата изменилась для chat_id={chat_id}: "
                    f"{old_date} → {current_user_date}, обновляю чеклист"
                )
                user_state.date = current_user_date
                save_user_state(chat_id, user_state)
                await update_checklist_for_user(bot, chat_id, user_state)
                return
            else:
                logger.info(
                    f"⏭️ Чеклист уже существует для chat_id={chat_id}, "
                    f"message_id={user_state.checklist_message_id}, "
                    f"дата актуальна ({current_user_date})"
                )
            return
        
        # 5. Если чеклиста ещё нет — создаём новый
        # ВАЖНО: перезагружаем состояние перед созданием, чтобы избежать дублирования при конкурентных запросах
        # Обновляем дату только если она изменилась или не была установлена
        if user_state.date != current_user_date:
            user_state.date = current_user_date
            save_user_state(chat_id, user_state)
        
        # Перезагружаем состояние из БД перед созданием, чтобы убедиться, что чеклист не был создан другим запросом
        from state import load_user_state
        fresh_user_state = load_user_state(chat_id)
        if fresh_user_state and fresh_user_state.checklist_message_id is not None:
            # Чеклист был создан другим запросом - используем его
            logger.info(f"⏭️ Чеклист уже существует (создан другим запросом), обновляю состояние для chat_id={chat_id}, message_id={fresh_user_state.checklist_message_id}")
            user_state.checklist_message_id = fresh_user_state.checklist_message_id
            user_state.date = fresh_user_state.date
            user_state.tasks = fresh_user_state.tasks
            save_user_state(chat_id, user_state)
            await update_checklist_for_user(bot, chat_id, user_state)
            return

        logger.info(f"🔨 Начинаю создание чеклиста для chat_id={chat_id}")
        
        # Если задач нет, создаем автоматическую задачу
        if not user_state.tasks:
            first_task_text = "улыбнуться себе в зеркало"
            user_state.tasks = [TaskItem(item_id=1, text=first_task_text, done=False)]
            save_user_state(chat_id, user_state)

        tasks = []
        total_tasks = len(user_state.tasks)
        done_count = sum(1 for t in user_state.tasks if t.done)
        logger.info(f"📊 Создание чеклиста: всего задач={total_tasks}, выполненных={done_count}, невыполненных={total_tasks - done_count}")
        
        task_position = 0  # Позиция в чеклисте (1-based)
        for task_item in user_state.tasks:
            # Пропускаем выполненные задачи при создании нового чеклиста
            if task_item.done:
                logger.warning(f"⏭️ ПРОПУСКАЕМ выполненную задачу при создании чеклиста: '{task_item.text[:50]}' (item_id={task_item.item_id}, done={task_item.done})")
                continue
            
            task_position += 1  # Увеличиваем позицию только для невыполненных задач
            # Формируем текст без номера
            task_text = task_item.text
            # Обрезаем до 100 символов (лимит Telegram API для чеклистов)
            if len(task_text) > 100:
                task_text = task_text[:97].rstrip() + "…"
            
            # ВАЖНО: id в чеклисте должен быть item_id из состояния, а не позицией
            # Это нужно для правильной синхронизации событий - marked_as_done_task_ids содержат item_id
            tasks.append(InputChecklistTask(
                id=task_item.item_id,  # Используем item_id из состояния для синхронизации
                text=task_text,
            ))

        # Если нет невыполненных задач - не создаем чеклист
        if not tasks:
            logger.info(f"⏭️ Нет невыполненных задач для создания чеклиста для chat_id={chat_id}, пропускаем")
            return

        checklist = InputChecklist(
            title=checklist_title,
            tasks=tasks,
            others_can_add_tasks=False,
            others_can_mark_tasks_as_done=True,
        )

        logger.info(f"📤 Отправляю чеклист для chat_id={chat_id}, title='{checklist_title}', задач={len(tasks)}")
        msg = await bot.send_checklist(
            business_connection_id=user_state.business_connection_id,
            chat_id=chat_id,
            checklist=checklist,
        )
        
        # ПЕРЕД сохранением - ещё раз проверяем, не был ли создан чеклист другим запросом
        final_check_state = load_user_state(chat_id)
        if final_check_state and final_check_state.checklist_message_id is not None:
            logger.warning(f"⚠️ Чеклист был создан другим запросом во время создания, удаляю дубликат message_id={msg.message_id} для chat_id={chat_id}")
            try:
                await bot.delete_business_messages(
                    business_connection_id=user_state.business_connection_id,
                    chat_id=chat_id,
                    message_ids=[msg.message_id],
                )
            except Exception as e:
                logger.error(f"❌ Ошибка при удалении дубликата чеклиста: {e}")
            # Используем существующий чеклист
            user_state.checklist_message_id = final_check_state.checklist_message_id
            user_state.date = final_check_state.date
            user_state.tasks = final_check_state.tasks
            save_user_state(chat_id, user_state)
            await update_checklist_for_user(bot, chat_id, user_state)
            return
        
        user_state.checklist_message_id = msg.message_id
        # Явно обновляем состояние
        save_user_state(chat_id, user_state)
        logger.info(f"✅ Чеклист создан для chat_id={chat_id}, message_id={msg.message_id}")
    except Exception as e:
        logger.error(f"❌ Ошибка при создании чеклиста для chat_id={chat_id}: {e}", exc_info=True)
        # Ничего не пробрасываем — просто логируем


async def update_checklist_for_user(
    bot,
    chat_id: int,
    user_state: UserState,
) -> None:
    """
    Обновляет существующий чеклист на основе user_state.tasks.
    ВАЖНО: пропускает выполненные задачи и использует позицию (1-based) для синхронизации с событиями.
    """
    try:
        if user_state.checklist_message_id is None:
            # на всякий случай: если вдруг нет чеклиста — создаём
            await create_checklist_for_user(bot, chat_id, user_state)
            return

        tasks = []
        task_position = 0  # Позиция в чеклисте (1-based)
        for task_item in user_state.tasks:
            # Пропускаем выполненные задачи при обновлении чеклиста
            if task_item.done:
                logger.debug(f"⏭️ ПРОПУСКАЕМ выполненную задачу при обновлении чеклиста: '{task_item.text[:50]}' (item_id={task_item.item_id}, done={task_item.done})")
                continue
            
            task_position += 1  # Увеличиваем позицию только для невыполненных задач
            # Формируем текст без номера
            task_text = task_item.text
            # Обрезаем до 100 символов (лимит Telegram API для чеклистов)
            if len(task_text) > 100:
                task_text = task_text[:97].rstrip() + "…"
            
            # ВАЖНО: id в чеклисте должен быть item_id из состояния, а не позицией
            # Это нужно для правильной синхронизации событий - marked_as_done_task_ids содержат item_id
            tasks.append(InputChecklistTask(
                id=task_item.item_id,  # Используем item_id из состояния для синхронизации
                text=task_text,
            ))

        # ВСЕГДА вычисляем актуальную дату для title чеклиста (формат: #4дек_чт)
        current_user_date = get_user_local_date(user_state)
        checklist_title = get_checklist_title_from_date(current_user_date)
        
        checklist = InputChecklist(
            title=checklist_title,
            tasks=tasks,
            others_can_add_tasks=False,
            others_can_mark_tasks_as_done=True,
        )

        try:
            await bot.edit_message_checklist(
                business_connection_id=user_state.business_connection_id,
                chat_id=chat_id,
                message_id=user_state.checklist_message_id,
                checklist=checklist,
            )
        except Exception as e:
            error_msg = str(e)
            # Если чеклист не найден (удален или неверный message_id), создаём новый
            if "Message_id_invalid" in error_msg or "message not found" in error_msg.lower():
                logger.warning(f"⚠️ Чеклист message_id={user_state.checklist_message_id} не найден, создаю новый для chat_id={chat_id}")
                user_state.checklist_message_id = None  # Сбрасываем старый ID
                await create_checklist_for_user(bot, chat_id, user_state)
            else:
                logger.error(f"❌ Ошибка при обновлении чеклиста для chat_id={chat_id}: {e}", exc_info=True)
                # Ничего не пробрасываем — просто логируем
    except Exception as e:
        logger.error(f"❌ Ошибка при обновлении чеклиста для chat_id={chat_id}: {e}", exc_info=True)
        # Ничего не пробрасываем — просто логируем


async def add_task_to_tag_checklist(
    bot,
    chat_id: int,
    user_state: UserState,
    tag: str,
    task_text: str,
) -> None:
    """
    Находит или создаёт чеклист для данного тега (title = tag),
    добавляет в него task_text и обновляет state + SQLite.
    """
    try:
        # Проверяем, есть ли уже чеклист для этого тега
        if tag in user_state.tag_checklists:
            # Чеклист уже существует - обновляем его
            tag_state = user_state.tag_checklists[tag]
            
            # Добавляем задачу в список как TaskItem
            next_id = max([t.item_id for t in tag_state.tasks], default=0) + 1
            tag_state.tasks.append(TaskItem(item_id=next_id, text=task_text, done=False))
            save_user_state(chat_id, user_state)
            
            # Формируем список задач без нумерации
            # ВАЖНО: пропускаем выполненные задачи и используем позицию (1-based) для синхронизации
            tasks = []
            task_position = 0  # Позиция в чеклисте (1-based)
            for task_item in tag_state.tasks:
                # Пропускаем выполненные задачи
                if task_item.done:
                    logger.debug(f"⏭️ ПРОПУСКАЕМ выполненную задачу в теговом чеклисте '{tag}': '{task_item.text[:50]}' (item_id={task_item.item_id}, done={task_item.done})")
                    continue
                
                task_position += 1  # Увеличиваем позицию только для невыполненных задач
                task_text = task_item.text
                # Обрезаем до 100 символов (лимит Telegram API для чеклистов)
                if len(task_text) > 100:
                    task_text = task_text[:97].rstrip() + "…"
                # ВАЖНО: id в чеклисте должен быть item_id из состояния, а не позицией
                # Это нужно для правильной синхронизации событий - marked_as_done_task_ids содержат item_id
                tasks.append(InputChecklistTask(
                    id=task_item.item_id,  # Используем item_id из состояния для синхронизации
                    text=task_text,
                ))
            
            checklist = InputChecklist(
                title=tag_state.title,
                tasks=tasks,
                others_can_add_tasks=False,
                others_can_mark_tasks_as_done=True,
            )
            
            try:
                await bot.edit_message_checklist(
                    business_connection_id=user_state.business_connection_id,
                    chat_id=chat_id,
                    message_id=tag_state.checklist_message_id,
                    checklist=checklist,
                )
                logger.info(f"✅ Задача добавлена в чеклист по тегу '{tag}' для chat_id={chat_id}: {task_text!r}")
            except Exception as e:
                error_msg = str(e)
                # Если чеклист не найден (удален или неверный message_id), создаём новый
                if "Message_id_invalid" in error_msg or "message not found" in error_msg.lower():
                    logger.warning(f"⚠️ Чеклист по тегу '{tag}' message_id={tag_state.checklist_message_id} не найден, создаю новый для chat_id={chat_id}")
                    # Удаляем старый чеклист из состояния и создадим новый ниже
                    del user_state.tag_checklists[tag]
                else:
                    logger.error(f"❌ Ошибка при обновлении чеклиста по тегу '{tag}' для chat_id={chat_id}: {e}", exc_info=True)
                    return
        
        # Если чеклиста нет или он был удален - создаём новый
        # ВАЖНО: перезагружаем состояние перед созданием, чтобы избежать дублирования при конкурентных запросах
        if tag not in user_state.tag_checklists:
            # Перезагружаем состояние из БД перед созданием, чтобы убедиться, что чеклист не был создан другим запросом
            from state import load_user_state
            fresh_user_state = load_user_state(chat_id)
            if fresh_user_state and tag in fresh_user_state.tag_checklists:
                # Чеклист был создан другим запросом - используем его
                logger.info(f"⏭️ Чеклист по тегу '{tag}' уже существует (создан другим запросом), обновляю состояние для chat_id={chat_id}")
                user_state.tag_checklists[tag] = fresh_user_state.tag_checklists[tag]
                # Продолжаем с обновлением существующего чеклиста
                tag_state = user_state.tag_checklists[tag]
                next_id = max([t.item_id for t in tag_state.tasks], default=0) + 1
                tag_state.tasks.append(TaskItem(item_id=next_id, text=task_text, done=False))
                save_user_state(chat_id, user_state)
                # Обновляем чеклист (код ниже)
            else:
                # Чеклиста действительно нет - создаём новый
                # Вычисляем next_id для новой задачи
                # Используем максимальный item_id из существующих задач для этого тега
                # Если задач нет (первый чеклист для тега), item_id будет 1
                # ВАЖНО: даже если tag_state был удалён из tag_checklists, 
                # нужно проверить, есть ли задачи для этого тега в состоянии
                # Но так как tag not in user_state.tag_checklists, значит задач нет
                # Однако для корректной нумерации при последующих созданиях чеклиста
                # используем максимальный item_id из всех задач всех теговых чеклистов
                # чтобы избежать конфликтов item_id между разными тегами
                all_tag_task_ids = []
                current_state = fresh_user_state if fresh_user_state else user_state
                for existing_tag, existing_tag_state in current_state.tag_checklists.items():
                    all_tag_task_ids.extend([t.item_id for t in existing_tag_state.tasks])
                
                # Вычисляем next_id как максимальный + 1, или 1 если задач нет
                next_id = max(all_tag_task_ids, default=0) + 1
                logger.debug(f"🔢 Вычислен next_id={next_id} для нового тегового чеклиста '{tag}' (максимальный item_id в существующих теговых чеклистах: {max(all_tag_task_ids, default=0)})")
                
                # Формируем первую задачу
                first_task_text = task_text
                if len(first_task_text) > 100:
                    first_task_text = first_task_text[:97].rstrip() + "…"
                
                tasks = [InputChecklistTask(
                    id=next_id,
                    text=first_task_text,
                )]
                
                checklist = InputChecklist(
                    title=tag,
                    tasks=tasks,
                    others_can_add_tasks=False,
                    others_can_mark_tasks_as_done=True,
                )
                
                logger.info(f"📤 Создаю чеклист по тегу '{tag}' для chat_id={chat_id}")
                msg = await bot.send_checklist(
                    business_connection_id=user_state.business_connection_id,
                    chat_id=chat_id,
                    checklist=checklist,
                )
                
                # ПЕРЕД сохранением - ещё раз проверяем, не был ли создан чеклист другим запросом
                final_check_state = load_user_state(chat_id)
                if final_check_state and tag in final_check_state.tag_checklists:
                    logger.warning(f"⚠️ Чеклист по тегу '{tag}' был создан другим запросом во время создания, удаляю дубликат message_id={msg.message_id} для chat_id={chat_id}")
                    try:
                        await bot.delete_business_messages(
                            business_connection_id=user_state.business_connection_id,
                            chat_id=chat_id,
                            message_ids=[msg.message_id],
                        )
                    except Exception as e:
                        logger.error(f"❌ Ошибка при удалении дубликата чеклиста: {e}")
                    # Используем существующий чеклист
                    user_state.tag_checklists[tag] = final_check_state.tag_checklists[tag]
                    # Добавляем задачу в существующий чеклист
                    tag_state = user_state.tag_checklists[tag]
                    next_id = max([t.item_id for t in tag_state.tasks], default=0) + 1
                    tag_state.tasks.append(TaskItem(item_id=next_id, text=task_text, done=False))
                    save_user_state(chat_id, user_state)
                    # Обновляем чеклист (код ниже)
                else:
                    # Сохраняем состояние чеклиста
                    tag_state = TagChecklistState(
                        title=tag,
                        checklist_message_id=msg.message_id,
                        tasks=[TaskItem(item_id=next_id, text=task_text, done=False)],
                    )
                    user_state.tag_checklists[tag] = tag_state
                    save_user_state(chat_id, user_state)
                    
                    logger.info(f"✅ Чеклист по тегу '{tag}' создан для chat_id={chat_id}, message_id={msg.message_id}, item_id={next_id}")
                    return  # Выходим, так как чеклист создан и задача добавлена
        
        # Если мы дошли сюда, значит чеклист существует - обновляем его
        if tag in user_state.tag_checklists:
            tag_state = user_state.tag_checklists[tag]
            
            # Проверяем, не была ли задача уже добавлена (защита от дублирования)
            task_already_exists = any(t.text == task_text for t in tag_state.tasks)
            if not task_already_exists:
                # Добавляем задачу в список как TaskItem
                next_id = max([t.item_id for t in tag_state.tasks], default=0) + 1
                tag_state.tasks.append(TaskItem(item_id=next_id, text=task_text, done=False))
                save_user_state(chat_id, user_state)
            
            # Формируем список задач без нумерации
            # ВАЖНО: пропускаем выполненные задачи и используем позицию (1-based) для синхронизации
            tasks = []
            task_position = 0  # Позиция в чеклисте (1-based)
            for task_item in tag_state.tasks:
                # Пропускаем выполненные задачи
                if task_item.done:
                    logger.debug(f"⏭️ ПРОПУСКАЕМ выполненную задачу в теговом чеклисте '{tag}': '{task_item.text[:50]}' (item_id={task_item.item_id}, done={task_item.done})")
                    continue
                
                task_position += 1  # Увеличиваем позицию только для невыполненных задач
                task_text_for_checklist = task_item.text
                # Обрезаем до 100 символов (лимит Telegram API для чеклистов)
                if len(task_text_for_checklist) > 100:
                    task_text_for_checklist = task_text_for_checklist[:97].rstrip() + "…"
                # ВАЖНО: id в чеклисте должен быть item_id из состояния, а не позицией
                # Это нужно для правильной синхронизации событий - marked_as_done_task_ids содержат item_id
                tasks.append(InputChecklistTask(
                    id=task_item.item_id,  # Используем item_id из состояния для синхронизации
                    text=task_text_for_checklist,
                ))
            
            checklist = InputChecklist(
                title=tag_state.title,
                tasks=tasks,
                others_can_add_tasks=False,
                others_can_mark_tasks_as_done=True,
            )
            
            try:
                await bot.edit_message_checklist(
                    business_connection_id=user_state.business_connection_id,
                    chat_id=chat_id,
                    message_id=tag_state.checklist_message_id,
                    checklist=checklist,
                )
                logger.info(f"✅ Задача добавлена в чеклист по тегу '{tag}' для chat_id={chat_id}: {task_text!r}")
            except Exception as e:
                error_msg = str(e)
                # Если чеклист не найден (удален или неверный message_id), создаём новый
                if "Message_id_invalid" in error_msg or "message not found" in error_msg.lower():
                    logger.warning(f"⚠️ Чеклист по тегу '{tag}' message_id={tag_state.checklist_message_id} не найден, создаю новый для chat_id={chat_id}")
                    # Удаляем старый чеклист из состояния и создадим новый ниже
                    del user_state.tag_checklists[tag]
                    # Рекурсивно вызываем функцию для создания нового чеклиста
                    await add_task_to_tag_checklist(bot, chat_id, user_state, tag, task_text)
                else:
                    logger.error(f"❌ Ошибка при обновлении чеклиста по тегу '{tag}' для chat_id={chat_id}: {e}", exc_info=True)
                    return
    except Exception as e:
        logger.error(f"❌ Ошибка при добавлении задачи в чеклист по тегу '{tag}' для chat_id={chat_id}: {e}", exc_info=True)
        # Ничего не пробрасываем — просто логируем


async def rebuild_tag_checklist_for_user(
    bot,
    chat_id: int,
    user_state: UserState,
    tag: str,
) -> None:
    """
    Создаёт новый нативный чеклист по тегу из уже существующих
    невыполненных задач в user_state.tag_checklists[tag].tasks.
    НИЧЕГО не добавляет в список задач, только синхронизирует их с Telegram.
    """
    try:
        tag_state = user_state.tag_checklists.get(tag)
        if not tag_state or not tag_state.tasks:
            logger.info(f"⏭️ Нет задач для тега '{tag}' для chat_id={chat_id}, пропускаем восстановление")
            return

        tasks = []
        task_position = 0  # Позиция в чеклисте (1-based)
        for task_item in tag_state.tasks:
            # Пропускаем выполненные задачи
            if task_item.done:
                logger.debug(f"⏭️ Пропускаем выполненную задачу в теговом чеклисте '{tag}': {task_item.text}")
                continue
            
            task_position += 1  # Увеличиваем позицию только для невыполненных задач
            text = task_item.text
            # Обрезаем до 100 символов (лимит Telegram API для чеклистов)
            if len(text) > 100:
                text = text[:97].rstrip() + "…"

            # ВАЖНО: id в чеклисте должен быть item_id из состояния, а не позицией
            # Это нужно для правильной синхронизации событий - marked_as_done_task_ids содержат item_id
            tasks.append(InputChecklistTask(
                id=task_item.item_id,  # Используем item_id из состояния для синхронизации
                text=text,
            ))

        checklist = InputChecklist(
            title=tag_state.title,
            tasks=tasks,
            others_can_add_tasks=False,
            others_can_mark_tasks_as_done=True,
        )

        logger.info(f"📤 Восстанавливаю чеклист по тегу '{tag}' для chat_id={chat_id} с {len(tasks)} задачами")
        msg = await bot.send_checklist(
            business_connection_id=user_state.business_connection_id,
            chat_id=chat_id,
            checklist=checklist,
        )

        # Просто сохраняем новый message_id, список задач НЕ меняем
        tag_state.checklist_message_id = msg.message_id
        user_state.tag_checklists[tag] = tag_state
        save_user_state(chat_id, user_state)

        logger.info(f"✅ Чеклист по тегу '{tag}' восстановлен для chat_id={chat_id}, message_id={msg.message_id}")
    except Exception as e:
        logger.error(f"❌ Ошибка при восстановлении чеклиста по тегу '{tag}' для chat_id={chat_id}: {e}", exc_info=True)
        # Ничего не пробрасываем — просто логируем


def resolve_checklist_type(user_state: UserState, checklist_message_id: int, checklist_title: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Определяет тип чеклиста по message_id и заголовку.
    
    Возвращает:
    - ('daily', None) для дневного чеклиста
    - ('tag', tag_name) для тегового чеклиста
    - (None, None) если чеклист не найден
    """
    # Проверяем дневной чеклист
    if user_state.checklist_message_id == checklist_message_id:
        return ('daily', None)
    
    # Проверяем по заголовку дневного чеклиста
    if user_state.date:
        expected_daily_title = get_checklist_title_from_date(user_state.date)
        if checklist_title == expected_daily_title:
            return ('daily', None)
    
    # Проверяем теговые чеклисты
    for tag, tag_state in user_state.tag_checklists.items():
        if tag_state.checklist_message_id == checklist_message_id:
            return ('tag', tag)
        
        # Проверяем по заголовку (начинается с # и равен tag_state.title)
        if checklist_title.startswith('#') and checklist_title == tag_state.title:
            return ('tag', tag)
    
    return (None, None)


def normalize(text: str) -> str:
    """
    Нормализует текст для сравнения:
    - Приводит к нижнему регистру (case-insensitive)
    - Убирает пробелы по краям (trim)
    - Убирает двойные пробелы (через split/join)
    
    Примеры:
    - "  Суп  " → "суп"
    - "Суп  с  хлебом" → "суп с хлебом"
    - "СУП" → "суп"
    """
    return " ".join(text.lower().strip().split())


def sync_task_status_by_text(user_state: UserState, task_text: str, new_done_status: bool) -> bool:
    """
    Синхронизирует статус выполнения задачи по тексту во всех чеклистах.
    
    Ищет задачи с таким же текстом (после нормализации) в:
    - user_state.tasks (дневной чеклист)
    - user_state.tag_checklists[tag].tasks (все теговые чеклисты)
    
    Устанавливает им тот же статус done, что и у исходной задачи.
    
    ВАЖНО: использует строгую нормализацию текста (case-insensitive, без двойных пробелов).
    
    Возвращает True, если были найдены и обновлены задачи.
    """
    task_text_normalized = normalize(task_text)
    if not task_text_normalized:
        return False
    
    updated = False
    
    # Синхронизируем в дневном чеклисте
    for task in user_state.tasks:
        if normalize(task.text) == task_text_normalized:
            if task.done != new_done_status:
                task.done = new_done_status
                updated = True
                logger.debug(f"🔄 Синхронизирован статус дневной задачи: text='{task.text[:30]}' (нормализовано: '{task_text_normalized[:30]}'), done={new_done_status}")
    
    # Синхронизируем во всех теговых чеклистах
    for tag, tag_state in user_state.tag_checklists.items():
        for task in tag_state.tasks:
            if normalize(task.text) == task_text_normalized:
                if task.done != new_done_status:
                    task.done = new_done_status
                    updated = True
                    logger.debug(f"🔄 Синхронизирован статус теговой задачи '{tag}': text='{task.text[:30]}' (нормализовано: '{task_text_normalized[:30]}'), done={new_done_status}")
    
    if updated:
        logger.info(f"✅ Синхронизирован статус задачи по тексту: text='{task_text[:50]}' (нормализовано: '{task_text_normalized[:50]}'), done={new_done_status}")
    
    return updated


async def handle_checklist_state_update(business_msg, user_state: UserState, chat_id: int) -> None:
    """
    Обрабатывает изменение состояния пунктов чек-листа.
    Использует checklist_message_id и item_id из событий checklist_tasks_done / checklist_tasks_added.
    Обновляет флаг done в user_state.tasks (дневной чеклист) или в user_state.tag_checklists[*].tasks (теговые чеклисты).
    Никаких эвристик по позициям не используется.
    
    ВАЖНО: сначала используем checklist_tasks_done.checklist_message.message_id,
    затем — reply_to_message, затем fallback на message_id сервисного сообщения.
    """
    try:
        # 1. Достаём объекты событий (если они есть)
        checklist_tasks_done = getattr(business_msg, "checklist_tasks_done", None)
        checklist_tasks_added = getattr(business_msg, "checklist_tasks_added", None)
        if not checklist_tasks_done and not checklist_tasks_added:
            logger.info(
                "ℹ️ handle_checklist_state_update: нет checklist_tasks_done/added для chat_id=%s",
                chat_id,
            )
            return

        # 2. Определяем checklist_message_id (original_message_id), к которому относится событие
        # Приоритет: checklist_tasks_done.checklist_message.message_id > определение по item_id > reply_to_message > business_msg.message_id
        original_message_id = None
        identified_by_item_id = False  # Флаг: определили ли мы чеклист по item_id (без checklist_message)

        # 1) Пытаемся взять ID из checklist_tasks_done.checklist_message
        if checklist_tasks_done is not None:
            checklist_message = getattr(checklist_tasks_done, "checklist_message", None)
            if checklist_message is not None:
                original_message_id = getattr(checklist_message, "message_id", None)
                if original_message_id is None:
                    try:
                        msg_dict = checklist_message.to_dict()
                        original_message_id = msg_dict.get("message_id")
                    except Exception:
                        pass
                
                if original_message_id is not None:
                    logger.info(
                        "🔍 checklist_tasks_done: используем checklist_message.message_id=%s для chat_id=%s",
                        original_message_id,
                        chat_id,
                    )
        
        # 2) Если checklist_message отсутствует, определяем чеклист по item_id из marked_as_done_task_ids
        # ВАЖНО: item_id могут совпадать между чеклистами, поэтому если определили по item_id,
        # нужно обновлять ВСЕ чеклисты, где есть такой item_id
        if original_message_id is None and checklist_tasks_done is not None:
            done_ids = set(getattr(checklist_tasks_done, "marked_as_done_task_ids", []) or [])
            not_done_ids = set(getattr(checklist_tasks_done, "marked_as_not_done_task_ids", []) or [])
            all_ids = done_ids | not_done_ids
            
            if all_ids:
                logger.info(
                    "🔍 Определяем чеклист по item_id для chat_id=%s: done_ids=%s, not_done_ids=%s",
                    chat_id,
                    sorted(done_ids),
                    sorted(not_done_ids),
                )
                
                # Логируем все item_id в дневном чеклисте для отладки
                daily_item_ids = [task.item_id for task in user_state.tasks]
                logger.info(
                    "🔍 DEBUG: дневной чеклист для chat_id=%s: item_ids=%s, checklist_message_id=%s",
                    chat_id,
                    daily_item_ids,
                    user_state.checklist_message_id,
                )
                
                # ВАЖНО: Приоритет дневному чеклисту, так как пользователь чаще взаимодействует с ним
                # и если item_id совпадает, лучше обновить дневной, а не теговый
                found_daily = False
                for task in user_state.tasks:
                    if task.item_id in all_ids:
                        found_daily = True
                        logger.info(
                            "🔍 Найден дневной чеклист по item_id=%s: message_id=%s для chat_id=%s",
                            task.item_id,
                            user_state.checklist_message_id,
                            chat_id,
                        )
                        break
                
                # Проверяем теговые чеклисты (второй приоритет)
                found_tag_checklists = []  # Список найденных теговых чеклистов с таким item_id
                for tag, tag_state in user_state.tag_checklists.items():
                    tag_item_ids = [task.item_id for task in tag_state.tasks]
                    logger.info(
                        "🔍 DEBUG: теговый чеклист '%s' для chat_id=%s: item_ids=%s, checklist_message_id=%s",
                        tag,
                        chat_id,
                        tag_item_ids,
                        tag_state.checklist_message_id,
                    )
                    for task in tag_state.tasks:
                        if task.item_id in all_ids:
                            found_tag_checklists.append((tag, tag_state.checklist_message_id))
                            logger.info(
                                "🔍 Найден теговый чеклист '%s' по item_id=%s: message_id=%s для chat_id=%s",
                                tag,
                                task.item_id,
                                tag_state.checklist_message_id,
                                chat_id,
                            )
                            break
                
                # Если нашли чеклисты по item_id, используем первый найденный для original_message_id
                # Приоритет: дневной > теговый
                if found_daily:
                    original_message_id = user_state.checklist_message_id
                    identified_by_item_id = True
                elif found_tag_checklists:
                    original_message_id = found_tag_checklists[0][1]  # Используем первый теговый
                    identified_by_item_id = True

        # Если не получилось из checklist_tasks_done, пробуем checklist_tasks_added
        if original_message_id is None and checklist_tasks_added is not None:
            checklist_message = getattr(checklist_tasks_added, "checklist_message", None)
            if checklist_message is not None:
                original_message_id = getattr(checklist_message, "message_id", None)
                
                # Если message_id нет напрямую, пробуем через to_dict()
                if original_message_id is None:
                    try:
                        msg_dict = checklist_message.to_dict()
                        original_message_id = msg_dict.get("message_id")
                    except Exception:
                        pass
                
                if original_message_id is not None:
                    logger.info(
                        "🔍 checklist_tasks_added: используем checklist_message.message_id=%s "
                        "как original_message_id для chat_id=%s",
                        original_message_id,
                        chat_id,
                    )

        # 2) Если по каким-то причинам checklist_message нет —
        #    используем reply_to_message, как раньше
        if original_message_id is None:
            reply_to = getattr(business_msg, "reply_to_message", None)
            if reply_to is not None:
                original_message_id = getattr(reply_to, "message_id", None)
                if original_message_id is not None:
                    logger.info(
                        "🔍 Событие чеклиста через reply_to_message: original_message_id=%s",
                        original_message_id,
                    )

        # 3) Если и этого нет — в самый последний момент fallback на business_msg.message_id
        if original_message_id is None:
            original_message_id = getattr(business_msg, "message_id", None)
            if original_message_id is not None:
                logger.info(
                    "🔍 Событие чеклиста напрямую: original_message_id=%s (message_id сервисного сообщения)",
                    original_message_id,
                )

        if original_message_id is None:
            logger.warning(
                "⚠️ handle_checklist_state_update: не удалось определить original_message_id "
                "для chat_id=%s, message_id=%s",
                chat_id,
                getattr(business_msg, "message_id", None),
            )
            return

        # Определяем, какой чеклист изменился
        target_checklist_type = None  # "daily" или tag name
        target_checklist_id = None
        target_checklist_title = None

        if user_state.checklist_message_id == original_message_id:
            target_checklist_type = "daily"
            target_checklist_id = original_message_id
            target_checklist_title = get_checklist_title_from_date(user_state.date) if user_state.date else "дневной"
            logger.info("🔍 Определён дневной чеклист: message_id=%s, title=%s", target_checklist_id, target_checklist_title)

        if not target_checklist_type:
            for tag, tag_state in user_state.tag_checklists.items():
                if tag_state.checklist_message_id == original_message_id:
                    target_checklist_type = tag
                    target_checklist_id = original_message_id
                    target_checklist_title = tag
                    logger.info("🔍 Определён теговый чеклист: tag='%s', message_id=%s", tag, target_checklist_id)
                    break

        if not target_checklist_type:
            logger.warning(
                "⚠️ Не удалось определить чеклист по message_id для chat_id=%s, original_message_id=%s. "
                "Дневной чеклист: message_id=%s, Теговые чеклисты: %s",
                chat_id,
                original_message_id,
                user_state.checklist_message_id,
                [(tag, ts.checklist_message_id) for tag, ts in user_state.tag_checklists.items()],
            )
            return

        # Используем target_checklist_type и target_checklist_id для дальнейшей обработки
        checklist_type = "daily" if target_checklist_type == "daily" else "tag"
        tag_name = None if target_checklist_type == "daily" else target_checklist_type
        checklist_message_id = original_message_id

        # 4. Собираем id выполненных / невыполненных пунктов
        done_ids: set[int] = set()
        not_done_ids: set[int] = set()
        if checklist_tasks_done is not None:
            done_ids = set(getattr(checklist_tasks_done, "marked_as_done_task_ids", []) or [])
            not_done_ids = set(getattr(checklist_tasks_done, "marked_as_not_done_task_ids", []) or [])

        logger.info(
            "🔧 checklist update: chat_id=%s type=%s tag=%s checklist_message_id=%s done_ids=%s not_done_ids=%s",
            chat_id,
            checklist_type,
            tag_name,
            checklist_message_id,
            sorted(done_ids),
            sorted(not_done_ids),
        )

        # 5. Обновляем задачи в нужном чек-листе
        # ВАЖНО: если определили чеклист по item_id (а не по checklist_message.message_id),
        # нужно найти задачу по item_id в определённом чеклисте, получить её текст,
        # и синхронизировать по тексту во всех чеклистах (а не по item_id, так как item_id могут совпадать)
        updated = False
        
        if identified_by_item_id:
            # Определили чеклист по item_id - это означает, что checklist_message отсутствует
            # Находим задачу по item_id в определённом чеклисте, обновляем её и синхронизируем по тексту
            # Это безопасно, потому что мы синхронизируем по тексту задачи, которую нашли в определённом чеклисте
            logger.info(
                "🔄 Определено по item_id: обновляем задачи в определённом чеклисте и синхронизируем по тексту для done_ids=%s, not_done_ids=%s",
                sorted(done_ids),
                sorted(not_done_ids),
            )
            
            # Находим задачи по item_id в определённом чеклисте, обновляем их и синхронизируем по тексту
            # Сначала обрабатываем done_ids
            for item_id in done_ids:
                task_text = None
                task_found = False
                
                # Ищем задачу в определённом чеклисте
                if checklist_type == "daily":
                    for task in user_state.tasks:
                        if task.item_id == item_id:
                            task_text = task.text
                            task_found = True
                            # Обновляем эту задачу
                            if not task.done:
                                task.done = True
                                updated = True
                                logger.info("✅ Дневная задача выполнена: id=%s text=%r", task.item_id, task.text)
                            break
                else:
                    # Теговый чеклист
                    tag_state = user_state.tag_checklists.get(tag_name)
                    if tag_state:
                        for task in tag_state.tasks:
                            if task.item_id == item_id:
                                task_text = task.text
                                task_found = True
                                # Обновляем эту задачу
                                if not task.done:
                                    task.done = True
                                    updated = True
                                    logger.info("✅ Теговая задача [%s] выполнена: id=%s text=%r", tag_name, task.item_id, task.text)
                                break
                
                # Синхронизируем по тексту во всех чеклистах, если задача найдена
                if task_found and task_text:
                    sync_updated = sync_task_status_by_text(user_state, task_text, True)
                    if sync_updated:
                        updated = True
                        logger.info("🔄 Синхронизировано по тексту после обновления по item_id: text=%r", task_text)
            
            # Теперь обрабатываем not_done_ids
            for item_id in not_done_ids:
                task_text = None
                task_found = False
                
                # Ищем задачу в определённом чеклисте
                if checklist_type == "daily":
                    for task in user_state.tasks:
                        if task.item_id == item_id:
                            task_text = task.text
                            task_found = True
                            # Обновляем эту задачу
                            if task.done:
                                task.done = False
                                updated = True
                                logger.info("🔄 Дневная задача снята: id=%s text=%r", task.item_id, task.text)
                            break
                else:
                    # Теговый чеклист
                    tag_state = user_state.tag_checklists.get(tag_name)
                    if tag_state:
                        for task in tag_state.tasks:
                            if task.item_id == item_id:
                                task_text = task.text
                                task_found = True
                                # Обновляем эту задачу
                                if task.done:
                                    task.done = False
                                    updated = True
                                    logger.info("🔄 Теговая задача [%s] снята: id=%s text=%r", tag_name, task.item_id, task.text)
                                break
                
                # Синхронизируем по тексту во всех чеклистах, если задача найдена
                if task_found and task_text:
                    sync_updated = sync_task_status_by_text(user_state, task_text, False)
                    if sync_updated:
                        updated = True
                        logger.info("🔄 Синхронизировано по тексту после обновления по item_id: text=%r", task_text)
        else:
            # Определили чеклист точно по checklist_message.message_id - обновляем задачи в нём и синхронизируем по тексту
            if checklist_type == "daily":
                for task in user_state.tasks:
                    if task.item_id in done_ids and not task.done:
                        task.done = True
                        updated = True
                        logger.info("✅ Дневная задача выполнена: id=%s text=%r", task.item_id, task.text)
                        # Синхронизируем по тексту во всех чеклистах
                        sync_updated = sync_task_status_by_text(user_state, task.text, True)
                        if sync_updated:
                            updated = True
                    if task.item_id in not_done_ids and task.done:
                        task.done = False
                        updated = True
                        logger.info("🔄 Дневная задача снята: id=%s text=%r", task.item_id, task.text)
                        # Синхронизируем по тексту во всех чеклистах
                        sync_updated = sync_task_status_by_text(user_state, task.text, False)
                        if sync_updated:
                            updated = True
            else:
                tag_state = user_state.tag_checklists.get(tag_name)
                if tag_state is None:
                    logger.warning(
                        "⚠️ handle_checklist_state_update: не найден tag_state для тега %r, хотя checklist_type='tag'",
                        tag_name,
                    )
                else:
                    for task in tag_state.tasks:
                        if task.item_id in done_ids and not task.done:
                            task.done = True
                            updated = True
                            logger.info("✅ Теговая задача [%s] выполнена: id=%s text=%r", tag_name, task.item_id, task.text)
                            # Синхронизируем по тексту во всех чеклистах
                            sync_updated = sync_task_status_by_text(user_state, task.text, True)
                            if sync_updated:
                                updated = True
                        if task.item_id in not_done_ids and task.done:
                            task.done = False
                            updated = True
                            logger.info("🔄 Теговая задача [%s] снята: id=%s text=%r", tag_name, task.item_id, task.text)
                            # Синхронизируем по тексту во всех чеклистах
                            sync_updated = sync_task_status_by_text(user_state, task.text, False)
                            if sync_updated:
                                updated = True

        # 6. Сохраняем состояние, если что-то изменилось
        if updated:
            save_user_state(chat_id, user_state)
            logger.info(
                "💾 Состояние user_state сохранено после checklist_update: chat_id=%s type=%s tag=%s",
                chat_id,
                checklist_type,
                tag_name,
            )
        else:
            logger.info(
                "ℹ️ checklist_update не изменил состояние user_state: chat_id=%s checklist_message_id=%s",
                chat_id,
                checklist_message_id,
            )

    except Exception as e:
        logger.error(
            "❌ Ошибка в handle_checklist_state_update для chat_id=%s: %s",
            chat_id,
            e,
            exc_info=True,
        )

