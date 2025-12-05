"""
Модуль для ежедневных отчётов и переноса невыполненных задач.
"""

import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional

from state import UserState, TaskItem, TagChecklistState, save_user_state
from helpers_checklist import get_today_human_date, get_human_date_from_iso, create_checklist_for_user, add_task_to_tag_checklist, rebuild_tag_checklist_for_user
from helpers_text import get_user_local_date
from helpers_delete import safe_delete

logger = logging.getLogger(__name__)

# Путь к папке с архивами (в корне проекта)
PROJECT_ROOT = Path(__file__).parent.parent
ARCHIVE_DIR = PROJECT_ROOT / "archive"


def calc_minutes_until_midnight_from_user_time(user_state: UserState) -> int:
    """
    Вычисляет, через сколько минут наступит локальная полуночь пользователя.
    Если user_state.time установлено, использует его как текущее локальное время пользователя.
    Иначе использует текущее локальное время (datetime.utcnow() + timezone_offset_minutes).
    
    Примеры:
    - Если user_state.time = "23:58" → 2 минуты
    - Если user_state.time = "22:30" → 90 минут
    - Если user_state.time = "00:00" → 24 * 60 минут (целые сутки)
    """
    from datetime import datetime, timedelta
    
    # Если есть user_state.time, используем его как текущее локальное время пользователя
    if user_state.time:
        try:
            h, m = map(int, user_state.time.split(":"))
            current_minutes = h * 60 + m
        except Exception:
            # Если не удалось распарсить, вычисляем на основе offset
            now = datetime.utcnow()
            offset_minutes = getattr(user_state, "timezone_offset_minutes", 0) or 0
            user_now = now + timedelta(minutes=offset_minutes)
            current_minutes = user_now.hour * 60 + user_now.minute
    else:
        # Вычисляем текущее локальное время пользователя
        now = datetime.utcnow()
        offset_minutes = getattr(user_state, "timezone_offset_minutes", 0) or 0
        user_now = now + timedelta(minutes=offset_minutes)
        current_minutes = user_now.hour * 60 + user_now.minute
    
    # Вычисляем минуты до полуночи
    minutes_to_midnight = (24 * 60 - current_minutes) % (24 * 60)
    if minutes_to_midnight == 0:
        minutes_to_midnight = 24 * 60  # Если уже полночь, следующая полночь через 24 часа
    
    return minutes_to_midnight


def generate_daily_report(user_state: UserState) -> str:
    """
    Генерирует текстовый отчёт в формате Markdown.
    Показывает только выполненные задачи (task.done is True).
    
    Формат:
    **3 декабря**
    
    [✅] Поесть
    [✅] Погулять
    """
    if not user_state.date:
        return "**Дата не указана**\n\nНет задач для отчёта."
    
    human_date = get_human_date_from_iso(user_state.date)
    
    # Логирование для диагностики
    total_daily_tasks = len(user_state.tasks)
    completed_daily_tasks = [task for task in user_state.tasks if task.done]
    completed_daily_count = len(completed_daily_tasks)
    
    total_tag_tasks = sum(len(tag_state.tasks) for tag_state in user_state.tag_checklists.values())
    completed_tag_count = sum(len([t for t in tag_state.tasks if t.done]) for tag_state in user_state.tag_checklists.values())
    
    logger.info(f"📊 generate_daily_report: дата={user_state.date}, дневных задач всего={total_daily_tasks}, выполненных={completed_daily_count}, теговых задач всего={total_tag_tasks}, выполненных={completed_tag_count}")
    
    report_lines = [f"**{human_date}**", ""]
    
    # Собираем только выполненные задачи из дневного чеклиста
    for task in completed_daily_tasks:
        report_lines.append(f"[✅] {task.text}")
        logger.debug(f"  ✓ Дневная задача: {task.text[:50]} (done={task.done})")
    
    # Собираем выполненные задачи из теговых чеклистов
    completed_tag_tasks = {}
    for tag, tag_state in user_state.tag_checklists.items():
        completed_in_tag = [task for task in tag_state.tasks if task.done]
        logger.info(f"🔍 Тег '{tag}': всего задач={len(tag_state.tasks)}, выполненных={len(completed_in_tag)}")
        if completed_in_tag:
            completed_tag_tasks[tag] = completed_in_tag
            logger.info(f"  ✓ Тег '{tag}': {len(completed_in_tag)} выполненных задач: {[t.text[:30] for t in completed_in_tag]}")
        else:
            # Логируем все задачи тега для диагностики
            for task in tag_state.tasks:
                logger.info(f"  - Задача тега '{tag}': item_id={task.item_id}, done={task.done}, text='{task.text[:50]}'")
    
    # Добавляем выполненные задачи из теговых чеклистов
    for tag, completed_tasks in completed_tag_tasks.items():
        report_lines.append("")
        report_lines.append(f"**{tag}**")
        for task in completed_tasks:
            report_lines.append(f"[✅] {task.text}")
    
    # Проверяем, есть ли хотя бы одна выполненная задача
    if not completed_daily_tasks and not completed_tag_tasks:
        logger.info(f"⚠️ Нет выполненных задач для отчёта: дневных={completed_daily_count}, теговых={completed_tag_count}")
        return f"**{human_date}**\n\nЗа сегодня нет выполненных задач."
    
    report_text = "\n".join(report_lines)
    logger.info(f"✅ Отчёт сгенерирован: {len(completed_daily_tasks)} дневных + {sum(len(tasks) for tasks in completed_tag_tasks.values())} теговых выполненных задач")
    return report_text


