"""
Модуль для управления состоянием пользователей бота.

Содержит:
- UserState: dataclass с полями состояния пользователя
- STATE: глобальное хранилище состояний (in-memory кэш)
- load_user_state/save_user_state: функции для работы со состоянием (SQLite + кэш)
"""

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Optional, Dict, List
from db import get_connection


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
    time: Optional[str] = None     # строка "HH:MM"
    timezone_offset_minutes: int = 0  # смещение часового пояса в минутах относительно UTC (пока не используется, по умолчанию 0)
    
    # Поля для чеклиста:
    checklist_message_id: Optional[int] = None   # message_id созданного чеклиста
    date: Optional[str] = None                   # дата чеклиста, можно хранить "YYYY-MM-DD"
    tasks: List[TaskItem] = field(default_factory=list)  # список задач
    last_closed_date: Optional[str] = None       # дата последнего закрытия дня (защита от двойного закрытия)
    last_opened_date: Optional[str] = None       # дата последнего открытия дня (защита от двойного открытия)
    
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
                checklist_message_id, date, tasks, service_message_ids,
                pending_task_text, pending_task_message_id, pending_service_message_ids,
                awaiting_tag, tags_history, tags_page_index, pending_confirm_job_id,
                tag_checklists, last_closed_date, last_opened_date, next_rollover_job_name
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
    
    # Парсим данные из БД
    business_connection_id = row[0]
    asked_for_time = bool(row[1]) if row[1] is not None else False
    waiting_for_time = bool(row[2]) if row[2] is not None else False
    time = row[3]
    checklist_message_id = row[4]
    date = row[5]
    # Десериализуем tasks из JSON в список TaskItem
    tasks_data = json.loads(row[6]) if row[6] else []
    tasks = []
    for item in tasks_data:
        if isinstance(item, dict):
            # Новый формат: TaskItem
            tasks.append(TaskItem(**item))
        elif isinstance(item, str):
            # Старый формат: просто строка (для миграции)
            # Присваиваем item_id на основе индекса + 1
            tasks.append(TaskItem(item_id=len(tasks) + 1, text=item, done=False))
        else:
            # На всякий случай
            tasks.append(TaskItem(item_id=len(tasks) + 1, text=str(item), done=False))
    service_message_ids = json.loads(row[7]) if row[7] else []
    pending_task_text = row[8]
    pending_task_message_id = row[9]
    pending_service_message_ids = json.loads(row[10]) if row[10] else []
    awaiting_tag = bool(row[11]) if row[11] is not None else False
    tags_history = json.loads(row[12]) if row[12] else []
    tags_page_index = row[13] if row[13] is not None else 0
    pending_confirm_job_id = row[14]
    # Новые поля (могут отсутствовать в старых БД)
    if has_new_fields and len(row) > 16:
        last_closed_date = row[16] if len(row) > 16 else None
        last_opened_date = row[17] if len(row) > 17 else None
        timezone_offset_minutes = row[18] if len(row) > 18 else 0
        next_rollover_job_name = row[19] if len(row) > 19 else None
    else:
        last_closed_date = None
        last_opened_date = None
        timezone_offset_minutes = 0
        next_rollover_job_name = None
    
    # Десериализуем tag_checklists из JSON
    tag_checklists = {}
    if row[15]:  # tag_checklists
        tag_checklists_json = json.loads(row[15])
        for tag, tag_data in tag_checklists_json.items():
            # Десериализуем tasks для каждого тега
            tag_tasks_data = tag_data.get("tasks", [])
            tag_tasks = []
            for item in tag_tasks_data:
                if isinstance(item, dict):
                    # Новый формат: TaskItem
                    tag_tasks.append(TaskItem(**item))
                elif isinstance(item, str):
                    # Старый формат: просто строка (для миграции)
                    tag_tasks.append(TaskItem(item_id=len(tag_tasks) + 1, text=item, done=False))
                else:
                    tag_tasks.append(TaskItem(item_id=len(tag_tasks) + 1, text=str(item), done=False))
            tag_checklists[tag] = TagChecklistState(
                title=tag_data["title"],
                checklist_message_id=tag_data["checklist_message_id"],
                tasks=tag_tasks
            )
    
    # Создаем объект UserState
    user_state = UserState(
        business_connection_id=business_connection_id,
        asked_for_time=asked_for_time,
        waiting_for_time=waiting_for_time,
        time=time,
        timezone_offset_minutes=timezone_offset_minutes,
        checklist_message_id=checklist_message_id,
        date=date,
        tasks=tasks,
        service_message_ids=service_message_ids,
        pending_task_text=pending_task_text,
        pending_task_message_id=pending_task_message_id,
        pending_service_message_ids=pending_service_message_ids,
        awaiting_tag=awaiting_tag,
        tags_history=tags_history,
        tags_page_index=tags_page_index,
        pending_confirm_job_id=pending_confirm_job_id,
        tag_checklists=tag_checklists,
        last_closed_date=last_closed_date,
        last_opened_date=last_opened_date,
        next_rollover_job_name=next_rollover_job_name,
    )
    
    # Сохраняем в кэш
    STATE[chat_id] = user_state
    
    return user_state


def save_user_state(chat_id: int, user_state: UserState) -> None:
    """
    Сохраняет состояние пользователя в SQLite и обновляет кэш.
    """
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
                tag_checklists, last_closed_date, last_opened_date, next_rollover_job_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
