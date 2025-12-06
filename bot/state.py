"""
Модуль для управления состоянием пользователей бота.

Содержит:
- UserState: dataclass с полями состояния пользователя
- STATE: глобальное хранилище состояний (in-memory кэш)
- load_user_state/save_user_state: функции для работы со состоянием (SQLite + кэш)
"""

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Optional, Dict, List
from db import get_connection

logger = logging.getLogger(__name__)


@dataclass
class TaskItem:
    """Элемент задачи в чеклисте"""
    item_id: int      # id пункта в Telegram Checklist
    text: str         # текст задачи
    done: bool = False  # выполнена ли задача


@dataclass
class TagChecklistState:
    """Состояние чеклиста по тегу"""
    title: str  # текст тега
    checklist_message_id: int  # message_id чеклиста в Telegram
    tasks: List[TaskItem] = field(default_factory=list)  # список задач


@dataclass
class UserState:
    business_connection_id: str
    asked_for_time: bool = False   # показывали интро и просили время?
    waiting_for_time: bool = False # ждём ввод времени HH:MM
    time: Optional[str] = None     # строка "HH:MM" (для обратной совместимости)
    day_end_time: Optional[str] = None  # строка "HH:MM" - время окончания дня (ежедневный конец дня)
    timezone_offset_minutes: int = 0  # смещение часового пояса в минутах относительно UTC
    
    # Поля для чеклиста:
    checklist_message_id: Optional[int] = None   # message_id созданного чеклиста
    date: Optional[str] = None                   # дата чеклиста, можно хранить "YYYY-MM-DD"
    tasks: List[TaskItem] = field(default_factory=list)  # список задач
    last_closed_date: Optional[str] = None       # дата последнего закрытия дня (защита от двойного закрытия)
    last_opened_date: Optional[str] = None       # дата последнего открытия дня (защита от двойного закрытия)
    
    # Служебные сообщения для удаления
    service_message_ids: List[int] = field(default_factory=list)
    
    # Поля для подтверждения задачи и тегов:
    pending_task_text: Optional[str] = None  # текущая "висящая" задача
    pending_task_message_id: Optional[int] = None  # сообщение пользователя с задачей
    pending_service_message_ids: List[int] = field(default_factory=list)  # все служебные сообщения вокруг задачи
    awaiting_tag: bool = False  # сейчас ждём тег вместо новой задачи
    tags_history: List[str] = field(default_factory=list)  # список последних используемых тегов
    tags_page_index: int = 0  # индекс страницы для листания тегов
    pending_confirm_job_id: Optional[str] = None  # id задачи в job_queue для авто-"Пропустить"
    next_rollover_job_name: Optional[str] = None  # имя job'а для смены дня (индивидуальный midnight job)
    
    # Чеклисты по тегам (ключ = текст тега, значение = TagChecklistState)
    tag_checklists: Dict[str, TagChecklistState] = field(default_factory=dict)


# Глобальное хранилище состояний пользователей (кэш в памяти для быстрого доступа)
STATE: Dict[int, UserState] = {}


