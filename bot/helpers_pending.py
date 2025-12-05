"""
Модуль для работы с pending-задачами (висящие задачи):
создание pending-состояния, обработка кнопок, авто-скип, финализация.
"""

import logging
from datetime import timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackContext

from state import UserState, TaskItem, load_user_state, save_user_state
from helpers_checklist import create_checklist_for_user, add_task_to_tag_checklist, update_checklist_for_user
from helpers_tags import build_tags_keyboard
from helpers_delete import safe_delete
from helpers_text import extract_task_text_from_business_message, normalize_tag

logger = logging.getLogger(__name__)

# Константы
AUTO_SKIP_TIMEOUT = 300  # Таймаут авто-пропуска в секундах (5 минут)
MAX_TASK_LENGTH = 95  # Максимальная длина текста задачи


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
        # Добавляем задачу без тега как TaskItem
        next_id = max([t.item_id for t in user_state.tasks], default=0) + 1
        user_state.tasks.append(TaskItem(item_id=next_id, text=user_state.pending_task_text, done=False))
        save_user_state(chat_id, user_state)
        logger.info(f"📋 Задач в списке: {len(user_state.tasks)}")
        
        # Обновляем чеклист
        await update_checklist_for_user(bot, chat_id, user_state)
        save_user_state(chat_id, user_state)
        
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
    save_user_state(chat_id, user_state)