async def close_day_for_user(bot, chat_id: int, user_state: UserState = None) -> None:
    """
    Закрывает день для пользователя:
    - Создаёт отчёт (только выполненные задачи) - ДО фильтрации задач
    - Отправляет отчёт пользователю
    - Удаляет нативные чеклисты
    - Подготавливает невыполненные задачи для переноса
    - Сохраняет отчёт в файл
    
    ВАЖНО: использует user_state.date (который установлен через get_user_local_date)
    Если user_state не передан, загружает актуальное состояние из базы.
    """
    try:
        # Загружаем актуальное состояние, если не передано
        if user_state is None:
            from state import load_user_state
            user_state = load_user_state(chat_id)
            if not user_state:
                logger.error(f"❌ Не удалось загрузить user_state для chat_id={chat_id}")
                return
        
        if not user_state.date:
            logger.info(f"📌 У chat_id={chat_id} нет установленной даты, нечего закрывать")
            return
        
        # Проверяем и логируем текущее состояние перед генерацией отчёта
        current_calculated_date = get_user_local_date(user_state)
        logger.info(f"📅 close_day_for_user: user_state.date={user_state.date}, вычисленная дата={current_calculated_date}, всего дневных задач={len(user_state.tasks)}, теговых чеклистов={len(user_state.tag_checklists)}")
        
        # Подсчитываем выполненные задачи ДО генерации отчёта
        completed_before = sum(1 for task in user_state.tasks if task.done)
        completed_tag_before = sum(len([t for t in tag_state.tasks if t.done]) for tag_state in user_state.tag_checklists.values())
        logger.info(f"📊 Состояние ДО генерации отчёта: выполненных дневных={completed_before}, теговых={completed_tag_before}")
        
        # 1. Генерируем отчёт ДО фильтрации задач (чтобы включить все выполненные)
        # ВАЖНО: отчёт должен генерироваться из исходного состояния с выполненными задачами
        report = generate_daily_report(user_state)
        logger.info(f"📋 Отчёт сгенерирован для chat_id={chat_id}, дата={user_state.date}, длина={len(report)} символов")
        
        # 2. Отправляем отчёт пользователю
        try:
            await bot.send_message(
                business_connection_id=user_state.business_connection_id,
                chat_id=chat_id,
                text=report,
                parse_mode="Markdown",
            )
            logger.info(f"✅ Отчёт отправлен пользователю chat_id={chat_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка при отправке отчёта для chat_id={chat_id}: {e}", exc_info=True)
        
        # 3. Сохраняем отчёт в файл
        try:
            ARCHIVE_DIR.mkdir(exist_ok=True)
            archive_file = ARCHIVE_DIR / f"{user_state.date}.txt"
            
            # Добавляем chat_id в начало файла для идентификации
            with open(archive_file, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"chat_id: {chat_id}\n")
                f.write(f"Дата: {user_state.date}\n")
                f.write(f"{'='*60}\n\n")
                f.write(report)
                f.write("\n\n")
            
            logger.info(f"✅ Отчёт сохранён в файл: {archive_file}")
        except Exception as e:
            logger.error(f"❌ Ошибка при сохранении отчёта в файл для chat_id={chat_id}: {e}", exc_info=True)
        
        # 4. Удаляем нативный дневной чеклист
        if user_state.checklist_message_id:
            await safe_delete(
                bot,
                user_state.business_connection_id,
                chat_id,
                user_state.checklist_message_id,
            )
            logger.info(f"✅ Дневной чеклист удалён для chat_id={chat_id}, message_id={user_state.checklist_message_id}")
        
        # 5. Удаляем все теговые чеклисты
        for tag, tag_state in user_state.tag_checklists.items():
            if tag_state.checklist_message_id:
                await safe_delete(
                    bot,
                    user_state.business_connection_id,
                    chat_id,
                    tag_state.checklist_message_id,
                )
                logger.info(f"✅ Теговый чеклист '{tag}' удалён для chat_id={chat_id}, message_id={tag_state.checklist_message_id}")
        
        # 6. Оставляем только невыполненные задачи для переноса
        # Дневные задачи
        unfinished_daily_tasks = [task for task in user_state.tasks if not task.done]
        completed_daily_count = len([task for task in user_state.tasks if task.done])
        
        logger.info(f"📊 Фильтрация дневных задач: всего={len(user_state.tasks)}, выполненных={completed_daily_count}, невыполненных={len(unfinished_daily_tasks)}")
        
        # Перенумеровываем задачи (начинаем с 1)
        for idx, task in enumerate(unfinished_daily_tasks, start=1):
            task.item_id = idx
        
        user_state.tasks = unfinished_daily_tasks
        
        # Теговые чеклисты - оставляем только с невыполненными задачами
        new_tag_checklists = {}
        for tag, tag_state in user_state.tag_checklists.items():
            unfinished_tag_tasks = [task for task in tag_state.tasks if not task.done]
            completed_tag_tasks_count = len([task for task in tag_state.tasks if task.done])
            
            logger.info(f"🔍 Тег '{tag}': всего задач={len(tag_state.tasks)}, выполненных={completed_tag_tasks_count}, невыполненных={len(unfinished_tag_tasks)}")
            
            if unfinished_tag_tasks:
                # Перенумеровываем задачи
                for idx, task in enumerate(unfinished_tag_tasks, start=1):
                    task.item_id = idx
                new_tag_checklists[tag] = TagChecklistState(
                    title=tag,
                    checklist_message_id=None,  # Будет создан новый чеклист
                    tasks=unfinished_tag_tasks,
                )
                logger.info(f"  ✅ Тег '{tag}' переносится в новый день с {len(unfinished_tag_tasks)} невыполненными задачами")
            else:
                logger.info(f"  ⏭️ Тег '{tag}' не переносится - все задачи выполнены")
        
        user_state.tag_checklists = new_tag_checklists
        
        # Сбрасываем checklist_message_id дневного чеклиста (будет создан новый)
        user_state.checklist_message_id = None
        
        # ВАЖНО: НЕ меняем user_state.date и НЕ устанавливаем last_closed_date здесь
        # Это делается в check_and_handle_new_day для идемпотентности
        
        # Сохраняем состояние
        save_user_state(chat_id, user_state)
        logger.info(f"✅ День закрыт для chat_id={chat_id}: {len(unfinished_daily_tasks)} невыполненных дневных задач, {len(new_tag_checklists)} теговых чеклистов")
        
    except Exception as e:
        logger.error(f"❌ Ошибка при закрытии дня для chat_id={chat_id}: {e}", exc_info=True)


