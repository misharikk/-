import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List
from dotenv import load_dotenv
from telegram import Update, InputChecklist, InputChecklistTask, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

# Загружаем переменные окружения
load_dotenv()

# ===== ВЕРСИЯ БОТА =====
PROJECT_ROOT = Path(__file__).parent
VERSION_FILE = PROJECT_ROOT / "VERSION"

try:
    BOT_VERSION = VERSION_FILE.read_text(encoding="utf-8").strip()
except Exception:
    BOT_VERSION = "0.0.0-unknown"

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ===== КОНСТАНТЫ =====
MAX_TASK_LENGTH = 95  # Максимальная длина текста задачи (Telegram API для чеклистов - максимум 100, с учетом нумерации "99. " и скобок с именем)
MAX_TAG_LENGTH = 20  # Максимальная длина тега (включая "#")
TAGS_PER_PAGE = 3  # Количество тегов на странице
AUTO_SKIP_TIMEOUT = 300  # Таймаут авто-пропуска в секундах (5 минут)

# ===== СОСТОЯНИЕ ПОЛЬЗОВАТЕЛЕЙ =====
@dataclass
class UserState:
    business_connection_id: str
    asked_for_time: bool = False   # показывали интро и просили время?
    waiting_for_time: bool = False # ждём ввод времени HH:MM
    time: Optional[str] = None     # строка "HH:MM"
    
    # Поля для чеклиста:
    checklist_message_id: Optional[int] = None   # message_id созданного чеклиста
    date: Optional[str] = None                   # дата чеклиста, можно хранить "YYYY-MM-DD"
    tasks: List[str] = field(default_factory=list)  # список текстов задач
    
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


STATE: Dict[int, UserState] = {}  # ключ = chat_id бизнес-чата


# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====
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


def get_or_create_user_state(update: Update) -> Optional[UserState]:
    """Получает или создаёт UserState для пользователя"""
    # Диагностическое логирование
    logger.info(f"DEBUG: get_or_create_user_state вызван")
    logger.info(f"DEBUG: update.business_message={bool(update.business_message)}")
    logger.info(f"DEBUG: update.message={bool(update.message)}")
    logger.info(f"DEBUG: update.callback_query={bool(update.callback_query)}")
    
    bmsg = update.business_message
    if not bmsg:
        logger.warning(f"DEBUG: business_message отсутствует в update")
        return None

    chat_id = bmsg.chat.id
    bconn = bmsg.business_connection_id
    
    logger.info(f"DEBUG: chat_id={chat_id}, business_connection_id={bconn}")

    if not bconn:
        print("NO BUSINESS CONNECTION ID — MESSAGE IGNORED")
        logger.error(f"DEBUG: business_connection_id отсутствует для chat_id={chat_id}")
        return None

    if chat_id not in STATE:
        STATE[chat_id] = UserState(
            business_connection_id=bconn,
            asked_for_time=False,
            waiting_for_time=False,
            time=None,
            checklist_message_id=None,
            date=None,
            tasks=[],
            service_message_ids=[],
            pending_task_text=None,
            pending_task_message_id=None,
            pending_service_message_ids=[],
            awaiting_tag=False,
            tags_history=[],
            tags_page_index=0,
            pending_confirm_job_id=None,
        )
        logger.info(f"🆕 Новый пользователь business_chat_id={chat_id}")

    return STATE[chat_id]


# ===== ФУНКЦИИ ДЛЯ РАБОТЫ С ЧЕКЛИСТОМ =====
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


def is_system_or_service_business_message(bmsg) -> bool:
    """
    Определяет, является ли business_message системным / сервисным событием,
    которое не нужно превращать в задачу:
    - служебные нотификации чеклиста (выполнено/отменено)
    - системные события без полезного контента
    - сообщения от бота и автопересылки
    """
    # Сообщения от самого бота — никогда не считаем задачами
    if getattr(bmsg, "from_user", None) and getattr(bmsg.from_user, "is_bot", False):
        logger.info(f"🔍 Фильтр: сообщение от бота")
        return True

    # Автоматические пересылки / сервисные автосообщения
    if getattr(bmsg, "is_automatic_forward", False):
        logger.info(f"🔍 Фильтр: автоматическая пересылка")
        return True

    # Сервисные сообщения (если библиотека помечает их как service)
    if getattr(bmsg, "service", False):
        logger.info(f"🔍 Фильтр: сервисное сообщение")
        return True

    # Попробуем отфильтровать специфичные события чеклиста:
    # если библиотека выставляет отдельные поля — можно учитывать и их
    checklist_attrs = [
        "new_checklist_item",
        "new_checklist_item_state",
        "new_checklist",
        "checklist_item_state",
        "checklist",
        "update_id",
    ]
    for attr in checklist_attrs:
        if hasattr(bmsg, attr) and getattr(bmsg, attr) is not None:
            logger.info(f"🔍 Фильтр: найдено checklist-поле {attr}={getattr(bmsg, attr)}")
            return True
    
    # Дополнительно проверяем любые атрибуты, которые могут указывать на системное событие
    # Если есть атрибут, который выглядит как системный (например, содержит "checklist" или "state")
    # НО игнорируем методы (callable объекты)
    for attr_name in dir(bmsg):
        if not attr_name.startswith("_"):  # Игнорируем приватные атрибуты
            if "checklist" in attr_name.lower() or "state" in attr_name.lower():
                try:
                    attr_value = getattr(bmsg, attr_name, None)
                    # Игнорируем методы (callable объекты) - это не системные поля
                    if callable(attr_value):
                        continue
                    if attr_value is not None and attr_name not in checklist_attrs:
                        # Это может быть новое системное поле
                        logger.info(f"🔍 Фильтр: найдено системное поле {attr_name}={attr_value}")
                        return True
                except Exception:
                    pass

    # Если нет текста/подписи и нет медиа — скорее всего это служебное событие
    has_text_or_caption = bool(getattr(bmsg, "text", None) or getattr(bmsg, "caption", None))
    has_media = any([
        getattr(bmsg, "photo", None),
        getattr(bmsg, "voice", None),
        getattr(bmsg, "video", None),
        getattr(bmsg, "document", None),
        getattr(bmsg, "audio", None),
        getattr(bmsg, "sticker", None),
    ])

    if not has_text_or_caption and not has_media:
        logger.info(f"🔍 Фильтр: нет текста и нет медиа")
        return True

    # Если дошли сюда - сообщение не системное
    return False