async def cancel_pending_task(bot, chat_id: int, user_state: UserState, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отменяет pending задачу без добавления в чеклист:
    - отменяет job авто-скипа
    - удаляет все связанные сообщения
    - очищает pending поля
    """
    try:
        # 1. Отменить job авто-скипа
        await cancel_pending_confirm_job(context.job_queue, user_state)
        
        # 2. Собрать все связанные сообщения: исходная задача + все сервисные
        messages_to_delete = []
        if user_state.pending_task_message_id:
            messages_to_delete.append(user_state.pending_task_message_id)
        messages_to_delete.extend(user_state.pending_service_message_ids)
        
        for msg_id in messages_to_delete:
            await safe_delete(bot, user_state.business_connection_id, chat_id, msg_id)
        
        # 3. Очистить pending-поля БЕЗ добавления задачи в чеклист
        user_state.pending_task_text = None
        user_state.pending_task_message_id = None
        user_state.pending_service_message_ids.clear()
        user_state.awaiting_tag = False
        user_state.pending_confirm_job_id = None
        save_user_state(chat_id, user_state)
        
        logger.info(f"✅ Задача отменена для chat_id={chat_id}")
    except Exception as e:
        logger.error(f"❌ Ошибка в cancel_pending_task для chat_id={chat_id}: {e}", exc_info=True)


async def finalize_task_with_tag(bot, chat_id: int, user_state: UserState, tag: str, additional_message_id: int = None) -> None:
    """
    Завершает добавление задачи с тегом:
    - добавляет задачу в чеклист по тегу
    - удаляет все связанные сообщения (включая optional additional_message_id)
    - очищает pending поля
    """
    if not user_state.pending_task_text:
        logger.warning(f"⚠️ finalize_task_with_tag вызвана без pending_task_text для chat_id={chat_id}")
        return
    
    task_text = user_state.pending_task_text
    
    try:
        await add_task_to_tag_checklist(
            bot=bot,
            chat_id=chat_id,
            user_state=user_state,
            tag=tag,
            task_text=task_text,
        )
        logger.info(f"✅ Задача добавлена в чеклист по тегу '{tag}' для chat_id={chat_id}: {task_text!r}")
    except Exception as e:
        logger.error(f"❌ Ошибка при добавлении задачи в чеклист по тегу для chat_id={chat_id}: {e}", exc_info=True)
    
    # Удаляем все связанные сообщения
    messages_to_delete = []
    if user_state.pending_task_message_id:
        messages_to_delete.append(user_state.pending_task_message_id)
    messages_to_delete.extend(user_state.pending_service_message_ids)
    if additional_message_id and additional_message_id not in messages_to_delete:
        messages_to_delete.append(additional_message_id)
    
    for msg_id in messages_to_delete:
        await safe_delete(bot, user_state.business_connection_id, chat_id, msg_id)
    
    # Очищаем pending поля
    user_state.pending_task_text = None
    user_state.pending_task_message_id = None
    user_state.pending_service_message_ids.clear()
    user_state.awaiting_tag = False
    user_state.pending_confirm_job_id = None
    save_user_state(chat_id, user_state)


async def auto_skip_pending_task(context: CallbackContext) -> None:
    """Автоматически пропускает задачу через 5 минут, если пользователь ничего не выбрал"""
    if not context.job:
        logger.warning(f"⚠️ auto_skip_pending_task: job отсутствует в context")
        return
    
    # Получаем chat_id из job (может быть в chat_id или в data)
    chat_id = None
    if hasattr(context.job, 'chat_id') and context.job.chat_id:
        chat_id = context.job.chat_id
    elif hasattr(context.job, 'data') and context.job.data and isinstance(context.job.data, dict) and 'chat_id' in context.job.data:
        chat_id = context.job.data['chat_id']
    
    if not chat_id:
        logger.warning(f"⚠️ auto_skip_pending_task: chat_id не найден в job")
        return
    
    # Загружаем состояние пользователя
    user_state = load_user_state(chat_id)
    if not user_state:
        logger.warning(f"⚠️ auto_skip_pending_task: user_state не найден для chat_id={chat_id}")
        return
    
    # Проверяем, есть ли задача для обработки
    if not user_state.pending_task_text:
        logger.info(f"ℹ️ auto_skip_pending_task: pending_task_text отсутствует для chat_id={chat_id}, ничего не делаем")
        return
    
    # Выполняем авто-скип: добавляем задачу в чеклист без тега и удаляем все сообщения
    # Это работает как для обычного режима, так и для режима выбора тега (awaiting_tag=True)
    logger.info(f"[AUTO_SKIP] Автоматически пропущена задача для chat_id={chat_id} (awaiting_tag={user_state.awaiting_tag})")
    
    # Получаем bot из context (в python-telegram-bot 20+ это может быть через application)
    bot = getattr(context, 'bot', None)
    if not bot and hasattr(context, 'application'):
        bot = getattr(context.application, 'bot', None)
    
    if not bot:
        logger.error(f"❌ auto_skip_pending_task: не удалось получить bot из context для chat_id={chat_id}")
        return
    
    await finalize_task_without_tag(bot, chat_id, user_state)


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
        task_text = extract_task_text_from_business_message(business_msg, MAX_TASK_LENGTH)
        
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
        user_state.tags_page_index = 0
        user_state.pending_service_message_ids.clear()
        
        # 4. Сразу запускаем tag flow (без промежуточного шага "Добавить")
        await start_tag_flow_for_pending_task(context, chat_id, user_state)
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_task_addition: {e}", exc_info=True)


async def handle_task_skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState, chat_id: int) -> None:
    """Обработка кнопки 'Пропустить'"""
    try:
        if not user_state.pending_task_text:
            return
        
        # Отменяем job, если есть
        await cancel_pending_confirm_job(context.job_queue, user_state)
        
        # Добавляем задачу без тега
        await finalize_task_without_tag(context.bot, chat_id, user_state)
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_task_skip_callback для chat_id={chat_id}: {e}", exc_info=True)


async def start_tag_flow_for_pending_task(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_state: UserState) -> None:
    """
    Запускает поток выбора тега для pending задачи:
    - устанавливает awaiting_tag = True
    - отправляет сообщение с запросом тега и клавиатурой
    - создаёт job для авто-скипа через 5 минут
    """
    try:
        if not user_state.pending_task_text:
            logger.warning(f"⚠️ start_tag_flow_for_pending_task: pending_task_text отсутствует для chat_id={chat_id}")
            return
        
        # Устанавливаем флаг ожидания тега
        user_state.awaiting_tag = True
        user_state.tags_page_index = 0
        
        # Отправляем сообщение с запросом тега и клавиатурой
        tag_msg = await context.bot.send_message(
            business_connection_id=user_state.business_connection_id,
            chat_id=chat_id,
            text="введи тэг или ткни один из недавних👇",
            reply_markup=build_tags_keyboard(user_state),
        )
        user_state.pending_service_message_ids.append(tag_msg.message_id)
        
        # Создаём job для авто-пропуска через 5 минут
        if not context.job_queue:
            logger.error(f"❌ job_queue отсутствует в context")
            return
        
        job_name = f"auto-skip-{chat_id}"
        job = context.job_queue.run_once(
            auto_skip_pending_task,
            when=timedelta(seconds=AUTO_SKIP_TIMEOUT),
            chat_id=chat_id,
            name=job_name,
            data={"chat_id": chat_id},  # Передаем chat_id также в data для надежности
        )
        user_state.pending_confirm_job_id = job.name
        
        # Сохраняем состояние
        save_user_state(chat_id, user_state)
        logger.info(f"✅ Tag flow запущен для chat_id={chat_id}, message_id={tag_msg.message_id}")
    except Exception as e:
        logger.error(f"❌ Ошибка в start_tag_flow_for_pending_task для chat_id={chat_id}: {e}", exc_info=True)


async def handle_task_tag_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState, chat_id: int) -> None:
    """Обработка кнопки 'Тэг' (для совместимости со старым UI)"""
    try:
        if not user_state.pending_task_text:
            return
        
        # Отменяем job
        await cancel_pending_confirm_job(context.job_queue, user_state)
        
        # Запускаем tag flow
        await start_tag_flow_for_pending_task(context, chat_id, user_state)
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_task_tag_callback для chat_id={chat_id}: {e}", exc_info=True)


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
            save_user_state(chat_id, user_state)
            return
        
        # Добавляем/поднимаем тег в истории
        if tag in user_state.tags_history:
            user_state.tags_history.remove(tag)
        user_state.tags_history.insert(0, tag)
        # Оставляем только последние 30 тегов
        if len(user_state.tags_history) > 30:
            user_state.tags_history = user_state.tags_history[:30]
        
        # Финализируем задачу с тегом (добавляет в чеклист по тегу и очищает pending)
        # Передаем message_id сообщения с тегом для удаления
        await finalize_task_with_tag(context.bot, chat_id, user_state, tag, additional_message_id=business_msg.message_id)
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_tag_input: {e}", exc_info=True)


async def handle_tag_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserState, chat_id: int, tag: str) -> None:
    """Обработка выбора тега из списка"""
    if not user_state.pending_task_text:
        logger.warning(f"⚠️ handle_tag_select_callback: pending_task_text отсутствует для chat_id={chat_id}")
        return
    
    # Отменяем job
    await cancel_pending_confirm_job(context.job_queue, user_state)
    
    # Поднимаем тег в истории
    if tag in user_state.tags_history:
        user_state.tags_history.remove(tag)
    user_state.tags_history.insert(0, tag)
    if len(user_state.tags_history) > 30:
        user_state.tags_history = user_state.tags_history[:30]
    
    # Финализируем задачу с тегом (добавляет в чеклист по тегу и очищает pending)
    await finalize_task_with_tag(context.bot, chat_id, user_state, tag)

