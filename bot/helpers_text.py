"""
Модуль для работы с текстом: парсинг времени, нормализация тегов, извлечение текста задач.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from state import UserState

logger = logging.getLogger(__name__)

# Константы
MAX_TAG_LENGTH = 250  # Максимальная длина тега для защиты от очень длинных строк


def parse_time_string(text: str) -> Optional[str]:
    """Парсит строку вида HH:MM и возвращает нормализованное время или None"""
    text = text.strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if not m:
        return None
    h = int(m.group(1))
    mnt = int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mnt <= 59):
        return None
    return f"{h:02d}:{mnt:02d}"


def normalize_tag(raw: str) -> Optional[str]:
    """
    Нормализует тег в формат: начинается с #, пробелы заменяются на _, все буквы в нижнем регистре.
    Примеры:
    - "Рабочие вопросы" → "#рабочие_вопросы"
    - "рабочие вопросы" → "#рабочие_вопросы"
    - "#Рабочие вопросы" → "#рабочие_вопросы"
    - ограничение по длине до MAX_TAG_LENGTH (250) для защиты от очень длинных строк
    Если после обработки тег пустой — вернуть None.
    """
    if not raw:
        return None
    
    # Убираем пробелы в начале и конце
    s = raw.strip()
    
    if not s:
        return None
    
    # Убираем # в начале, если есть (чтобы не дублировать)
    if s.startswith('#'):
        s = s[1:].strip()
    
    if not s:
        return None
    
    # Заменяем пробелы на нижнее подчеркивание
    s = s.replace(' ', '_')
    
    # Приводим к нижнему регистру
    s = s.lower()
    
    # Защита от очень длинных строк (обрезаем до MAX_TAG_LENGTH, учитывая # в начале)
    max_length = MAX_TAG_LENGTH - 1  # -1 для символа #
    if len(s) > max_length:
        s = s[:max_length].rstrip('_')  # Убираем _ в конце, если обрезали
    
    # Если после обрезки строка пустая, возвращаем None
    if not s:
        return None
    
    # Добавляем # в начало
    return f"#{s}"


def extract_task_text_from_business_message(bmsg, max_task_length: int = 95) -> Optional[str]:
    """
    Возвращает текст задачи или None.
    - если нет текста/подписи — вернёт None (такие сообщения не пойдут в задачи)
    - если есть текст/подпись — вернёт обрезанную строку (до max_task_length)
    - если сообщение пересланное — добавляет отправителя в скобках: (Имя), (@username), (Скрытый отправитель)
    """
    # Проверяем наличие текста или подписи
    raw_text = bmsg.text or bmsg.caption
    
    # Проверяем медиа без текста/caption - такие сообщения удаляем
    has_media = any([
        getattr(bmsg, "photo", None),
        getattr(bmsg, "video", None),
        getattr(bmsg, "video_note", None),
        getattr(bmsg, "audio", None),
        getattr(bmsg, "voice", None),
        getattr(bmsg, "document", None),
    ])
    
    # Если есть медиа, но нет текста/caption - удаляем
    if has_media and not raw_text:
        return None
    
    # Если вообще нет текста (не медиа) - удаляем
    if not raw_text:
        return None

    text = raw_text.strip()

    # Обрезаем слишком длинные тексты
    if len(text) > max_task_length:
        text = text[:max_task_length].rstrip() + "…"

    sender = None

    # ===== СТАРЫЕ ПОЛЯ forward_* =====
    if getattr(bmsg, "forward_from", None):
        u = bmsg.forward_from
        if getattr(u, "username", None):
            sender = f"@{u.username}"
        elif getattr(u, "first_name", None):
            name = u.first_name
            if getattr(u, "last_name", None):
                name += f" {u.last_name}"
            sender = name
        else:
            sender = "Пользователь"

    elif getattr(bmsg, "forward_from_chat", None):
        c = bmsg.forward_from_chat
        if getattr(c, "title", None):
            sender = c.title
        elif getattr(c, "username", None):
            sender = f"@{c.username}"
        else:
            sender = "Чат"

    elif getattr(bmsg, "forward_sender_name", None):
        sender = bmsg.forward_sender_name

    elif getattr(bmsg, "forward_from_message_id", None):
        sender = "Скрытый отправитель"

    # ===== НОВЫЕ ПОЛЯ origin / forward_origin (если они есть) =====
    if sender is None:
        origin = getattr(bmsg, "forward_origin", None) or getattr(bmsg, "origin", None)
        if origin is not None:
            # type: "user" | "hidden_user" | "chat" | "channel"
            otype = getattr(origin, "type", None)

            if otype == "user" and getattr(origin, "sender_user", None):
                u = origin.sender_user
                if getattr(u, "username", None):
                    sender = f"@{u.username}"
                else:
                    name = getattr(u, "first_name", "") or ""
                    last = getattr(u, "last_name", "") or ""
                    sender = (name + " " + last).strip() or "Пользователь"

            elif otype == "hidden_user":
                # origin.sender_user_name
                sender = getattr(origin, "sender_user_name", None) or "Скрытый отправитель"

            elif otype == "chat":
                chat = getattr(origin, "sender_chat", None)
                if chat:
                    if getattr(chat, "title", None):
                        sender = chat.title
                    elif getattr(chat, "username", None):
                        sender = f"@{chat.username}"
                    else:
                        sender = "Чат"

            elif otype == "channel":
                chat = getattr(origin, "chat", None)
                if chat:
                    if getattr(chat, "title", None):
                        sender = chat.title
                    elif getattr(chat, "username", None):
                        sender = f"@{chat.username}"
                    else:
                        sender = "Канал"

    # Добавляем отправителя в скобках, если нашли
    if sender:
        full = f"{text} ({sender})"
    else:
        full = text
    
    # Финальная обрезка до max_task_length после всех добавлений
    if len(full) > max_task_length:
        full = full[:max_task_length].rstrip() + "…"
    
    return full.strip()


def get_user_local_date(user_state: UserState, now: Optional[datetime] = None) -> str:
    """
    Возвращает "дату пользователя" в формате YYYY-MM-DD,
    используя timezone_offset_minutes для вычисления локального времени пользователя.
    
    Логика:
    - Применяем timezone_offset_minutes к UTC времени сервера
    - День пользователя = (datetime.utcnow() + offset).date()
    - Смена дня происходит когда user_now.date() меняется (переход через 00:00)
    """
    if now is None:
        now = datetime.utcnow()
    
    # Применяем смещение часового пояса
    offset_minutes = getattr(user_state, "timezone_offset_minutes", 0) or 0
    user_now = now + timedelta(minutes=offset_minutes)
    
    # День пользователя = дата его локального времени
    user_date = user_now.date()
    
    return user_date.strftime("%Y-%m-%d")