def extract_task_text_from_business_message(bmsg) -> Optional[str]:
    """
    Возвращает текст задачи или None.
    - если нет текста/подписи — вернёт None (такие сообщения не пойдут в задачи)
    - если есть текст/подпись — вернёт обрезанную строку (до MAX_TASK_LENGTH)
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
    if len(text) > MAX_TASK_LENGTH:
        text = text[:MAX_TASK_LENGTH].rstrip() + "…"

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

    # Логирование отключено для уменьшения объема логов

    # Добавляем отправителя в скобках, если нашли
    if sender:
        full = f"{text} ({sender})"
    else:
        full = text
    
    # Финальная обрезка до MAX_TASK_LENGTH (95) после всех добавлений
    if len(full) > MAX_TASK_LENGTH:
        full = full[:MAX_TASK_LENGTH].rstrip() + "…"
    
    return full.strip()


# ===== ФУНКЦИИ ДЛЯ РАБОТЫ С ТЕГАМИ =====
def normalize_tag(raw: str) -> Optional[str]:
    """
    Превращает произвольный ввод в тег формата '#дом_семья'.
    - всё в нижнем регистре
    - пробелы -> '_'
    - начинается с '#'
    - длина <= MAX_TAG_LENGTH
    Если после обработки тег пустой — вернуть None.
    """
    if not raw:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    s = re.sub(r"\s+", "_", s)
    if not s.startswith("#"):
        s = "#" + s
    if len(s) > MAX_TAG_LENGTH:
        s = s[:MAX_TAG_LENGTH]
    if s == "#":
        return None
    return s


def build_tags_keyboard(user_state: UserState) -> InlineKeyboardMarkup:
    """
    Создаёт клавиатуру с тегами для текущей страницы.
    До 3 тегов на странице, с кнопками навигации.
    Если история тегов пустая - возвращает пустую клавиатуру.
    """
    tags = user_state.tags_history
    
    # Если история тегов пустая - возвращаем пустую клавиатуру
    if not tags:
        return InlineKeyboardMarkup([])
    
    page = user_state.tags_page_index
    total_pages = (len(tags) + TAGS_PER_PAGE - 1) // TAGS_PER_PAGE if tags else 0
    
    buttons = []
    
    # Кнопки с тегами для текущей страницы
    start_idx = page * TAGS_PER_PAGE
    end_idx = min(start_idx + TAGS_PER_PAGE, len(tags))
    
    for tag in tags[start_idx:end_idx]:
        buttons.append([InlineKeyboardButton(tag, callback_data=f"TAG_SELECT:{tag}")])
    
    # Кнопки навигации
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data="TAGS_PAGE_PREV"))
    if end_idx < len(tags):
        nav_row.append(InlineKeyboardButton("➡️", callback_data="TAGS_PAGE_NEXT"))
    
    if nav_row:
        buttons.append(nav_row)
    
    return InlineKeyboardMarkup(buttons)


async def cancel_pending_confirm_job(job_queue, user_state: UserState) -> None:
    """Отменяет job авто-таймаута, если он существует"""
    if user_state.pending_confirm_job_id:
        try:
            jobs = job_queue.get_jobs_by_name(user_state.pending_confirm_job_id)
            for j in jobs:
                j.schedule_removal()
        except Exception as e:
            logger.warning(f"⚠️ Не удалось отменить job {user_state.pending_confirm_job_id}: {e}")
        user_state.pending_confirm_job_id = None


async def auto_skip_pending_task(context: CallbackContext) -> None:
    """Автоматически пропускает задачу через 5 минут, если пользователь ничего не выбрал"""
    if not context.job or not context.job.chat_id:
        logger.warning(f"⚠️ auto_skip_pending_task: job или chat_id отсутствует")
        return
    chat_id = context.job.chat_id
    user_state = STATE.get(chat_id)
    if not user_state:
        logger.warning(f"⚠️ auto_skip_pending_task: user_state не найден для chat_id={chat_id}")
        return
    
    if not user_state.pending_task_text:
        logger.info(f"ℹ️ auto_skip_pending_task: pending_task_text отсутствует для chat_id={chat_id}, ничего не делаем")
        return
    
    # Если пользователь в режиме выбора тега - не делаем авто-скип
    if user_state.awaiting_tag:
        logger.info(f"ℹ️ auto_skip_pending_task: awaiting_tag=True для chat_id={chat_id}, пропускаем авто-скип")
        return
    
    logger.info(f"⏰ Авто-пропуск задачи для chat_id={chat_id} после таймаута 5 минут")
    await finalize_task_without_tag(context.bot, chat_id, user_state)


async def finalize_task_without_tag(bot, chat_id: int, user_state: UserState) -> None:
    """
    Завершает добавление задачи без тега:
    - добавляет pending_task_text в user_state.tasks
    - обновляет чеклист
    - удаляет все связанные сообщения
    - очищает pending поля
    """
    if not user_state.pending_task_text:
        logger.warning(f"⚠️ finalize_task_without_tag вызвана без pending_task_text для chat_id={chat_id}")
        return
    
    try:
        # Добавляем задачу без тега
        user_state.tasks.append(user_state.pending_task_text)
        STATE[chat_id] = user_state
        logger.info(f"📋 Задач в списке: {len(user_state.tasks)}")
        
        # Обновляем чеклист
        await update_checklist_for_user(bot, chat_id, user_state)
        STATE[chat_id] = user_state
        
        logger.info(f"✅ Задача добавлена в чеклист без тега для chat_id={chat_id}: {user_state.pending_task_text!r}")
    except Exception as e:
        logger.error(f"❌ Ошибка при добавлении задачи без тега для chat_id={chat_id}: {e}", exc_info=True)
    
    # Удаляем все связанные сообщения
    messages_to_delete = []
    if user_state.pending_task_message_id:
        messages_to_delete.append(user_state.pending_task_message_id)
    messages_to_delete.extend(user_state.pending_service_message_ids)
    
    for msg_id in messages_to_delete:
        await safe_delete(bot, user_state.business_connection_id, chat_id, msg_id)
    
    # Очищаем pending поля
    user_state.pending_task_text = None
    user_state.pending_task_message_id = None
    user_state.pending_service_message_ids.clear()
    user_state.awaiting_tag = False
    user_state.pending_confirm_job_id = None
    STATE[chat_id] = user_state


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
        human_date = get_today_human_date()
        user_state.date = datetime.now().strftime("%Y-%m-%d")
        user_state.tasks = ["улыбнуться себе в зеркало"]

        tasks = []
        for idx, text in enumerate(user_state.tasks, start=1):
            # Формируем текст с номером
            numbered_text = f"{idx}. {text}"
            # Обрезаем до 100 символов (лимит Telegram API для чеклистов)
            if len(numbered_text) > 100:
                numbered_text = numbered_text[:97].rstrip() + "…"
            tasks.append(InputChecklistTask(
                id=idx,
                text=numbered_text,
            ))

        checklist = InputChecklist(
            title=human_date,
            tasks=tasks,
            others_can_add_tasks=False,
            others_can_mark_tasks_as_done=True,
        )

        logger.info(f"📤 Отправляю чеклист для chat_id={chat_id}, title='{human_date}'")
        msg = await bot.send_checklist(
            business_connection_id=user_state.business_connection_id,
            chat_id=chat_id,
            checklist=checklist,
        )
        user_state.checklist_message_id = msg.message_id
        # Явно обновляем состояние в словаре STATE
        STATE[chat_id] = user_state
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
        for idx, text in enumerate(user_state.tasks, start=1):
            # Формируем текст с номером
            numbered_text = f"{idx}. {text}"
            # Обрезаем до 100 символов (лимит Telegram API для чеклистов)
            if len(numbered_text) > 100:
                numbered_text = numbered_text[:97].rstrip() + "…"
            tasks.append(InputChecklistTask(
                id=idx,
                text=numbered_text,
            ))

        checklist = InputChecklist(
            title=get_today_human_date(),
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


# ===== БЕЗОПАСНОЕ УДАЛЕНИЕ СООБЩЕНИЙ =====
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


# ===== ОБРАБОТЧИКИ БИЗНЕС-СООБЩЕНИЙ =====
async def handle_first_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState) -> None:
    """Обработка первого сообщения: отправка интро и запрос времени"""
    business_msg = update.business_message
    if not business_msg:
        return
    chat_id = business_msg.chat.id
    
    # отправляем первое приветственное сообщение
    welcome_1_text = (
        "👋 Привет!\n\n"
        "Я — твой чат для ежедневных чек-листов.\n\n"
        "Пиши или пересылай мне любое сообщение — я превращу его в задачу дня.\n\n"
        "Чтобы использовать все функции чек-листов, оформите Premium в @PremiumBot."
    )
    try:
        welcome_1 = await context.bot.send_message(
            business_connection_id=user_state.business_connection_id,
            chat_id=chat_id,
            text=welcome_1_text,
        )
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке первого сообщения: {e}", exc_info=True)
        return
    
    # отправляем второе сообщение с запросом времени
    welcome_2_text = (
        "Укажи текущее время в формате HH:MM ⏰"
    )
    try:
        welcome_2 = await context.bot.send_message(
            business_connection_id=user_state.business_connection_id,
            chat_id=chat_id,
            text=welcome_2_text,
        )
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке второго сообщения: {e}", exc_info=True)
        return
    
    # Сохраняем ID служебных сообщений
    user_state.service_message_ids.append(welcome_1.message_id)
    user_state.service_message_ids.append(welcome_2.message_id)
    
    # удаляем первое сообщение пользователя
    await safe_delete(
        context.bot,
        user_state.business_connection_id,
        chat_id,
        business_msg.message_id,
    )
    
    user_state.asked_for_time = True
    user_state.waiting_for_time = True
    # Явно обновляем состояние в словаре STATE
    STATE[chat_id] = user_state


async def handle_time_input(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState) -> None:
    """Обработка ввода времени в формате HH:MM"""
    business_msg = update.business_message
    if not business_msg:
        return
    chat_id = business_msg.chat.id
    text = business_msg.text or ""
    
    if not text:
        # Сообщаем, что нужен текст, остаемся в режиме ожидания времени
        await context.bot.send_message(
            business_connection_id=user_state.business_connection_id,
            chat_id=chat_id,
            text="Пожалуйста, отправь время в формате HH:MM, например 09:30.",
        )
        # Убеждаемся, что остаемся в режиме ожидания времени
        user_state.waiting_for_time = True
        return
    
    parsed = parse_time_string(text)
    if not parsed:
        # Сообщаем об ошибке, но НЕ меняем состояние - остаемся в ожидании времени
        await context.bot.send_message(
            business_connection_id=user_state.business_connection_id,
            chat_id=chat_id,
            text="❌ Неверный формат времени. Введи, пожалуйста, в формате HH:MM, например 09:30.",
        )
        # Убеждаемся, что остаемся в режиме ожидания времени
        user_state.waiting_for_time = True
        return
    
    user_state.time = parsed
    user_state.waiting_for_time = False
    
    # Добавляем сообщение с временем в список служебных
    user_state.service_message_ids.append(business_msg.message_id)
    
    # Отправляем подтверждение и сохраняем его ID
    confirm_msg = await context.bot.send_message(
        business_connection_id=user_state.business_connection_id,
        chat_id=chat_id,
        text=f"✅ Время установлено: {parsed}",
    )
    user_state.service_message_ids.append(confirm_msg.message_id)
    
    # Удаляем все служебные сообщения
    for mid in user_state.service_message_ids:
        await safe_delete(
            context.bot,
            user_state.business_connection_id,
            chat_id,
            mid,
        )
    user_state.service_message_ids.clear()
    
    # Сразу создаем чеклист
    await create_checklist_for_user(context.bot, chat_id, user_state)
    
    # Обновляем состояние в словаре STATE
    STATE[chat_id] = user_state


async def handle_tag_input(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState) -> None:
    """Обработка ввода тега текстом"""
    try:
        business_msg = update.business_message
        if not business_msg or not business_msg.text:
            return
        
        chat_id = business_msg.chat.id
        raw_tag = business_msg.text.strip()
        
        # Нормализуем тег
        tag = normalize_tag(raw_tag)
        if not tag:
            # Некорректный тег — просим ещё раз
            error_msg = await context.bot.send_message(
                business_connection_id=user_state.business_connection_id,
                chat_id=chat_id,
                text="Некорректный тег, попробуйте снова",
            )
            user_state.pending_service_message_ids.append(error_msg.message_id)
            user_state.pending_service_message_ids.append(business_msg.message_id)
            STATE[chat_id] = user_state
            return
        
        # Добавляем/поднимаем тег в истории
        if tag in user_state.tags_history:
            user_state.tags_history.remove(tag)
        user_state.tags_history.insert(0, tag)
        # Оставляем только последние 30 тегов
        if len(user_state.tags_history) > 30:
            user_state.tags_history = user_state.tags_history[:30]
        
        # Формируем задачу с тегом
        final_task = f"{tag} {user_state.pending_task_text}"
        # Обрезаем до MAX_TASK_LENGTH (95) после добавления тега
        if len(final_task) > MAX_TASK_LENGTH:
            final_task = final_task[:MAX_TASK_LENGTH].rstrip() + "…"
        
        # Добавляем задачу
        try:
            user_state.tasks.append(final_task)
            STATE[chat_id] = user_state
            logger.info(f"📋 Задач в списке: {len(user_state.tasks)}")
            
            # Обновляем чеклист
            await update_checklist_for_user(context.bot, chat_id, user_state)
            STATE[chat_id] = user_state
            
            logger.info(f"✅ Задача добавлена в чеклист с тегом для chat_id={chat_id}: {final_task!r}")
        except Exception as e:
            logger.error(f"❌ Ошибка при добавлении задачи с тегом для chat_id={chat_id}: {e}", exc_info=True)
        
        # Удаляем все связанные сообщения
        messages_to_delete = []
        if user_state.pending_task_message_id:
            messages_to_delete.append(user_state.pending_task_message_id)
        messages_to_delete.extend(user_state.pending_service_message_ids)
        messages_to_delete.append(business_msg.message_id)  # сообщение с тегом
        
        for msg_id in messages_to_delete:
            await safe_delete(context.bot, user_state.business_connection_id, chat_id, msg_id)
        
        # Очищаем pending поля
        user_state.pending_task_text = None
        user_state.pending_task_message_id = None
        user_state.pending_service_message_ids.clear()
        user_state.awaiting_tag = False
        user_state.pending_confirm_job_id = None
        STATE[chat_id] = user_state
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_tag_input: {e}", exc_info=True)


async def handle_task_addition(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState) -> None:
    """Обработка сообщения как задачи для чеклиста"""
    try:
        business_msg = update.business_message
        if not business_msg:
            return
        chat_id = business_msg.chat.id
        
        # Проверяем, не ждём ли мы тег
        if user_state.awaiting_tag:
            # Обрабатываем как ввод тега (будет обработано в handle_all_updates)
            return
        
        # СНАЧАЛА проверяем медиа без текста и удаляем сразу
        raw_text = business_msg.text or business_msg.caption
        has_photo = bool(getattr(business_msg, "photo", None))
        has_video = bool(getattr(business_msg, "video", None))
        has_audio = bool(getattr(business_msg, "audio", None))
        has_voice = bool(getattr(business_msg, "voice", None))
        has_document = bool(getattr(business_msg, "document", None))
        has_video_note = bool(getattr(business_msg, "video_note", None))
        
        has_media = has_photo or has_video or has_video_note or has_audio or has_voice or has_document
        
        # Логирование для отладки медиа без текста
        if has_media:
            logger.info(f"🔍 Медиа-сообщение: audio={has_audio}, voice={has_voice}, photo={has_photo}, video={has_video}, document={has_document}, text={bool(raw_text)}, caption={bool(business_msg.caption)}")
        
        # Если есть медиа, но нет текста/caption - удаляем сразу
        if has_media and not raw_text:
            logger.info(f"🗑️ Удаляю медиа-сообщение без текста: message_id={business_msg.message_id}")
            await safe_delete(
                context.bot,
                user_state.business_connection_id,
                chat_id,
                business_msg.message_id,
            )
            return
        
        # Отменяем предыдущий pending, если есть
        if user_state.pending_task_text:
            await cancel_pending_confirm_job(context.job_queue, user_state)
            # Удаляем старые pending сообщения
            for msg_id in user_state.pending_service_message_ids:
                await safe_delete(context.bot, user_state.business_connection_id, chat_id, msg_id)
            if user_state.pending_task_message_id:
                await safe_delete(context.bot, user_state.business_connection_id, chat_id, user_state.pending_task_message_id)
            user_state.pending_service_message_ids.clear()
        
        # 1. Убедиться, что чеклист создан
        await create_checklist_for_user(context.bot, chat_id, user_state)
        
        # 2. Получить текст задачи
        task_text = extract_task_text_from_business_message(business_msg)
        
        # Если текст задачи не получен (сообщение без текста) — просто удаляем сообщение и выходим
        if task_text is None:
            await safe_delete(
                context.bot,
                user_state.business_connection_id,
                chat_id,
                business_msg.message_id,
            )
            return
        
        # 3. Сохраняем задачу как pending
        user_state.pending_task_text = task_text
        user_state.pending_task_message_id = business_msg.message_id
        user_state.awaiting_tag = False
        user_state.tags_page_index = 0
        user_state.pending_service_message_ids.clear()
        
        # 4. Отправляем сообщение "Добавить" с кнопками
        keyboard = [
            [
                InlineKeyboardButton("⏭️ Пропустить", callback_data="TASK_SKIP"),
                InlineKeyboardButton("🏷 Тэг", callback_data="TASK_TAG"),
            ]
        ]
        markup = InlineKeyboardMarkup(keyboard)
        
        try:
            logger.info(f"📤 Отправляю сообщение 'Добавить' с кнопками для chat_id={chat_id}")
            confirm_msg = await context.bot.send_message(
                business_connection_id=user_state.business_connection_id,
                chat_id=chat_id,
                text="Добавить",
                reply_markup=markup,
            )
            user_state.pending_service_message_ids.append(confirm_msg.message_id)
            logger.info(f"✅ Сообщение 'Добавить' отправлено, message_id={confirm_msg.message_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка при отправке сообщения 'Добавить': {e}", exc_info=True)
            # Очищаем pending в случае ошибки
            user_state.pending_task_text = None
            user_state.pending_task_message_id = None
            STATE[chat_id] = user_state
            return
        
        # 5. Создаём job для авто-пропуска через 5 минут
        if not context.job_queue:
            logger.error(f"❌ job_queue отсутствует в context")
            return
        
        job_name = f"auto-skip-{chat_id}"
        job = context.job_queue.run_once(
            auto_skip_pending_task,
            when=timedelta(seconds=AUTO_SKIP_TIMEOUT),
            chat_id=chat_id,
            name=job_name,
        )
        user_state.pending_confirm_job_id = job.name
        
        # Сохраняем состояние
        STATE[chat_id] = user_state
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_task_addition: {e}", exc_info=True)


# ===== ОБРАБОТЧИКИ CALLBACK QUERIES =====
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик всех callback queries"""
    query = update.callback_query
    if not query or not query.data:
        return
    
    await query.answer()
    
    if not query.message:
        logger.warning(f"⚠️ handle_callback_query: query.message отсутствует")
        return
    
    chat_id = query.message.chat.id
    callback_data = query.data
    
    user_state = STATE.get(chat_id)
    if not user_state:
        logger.warning(f"⚠️ handle_callback_query: user_state не найден для chat_id={chat_id}")
        return
    
    if callback_data == "TASK_SKIP":
        await on_task_skip(update, context, user_state, chat_id)
    elif callback_data == "TASK_TAG":
        await on_task_tag(update, context, user_state, chat_id)
    elif callback_data.startswith("TAG_SELECT:"):
        tag = callback_data.replace("TAG_SELECT:", "")
        await on_tag_select(update, context, user_state, chat_id, tag)
    elif callback_data == "TAGS_PAGE_NEXT":
        await on_tags_page_next(update, context, user_state, chat_id)
    elif callback_data == "TAGS_PAGE_PREV":
        await on_tags_page_prev(update, context, user_state, chat_id)