def load_user_state(chat_id: int) -> Optional[UserState]:
    """
    Возвращает состояние пользователя из SQLite (с кэшированием в памяти).
    Если нет в БД - возвращает None.
    """
    # Сначала проверяем кэш
    if chat_id in STATE:
        return STATE[chat_id]
    
    # Загружаем из SQLite
    conn = get_connection()
    cursor = conn.cursor()
    
    # Пытаемся загрузить с новыми полями
    try:
        cursor.execute("""
            SELECT 
                business_connection_id, asked_for_time, waiting_for_time, time,
                timezone_offset_minutes,
                checklist_message_id, date, tasks, service_message_ids,
                pending_task_text, pending_task_message_id, pending_service_message_ids,
                awaiting_tag, tags_history, tags_page_index, pending_confirm_job_id,
                tag_checklists, last_closed_date, last_opened_date, next_rollover_job_name, day_end_time
            FROM user_state
            WHERE chat_id = ?
        """, (chat_id,))
        has_new_fields = True
    except sqlite3.OperationalError:
        # Если колонок нет - загружаем без них
        cursor.execute("""
            SELECT 
                business_connection_id, asked_for_time, waiting_for_time, time,
                checklist_message_id, date, tasks, service_message_ids,
                pending_task_text, pending_task_message_id, pending_service_message_ids,
                awaiting_tag, tags_history, tags_page_index, pending_confirm_job_id,
                tag_checklists
            FROM user_state
            WHERE chat_id = ?
        """, (chat_id,))
        has_new_fields = False
    
    row = cursor.fetchone()
    conn.close()
    
    if row is None:
        return None
    
    # Распаковываем данные из БД через кортеж (избегаем проблем с индексами)
    if has_new_fields:
        (
            business_connection_id,
            asked_for_time_raw,
            waiting_for_time_raw,
            time,
            timezone_offset_minutes,
            checklist_message_id,
            date,
            tasks_json,
            service_message_ids_json,
            pending_task_text,
            pending_task_message_id,
            pending_service_message_ids_json,
            awaiting_tag_raw,
            tags_history_json,
            tags_page_index_raw,
            pending_confirm_job_id,
            tag_checklists_json,
            last_closed_date,
            last_opened_date,
            next_rollover_job_name,
            day_end_time,
        ) = row
    else:
        (
            business_connection_id,
            asked_for_time_raw,
            waiting_for_time_raw,
            time,
            checklist_message_id,
            date,
            tasks_json,
            service_message_ids_json,
            pending_task_text,
            pending_task_message_id,
            pending_service_message_ids_json,
            awaiting_tag_raw,
            tags_history_json,
            tags_page_index_raw,
            pending_confirm_job_id,
            tag_checklists_json,
        ) = row
        # Для старой схемы поля отсутствуют — выставляем дефолты
        timezone_offset_minutes = 0
        last_closed_date = None
        last_opened_date = None
        next_rollover_job_name = None
        day_end_time = None
    
    # Нормализуем типы
    asked_for_time = bool(asked_for_time_raw) if asked_for_time_raw is not None else False
    waiting_for_time = bool(waiting_for_time_raw) if waiting_for_time_raw is not None else False
    tags_page_index = tags_page_index_raw if tags_page_index_raw is not None else 0
    awaiting_tag = bool(awaiting_tag_raw) if awaiting_tag_raw is not None else False
    
    # Десериализуем tasks из JSON в список TaskItem
    tasks_data = json.loads(tasks_json) if tasks_json else []
    tasks: List[TaskItem] = []
    for item in tasks_data:
        if isinstance(item, dict):
            tasks.append(TaskItem(**item))
        elif isinstance(item, str):
            tasks.append(TaskItem(item_id=len(tasks) + 1, text=item, done=False))
        else:
            tasks.append(TaskItem(item_id=len(tasks) + 1, text=str(item), done=False))
    
    # Десериализуем service_message_ids
    service_message_ids = json.loads(service_message_ids_json) if service_message_ids_json else []
    
    # Десериализуем pending_service_message_ids
    pending_service_message_ids = json.loads(pending_service_message_ids_json) if pending_service_message_ids_json else []
    
    # Десериализуем tags_history
    tags_history = json.loads(tags_history_json) if tags_history_json else []
    
    # Десериализуем tag_checklists из JSON
    tag_checklists: Dict[str, TagChecklistState] = {}
    if tag_checklists_json:
        tag_checklists_raw = json.loads(tag_checklists_json)
        for tag, tag_data in tag_checklists_raw.items():
            tag_tasks_data = tag_data.get("tasks", [])
            tag_tasks: List[TaskItem] = []
            for item in tag_tasks_data:
                if isinstance(item, dict):
                    tag_tasks.append(TaskItem(**item))
                elif isinstance(item, str):
                    tag_tasks.append(TaskItem(item_id=len(tag_tasks) + 1, text=item, done=False))
                else:
                    tag_tasks.append(TaskItem(item_id=len(tag_tasks) + 1, text=str(item), done=False))
            tag_checklists[tag] = TagChecklistState(
                title=tag_data["title"],
                checklist_message_id=tag_data["checklist_message_id"],
                tasks=tag_tasks,
            )
    
    # Создаем объект UserState с явным указанием всех полей
    user_state = UserState(
        business_connection_id=business_connection_id,
        asked_for_time=asked_for_time,
        waiting_for_time=waiting_for_time,
        time=time,
        day_end_time=day_end_time,
        timezone_offset_minutes=timezone_offset_minutes or 0,
        checklist_message_id=checklist_message_id,
        date=date,
        tasks=tasks,
        last_closed_date=last_closed_date,
        last_opened_date=last_opened_date,
        service_message_ids=service_message_ids,
        pending_task_text=pending_task_text,
        pending_task_message_id=pending_task_message_id,
        pending_service_message_ids=pending_service_message_ids,
        awaiting_tag=awaiting_tag,
        tags_history=tags_history,
        tags_page_index=tags_page_index,
        pending_confirm_job_id=pending_confirm_job_id,
        next_rollover_job_name=next_rollover_job_name,
        tag_checklists=tag_checklists,
    )
    
    # Сохраняем в кэш
    STATE[chat_id] = user_state
    
    return user_state


