"""
Telegram Business бот-чеклист.

Кратко:
- Бот работает только через business_message (бизнес-чат, привязанный к бизнес-аккаунту).
- У каждого пользователя (chat_id бизнес-чата) есть UserState:
  - asked_for_time / waiting_for_time / time — онбординг и время ежедневного чеклиста.
  - checklist_message_id / date / tasks — нативный Telegram-checklist и список задач.
  - pending_task_text / pending_task_message_id / pending_service_message_ids — текущая "висящая" задача, кнопки "Пропустить / Тэг" и служебные сообщения.
  - awaiting_tag / tags_history / tags_page_index — режим выбора тега и история тегов.
  - pending_confirm_job_id — job в job_queue для авто-пропуска через 5 минут.

Общий флоу:
1) Пользователь пишет в бизнес-чат → онбординг → бот просит время (HH:MM).
2) В указанное время создаётся нативный чеклист на день.
3) Любое сообщение с текстом → превращается в "висящую" задачу:
   - бот показывает "Добавить" с кнопками "⏭️ Пропустить" и "🏷 Тэг".
   - если "Пропустить" или таймаут 5 минут → задача добавляется без тега.
   - если "Тэг" → бот просит тег (ввод или выбор из последних тегов).
4) В чеклисте каждая задача нумеруется, может иметь тег "#дом_семья" и имя пересланного автора.
"""

# ===============================
# Импорты и общая конфигурация
# ===============================

import logging
import os
import re
import shutil
from datetime import datetime, time
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

# Импорт состояния из отдельного модуля
from state import UserState, load_user_state, save_user_state, STATE
from db import init_db, DB_PATH

# Импорт хелперов из отдельных модулей
from helpers_text import parse_time_string, normalize_tag
from helpers_checklist import get_today_human_date, create_checklist_for_user, handle_checklist_state_update
from helpers_daily import close_day_for_user, start_new_day_for_user, check_and_handle_new_day
from helpers_text import get_user_local_date
from db import get_all_chat_ids
from helpers_tags import on_tags_page_next, on_tags_page_prev
from helpers_delete import safe_delete
from helpers_pending import (
    handle_task_addition,
    handle_task_skip_callback,
    handle_task_tag_callback,
    handle_tag_input,
    handle_tag_select_callback,
    auto_skip_pending_task,
    cancel_pending_task,
)

# ===============================
# Константы и глобальные настройки
# ===============================

# Определяем корень проекта (на уровень выше bot/)
PROJECT_ROOT = Path(__file__).parent.parent

# Загружаем переменные окружения из корня проекта
load_dotenv(PROJECT_ROOT / ".env")

# Версия бота
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

# Константы
TAGS_PER_PAGE = 3  # Количество тегов на странице

# ===============================
# Хелперы: фильтрация системных сообщений
# ===============================

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

    user_state = load_user_state(chat_id)
    if user_state is None:
        user_state = UserState(
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
            tag_checklists={},
        )
        save_user_state(chat_id, user_state)
        logger.info(f"🆕 Новый пользователь business_chat_id={chat_id}")

    return user_state


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


# ===============================
# Обработчики: онбординг, время, задачи, теги
# ===============================
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
        # Обновляем состояние
    save_user_state(chat_id, user_state)


async def handle_time_command(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState) -> None:
    """
    Команда /время в бизнес-чате:
    переводит пользователя в режим обновления времени (ожидание нового HH:MM)
    """
    business_msg = update.business_message
    if not business_msg:
        return

    chat_id = business_msg.chat.id

    # Добавляем само сообщение /время в служебные, чтобы потом удалить
    user_state.service_message_ids.append(business_msg.message_id)

    # Переводим пользователя в режим "запрос времени"
    user_state.asked_for_time = True           # уже спрашивали, но сейчас заново
    user_state.waiting_for_time = True
    user_state.time = None                     # сбрасываем старое время, будем ставить новое

    # Спрашиваем новое время
    msg = await context.bot.send_message(
        business_connection_id=user_state.business_connection_id,
        chat_id=chat_id,
        text="⏰ Обновим время чек-листа.\nОтправь новое время в формате HH:MM, например 09:30.",
    )
    user_state.service_message_ids.append(msg.message_id)

    save_user_state(chat_id, user_state)