async def on_task_skip(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState, chat_id: int) -> None:
    """Обработка кнопки 'Пропустить'"""
    try:
        if not user_state.pending_task_text:
            return
        
        # Отменяем job, если есть
        await cancel_pending_confirm_job(context.job_queue, user_state)
        
        # Добавляем задачу без тега
        await finalize_task_without_tag(context.bot, chat_id, user_state)
    except Exception as e:
        logger.error(f"❌ Ошибка в on_task_skip для chat_id={chat_id}: {e}", exc_info=True)


async def on_task_tag(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState, chat_id: int) -> None:
    """Обработка кнопки 'Тэг'"""
    try:
        if not user_state.pending_task_text:
            return
        
        # Отменяем job
        await cancel_pending_confirm_job(context.job_queue, user_state)
        
        # Устанавливаем флаг ожидания тега
        user_state.awaiting_tag = True
        user_state.tags_page_index = 0
        
        # Отправляем сообщение с запросом тега и клавиатурой
        tag_msg = await context.bot.send_message(
            business_connection_id=user_state.business_connection_id,
            chat_id=chat_id,
            text="Напишите тэг, не более 20 символов, или выберите один из последних:",
            reply_markup=build_tags_keyboard(user_state),
        )
        user_state.pending_service_message_ids.append(tag_msg.message_id)
        STATE[chat_id] = user_state
    except Exception as e:
        logger.error(f"❌ Ошибка в on_task_tag для chat_id={chat_id}: {e}", exc_info=True)


