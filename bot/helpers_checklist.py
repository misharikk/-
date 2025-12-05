"""
Модуль для работы с Telegram Checklist: создание и обновление чеклистов.
"""

import logging
from datetime import datetime
from telegram import InputChecklist, InputChecklistTask

# Импорты из других модулей (будут добавлены после создания)
from state import UserState, TagChecklistState, TaskItem, save_user_state
from helpers_text import get_user_local_date

logger = logging.getLogger(__name__)


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
        if user_state.checklist_message_id is not None:
            # уже есть чеклист — ничего не делаем
            logger.info(f"⏭️ Чеклист уже существует для chat_id={chat_id}, message_id={user_state.checklist_message_id}")
            return

        logger.info(f"🔨 Начинаю создание чеклиста для chat_id={chat_id}")
        
        # Определяем дату чеклиста: если её ещё нет, вычисляем по "локальному" дню пользователя
        if not user_state.date:
            user_state.date = get_user_local_date(user_state)
        
        human_date = get_human_date_from_iso(user_state.date)
        
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
            
            # ВАЖНО: id в чеклисте должен быть позицией (1-based), а не item_id из состояния
            # Это нужно для правильной синхронизации событий
            tasks.append(InputChecklistTask(
                id=task_position,  # Используем позицию, а не task_item.item_id
                text=task_text,
            ))

        # Если нет невыполненных задач - не создаем чеклист
        if not tasks:
            logger.info(f"⏭️ Нет невыполненных задач для создания чеклиста для chat_id={chat_id}, пропускаем")
            return

        checklist = InputChecklist(
            title=human_date,
            tasks=tasks,
            others_can_add_tasks=False,
            others_can_mark_tasks_as_done=True,
        )

        logger.info(f"📤 Отправляю чеклист для chat_id={chat_id}, title='{human_date}', задач={len(tasks)}")
        msg = await bot.send_checklist(
            business_connection_id=user_state.business_connection_id,
            chat_id=chat_id,
            checklist=checklist,
        )
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
    """
    try:
        if user_state.checklist_message_id is None:
            # на всякий случай: если вдруг нет чеклиста — создаём
            await create_checklist_for_user(bot, chat_id, user_state)
            return

        tasks = []
        for task_item in user_state.tasks:
            # Формируем текст без номера
            task_text = task_item.text
            # Обрезаем до 100 символов (лимит Telegram API для чеклистов)
            if len(task_text) > 100:
                task_text = task_text[:97].rstrip() + "…"
            tasks.append(InputChecklistTask(
                id=task_item.item_id,
                text=task_text,
            ))

        # Используем дату из user_state для title чеклиста
        human_date = get_human_date_from_iso(user_state.date) if user_state.date else get_today_human_date()
        
        checklist = InputChecklist(
            title=human_date,
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
            tasks = []
            for task_item in tag_state.tasks:
                task_text = task_item.text
                # Обрезаем до 100 символов (лимит Telegram API для чеклистов)
                if len(task_text) > 100:
                    task_text = task_text[:97].rstrip() + "…"
                tasks.append(InputChecklistTask(
                    id=task_item.item_id,
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
        if tag not in user_state.tag_checklists:
            # Формируем первую задачу без нумерации
            first_task_text = task_text
            if len(first_task_text) > 100:
                first_task_text = first_task_text[:97].rstrip() + "…"
            
            tasks = [InputChecklistTask(
                id=1,
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
            
            # Сохраняем состояние чеклиста
            tag_state = TagChecklistState(
                title=tag,
                checklist_message_id=msg.message_id,
                tasks=[TaskItem(item_id=1, text=task_text, done=False)],
            )
            user_state.tag_checklists[tag] = tag_state
            save_user_state(chat_id, user_state)
            
            logger.info(f"✅ Чеклист по тегу '{tag}' создан для chat_id={chat_id}, message_id={msg.message_id}")
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

            # ВАЖНО: id в чеклисте должен быть позицией (1-based), а не item_id из состояния
            # Это нужно для правильной синхронизации событий
            tasks.append(InputChecklistTask(
                id=task_position,  # Используем позицию, а не task_item.item_id
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


async def handle_checklist_state_update(business_msg, user_state: UserState, chat_id: int) -> None:
    """
    Обрабатывает изменение состояния пункта чеклиста (галочка/снятие).
    Определяет, какой чеклист изменился (дневной или теговый),
    какой item_id, новое состояние done (True/False),
    и обновляет user_state.tasks или user_state.tag_checklists[*].tasks.
    """
    try:
        # Определяем message_id чеклиста (самого сообщения с чеклистом)
        # Когда пользователь отмечает пункт, Telegram может отправлять событие как reply к сообщению с чеклистом
        checklist_message_id = None
        reply_to = getattr(business_msg, "reply_to_message", None)
        if reply_to:
            checklist_message_id = reply_to.message_id
            logger.info(f"🔍 Событие чеклиста через reply_to_message: checklist_message_id={checklist_message_id}")
        else:
            checklist_message_id = business_msg.message_id
            logger.info(f"🔍 Событие чеклиста напрямую: checklist_message_id={checklist_message_id}")
        
        # Пробуем найти информацию об изменении в различных полях
        changed_item_id = None
        is_done = None
        
        # Проверяем различные возможные поля для событий чеклиста
        # Сначала пробуем через to_dict() для полного просмотра структуры
        msg_dict = business_msg.to_dict()
        
        # Логируем структуру для отладки
        checklist_keys = [k for k in msg_dict.keys() if 'checklist' in k.lower() or 'item' in k.lower()]
        logger.info(f"🔍 Обработка события чеклиста для chat_id={chat_id}, checklist_message_id={checklist_message_id}")
        if checklist_keys:
            logger.info(f"🔍 Доступные checklist-поля: {checklist_keys}")
        
        # Пробуем найти информацию о измененном пункте
        # Telegram может отправлять информацию в разных форматах
        
        # Сначала проверяем checklist_tasks_done (новый формат событий)
        checklist_tasks_done = getattr(business_msg, "checklist_tasks_done", None)
        if checklist_tasks_done:
            logger.info(f"🔍 Найдено checklist_tasks_done: {checklist_tasks_done}")
            
            # Обрабатываем marked_as_not_done_task_ids (снятие выполнения)
            if hasattr(checklist_tasks_done, "marked_as_not_done_task_ids"):
                undone_ids = checklist_tasks_done.marked_as_not_done_task_ids
                if undone_ids:
                    logger.info(f"🔄 Найдены снятые с выполнения задачи через checklist_tasks_done: {undone_ids}")
                    # Ищем чеклист и снимаем выполнение
                    reply_to = getattr(business_msg, "reply_to_message", None)
                    target_checklist_id = reply_to.message_id if reply_to else None
                    
                    if not target_checklist_id:
                        # Ищем по item_id
                        all_checklists = []
                        if user_state.checklist_message_id:
                            all_checklists.append(("daily", user_state.checklist_message_id, user_state.tasks))
                        for tag, tag_state in user_state.tag_checklists.items():
                            if tag_state.checklist_message_id:
                                all_checklists.append((tag, tag_state.checklist_message_id, tag_state.tasks))
                        
                        for checklist_type, msg_id, tasks in all_checklists:
                            task_ids_in_checklist = {task.item_id for task in tasks}
                            if all(item_id in task_ids_in_checklist for item_id in undone_ids):
                                target_checklist_id = msg_id
                                break
                    
                    updated = False
                    if target_checklist_id and user_state.checklist_message_id == target_checklist_id:
                        for task in user_state.tasks:
                            if task.item_id in undone_ids:
                                task.done = False
                                updated = True
                                logger.info(f"🔄 Снято выполнение дневной задачи: item_id={task.item_id}")
                    else:
                        for tag, tag_state in user_state.tag_checklists.items():
                            if tag_state.checklist_message_id == target_checklist_id:
                                for task in tag_state.tasks:
                                    if task.item_id in undone_ids:
                                        task.done = False
                                        updated = True
                                        logger.info(f"🔄 Снято выполнение теговой задачи '{tag}': item_id={task.item_id}")
                    
                    if not updated:
                        # Пробуем обновить по item_id напрямую
                        for task in user_state.tasks:
                            if task.item_id in undone_ids:
                                task.done = False
                                updated = True
                        for tag, tag_state in user_state.tag_checklists.items():
                            for task in tag_state.tasks:
                                if task.item_id in undone_ids:
                                    task.done = False
                                    updated = True
                    
                    if updated:
                        save_user_state(chat_id, user_state)
                        logger.info(f"✅ Состояние обновлено после снятия выполнения")
                        return
            
            # Извлекаем marked_as_done_task_ids
            if hasattr(checklist_tasks_done, "marked_as_done_task_ids"):
                marked_ids = checklist_tasks_done.marked_as_done_task_ids
                if marked_ids:
                    logger.info(f"✅ Найдены выполненные задачи через checklist_tasks_done: {marked_ids}")
                    
                    # ВАЖНО: для checklist_tasks_done события приходят как отдельные сообщения
                    # Нужно найти чеклист по reply_to_message или попробовать все чеклисты
                    reply_to = getattr(business_msg, "reply_to_message", None)
                    target_checklist_id = None
                    
                    if reply_to:
                        target_checklist_id = reply_to.message_id
                        logger.info(f"🔍 Чеклист определён через reply_to_message: {target_checklist_id}")
                    else:
                        # Если нет reply_to, ищем чеклист, который содержит все указанные item_id
                        # Сначала пробуем найти среди всех чеклистов (и дневных, и теговых)
                        logger.info(f"🔍 reply_to_message отсутствует, ищем чеклист по item_id среди всех чеклистов")
                        
                        # Собираем все чеклисты для поиска
                        all_checklists = []
                        if user_state.checklist_message_id:
                            all_checklists.append(("daily", user_state.checklist_message_id, user_state.tasks))
                        
                        for tag, tag_state in user_state.tag_checklists.items():
                            if tag_state.checklist_message_id:
                                all_checklists.append((tag, tag_state.checklist_message_id, tag_state.tasks))
                        
                        # Ищем чеклист по позиции задач среди невыполненных задач
                        # marked_ids - это позиции в чеклисте (1-based), а не item_id
                        for checklist_type, msg_id, tasks in all_checklists:
                            # Считаем невыполненные задачи
                            unfinished_tasks_in_checklist = [t for t in tasks if not t.done]
                            max_position = len(unfinished_tasks_in_checklist)
                            # Проверяем, что ВСЕ marked_ids (позиции) находятся в допустимом диапазоне
                            if max_position > 0 and all(1 <= pos <= max_position for pos in marked_ids):
                                target_checklist_id = msg_id
                                checklist_name = "дневной" if checklist_type == "daily" else f"теговый '{checklist_type}'"
                                logger.info(f"🔍 Найден {checklist_name} чеклист по позиции: {target_checklist_id} (невыполненных задач: {max_position}, позиции: {marked_ids})")
                                break
                    
                    # Обновляем дневной чеклист
                    if target_checklist_id and user_state.checklist_message_id == target_checklist_id:
                        # ВАЖНО: item_id в событиях - это позиция в чеклисте (1-based) среди невыполненных задач
                        # При создании чеклиста мы используем позицию только для невыполненных задач
                        # Поэтому нужно найти задачу по позиции среди невыполненных задач в том же порядке, как при создании
                        unfinished_tasks = [t for t in user_state.tasks if not t.done]
                        for marked_id in marked_ids:
                            # marked_id - это позиция в чеклисте (1-based) среди невыполненных задач
                            if 1 <= marked_id <= len(unfinished_tasks):
                                task = unfinished_tasks[marked_id - 1]
                                if not task.done:  # Дополнительная проверка
                                    task.done = True
                                    logger.info(f"✅ Обновлен дневной чеклист: позиция={marked_id}, item_id={task.item_id}, text='{task.text[:30]}', done=True")
                                else:
                                    logger.warning(f"⚠️ Задача на позиции {marked_id} уже выполнена: item_id={task.item_id}")
                            else:
                                logger.warning(f"⚠️ Позиция {marked_id} не найдена в дневном чеклисте (невыполненных задач: {len(unfinished_tasks)})")
                        save_user_state(chat_id, user_state)
                        return
                    
                    # Обновляем теговые чеклисты
                    if target_checklist_id:
                        for tag, tag_state in user_state.tag_checklists.items():
                            if tag_state.checklist_message_id == target_checklist_id:
                                # ВАЖНО: item_id в событиях - это позиция в чеклисте (1-based) среди невыполненных задач
                                # При создании чеклиста мы используем позицию только для невыполненных задач
                                # Поэтому нужно найти задачу по позиции среди невыполненных задач в том же порядке, как при создании
                                unfinished_tasks = [t for t in tag_state.tasks if not t.done]
                                for marked_id in marked_ids:
                                    # marked_id - это позиция в чеклисте (1-based) среди невыполненных задач
                                    if 1 <= marked_id <= len(unfinished_tasks):
                                        task = unfinished_tasks[marked_id - 1]
                                        if not task.done:  # Дополнительная проверка
                                            task.done = True
                                            logger.info(f"✅ Обновлен теговый чеклист '{tag}': позиция={marked_id}, item_id={task.item_id}, text='{task.text[:30]}', done=True")
                                        else:
                                            logger.warning(f"⚠️ Задача на позиции {marked_id} в чеклисте '{tag}' уже выполнена: item_id={task.item_id}")
                                    else:
                                        logger.warning(f"⚠️ Позиция {marked_id} не найдена в чеклисте '{tag}' (невыполненных задач: {len(unfinished_tasks)})")
                                save_user_state(chat_id, user_state)
                                return
                    
                    # Если не нашли по message_id, пробуем обновить по item_id напрямую
                    logger.warning(f"⚠️ Не найден чеклист по message_id, пробую обновить по item_id напрямую")
                    
                    # Обновляем дневной чеклист по позиции (если не нашли по message_id)
                    updated = False
                    unfinished_tasks = [t for t in user_state.tasks if not t.done]
                    for marked_id in marked_ids:
                        # marked_id - это позиция в чеклисте (1-based)
                        if 1 <= marked_id <= len(unfinished_tasks):
                            task = unfinished_tasks[marked_id - 1]
                            task.done = True
                            updated = True
                            logger.info(f"✅ Обновлен дневной чеклист (по позиции): позиция={marked_id}, item_id={task.item_id}, done=True")
                    
                    if updated:
                        save_user_state(chat_id, user_state)
                        return
                    
                    # Обновляем теговые чеклисты по позиции
                    for tag, tag_state in user_state.tag_checklists.items():
                        unfinished_tag_tasks = [t for t in tag_state.tasks if not t.done]
                        for marked_id in marked_ids:
                            # marked_id - это позиция в чеклисте (1-based)
                            if 1 <= marked_id <= len(unfinished_tag_tasks):
                                task = unfinished_tag_tasks[marked_id - 1]
                                task.done = True
                                updated = True
                                logger.info(f"✅ Обновлен теговый чеклист '{tag}' (по позиции): позиция={marked_id}, item_id={task.item_id}, done=True")
                    
                    if updated:
                        save_user_state(chat_id, user_state)
                        return
                    
                    logger.warning(f"⚠️ Не удалось обновить задачи: не найдены item_id {marked_ids} ни в дневном, ни в теговых чеклистах")
                    logger.warning(f"⚠️ Дневной чеклист: message_id={user_state.checklist_message_id}, задач={len(user_state.tasks)}")
                    logger.warning(f"⚠️ Теговые чеклисты: {[(tag, ts.checklist_message_id, len(ts.tasks)) for tag, ts in user_state.tag_checklists.items()]}")
                    return
        
        checklist_item_state = getattr(business_msg, "new_checklist_item_state", None) or getattr(business_msg, "checklist_item_state", None)
        
        if checklist_item_state and changed_item_id is None:
            # Если это объект, пробуем извлечь данные
            if hasattr(checklist_item_state, "item_id"):
                changed_item_id = checklist_item_state.item_id
            elif hasattr(checklist_item_state, "id"):
                changed_item_id = checklist_item_state.id
            
            if hasattr(checklist_item_state, "is_checked"):
                is_done = checklist_item_state.is_checked
            elif hasattr(checklist_item_state, "checked"):
                is_done = checklist_item_state.checked
            elif hasattr(checklist_item_state, "state"):
                # Возможно, состояние в поле state
                state = checklist_item_state.state
                if isinstance(state, bool):
                    is_done = state
                elif isinstance(state, str):
                    is_done = state.lower() in ["checked", "done", "true", "1"]
        else:
            # Пробуем найти в других полях
            # Проверяем new_checklist_item
            new_checklist_item = getattr(business_msg, "new_checklist_item", None)
            if new_checklist_item:
                if hasattr(new_checklist_item, "item_id"):
                    changed_item_id = new_checklist_item.item_id
                elif hasattr(new_checklist_item, "id"):
                    changed_item_id = new_checklist_item.id
                
                if hasattr(new_checklist_item, "is_checked"):
                    is_done = new_checklist_item.is_checked
                elif hasattr(new_checklist_item, "checked"):
                    is_done = new_checklist_item.checked
            
            # Если не нашли в new_checklist_item, пробуем искать в словаре
            if changed_item_id is None or is_done is None:
                # Ищем в msg_dict напрямую
                if "new_checklist_item_state" in msg_dict:
                    item_state = msg_dict["new_checklist_item_state"]
                    if isinstance(item_state, dict):
                        changed_item_id = item_state.get("item_id") or item_state.get("id")
                        is_done = item_state.get("is_checked") or item_state.get("checked")
                        if isinstance(is_done, str):
                            is_done = is_done.lower() in ["checked", "done", "true", "1"]
                
                if (changed_item_id is None or is_done is None) and "checklist_item_state" in msg_dict:
                    item_state = msg_dict["checklist_item_state"]
                    if isinstance(item_state, dict):
                        changed_item_id = item_state.get("item_id") or item_state.get("id")
                        is_done = item_state.get("is_checked") or item_state.get("checked")
                        if isinstance(is_done, str):
                            is_done = is_done.lower() in ["checked", "done", "true", "1"]
        
        # Если не удалось извлечь данные, пробуем через reply_to_message
        # Когда пользователь отмечает пункт, Telegram может отправлять событие как reply к сообщению с чеклистом
        if changed_item_id is None or is_done is None:
            reply_to = getattr(business_msg, "reply_to_message", None)
            if reply_to:
                checklist_message_id = reply_to.message_id
                # Пробуем извлечь информацию из текста или других полей
                logger.info(f"🔍 Проверяю reply_to_message для chat_id={chat_id}, reply_message_id={checklist_message_id}")
        
        # Если всё ещё не нашли, логируем всю структуру для отладки
        if changed_item_id is None or is_done is None:
            logger.warning(f"⚠️ Не удалось извлечь item_id или is_done из события чеклиста для chat_id={chat_id}")
            logger.warning(f"⚠️ Структура business_msg: {list(msg_dict.keys())[:20]}...")  # Первые 20 ключей
            return
        
        logger.info(f"✅ Извлечены данные: item_id={changed_item_id}, is_done={is_done}, checklist_message_id={checklist_message_id}")
        
        # Определяем, какой чеклист изменился
        updated = False
        
        # Проверяем дневной чеклист
        if user_state.checklist_message_id == checklist_message_id:
            # Это дневной чеклист
            for task in user_state.tasks:
                if task.item_id == changed_item_id:
                    task.done = is_done
                    updated = True
                    logger.info(f"✅ Обновлен дневной чеклист: item_id={changed_item_id}, done={is_done}")
                    break
            
            if not updated:
                logger.warning(f"⚠️ Не найден item_id={changed_item_id} в дневном чеклисте для chat_id={chat_id}")
        else:
            # Проверяем теговые чеклисты
            for tag, tag_state in user_state.tag_checklists.items():
                if tag_state.checklist_message_id == checklist_message_id:
                    # Нашли соответствующий теговый чеклист
                    for task in tag_state.tasks:
                        if task.item_id == changed_item_id:
                            task.done = is_done
                            updated = True
                            logger.info(f"✅ Обновлен теговый чеклист '{tag}': item_id={changed_item_id}, done={is_done}")
                            break
                    
                    if not updated:
                        logger.warning(f"⚠️ Не найден item_id={changed_item_id} в теговом чеклисте '{tag}' для chat_id={chat_id}")
                    break
        
        if updated:
            # Сохраняем состояние в SQLite
            save_user_state(chat_id, user_state)
            logger.info(f"✅ Состояние сохранено для chat_id={chat_id}")
        else:
            logger.warning(f"⚠️ Не найден чеклист с message_id={checklist_message_id} для chat_id={chat_id}")
            
    except Exception as e:
        logger.error(f"❌ Ошибка при обработке изменения состояния чеклиста для chat_id={chat_id}: {e}", exc_info=True)
        # Не пробрасываем ошибку, чтобы не сломать обработку других сообщений