def clean_tasks_list(tasks: List[TaskItem]) -> List[TaskItem]:
    """
    Очищает список задач от дубликатов:
    - Удаляет дубликаты по item_id (оставляет первую задачу с таким item_id)
    - Удаляет дубликаты по тексту (оставляет первую задачу с таким текстом)
    
    Возвращает очищенный список задач.
    """
    if not tasks:
        return tasks
    
    # Шаг 1: Удаляем дубликаты по item_id (оставляем первую задачу с таким item_id)
    seen_item_ids = set()
    clean_by_id = []
    for task in tasks:
        if task.item_id not in seen_item_ids:
            clean_by_id.append(task)
            seen_item_ids.add(task.item_id)
    
    # Шаг 2: Удаляем дубликаты по тексту (оставляем первую задачу с таким текстом)
    seen_texts = set()
    clean = []
    for task in clean_by_id:
        # Нормализуем текст для сравнения (убираем пробелы по краям, приводим к нижнему регистру)
        normalized_text = task.text.strip().lower()
        if normalized_text not in seen_texts:
            clean.append(task)
            seen_texts.add(normalized_text)
    
    return clean


def validate_and_clean_user_state(user_state: UserState) -> None:
    """
    Валидирует и очищает состояние пользователя перед сохранением:
    - Удаляет дубликаты задач по item_id и тексту
    - Применяется к user_state.tasks и user_state.tag_checklists[tag].tasks
    """
    # Очищаем дневные задачи
    original_count = len(user_state.tasks)
    user_state.tasks = clean_tasks_list(user_state.tasks)
    if len(user_state.tasks) != original_count:
        logger.warning(f"🧹 Очищены дневные задачи: было {original_count}, стало {len(user_state.tasks)}")
    
    # Очищаем задачи в теговых чеклистах
    for tag, tag_state in user_state.tag_checklists.items():
        original_count = len(tag_state.tasks)
        tag_state.tasks = clean_tasks_list(tag_state.tasks)
        if len(tag_state.tasks) != original_count:
            logger.warning(f"🧹 Очищены задачи в теговом чеклисте '{tag}': было {original_count}, стало {len(tag_state.tasks)}")