async def on_tag_select(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState, chat_id: int, tag: str) -> None:
    """Обработка выбора тега из списка"""
    if not user_state.pending_task_text:
        logger.warning(f"⚠️ on_tag_select: pending_task_text отсутствует для chat_id={chat_id}")
        return
    
    # Отменяем job
    await cancel_pending_confirm_job(context.job_queue, user_state)
    
    # Поднимаем тег в истории
    if tag in user_state.tags_history:
        user_state.tags_history.remove(tag)
    user_state.tags_history.insert(0, tag)
    if len(user_state.tags_history) > 30:
        user_state.tags_history = user_state.tags_history[:30]
    
    # Формируем задачу с тегом
    final_task = f"{tag} {user_state.pending_task_text}"
    # Обрезаем до MAX_TASK_LENGTH (95) после добавления тега
    if len(final_task) > MAX_TASK_LENGTH:
        final_task = final_task[:MAX_TASK_LENGTH].rstrip() + "…"
    
    # Добавляем задачу
    try:
        user_state.tasks.append(final_task)
        STATE[chat_id] = user_state
        logger.info(f"📋 Задач в списке: {len(user_state.tasks)}")
        
        # Обновляем чеклист
        await update_checklist_for_user(context.bot, chat_id, user_state)
        STATE[chat_id] = user_state
        
        logger.info(f"✅ Задача добавлена в чеклист с тегом для chat_id={chat_id}: {final_task!r}")
    except Exception as e:
        logger.error(f"❌ Ошибка при добавлении задачи с тегом для chat_id={chat_id}: {e}", exc_info=True)
    
    # Удаляем все связанные сообщения
    messages_to_delete = []
    if user_state.pending_task_message_id:
        messages_to_delete.append(user_state.pending_task_message_id)
    messages_to_delete.extend(user_state.pending_service_message_ids)
    
    for msg_id in messages_to_delete:
        await safe_delete(context.bot, user_state.business_connection_id, chat_id, msg_id)
    
    # Очищаем pending поля
    user_state.pending_task_text = None
    user_state.pending_task_message_id = None
    user_state.pending_service_message_ids.clear()
    user_state.awaiting_tag = False
    user_state.pending_confirm_job_id = None
    STATE[chat_id] = user_state