async def handle_force_close(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState) -> None:
    """
    Принудительно закрывает текущий день без проверки даты.
    Вызывает close_day_for_user напрямую, обходя проверки в check_and_handle_new_day.
    """
    business_msg = update.business_message
    if not business_msg:
        return

    chat_id = business_msg.chat.id
    logger.info(f"🔄 Принудительное закрытие дня для chat_id={chat_id}")
    
    # Загружаем актуальное состояние перед закрытием дня
    # (чтобы получить все последние синхронизации выполненных задач)
    from state import load_user_state
    fresh_user_state = load_user_state(chat_id)
    if not fresh_user_state:
        logger.error(f"❌ Не удалось загрузить user_state для chat_id={chat_id}")
        return
    
    # Просто вызываем close_day_for_user с актуальным состоянием
    # Обновление last_closed_date должно оставаться в дневной логике
    await close_day_for_user(context.bot, chat_id, fresh_user_state)
    save_user_state(chat_id, fresh_user_state)


async def handle_force_newday(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState) -> None:
    """
    Принудительно открывает новый день без проверки даты.
    Вызывает start_new_day_for_user напрямую, обходя проверки в check_and_handle_new_day.
    """
    business_msg = update.business_message
    if not business_msg:
        return

    chat_id = business_msg.chat.id
    logger.info(f"🔄 Принудительное открытие нового дня для chat_id={chat_id}")
    
    # start_new_day_for_user сама обновит дату на актуальную
    # Не нужно менять user_state.date вручную
    await start_new_day_for_user(context.bot, chat_id, user_state)
    save_user_state(chat_id, user_state)


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
    
    # 1) Вычисляем timezone_offset_minutes на основе разницы между серверным временем и временем пользователя
    # /время 22:45 означает "сейчас у меня локальное время 22:45"
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    
    # Парсим введенное время
    h, m = map(int, parsed.split(":"))
    user_minutes = h * 60 + m
    server_minutes = now.hour * 60 + now.minute
    
    # Вычисляем смещение
    offset = user_minutes - server_minutes
    
    # Если разница больше 12 часов, корректируем на ±24 часа (берем ближайший вариант)
    if abs(offset) > 12 * 60:
        if offset > 0:
            offset -= 24 * 60
        else:
            offset += 24 * 60
    
    user_state.timezone_offset_minutes = offset
    logger.info(f"📅 Вычислен timezone_offset_minutes для chat_id={chat_id}: {offset} минут (время пользователя: {parsed}, серверное: {now.hour:02d}:{now.minute:02d})")
    
    # 2) Фиксируем дату на основе локального времени пользователя
    user_now = now + timedelta(minutes=offset)
    current_date = user_now.date().isoformat()
    user_state.date = current_date
    user_state.last_closed_date = current_date
    user_state.last_opened_date = current_date
    
    # 2) Поставить job на смену дня для этого пользователя
    job_queue = None
    try:
        if hasattr(context, "application") and context.application:
            job_queue = getattr(context.application, "job_queue", None)
            if job_queue is None and hasattr(context.application, "job_queue"):
                # Пробуем получить напрямую
                job_queue = context.application.job_queue
        if job_queue is None and hasattr(context, "job_queue"):
            job_queue = context.job_queue
    except Exception as e:
        logger.warning(f"⚠️ Ошибка при получении job_queue: {e}")
    
    if job_queue:
        from helpers_daily import schedule_user_midnight_job
        logger.info(f"📅 Создание midnight job для chat_id={chat_id}, время={parsed}, offset={offset} минут")
        try:
            schedule_user_midnight_job(job_queue, chat_id, user_state)
        except Exception as e:
            logger.error(f"❌ Ошибка при создании midnight job: {e}", exc_info=True)
    else:
        logger.warning(f"⚠️ job_queue отсутствует при установке времени для chat_id={chat_id}")
        logger.warning(f"⚠️ Резервный механизм check_new_day_for_all_users будет проверять смену дня каждые 60 секунд")
    
    # 3) Служебные сообщения / чеклист — как у тебя уже есть
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
    
    # Обновляем состояние
    save_user_state(chat_id, user_state)




