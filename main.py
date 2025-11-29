import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List
from dotenv import load_dotenv
from telegram import Update, InputChecklist, InputChecklistTask
from telegram.ext import (
    ApplicationBuilder,
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
    bmsg = update.business_message
    if not bmsg:
        return None

    chat_id = bmsg.chat.id
    bconn = bmsg.business_connection_id

    if not bconn:
        logger.error("business_connection_id отсутствует")
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
        )
        logger.info(f"🆕 Новый пользователь business_chat_id={chat_id}, b_conn={bconn}")
    else:
        logger.info(f"♻️ Существующий пользователь chat_id={chat_id}, asked_for_time={STATE[chat_id].asked_for_time}, waiting_for_time={STATE[chat_id].waiting_for_time}, time={STATE[chat_id].time}")

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


def extract_task_text_from_business_message(bmsg) -> str:
    """
    Формирует текст задачи для любого business_message:
    - Если есть текст или подпись — берём их
    - Если это медиа без текста — даём осмысленное название
    """
    # 1. Если есть текст или подпись — берём их
    if bmsg.text or bmsg.caption:
        return (bmsg.text or bmsg.caption).strip()
    
    # 2. Если это медиа без текста — даём осмысленное название
    if bmsg.photo:
        return "Фото"
    if bmsg.voice:
        return "Голосовое сообщение"
    if bmsg.video:
        return "Видео"
    if bmsg.document:
        filename = bmsg.document.file_name if bmsg.document else None
        return f"Файл: {filename}" if filename else "Документ"
    if bmsg.audio:
        return "Аудиофайл"
    if bmsg.sticker:
        return "Стикер"
    
    # 3. На всякий случай общий fallback
    return "Сообщение"


async def create_checklist_for_user(
    bot,
    chat_id: int,
    user_state: UserState,
) -> None:
    """
    Создаёт нативный чеклист для данного пользователя, если он ещё не создан.
    - title = сегодняшняя дата (например, '29 ноября')
    - первая задача = 'Готовность оседлать все задачи!'
    - others_can_add_tasks = False
    - others_can_mark_tasks_as_done = True
    - сохраняет checklist_message_id, дату и список tasks в user_state
    """
    if user_state.checklist_message_id is not None:
        # уже есть чеклист — ничего не делаем
        logger.info(f"⏭️ Чеклист уже существует для chat_id={chat_id}, message_id={user_state.checklist_message_id}")
        return

    logger.info(f"🔨 Начинаю создание чеклиста для chat_id={chat_id}")
    human_date = get_today_human_date()
    user_state.date = datetime.now().strftime("%Y-%m-%d")
    user_state.tasks = ["Готовность оседлать все задачи!"]

    tasks = [
        InputChecklistTask(
            id=idx,
            text=text,
        )
        for idx, text in enumerate(user_state.tasks, start=1)
    ]

    checklist = InputChecklist(
        title=human_date,
        tasks=tasks,
        others_can_add_tasks=False,
        others_can_mark_tasks_as_done=True,
    )

    try:
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
        raise


async def update_checklist_for_user(
    bot,
    chat_id: int,
    user_state: UserState,
) -> None:
    """
    Обновляет существующий чеклист на основе user_state.tasks.
    """
    if user_state.checklist_message_id is None:
        # на всякий случай: если вдруг нет чеклиста — создаём
        await create_checklist_for_user(bot, chat_id, user_state)
        return

    tasks = [
        InputChecklistTask(
            id=idx,
            text=text,
        )
        for idx, text in enumerate(user_state.tasks, start=1)
    ]

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
        logger.info(f"📝 Чеклист обновлён для chat_id={chat_id}, задач: {len(user_state.tasks)}")
    except Exception as e:
        logger.error(f"❌ Ошибка при обновлении чеклиста для chat_id={chat_id}: {e}", exc_info=True)
        raise