async def on_tags_page_next(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState, chat_id: int) -> None:
    """Обработка кнопки 'Вперёд' в пагинации тегов"""
    total_pages = (len(user_state.tags_history) + TAGS_PER_PAGE - 1) // TAGS_PER_PAGE if user_state.tags_history else 0
    if user_state.tags_page_index + 1 < total_pages:
        user_state.tags_page_index += 1
        STATE[chat_id] = user_state
        
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
        STATE[chat_id] = user_state
        
        # Обновляем клавиатуру
        if update.callback_query:
            try:
                await update.callback_query.edit_message_reply_markup(
                    reply_markup=build_tags_keyboard(user_state)
                )
            except Exception as e:
                logger.error(f"❌ Ошибка при обновлении клавиатуры тегов: {e}", exc_info=True)


# ===== ОБРАБОТЧИКИ =====
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start для обычных сообщений"""
    if update.message:
        await update.message.reply_text(f"Бот запущен. Версия: {BOT_VERSION}")
        logger.info("Команда /start получена")


async def handle_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик всех обновлений"""
    print("UPDATE RECEIVED:", update.to_dict().keys())
    
    # Диагностическое логирование входящих обновлений (без больших JSON)
    update_type = []
    chat_id_info = "N/A"
    
    if update.business_message:
        update_type.append("business_message")
        chat_id_info = f"business_chat={update.business_message.chat.id}"
    if update.message:
        update_type.append("message")
        chat_id_info = f"chat={update.message.chat.id}"
    if update.callback_query:
        update_type.append("callback_query")
        if update.callback_query.message:
            chat_id_info = f"callback_chat={update.callback_query.message.chat.id}"
    
    logger.info(f"DEBUG: 📥 Входящее обновление: тип={', '.join(update_type) or 'unknown'}, {chat_id_info}, update_id={update.update_id}")
    
    # Логирование всех обновлений для отладки
    logger.info(f"📥 Получено обновление: business_message={bool(update.business_message)}, message={bool(update.message)}, callback_query={bool(update.callback_query)}")
    
    # Если это обычное сообщение (не business), просто логируем
    if update.message and not update.business_message:
        logger.info(f"ℹ️ Получено обычное сообщение (не business_message): chat_id={update.message.chat.id}, text={update.message.text}")
        return
    
    # Обработка business_message
    if update.business_message:
        try:
            business_msg = update.business_message
            chat_id = business_msg.chat.id
            logger.info(f"✅ business_message получено: chat_id={chat_id}, message_id={business_msg.message_id}, text={bool(business_msg.text)}, caption={bool(business_msg.caption)}")
            
            # Отбрасываем системные / служебные бизнес-сообщения (в т.ч. чеклист-нотификации)
            if is_system_or_service_business_message(business_msg):
                logger.info(f"ℹ️ Сообщение отфильтровано как системное: chat_id={chat_id}, message_id={business_msg.message_id}")
                return
            
            # Логирование для отладки
            has_audio = bool(getattr(business_msg, "audio", None))
            has_voice = bool(getattr(business_msg, "voice", None))
            has_text = bool(getattr(business_msg, "text", None))
            has_caption = bool(getattr(business_msg, "caption", None))
            if has_audio or has_voice:
                logger.info(f"🎵 Аудио/голосовое сообщение: audio={has_audio}, voice={has_voice}, text={has_text}, caption={has_caption}")
            
            # Получаем или создаём состояние пользователя
            user_state = get_or_create_user_state(update)
            if not user_state:
                logger.error(f"❌ Не удалось получить user_state для chat_id={chat_id}")
                return
            
            # ЧЁТКИЙ ПОРЯДОК ПРОВЕРОК:
            # 1) Ещё не просили время → интро + запрос
            if not user_state.asked_for_time:
                await handle_first_message(update, context, user_state)
                return
            
            # 2) Уже просили время, но оно ещё НЕ установлено → парсим HH:MM
            if user_state.asked_for_time and user_state.time is None:
                await handle_time_input(update, context, user_state)
                return
            
            # 3) Ждём тег (awaiting_tag) → обрабатываем как ввод тега
            if user_state.awaiting_tag and user_state.pending_task_text:
                await handle_tag_input(update, context, user_state)
                return
            
            # 4) Время установлено (time is not None) → обрабатываем сообщение как задачу
            await handle_task_addition(update, context, user_state)
            return
        except Exception as e:
            logger.error(f"❌ Ошибка в handle_all_updates при обработке business_message: {e}", exc_info=True)
            return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Глобальный обработчик ошибок — логируем, но не даём боту упасть.
    """
    logger.error("❌ Ошибка в обработке апдейта:", exc_info=context.error)

    # Дополнительно можно различать типы ошибок
    err = context.error
    if isinstance(err, TelegramError):
        logger.warning(f"⚠️ Ошибка Telegram API: {err}")
    else:
        logger.warning(f"⚠️ Неожиданная ошибка: {err}")


def main():
    """Запуск бота"""
    print("=" * 60)
    print("DEBUG: Начало запуска бота")
    print("=" * 60)
    
    # Проверка зависимостей
    try:
        import telegram
        print(f"DEBUG: python-telegram-bot версия: {telegram.__version__}")
    except ImportError as e:
        print(f"❌ ОШИБКА: python-telegram-bot не установлен: {e}")
        print("Установите зависимости: pip install -r requirements.txt")
        return
    
    try:
        import dotenv
        print("DEBUG: python-dotenv установлен")
    except ImportError as e:
        print(f"❌ ОШИБКА: python-dotenv не установлен: {e}")
        print("Установите зависимости: pip install -r requirements.txt")
        return
    
    # Загрузка токена из .env
    env_path = Path(__file__).parent / ".env"
    print(f"DEBUG: Проверка .env файла: {env_path}")
    print(f"DEBUG: .env файл существует: {env_path.exists()}")
    
    if not env_path.exists():
        print("=" * 60)
        print("❌ ОШИБКА: Файл .env не найден!")
        print("=" * 60)
        print("Создайте файл .env в корне проекта со следующим содержимым:")
        print("")
        print("BOT_TOKEN=your_bot_token_here")
        print("")
        print("Где your_bot_token_here - токен вашего бота от @BotFather")
        print("=" * 60)
        logger.error("Файл .env не найден в корне проекта")
        return
    
    # Перезагружаем .env для уверенности
    load_dotenv(env_path)
    
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    
    # Отладочный вывод токена (частично скрыт для безопасности)
    if BOT_TOKEN:
        token_preview = BOT_TOKEN[:10] + "..." + BOT_TOKEN[-5:] if len(BOT_TOKEN) > 15 else "***"
        print(f"DEBUG: BOT_TOKEN загружен: {token_preview}")
        logger.info(f"DEBUG: BOT_TOKEN загружен (длина: {len(BOT_TOKEN)} символов)")
    else:
        print("=" * 60)
        print("❌ ОШИБКА: BOT_TOKEN не найден в .env!")
        print("=" * 60)
        print("Убедитесь, что файл .env содержит строку:")
        print("BOT_TOKEN=your_bot_token_here")
        print("")
        print("Где your_bot_token_here - токен вашего бота от @BotFather")
        print("=" * 60)
        logger.error("BOT_TOKEN не найден в .env")
        return
    
    print("DEBUG: BOT_TOKEN=", BOT_TOKEN)
    
    print("DEBUG: Создание приложения...")
    
    try:
        # Создание приложения с job_queue
        app = ApplicationBuilder().token(BOT_TOKEN).job_queue(None).build()
        print("DEBUG: Приложение создано успешно")
    except Exception as e:
        print(f"❌ ОШИБКА при создании приложения: {e}")
        logger.error(f"Ошибка при создании приложения: {e}", exc_info=True)
        return
    
    # Добавление обработчиков
    print("DEBUG: Добавление обработчиков...")
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    # Обработчик всех обновлений (должен быть последним, чтобы не перехватывать команды)
    app.add_handler(TypeHandler(Update, handle_all_updates), group=-1)
    
    # Обработчик ошибок
    app.add_error_handler(error_handler)
    
    print("DEBUG: Обработчики добавлены")
    logger.info(f"🚀 Запуск бота, версия {BOT_VERSION}")
    logger.info("🤖 Бот запускается...")
    logger.info(f"Ожидаю business_message с бизнес-аккаунта...")
    
    print("=" * 60)
    print("DEBUG: Запуск polling...")
    print("=" * 60)
    
    print("DEBUG: BOT STARTED AND WAITING FOR UPDATES")
    
    # Запуск бота с глобальной обработкой ошибок
    try:
        app.run_polling(
        drop_pending_updates=True,
            allowed_updates=["message", "business_message", "edited_business_message", "callback_query"]
        )
    except KeyboardInterrupt:
        print("\nDEBUG: Получен сигнал прерывания (Ctrl+C)")
        logger.info("Получен сигнал прерывания, остановка бота...")
    except Exception as e:
        print("=" * 60)
        print(f"❌ КРИТИЧЕСКАЯ ОШИБКА при запуске polling:")
        print(f"Тип ошибки: {type(e).__name__}")
        print(f"Сообщение: {e}")
        print("=" * 60)
        logger.error(f"Критическая ошибка при запуске polling: {e}", exc_info=True)
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