async def check_and_handle_new_day(bot, chat_id: int, user_state: UserState) -> None:
    """
    Проверяет, произошла ли смена дня для пользователя, и обрабатывает её.
    Вызывается периодически для каждого пользователя.
    
    Логика:
    - Вычисляем текущую системную дату на основе локального времени пользователя
    - Если дата сменилась → закрываем старый день, открываем новый
    - Используем last_closed_date и last_opened_date для идемпотентности
    
    ВАЖНО: Основной триггер смены дня теперь — handle_user_midnight (индивидуальные job'ы для каждого пользователя).
    Эта функция служит как резервный механизм (страховка на случай рестарта бота или потерянных job'ов).
    """
    try:
        # Проверяем, что у пользователя установлено время
        if not user_state.time or not hasattr(user_state, "timezone_offset_minutes"):
            logger.debug(f"⏭️ Пропуск проверки смены дня для chat_id={chat_id}: время не установлено")
            return
        
        current_date = get_user_local_date(user_state)
        
        logger.debug(f"🔍 Проверка смены дня для chat_id={chat_id}: date={user_state.date}, current_date={current_date}, time={user_state.time}, offset={user_state.timezone_offset_minutes}")
        
        # Если нет предыдущей даты — устанавливаем
        if user_state.date is None:
            logger.info(f"📅 Первая инициализация даты для chat_id={chat_id}: {current_date}")
            user_state.date = current_date
            user_state.last_opened_date = current_date
            user_state.last_closed_date = current_date
            save_user_state(chat_id, user_state)
            return
        
        # Если день не сменился — ничего не делаем
        if current_date == user_state.date:
            logger.debug(f"⏭️ День не сменился для chat_id={chat_id}: date={user_state.date}, current={current_date}")
            return
        
        # Обнаружена смена дня
        logger.info(f"🔄 Обнаружена смена дня для chat_id={chat_id}: {user_state.date} → {current_date}")
        
        # День сменился → закрываем старый
        if user_state.last_closed_date != user_state.date:
            logger.info(f"🔄 Закрытие дня для chat_id={chat_id}: last_closed_date={user_state.last_closed_date}, date={user_state.date}")
            await close_day_for_user(bot, chat_id, user_state)
            user_state.last_closed_date = user_state.date
            save_user_state(chat_id, user_state)
        
        # Открываем новый день
        user_state.date = current_date
        
        if user_state.last_opened_date != current_date:
            logger.info(f"🔄 Открытие нового дня для chat_id={chat_id}: last_opened_date={user_state.last_opened_date}, current={current_date}")
            await start_new_day_for_user(bot, chat_id, user_state)
            user_state.last_opened_date = current_date
            save_user_state(chat_id, user_state)
            
    except Exception as e:
        logger.error(f"❌ Ошибка при проверке смены дня для chat_id={chat_id}: {e}", exc_info=True)