# ===== БЕЗОПАСНОЕ УДАЛЕНИЕ СООБЩЕНИЙ =====
async def safe_delete(bot, business_connection_id: str, chat_id: int, message_id: int) -> None:
    """Безопасно удаляет business сообщение, игнорируя ошибки"""
    try:
        # Используем delete_business_messages - НЕ требует chat_id, только business_connection_id и message_ids
        await bot.delete_business_messages(
            business_connection_id=business_connection_id,
            message_ids=[message_id],
        )
        logger.info(f"✅ Удалено сообщение message_id={message_id}")
    except Exception as e:
        logger.warning(f"⚠️ Не удалось удалить message_id={message_id}: {e}")


# ===== ОБРАБОТЧИКИ БИЗНЕС-СООБЩЕНИЙ =====
async def handle_first_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState) -> None:
    """Обработка первого сообщения: отправка интро и запрос времени"""
    logger.info("🔔 handle_first_message вызван")
    business_msg = update.business_message
    if not business_msg:
        logger.warning("⚠️ handle_first_message: business_msg отсутствует")
        return
    chat_id = business_msg.chat.id
    logger.info(f"🔔 Начинаю отправку интро для chat_id={chat_id}")
    
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
        logger.info(f"✅ Первое приветственное сообщение отправлено (message_id={welcome_1.message_id})")
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
        logger.info(f"✅ Второе приветственное сообщение отправлено (message_id={welcome_2.message_id})")
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке второго сообщения: {e}", exc_info=True)
        return
    
    # Сохраняем ID служебных сообщений
    user_state.service_message_ids.append(welcome_1.message_id)
    user_state.service_message_ids.append(welcome_2.message_id)
    logger.info(f"📝 Сохранены ID служебных сообщений: {len(user_state.service_message_ids)} сообщений")
    
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
    logger.info(f"🔔 Запрошено время у пользователя chat_id={chat_id}, asked_for_time={user_state.asked_for_time}")


async def handle_time_input(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState) -> None:
    """Обработка ввода времени в формате HH:MM"""
    business_msg = update.business_message
    if not business_msg:
        return
    chat_id = business_msg.chat.id
    text = business_msg.text or ""
    
    logger.info(f"⏰ Ожидаю ввод времени от chat_id={chat_id}, текст: {text!r}")
    
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
    
    logger.info(f"💾 Сохраняю время {parsed} для chat_id={chat_id}, служебных сообщений для удаления: {len(user_state.service_message_ids)}")
    
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
    logger.info(f"✅ Время {parsed} сохранено, служебные сообщения удалены, чеклист создан для chat_id={chat_id}")


async def handle_task_addition(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState) -> None:
    """Обработка сообщения как задачи для чеклиста"""
    business_msg = update.business_message
    if not business_msg:
        return
    chat_id = business_msg.chat.id
    
    logger.info(f"📝 Получено сообщение для создания/обновления чеклиста, chat_id={chat_id}, time={user_state.time}, checklist_id={user_state.checklist_message_id}")
    
    # 1. Убедиться, что чеклист создан
    logger.info(f"🔧 Проверяю, нужно ли создавать чеклист для chat_id={chat_id}")
    await create_checklist_for_user(context.bot, chat_id, user_state)
    
    # 2. Получить текст задачи
    task_text = extract_task_text_from_business_message(business_msg)
    logger.info(f"📄 Извлеченный текст задачи: {task_text!r}")
    
    # 3. Добавить задачу в state
    user_state.tasks.append(task_text)
    # Явно обновляем состояние
    STATE[chat_id] = user_state
    logger.info(f"📋 Задач в списке: {len(user_state.tasks)}")
    
    # 4. Обновить чеклист
    await update_checklist_for_user(context.bot, chat_id, user_state)
    # Явно обновляем состояние после обновления чеклиста
    STATE[chat_id] = user_state
    
    # 5. Удалить оригинальное сообщение пользователя
    await safe_delete(
        context.bot,
        user_state.business_connection_id,
        chat_id,
        business_msg.message_id,
    )
    
    logger.info(f"✅ Задача добавлена в чеклист для chat_id={chat_id}: {task_text!r}")


