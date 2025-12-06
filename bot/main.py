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
    Корректная фильтрация бизнес-сообщений.

    ВАЖНО:
    События чеклиста (marked done/undone, checklist_item_state, new_checklist_item)
    НЕЛЬЗЯ фильтровать, иначе чеклист перестаёт работать.
    """
    # 1. Сообщения от самого бота — фильтруем
    if getattr(bmsg, "from_user", None) and getattr(bmsg.from_user, "is_bot", False):
        return True

    # 2. Автоматические пересылки
    if getattr(bmsg, "is_automatic_forward", False):
        return True

    # ❗️ 3. НЕ фильтруем события чеклиста — возвращаем False
    # Это важные события для логики чеклистов — всегда пропускаем
    if getattr(bmsg, "checklist", None) \
       or getattr(bmsg, "checklist_tasks_done", None) \
       or getattr(bmsg, "checklist_tasks_added", None):
        return False

    # 4. Если нет текста/подписи и нет медиа — это сервисное сообщение
    has_text = bool(getattr(bmsg, "text", None) or getattr(bmsg, "caption", None))
    has_media = any([
        getattr(bmsg, "photo", None),
        getattr(bmsg, "voice", None),
        getattr(bmsg, "video", None),
        getattr(bmsg, "document", None),
        getattr(bmsg, "audio", None),
        getattr(bmsg, "sticker", None),
    ])

    if not has_text and not has_media:
        return True

    # 5. Всё остальное → не системное
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


async def handle_force_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик команды /force_close — принудительно закрывает текущий день.
    Вызывает close_day_for_user для закрытия дня и формирования отчёта.
    """
    try:
        business_msg = update.business_message
        if not business_msg:
            logger.warning("⚠️ handle_force_close: business_message отсутствует")
            return

        chat_id = business_msg.chat.id
        logger.info(f"🔄 Команда /force_close вызвана для chat_id={chat_id}")
        
        # Загружаем актуальное состояние перед закрытием дня
        # (чтобы получить все последние синхронизации выполненных задач)
        from state import load_user_state
        from helpers_daily import close_day_for_user
        
        fresh_user_state = load_user_state(chat_id)
        if not fresh_user_state:
            logger.error(f"❌ Не удалось загрузить user_state для chat_id={chat_id}")
            return
        
        # Используем текущий user_state.date как "день, который закрываем"
        close_date = fresh_user_state.date
        if not close_date:
            logger.warning(f"⚠️ У пользователя chat_id={chat_id} нет установленной даты")
            await context.bot.send_message(
                business_connection_id=fresh_user_state.business_connection_id,
                chat_id=chat_id,
                text="❌ Не установлена дата. Используйте команду /время для установки времени.",
            )
            return
        
        # Вызываем close_day_for_user с актуальным состоянием
        # Функция сама:
        # - сгенерирует отчёт (только выполненные задачи)
        # - отправит отчёт пользователю
        # - удалит чеклисты
        # - оставит в состоянии только невыполненные задачи
        # - обновит last_closed_date
        # - сохранит состояние
        await close_day_for_user(context.bot, chat_id, fresh_user_state)
        
        logger.info(f"FORCE_DAY_CLOSE chat_id={chat_id} date={close_date}")
        
        await context.bot.send_message(
            business_connection_id=fresh_user_state.business_connection_id,
            chat_id=chat_id,
            text=f"✅ День закрыт. Используйте /force_newday для открытия нового дня.",
        )
        
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_force_close для chat_id={business_msg.chat.id if business_msg else 'unknown'}: {e}", exc_info=True)