async def start_new_day_for_user(bot, chat_id: int, user_state: UserState) -> None:
    """
    Создаёт новый день для пользователя:
    - Вычисляет новую дату на основе локального времени пользователя
    - Создаёт новый дневной чеклист из невыполненных задач (которые остались после close_day_for_user)
    - Создаёт теговые чеклисты для невыполненных задач
    """
    try:
        # Вычисляем текущую дату пользователя на основе локального времени
        from datetime import datetime, timedelta
        
        now = datetime.utcnow()
        offset_minutes = getattr(user_state, "timezone_offset_minutes", 0) or 0
        user_now = now + timedelta(minutes=offset_minutes)
        today_date = user_now.date().isoformat()
        
        current_date = user_state.date
        
        logger.info(f"📅 Создание нового дня для chat_id={chat_id}, дата в state={current_date}, вычисленная дата пользователя={today_date}")
        
        # ВСЕГДА используем вычисленную дату пользователя для нового дня
        user_state.date = today_date
        if current_date != today_date:
            logger.info(f"🔄 Обновление даты: {current_date} → {today_date}")
        
        # Если нет невыполненных задач, добавляем автоматическую задачу
        if not user_state.tasks:
            first_task = TaskItem(item_id=1, text="улыбнуться себе в зеркало", done=False)
            user_state.tasks = [first_task]
            save_user_state(chat_id, user_state)
        
        # Создаём новый дневной чеклист (checklist_message_id уже сброшен в close_day_for_user)
        await create_checklist_for_user(bot, chat_id, user_state)
        
        # Создаём теговые чеклисты для невыполненных задач
        for tag, tag_state in user_state.tag_checklists.items():
            if tag_state.tasks:
                # Восстанавливаем чеклист из уже существующих задач (не добавляем их снова)
                await rebuild_tag_checklist_for_user(bot, chat_id, user_state, tag)
        
        # Сохраняем состояние (last_opened_date обновляется в check_and_handle_new_day или handle_force_newday)
        save_user_state(chat_id, user_state)
        logger.info(f"✅ Новый день создан для chat_id={chat_id}, дата={current_date}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка при создании нового дня для chat_id={chat_id}: {e}", exc_info=True)