# ===== ОБРАБОТЧИКИ =====
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start для обычных сообщений"""
    if update.message:
        await update.message.reply_text(f"Бот запущен. Версия: {BOT_VERSION}")
        logger.info("Команда /start получена")


async def handle_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик всех обновлений"""
    # ДИАГНОСТИКА: Логируем все приходящие обновления
    logger.info(f"🔔 Получен update: business_message={update.business_message is not None}, message={update.message is not None}, update_id={update.update_id}")
    if update.business_message:
        logger.info(f"✅ Найден business_message!")
    if update.message:
        logger.info(f"⚠️ Найден обычный message (не business)")
    
    # Обработка business_message
    if update.business_message:
        business_msg = update.business_message
        
        business_connection_id = business_msg.business_connection_id
        chat_id = business_msg.chat.id
        message_text = business_msg.text if business_msg.text else "[нет текста]"
        
        # Логируем основную информацию (INFO)
        logger.info(f"📨 business_message получен")
        logger.info(f"   business_connection_id: {business_connection_id}")
        logger.info(f"   chat.id: {chat_id}")
        logger.info(f"   текст сообщения: {message_text}")
        
        # Полный update.to_dict() только на DEBUG уровне
        logger.debug(f"Полная структура update: {update.to_dict()}")
        
        # Получаем или создаём состояние пользователя
        user_state = get_or_create_user_state(update)
        if not user_state:
            logger.error(f"❌ Не удалось получить user_state для chat_id={chat_id}")
            return
        
        text = business_msg.text or ""
        
        # Логируем текущее состояние для отладки
        logger.info(f"🔍 Состояние пользователя chat_id={chat_id}: asked_for_time={user_state.asked_for_time}, waiting_for_time={user_state.waiting_for_time}, time={user_state.time!r}, текст: {text!r}")
        logger.info(f"🔍 STATE содержит chat_id={chat_id}: {chat_id in STATE}, всего пользователей в STATE: {len(STATE)}")
        
        # ЧЁТКИЙ ПОРЯДОК ПРОВЕРОК:
        # 1) Ещё не просили время → интро + запрос
        if not user_state.asked_for_time:
            logger.info(f"🆕 Первый контакт для chat_id={chat_id}, вызываю handle_first_message")
            await handle_first_message(update, context, user_state)
            return
        
        # 2) Уже просили время, но оно ещё НЕ установлено → парсим HH:MM
        # (проверяем именно time is None, чтобы ловить все попытки ввода времени)
        if user_state.asked_for_time and user_state.time is None:
            logger.info(f"⏰ Ожидание времени для chat_id={chat_id}, вызываю handle_time_input")
            await handle_time_input(update, context, user_state)
            return
        
        # 3) Время установлено (time is not None) → обрабатываем сообщение как задачу
        logger.info(f"📝 Время установлено для chat_id={chat_id}, вызываю handle_task_addition")
        await handle_task_addition(update, context, user_state)
        return
    
    # Обработка обычного message
    if update.message:
        logger.info(f"📩 Обычное message, игнорирую (update_id={update.update_id})")
        return
    
    # Если не обработано ни одно из условий
    logger.warning(f"⚠️ Update не обработан: update_id={update.update_id}, type={type(update)}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик ошибок"""
    logger.error(f"Ошибка при обработке update: {context.error}", exc_info=context.error)


def main():
    """Запуск бота"""
    # Загрузка токена из .env
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    
    if not BOT_TOKEN:
        error_msg = "BOT_TOKEN не найден в .env. Убедитесь, что файл .env существует и содержит BOT_TOKEN=your_token_here"
        logger.error(error_msg)
        raise ValueError(error_msg)
    
    # Создание приложения
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Добавление обработчиков
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(TypeHandler(Update, handle_all_updates), group=-1)
    
    # Обработчик ошибок
    app.add_error_handler(error_handler)
    
    logger.info(f"🚀 Запуск бота, версия {BOT_VERSION}")
    logger.info("🤖 Бот запускается...")
    logger.info(f"Ожидаю business_message с бизнес-аккаунта...")
    
    # Запуск бота
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "business_message", "edited_business_message"]
    )


if __name__ == "__main__":
    main()