async def handle_force_newday(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик команды /force_newday — принудительно открывает новый день.
    Вызывает start_new_day_for_user для создания новых чеклистов из невыполненных задач.
    """
    try:
        business_msg = update.business_message
        if not business_msg:
            logger.warning("⚠️ handle_force_newday: business_message отсутствует")
            return

        chat_id = business_msg.chat.id
        logger.info(f"🔄 Команда /force_newday вызвана для chat_id={chat_id}")
        
        # Загружаем актуальное состояние (после close_day_for_user там только невыполненные задачи)
        from state import load_user_state
        from helpers_daily import start_new_day_for_user
        
        fresh_user_state = load_user_state(chat_id)
        if not fresh_user_state:
            logger.error(f"❌ Не удалось загрузить user_state для chat_id={chat_id}")
            return
        
        # start_new_day_for_user:
        # - обновит дату на актуальную (вычисленную на основе локального времени)
        # - создаст новые чеклисты из невыполненных задач (которые остались после close_day_for_user)
        # - сохранит состояние
        await start_new_day_for_user(context.bot, chat_id, fresh_user_state)
        
        # Перезагружаем состояние после открытия нового дня
        fresh_user_state = load_user_state(chat_id)
        if fresh_user_state:
            new_date = fresh_user_state.date
            logger.info(f"FORCE_NEW_DAY chat_id={chat_id} date={new_date}")
            
            await context.bot.send_message(
                business_connection_id=fresh_user_state.business_connection_id,
                chat_id=chat_id,
                text=f"✅ Новый день открыт (дата: {new_date}).",
            )
        else:
            logger.error(f"❌ Не удалось загрузить user_state после start_new_day_for_user для chat_id={chat_id}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_force_newday для chat_id={business_msg.chat.id if business_msg else 'unknown'}: {e}", exc_info=True)


async def apply_user_time(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_state: UserState,
    time_str: str,
    now_utc: datetime,
) -> bool:
    """
    Применяет время пользователя через set_user_time_info и выполняет дополнительные действия:
    - Сбрасывает waiting_for_time = False
    - Создает midnight job для автоматического закрытия дня
    - Создает первый чеклист, если его еще нет
    
    Возвращает True если время успешно применено, False если ошибка парсинга.
    """
    business_msg = update.business_message
    if not business_msg:
        return False
    
    chat_id = business_msg.chat.id
    
    # Используем общую функцию для установки времени
    from state import set_user_time_info
    success = set_user_time_info(chat_id, time_str)
    
    if not success:
        return False
    
    # Перезагружаем состояние после установки времени
    user_state = load_user_state(chat_id)
    if not user_state:
        return False
    
    user_state.waiting_for_time = False
    user_state.last_opened_date = user_state.date  # инициализируем
    
    # Поставить job на смену дня для этого пользователя
    job_queue = None
    try:
        if hasattr(context, "application") and context.application:
            job_queue = getattr(context.application, "job_queue", None)
            if job_queue is None and hasattr(context.application, "job_queue"):
                job_queue = context.application.job_queue
        if job_queue is None and hasattr(context, "job_queue"):
            job_queue = context.job_queue
    except Exception as e:
        logger.warning(f"⚠️ Ошибка при получении job_queue: {e}")
    
    if job_queue:
        from helpers_daily import schedule_user_midnight_job
        parsed = parse_time_string(time_str)
        logger.info(f"📅 Создание midnight job для chat_id={chat_id}, время={parsed}, offset={user_state.timezone_offset_minutes} минут")
        try:
            schedule_user_midnight_job(job_queue, chat_id, user_state)
        except Exception as e:
            logger.error(f"❌ Ошибка при создании midnight job: {e}", exc_info=True)
    else:
        logger.warning(f"⚠️ job_queue отсутствует при установке времени для chat_id={chat_id}")
        logger.warning(f"⚠️ Резервный механизм check_day_rollover будет проверять смену дня каждые 60 секунд")
    
    # Создаем первый чеклист, если его еще нет
    await create_checklist_for_user(context.bot, chat_id, user_state)
    
    # Сохраняем состояние
    save_user_state(chat_id, user_state)
    
    return True


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
        save_user_state(chat_id, user_state)
        return
    
    # Используем общую функцию для применения времени
    from datetime import datetime
    now_utc = datetime.utcnow()
    
    success = await apply_user_time(update, context, user_state, text, now_utc)
    
    if not success:
        # Сообщаем об ошибке, но НЕ меняем состояние - остаемся в ожидании времени
        await context.bot.send_message(
            business_connection_id=user_state.business_connection_id,
            chat_id=chat_id,
            text="❌ Неверный формат времени. Введи, пожалуйста, в формате HH:MM, например 09:30.",
        )
        # Убеждаемся, что остаемся в режиме ожидания времени
        user_state.waiting_for_time = True
        save_user_state(chat_id, user_state)
        return
    
    # Добавляем сообщение с временем в список служебных
    user_state.service_message_ids.append(business_msg.message_id)
    
    # Отправляем подтверждение и сохраняем его ID
    parsed = parse_time_string(text)
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
            
            # Команды /force_close и /force_newday - обрабатываем вручную для business_message
            # (CommandHandler не работает с business_message)
            text = (business_msg.text or "").strip()
            if text.startswith("/force_close"):
                await handle_force_close(update, context)
                return
            
            if text.startswith("/force_newday"):
                await handle_force_newday(update, context)
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
            # 0) Проверяем и обновляем дату чеклиста, если она устарела
            if user_state.checklist_message_id is not None:
                current_user_date = get_user_local_date(user_state)
                if user_state.date != current_user_date:
                    logger.info(f"🔄 Дата устарела для chat_id={chat_id}: {user_state.date} → {current_user_date}, обновляю чеклист")
                    user_state.date = current_user_date
                    save_user_state(chat_id, user_state)
                    await create_checklist_for_user(context.bot, chat_id, user_state)
            
            # 1) Ждём ввод времени (waiting_for_time) → обрабатываем как ввод времени
            if user_state.waiting_for_time:
                await handle_time_input(update, context, user_state)
                return
            
            # 2) Ещё не просили время → интро + запрос
            if not user_state.asked_for_time:
                await handle_first_message(update, context, user_state)
                return
            
            # 3) Уже просили время, но оно ещё НЕ установлено → парсим HH:MM (резервная проверка)
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
async def check_day_rollover(context: CallbackContext) -> None:
    """
    Фоновая задача, которая проверяет всех пользователей и закрывает/открывает день
    по их локальному времени.
    
    Вызывается каждые 60 секунд через JobQueue.run_repeating().
    
    Логика для каждого пользователя:
    - Вычисляет локальное время: now_local = utc_now + timedelta(minutes=utc_offset_minutes)
    - Проверяет условия закрытия дня:
      - utc_offset_minutes и day_end_time установлены
      - local_date > last_closed_date ИЛИ (local_date == last_closed_date и local_time >= day_end_time)
    - Если условия выполнены: закрывает день и создает новый
    """
    try:
        logger.debug(f"🔄 [check_day_rollover] Запуск проверки смены дня для всех пользователей")
        
        # Получаем bot из context
        bot = getattr(context, 'bot', None)
        if not bot and hasattr(context, 'application'):
            bot = getattr(context.application, 'bot', None)
        
        if not bot:
            logger.error("❌ Не удалось получить bot из context в check_day_rollover")
            return
        
        from db import get_all_chat_ids
        from state import load_user_state, save_user_state
        from helpers_daily import close_day_for_user, start_new_day_for_user
        from datetime import datetime, timedelta, time
        
        # Загружаем всех пользователей из базы данных
        chat_ids = get_all_chat_ids()
        
        utc_now = datetime.utcnow()
        
        for chat_id in chat_ids:
            try:
                # Загружаем состояние из базы
                user_state = load_user_state(chat_id)
                if not user_state:
                    continue
                
                # Проверяем, что utc_offset_minutes и day_end_time установлены
                utc_offset_minutes = getattr(user_state, "timezone_offset_minutes", 0) or 0
                if not user_state.day_end_time or utc_offset_minutes == 0 and user_state.day_end_time is None:
                    continue
                
                # Вычисляем локальное время пользователя
                now_local = utc_now + timedelta(minutes=utc_offset_minutes)
                local_date = now_local.date().isoformat()
                local_time = now_local.time()
                
                # Парсим day_end_time из "HH:MM"
                try:
                    h, m = map(int, user_state.day_end_time.split(":"))
                    day_end_time_obj = time(h, m)
                except Exception:
                    logger.warning(f"⚠️ Неверный формат day_end_time для chat_id={chat_id}: {user_state.day_end_time}")
                    continue
                
                # Проверяем условия для закрытия дня
                should_close = False
                
                if user_state.last_closed_date:
                    # Условие: local_date > last_closed_date ИЛИ (local_date == last_closed_date и local_time >= day_end_time)
                    if local_date > user_state.last_closed_date:
                        should_close = True
                        logger.info(f"AUTO_DAY_CLOSE chat_id={chat_id} local_date={local_date} (дата сменилась: {user_state.last_closed_date} → {local_date})")
                    elif local_date == user_state.last_closed_date and local_time >= day_end_time_obj:
                        should_close = True
                        logger.info(f"AUTO_DAY_CLOSE chat_id={chat_id} local_date={local_date} (время достигло day_end_time: {local_time} >= {day_end_time_obj})")
                else:
                    # last_closed_date не установлено - проверяем только время
                    if local_time >= day_end_time_obj:
                        should_close = True
                        logger.info(f"AUTO_DAY_CLOSE chat_id={chat_id} local_date={local_date} (первое закрытие, время достигло day_end_time: {local_time} >= {day_end_time_obj})")
                
                if should_close:
                    # ЗАЩИТА ОТ ДВОЙНОГО ЗАКРЫТИЯ: проверяем, не закрыли ли уже день
                    # Если last_closed_date уже равен local_date, значит день уже закрыт
                    if user_state.last_closed_date == local_date:
                        logger.debug(f"⏭️ День уже закрыт для chat_id={chat_id}, last_closed_date={user_state.last_closed_date}, local_date={local_date}")
                        continue
                    
                    # Закрываем день (сохраняет дату, которую закрываем, в last_closed_date)
                    await close_day_for_user(bot, chat_id, user_state)
                    
                    # Перезагружаем состояние после закрытия
                    user_state = load_user_state(chat_id)
                    if not user_state:
                        logger.error(f"❌ Не удалось загрузить user_state после close_day_for_user для chat_id={chat_id}")
                        continue
                    
                    # Проверяем, что день действительно закрыт (защита от повторного закрытия)
                    if user_state.last_closed_date == local_date:
                        # Открываем новый день (обновляет user_state.date на новую дату)
                        await start_new_day_for_user(bot, chat_id, user_state)
                        
                        # Перезагружаем состояние после открытия нового дня
                        user_state = load_user_state(chat_id)
                        if user_state:
                            logger.info(f"AUTO_NEW_DAY chat_id={chat_id} local_date={user_state.date}")
                        else:
                            logger.error(f"❌ Не удалось загрузить user_state после start_new_day_for_user для chat_id={chat_id}")
                    else:
                        logger.warning(f"⚠️ День не был закрыт для chat_id={chat_id}, last_closed_date={user_state.last_closed_date}, ожидалось={local_date}")
                    
            except Exception as e:
                logger.error(f"ERROR_DAY_ROLLOVER chat_id={chat_id} error={e}", exc_info=True)
                # Продолжаем обработку остальных пользователей
    except Exception as e:
        logger.error(f"❌ Критическая ошибка в check_day_rollover: {e}", exc_info=True)


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
    app.add_handler(CommandHandler("force_close", handle_force_close))
    app.add_handler(CommandHandler("force_newday", handle_force_newday))
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
                    callback=check_day_rollover,
                    interval=60,
                    first=60,
                )
                logger.info("✅ Настроена периодическая проверка конца дня (post_init): каждые 60 секунд")
                
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
                    callback=check_day_rollover,
                    interval=60,
                    first=60,
                )
                logger.info("✅ Настроена периодическая проверка конца дня (после первого обновления): каждые 60 секунд")
                
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