# ===============================
# Обработчики callback-запросов (кнопки)
# ===============================
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
    
    user_state = load_user_state(chat_id)
    if not user_state:
        logger.warning(f"⚠️ handle_callback_query: user_state не найден для chat_id={chat_id}")
        return
    
    if callback_data == "TASK_SKIP":
        await handle_task_skip_callback(update, context, user_state, chat_id)
    elif callback_data == "TASK_TAG":
        await handle_task_tag_callback(update, context, user_state, chat_id)
    elif callback_data == "TASK_DELETE":
        await cancel_pending_task(context.bot, chat_id, user_state, update, context)
    elif callback_data.startswith("TAG_SELECT:"):
        tag = callback_data.replace("TAG_SELECT:", "")
        await handle_tag_select_callback(update, context, user_state, chat_id, tag)
    elif callback_data == "TAGS_PAGE_NEXT":
        await on_tags_page_next(update, context, user_state, chat_id)
    elif callback_data == "TAGS_PAGE_PREV":
        await on_tags_page_prev(update, context, user_state, chat_id)




# ===============================
# Резервирование базы данных
# ===============================
def backup_state_db():
    """
    Создает резервную копию файла базы данных перед запуском бота.
    Если файл БД существует, создается копия с timestamp в имени.
    """
    db_path = DB_PATH
    
    if not os.path.exists(db_path):
        # Файл БД еще не создан - это нормально для первого запуска
        return
    
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"state_backup_{timestamp}.db"
        backup_path = db_path.parent / backup_filename
        
        shutil.copy(db_path, backup_path)
        logger.info(f"Создан резервный бэкап состояния: {backup_filename}")
    except Exception as e:
        logger.error(f"Не удалось создать бэкап state.db: {e}")


# ===============================
# Точка входа: main()
# ===============================
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
            
            # 0. Если это событие изменения чеклиста (галочка/снятие) — обрабатываем и выходим
            # Проверяем наличие полей, указывающих на событие изменения чеклиста
            # ВАЖНО: проверяем ДО фильтрации системных сообщений!
            is_checklist_state_event = (
                (hasattr(business_msg, "new_checklist_item_state") and business_msg.new_checklist_item_state is not None)
                or (hasattr(business_msg, "checklist_item_state") and business_msg.checklist_item_state is not None)
                or (hasattr(business_msg, "new_checklist_item") and business_msg.new_checklist_item is not None)
                or (hasattr(business_msg, "checklist_tasks_done") and business_msg.checklist_tasks_done is not None)
            )
            
            if is_checklist_state_event:
                logger.info(f"📋 Обнаружено событие изменения состояния чеклиста для chat_id={chat_id}")
                user_state = load_user_state(chat_id)
                if user_state:
                    await handle_checklist_state_update(business_msg, user_state, chat_id)
                # После обработки события чеклиста выходим - не превращаем его в задачу
                return
            
            # Получаем или создаём состояние пользователя (нужно для проверки команды)
            user_state = get_or_create_user_state(update)
            if not user_state:
                logger.error(f"❌ Не удалось получить user_state для chat_id={chat_id}")
                return
            
            # Команда /force_close — принудительно закрыть текущий день
            text = (business_msg.text or "").strip()
            if text.startswith("/force_close"):
                await handle_force_close(update, context, user_state)
                return

            # Команда /force_newday — принудительно открыть новый день
            if text.startswith("/force_newday"):
                await handle_force_newday(update, context, user_state)
                return

            # Команда /время (смена времени чек-листа) - проверяем ДО фильтра системных сообщений
            if text:
                # Проверяем команду в разных форматах (регистронезависимо)
                text_lower = text.lower()
                is_time_command = (
                    text_lower.startswith("/время") or 
                    text_lower.startswith("/time") or
                    (text.startswith("@") and ("/время" in text_lower or "/time" in text_lower))
                )
                
                if is_time_command:
                    logger.info(f"✅ Команда /время обнаружена для chat_id={chat_id}, text='{text}'")
                    await handle_time_command(update, context, user_state)
                    return
            
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