async def handle_user_midnight(context) -> None:
    """
    Job, которая вызывается в 'полночь' пользователя:
    - закрывает день
    - открывает новый
    - перепланирует себя ещё через 24 часа
    """
    try:
        from telegram.ext import CallbackContext
        
        # Получаем данные из job
        data = context.job.data or {} if hasattr(context, 'job') else {}
        chat_id = data.get("chat_id")
        if not chat_id:
            logger.warning(f"⚠️ handle_user_midnight: chat_id отсутствует в data")
            return
        
        from state import load_user_state, save_user_state
        
        user_state = load_user_state(chat_id)
        if not user_state:
            logger.warning(f"⚠️ handle_user_midnight: user_state не найден для chat_id={chat_id}")
            return
        
        # Получаем bot из context
        bot = getattr(context, 'bot', None)
        if not bot and hasattr(context, 'application'):
            bot = getattr(context.application, 'bot', None)
        
        if not bot:
            logger.error(f"❌ handle_user_midnight: не удалось получить bot из context для chat_id={chat_id}")
            return
        
        logger.info(f"🕛 Смена дня для пользователя chat_id={chat_id} (midnight job)")
        
        # 1. Закрываем день
        await close_day_for_user(bot, chat_id, user_state)
        
        # Перезагружаем состояние после закрытия дня
        user_state = load_user_state(chat_id)
        if not user_state:
            logger.error(f"❌ handle_user_midnight: не удалось загрузить user_state после close_day_for_user для chat_id={chat_id}")
            return
        
        # 2. Открываем новый день
        await start_new_day_for_user(bot, chat_id, user_state)
        
        # Перезагружаем состояние после открытия дня
        user_state = load_user_state(chat_id)
        if not user_state:
            logger.error(f"❌ handle_user_midnight: не удалось загрузить user_state после start_new_day_for_user для chat_id={chat_id}")
            return
        
        # 3. Перепланируем следующий запуск через 24 часа
        job_queue = getattr(context, 'job_queue', None)
        if not job_queue and hasattr(context, 'application'):
            job_queue = getattr(context.application, 'job_queue', None)
        
        if job_queue:
            job_name = f"user_midnight_{chat_id}"
            
            job_queue.run_once(
                handle_user_midnight,
                when=24 * 60 * 60,  # 24 часа в секундах
                name=job_name,
                data={"chat_id": chat_id},
            )
            
            user_state.next_rollover_job_name = job_name
            save_user_state(chat_id, user_state)
            logger.info(f"✅ Следующий midnight job запланирован для chat_id={chat_id} через 24 часа")
        else:
            logger.error(f"❌ handle_user_midnight: job_queue отсутствует для chat_id={chat_id}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_user_midnight: {e}", exc_info=True)


def schedule_user_midnight_job(job_queue, chat_id: int, user_state: UserState) -> None:
    """
    Ставит/переставляет job смены дня для конкретного пользователя
    на 'его полуночь', исходя из user_state.time как текущего времени.
    """
    try:
        # 0. Если есть старый job — снимаем
        if user_state.next_rollover_job_name:
            try:
                jobs = job_queue.get_jobs_by_name(user_state.next_rollover_job_name)
                for job in jobs:
                    job.schedule_removal()
                logger.info(f"🗑️ Удалён старый midnight job '{user_state.next_rollover_job_name}' для chat_id={chat_id}")
            except Exception as e:
                logger.warning(f"⚠️ Не удалось удалить старый job '{user_state.next_rollover_job_name}': {e}")
        
        minutes_to_midnight = calc_minutes_until_midnight_from_user_time(user_state)
        delay_seconds = minutes_to_midnight * 60
        
        job_name = f"user_midnight_{chat_id}"
        
        job_queue.run_once(
            handle_user_midnight,
            when=delay_seconds,
            name=job_name,
            data={"chat_id": chat_id},
        )
        
        user_state.next_rollover_job_name = job_name
        from state import save_user_state
        save_user_state(chat_id, user_state)
        
        logger.info(f"✅ Midnight job запланирован для chat_id={chat_id}: через {minutes_to_midnight} минут (время пользователя: {user_state.time})")
    except Exception as e:
        logger.error(f"❌ Ошибка в schedule_user_midnight_job для chat_id={chat_id}: {e}", exc_info=True)