def save_user_state(chat_id: int, user_state: UserState) -> None:
    """
    Сохраняет состояние пользователя в SQLite и обновляет кэш.
    Перед сохранением валидирует и очищает данные (удаляет дубликаты по item_id и тексту).
    """
    # Валидируем и очищаем данные перед сохранением
    validate_and_clean_user_state(user_state)
    
    # Сохраняем в кэш
    STATE[chat_id] = user_state
    
    # Сохраняем в SQLite
    conn = get_connection()
    cursor = conn.cursor()
    
    # Сериализуем tasks в JSON (список словарей)
    tasks_json = [{"item_id": task.item_id, "text": task.text, "done": task.done} for task in user_state.tasks]
    
    # Сериализуем tag_checklists в JSON
    tag_checklists_json = {}
    for tag, tag_state in user_state.tag_checklists.items():
        tag_tasks_json = [{"item_id": task.item_id, "text": task.text, "done": task.done} for task in tag_state.tasks]
        tag_checklists_json[tag] = {
            "title": tag_state.title,
            "checklist_message_id": tag_state.checklist_message_id,
            "tasks": tag_tasks_json
        }
    
    # Пытаемся сохранить с новыми полями
    try:
        cursor.execute("""
            INSERT OR REPLACE INTO user_state (
                chat_id, business_connection_id, asked_for_time, waiting_for_time, time,
                timezone_offset_minutes, checklist_message_id, date, tasks, service_message_ids,
                pending_task_text, pending_task_message_id, pending_service_message_ids,
                awaiting_tag, tags_history, tags_page_index, pending_confirm_job_id,
                tag_checklists, last_closed_date, last_opened_date, next_rollover_job_name, day_end_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            chat_id,
            user_state.business_connection_id,
            1 if user_state.asked_for_time else 0,
            1 if user_state.waiting_for_time else 0,
            user_state.time,
            user_state.timezone_offset_minutes,
            user_state.checklist_message_id,
            user_state.date,
            json.dumps(tasks_json, ensure_ascii=False),
            json.dumps(user_state.service_message_ids, ensure_ascii=False),
            user_state.pending_task_text,
            user_state.pending_task_message_id,
            json.dumps(user_state.pending_service_message_ids, ensure_ascii=False),
            1 if user_state.awaiting_tag else 0,
            json.dumps(user_state.tags_history, ensure_ascii=False),
            user_state.tags_page_index,
            user_state.pending_confirm_job_id,
            json.dumps(tag_checklists_json, ensure_ascii=False),
            user_state.last_closed_date,
            user_state.last_opened_date,
            user_state.next_rollover_job_name,
            user_state.day_end_time,
        ))
    except sqlite3.OperationalError:
        # Если колонок нет - сохраняем без них (миграция добавит их при следующем запуске)
        cursor.execute("""
            INSERT OR REPLACE INTO user_state (
                chat_id, business_connection_id, asked_for_time, waiting_for_time, time,
                checklist_message_id, date, tasks, service_message_ids,
                pending_task_text, pending_task_message_id, pending_service_message_ids,
                awaiting_tag, tags_history, tags_page_index, pending_confirm_job_id,
                tag_checklists
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            chat_id,
            user_state.business_connection_id,
            1 if user_state.asked_for_time else 0,
            1 if user_state.waiting_for_time else 0,
            user_state.time,
            user_state.checklist_message_id,
            user_state.date,
            json.dumps(tasks_json, ensure_ascii=False),
            json.dumps(user_state.service_message_ids, ensure_ascii=False),
            user_state.pending_task_text,
            user_state.pending_task_message_id,
            json.dumps(user_state.pending_service_message_ids, ensure_ascii=False),
            1 if user_state.awaiting_tag else 0,
            json.dumps(user_state.tags_history, ensure_ascii=False),
            user_state.tags_page_index,
            user_state.pending_confirm_job_id,
            json.dumps(tag_checklists_json, ensure_ascii=False),
        ))
    
    conn.commit()
    conn.close()


def set_user_time_info(chat_id: int, local_time_str: str) -> bool:
    """
    Вычисляет и сохраняет информацию о времени пользователя:
    - timezone_offset_minutes (UTC-смещение)
    - day_end_time (время окончания дня)
    - date (локальная дата пользователя)
    - last_closed_date = date (сбрасывает на текущую дату, если не установлено)
    
    Возвращает True если время успешно установлено, False если ошибка парсинга.
    """
    import logging
    from helpers_text import parse_time_string
    from helpers_daily import compute_local_datetime_and_offset
    from datetime import datetime
    
    logger = logging.getLogger(__name__)
    
    # Парсим время
    parsed = parse_time_string(local_time_str)
    if not parsed:
        return False
    
    # Загружаем состояние
    user_state = load_user_state(chat_id)
    if not user_state:
        return False
    
    # Вычисляем локальное время и смещение
    now_utc = datetime.utcnow()
    try:
        local_dt, utc_offset_minutes = compute_local_datetime_and_offset(now_utc, parsed)
    except Exception as e:
        logger.error(f"❌ Ошибка при вычислении локального времени для chat_id={chat_id}: {e}", exc_info=True)
        return False
    
    # Сохраняем время
    user_state.time = parsed  # для обратной совместимости
    user_state.day_end_time = parsed  # время окончания дня (локальное время пользователя)
    user_state.timezone_offset_minutes = utc_offset_minutes
    
    # Фиксируем дату на основе локального времени пользователя
    local_date = local_dt.date().isoformat()
    user_state.date = local_date
    
    # Если last_closed_date не установлено, выставляем текущую локальную дату
    if user_state.last_closed_date is None:
        user_state.last_closed_date = local_date
    
    # Сохраняем состояние
    save_user_state(chat_id, user_state)
    
    # Логируем установку времени
    logger.info(f"SET_TIME chat_id={chat_id} user_time={parsed} utc_offset={utc_offset_minutes} local_date={local_date} utc_now={now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
    
    return True


def delete_user_state(chat_id: int) -> bool:
    """
    Удаляет состояние пользователя из базы данных и из кэша STATE.
    Возвращает True, если запись была удалена, False если не найдена.
    """
    # Удаляем из кэша
    if chat_id in STATE:
        del STATE[chat_id]
    
    # Удаляем из БД
    from db import delete_user_state as db_delete_user_state
    return db_delete_user_state(chat_id)