# ===============================
# Ежедневные задачи (закрытие дня и создание нового)
# ===============================
async def check_new_day_for_all_users(context: CallbackContext) -> None:
    """
    Проверяет смену дня для всех пользователей.
    Вызывается каждые 60 секунд.
    Загружает пользователей из базы данных, а не только из STATE (кэша).
    
    ВАЖНО: Основной триггер смены дня теперь — handle_user_midnight (индивидуальные job'ы для каждого пользователя).
    Эта функция служит как резервный механизм (страховка на случай рестарта бота или потерянных job'ов).
    Она не навредит, максимум дважды подряд закроет/откроет тот же день, но логика по датам это отфильтрует.
    """
    try:
        logger.info(f"🔄 [check_new_day_for_all_users] Запуск проверки смены дня для всех пользователей")
        
        # Получаем bot из context
        bot = getattr(context, 'bot', None)
        if not bot and hasattr(context, 'application'):
            bot = getattr(context.application, 'bot', None)
        
        if not bot:
            logger.error("❌ Не удалось получить bot из context в check_new_day_for_all_users")
            return
        
        from db import get_all_chat_ids
        
        # Загружаем всех пользователей из базы данных
        chat_ids = get_all_chat_ids()
        logger.info(f"🔄 Проверка смены дня для всех пользователей. Найдено в БД: {len(chat_ids)}")
        
        for chat_id in chat_ids:
            try:
                # Загружаем состояние из базы (если нет в кэше)
                user_state = load_user_state(chat_id)
                if user_state:
                    logger.debug(f"🔍 Проверка смены дня для chat_id={chat_id}, date={user_state.date}, time={user_state.time}, offset={getattr(user_state, 'timezone_offset_minutes', None)}")
                    await check_and_handle_new_day(bot, chat_id, user_state)
                else:
                    logger.debug(f"⏭️ Пропуск chat_id={chat_id}: user_state не найден")
            except Exception as e:
                logger.error(f"❌ Ошибка при проверке смены дня для chat_id={chat_id}: {e}", exc_info=True)
                # Продолжаем обработку остальных пользователей
    except Exception as e:
        logger.error(f"❌ Критическая ошибка при проверке смены дня: {e}", exc_info=True)


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
    
    # Резервирование базы данных перед запуском
    backup_state_db()
    
    # Инициализация базы данных
    try:
        init_db()
        print("DEBUG: База данных инициализирована")
        logger.info("✅ База данных инициализирована")
    except Exception as e:
        print(f"❌ ОШИБКА при инициализации БД: {e}")
        logger.error(f"Ошибка при инициализации БД: {e}", exc_info=True)
        return
    
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
        # Создание приложения
        app = ApplicationBuilder().token(BOT_TOKEN).build()
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
    
    # Настраиваем job_queue для периодической проверки смены дня
    # В python-telegram-bot 22.x job_queue настраивается через обработчик первого обновления
    job_queue_configured = False
    
    # Настраиваем job_queue через application.post_init (вызывается после инициализации)
    async def setup_jobs_post_init(app_instance):
        """Настраивает job_queue после инициализации приложения"""
        try:
            # Ждем немного, чтобы job_queue точно был готов
            import asyncio
            await asyncio.sleep(3)
            
            if hasattr(app_instance, 'job_queue') and app_instance.job_queue:
                job_queue = app_instance.job_queue
                
                # 1. Настраиваем резервный механизм проверки смены дня
                job_queue.run_repeating(
                    callback=check_new_day_for_all_users,
                    interval=60,
                    first=10,
                )
                logger.info("✅ Настроена периодическая проверка смены дня (post_init): каждые 60 секунд")
                
                # 2. Восстанавливаем индивидуальные midnight job'ы для всех существующих пользователей
                try:
                    from helpers_daily import schedule_user_midnight_job
                    chat_ids = get_all_chat_ids()
                    restored_count = 0
                    for chat_id in chat_ids:
                        user_state = load_user_state(chat_id)
                        if user_state and user_state.time:
                            # Восстанавливаем job для пользователя с установленным временем
                            schedule_user_midnight_job(job_queue, chat_id, user_state)
                            restored_count += 1
                    logger.info(f"✅ Восстановлено {restored_count} индивидуальных midnight job'ов для существующих пользователей")
                except Exception as e:
                    logger.error(f"❌ Ошибка при восстановлении midnight job'ов: {e}", exc_info=True)
                
                print("DEBUG: ✅ job_queue настроен для проверки смены дня (post_init)")
            else:
                logger.warning("⚠️ job_queue отсутствует в post_init, будет настроен при первом обновлении")
        except Exception as e:
            logger.error(f"❌ Ошибка при настройке job_queue в post_init: {e}", exc_info=True)
    
    app.post_init = setup_jobs_post_init
    
    logger.info(f"🚀 Запуск бота, версия {BOT_VERSION}")
    logger.info("🤖 Бот запускается...")
    logger.info(f"Ожидаю business_message с бизнес-аккаунта...")
    
    print("=" * 60)
    print("DEBUG: Запуск polling...")
    print("=" * 60)
    
    print("DEBUG: BOT STARTED AND WAITING FOR UPDATES")
    
    # Запуск бота с глобальной обработкой ошибок
    try:
        # Настраиваем job_queue через обработчик первого обновления
        async def setup_job_on_first_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Настраивает job_queue при первом обновлении"""
            nonlocal job_queue_configured
            if job_queue_configured:
                return
            
            try:
                # Получаем job_queue из application
                if not hasattr(context, 'application') or not context.application:
                    logger.warning("⚠️ context.application отсутствует")
                    return
                
                job_queue = context.application.job_queue
                if not job_queue:
                    logger.warning("⚠️ job_queue отсутствует в application")
                    return
                
                # 1. Настраиваем резервный механизм проверки смены дня
                job_queue.run_repeating(
                    callback=check_new_day_for_all_users,
                    interval=60,
                    first=10,
                )
                logger.info("✅ Настроена периодическая проверка смены дня (после первого обновления): каждые 60 секунд")
                
                # 2. Восстанавливаем индивидуальные midnight job'ы для всех существующих пользователей
                try:
                    from helpers_daily import schedule_user_midnight_job
                    chat_ids = get_all_chat_ids()
                    restored_count = 0
                    for chat_id in chat_ids:
                        user_state = load_user_state(chat_id)
                        if user_state and user_state.time:
                            # Восстанавливаем job для пользователя с установленным временем
                            schedule_user_midnight_job(job_queue, chat_id, user_state)
                            restored_count += 1
                    logger.info(f"✅ Восстановлено {restored_count} индивидуальных midnight job'ов для существующих пользователей")
                except Exception as e:
                    logger.error(f"❌ Ошибка при восстановлении midnight job'ов: {e}", exc_info=True)
                
                print("DEBUG: ✅ job_queue настроен для проверки смены дня")
                job_queue_configured = True
                
                # Удаляем этот обработчик после успешной настройки
                try:
                    # Удаляем обработчик через application
                    handlers = context.application.handlers[0]
                    for handler in handlers[:]:
                        if hasattr(handler, 'callback') and handler.callback == setup_job_on_first_update:
                            handlers.remove(handler)
                            logger.info("✅ Обработчик setup_job_on_first_update удален")
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось удалить обработчик: {e}")
                    
            except Exception as e:
                logger.error(f"❌ Ошибка при настройке job_queue: {e}", exc_info=True)
        
        # Добавляем обработчик для настройки job_queue при первом обновлении
        app.add_handler(TypeHandler(Update, setup_job_on_first_update), group=0)
        
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
