"""
Модуль для ежедневных отчётов и переноса невыполненных задач.
"""

import logging
from pathlib import Path
from datetime import datetime, timedelta, time
from typing import List, Optional, Dict

from state import UserState, TaskItem, TagChecklistState, save_user_state
from helpers_checklist import get_today_human_date, get_human_date_from_iso, create_checklist_for_user, add_task_to_tag_checklist, rebuild_tag_checklist_for_user
from helpers_text import get_user_local_date
from helpers_delete import safe_delete

logger = logging.getLogger(__name__)

# Путь к папке с архивами (в корне проекта)
PROJECT_ROOT = Path(__file__).parent.parent
ARCHIVE_DIR = PROJECT_ROOT / "archive"


def compute_local_datetime_and_offset(now_utc: datetime, user_time_str: str) -> tuple[datetime, int]:
    """
    Вычисляет локальное datetime пользователя и UTC-смещение на основе введенного времени.
    
    Args:
        now_utc: текущее время сервера в UTC
        user_time_str: строка вида 'HH:MM', которую ввёл пользователь как своё ЛОКАЛЬНОЕ время
    
    Returns:
        tuple[datetime, int]: (local_dt, utc_offset_minutes)
        - local_dt: локальное datetime пользователя
        - utc_offset_minutes: смещение в минутах относительно UTC (в диапазоне [-12ч, +14ч])
    
    Алгоритм:
    - Парсит HH:MM из user_time_str
    - Вычисляет смещение так, чтобы (now_utc + offset).time() == (HH:MM)
    - Нормализует смещение к диапазону [-12ч, +12ч]
    """
    from helpers_text import parse_time_string
    
    # Парсим время
    parsed = parse_time_string(user_time_str)
    if not parsed:
        raise ValueError(f"Неверный формат времени: {user_time_str}")
    
    h, m = map(int, parsed.split(":"))
    user_minutes = h * 60 + m
    utc_minutes = now_utc.hour * 60 + now_utc.minute
    
    # Вычисляем raw_delta
    raw_delta = (user_minutes - utc_minutes) % 1440
    
    # Нормализуем к диапазону [-12h, +12h]
    # (raw_delta + 720) % 1440 - 720 переводит [0, 1440) в [-720, 720)
    delta = (raw_delta + 720) % 1440 - 720
    
    # Вычисляем локальное datetime
    local_dt = now_utc + timedelta(minutes=delta)
    
    return local_dt, delta


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


def generate_daily_report(user_state: UserState, report_date: str = None) -> str:
    """
    Генерирует текстовый отчёт в формате Markdown.
    Показывает только выполненные задачи (task.done is True).
    
    Формат:
    **3 декабря**
    
    [✅] Поесть
    [✅] Погулять
    
    Args:
        user_state: Состояние пользователя с задачами
        report_date: Дата для отчёта (если не указана, используется user_state.date)
    """
    # Используем переданную дату или дату из user_state
    date_for_report = report_date or user_state.date
    if not date_for_report:
        return "**Дата не указана**\n\nНет задач для отчёта."
    
    # Используем формат "#6дек_сб" вместо "6 декабря"
    from helpers_checklist import get_checklist_title_from_date
    human_date = get_checklist_title_from_date(date_for_report)
    
    # Логирование для диагностики
    total_daily_tasks = len(user_state.tasks)
    completed_daily_tasks = [task for task in user_state.tasks if task.done]
    completed_daily_count = len(completed_daily_tasks)
    
    total_tag_tasks = sum(len(tag_state.tasks) for tag_state in user_state.tag_checklists.values())
    completed_tag_count = sum(len([t for t in tag_state.tasks if t.done]) for tag_state in user_state.tag_checklists.values())
    
    logger.info(f"📊 generate_daily_report: дата отчёта={date_for_report}, дневных задач всего={total_daily_tasks}, выполненных={completed_daily_count}, теговых задач всего={total_tag_tasks}, выполненных={completed_tag_count}")
    
    report_lines = [f"**{human_date}**", ""]
    
    # Собираем только выполненные задачи из дневного чеклиста
    # ИСКЛЮЧАЕМ автоматическую задачу "улыбнуться себе в зеркало" из отчёта
    for task in completed_daily_tasks:
        # Пропускаем автоматическую задачу, даже если она выполнена
        if task.text == "улыбнуться себе в зеркало":
            logger.debug(f"  ⏭️ Пропускаем автоматическую задачу в отчёте: '{task.text}'")
            continue
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
        
        # ЗАЩИТА ОТ ДВОЙНОГО ЗАКРЫТИЯ: проверяем, не закрыт ли уже день для этой даты
        if user_state.last_closed_date == current_calculated_date:
            logger.info(f"⏭️ День уже закрыт для chat_id={chat_id}, last_closed_date={user_state.last_closed_date}, current_date={current_calculated_date}")
            return
        
        # Сохраняем дату закрытого дня ДО обновления на новую дату
        closed_date = user_state.date
        
        # Обновляем user_state.date на актуальную вычисленную дату (для нового дня)
        if user_state.date != current_calculated_date:
            logger.info(f"🔄 Обновление даты для нового дня: {user_state.date} → {current_calculated_date}")
            user_state.date = current_calculated_date
            save_user_state(chat_id, user_state)
        
        # Подсчитываем выполненные задачи ДО генерации отчёта
        completed_before = sum(1 for task in user_state.tasks if task.done)
        completed_tag_before = sum(len([t for t in tag_state.tasks if t.done]) for tag_state in user_state.tag_checklists.values())
        logger.info(f"📊 Состояние ДО генерации отчёта: выполненных дневных={completed_before}, теговых={completed_tag_before}")
        
        # 1. Генерируем отчёт ДО фильтрации задач (чтобы включить все выполненные)
        # ВАЖНО: отчёт должен генерироваться из исходного состояния с выполненными задачами
        # Используем дату закрытого дня (closed_date), а не новую дату
        report = generate_daily_report(user_state, report_date=closed_date)
        logger.info(f"📋 Отчёт сгенерирован для chat_id={chat_id}, дата закрытого дня={closed_date}, длина={len(report)} символов")
        
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
            # Используем дату закрытого дня для имени файла
            archive_file = ARCHIVE_DIR / f"{closed_date}.txt"
            with open(archive_file, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"chat_id: {chat_id}\n")
                f.write(f"Дата: {closed_date}\n")
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
        
        # 6. ЯВНО разделяем задачи на выполненные и невыполненные
        # Дневные задачи
        completed_daily_tasks: List[TaskItem] = []
        pending_daily_tasks: List[TaskItem] = []
        
        for task in user_state.tasks:
            if task.done:
                completed_daily_tasks.append(task)
            else:
                pending_daily_tasks.append(task)
        
        logger.info(f"📊 Разделение дневных задач: всего={len(user_state.tasks)}, выполненных={len(completed_daily_tasks)}, невыполненных={len(pending_daily_tasks)}")
        
        # Теговые чеклисты - разделяем задачи
        completed_tag_tasks: Dict[str, List[TaskItem]] = {}  # tag -> список выполненных задач
        pending_tag_tasks: Dict[str, List[TaskItem]] = {}    # tag -> список невыполненных задач
        
        for tag, tag_state in user_state.tag_checklists.items():
            completed_in_tag: List[TaskItem] = []
            pending_in_tag: List[TaskItem] = []
            
            for task in tag_state.tasks:
                if task.done:
                    completed_in_tag.append(task)
                else:
                    pending_in_tag.append(task)
            
            if completed_in_tag:
                completed_tag_tasks[tag] = completed_in_tag
            if pending_in_tag:
                pending_tag_tasks[tag] = pending_in_tag
            
            logger.info(f"🔍 Тег '{tag}': всего задач={len(tag_state.tasks)}, выполненных={len(completed_in_tag)}, невыполненных={len(pending_in_tag)}")
        
        # 7. В состоянии оставляем ТОЛЬКО невыполненные задачи
        # Дневные задачи
        for idx, task in enumerate(pending_daily_tasks, start=1):
            task.item_id = idx
        user_state.tasks = pending_daily_tasks
        
        # Теговые чеклисты - оставляем только с невыполненными задачами
        new_tag_checklists = {}
        for tag, pending_tasks in pending_tag_tasks.items():
            # Перенумеровываем задачи
            for idx, task in enumerate(pending_tasks, start=1):
                task.item_id = idx
            new_tag_checklists[tag] = TagChecklistState(
                title=tag,
                checklist_message_id=None,  # Будет создан новый чеклист
                tasks=pending_tasks,
            )
            logger.info(f"  ✅ Тег '{tag}' переносится в новый день с {len(pending_tasks)} невыполненными задачами")
        
        user_state.tag_checklists = new_tag_checklists
        
        # Сбрасываем checklist_message_id дневного чеклиста (будет создан новый)
        user_state.checklist_message_id = None
        
        # Обновляем last_closed_date на текущую дату (локальная дата, за которую день закрыли)
        # ВАЖНО: используем current_calculated_date, а не user_state.date, чтобы избежать проблем с устаревшей датой
        old_date = user_state.date
        user_state.last_closed_date = current_calculated_date
        
        # Сохраняем состояние
        save_user_state(chat_id, user_state)
        
        # Логируем результат закрытия дня
        completed_daily_count = len(completed_daily_tasks)
        completed_tag_count = sum(len(tasks) for tasks in completed_tag_tasks.values())
        pending_daily_count = len(pending_daily_tasks)
        pending_tag_count = sum(len(tasks) for tasks in pending_tag_tasks.values())
        
        logger.info(f"CLOSE_DAY chat_id={chat_id} date={old_date} completed_daily={completed_daily_count} pending_daily={pending_daily_count} completed_tag={completed_tag_count} pending_tag={pending_tag_count}")
        logger.info(f"✅ День закрыт для chat_id={chat_id}: {pending_daily_count} невыполненных дневных задач, {len(new_tag_checklists)} теговых чеклистов")
        
    except Exception as e:
        logger.error(f"❌ Ошибка при закрытии дня для chat_id={chat_id}: {e}", exc_info=True)


def get_user_local_datetime(user_state: UserState, now: Optional[datetime] = None) -> datetime:
    """
    Возвращает текущее локальное datetime пользователя на основе timezone_offset_minutes.
    """
    if now is None:
        now = datetime.utcnow()
    offset_minutes = getattr(user_state, "timezone_offset_minutes", 0) or 0
    return now + timedelta(minutes=offset_minutes)


async def check_and_handle_day_end_for_user(bot, chat_id: int, user_state: UserState) -> None:
    """
    Проверяет, наступил ли конец дня для пользователя, и обрабатывает его.
    Вызывается периодически (каждые 60 секунд) для каждого пользователя.
    
    Логика:
    - Вычисляем текущее локальное datetime пользователя
    - Если day_end_time установлено, local_date > last_closed_date и local_time >= day_end_time,
      тогда закрываем день и открываем новый.
    """
    try:
        # Проверяем, что у пользователя установлено day_end_time
        if not user_state.day_end_time:
            return
        
        # Вычисляем текущее локальное datetime пользователя
        user_now = get_user_local_datetime(user_state)
        local_date = user_now.date().isoformat()
        local_time = user_now.time()
        
        # Парсим day_end_time
        try:
            h, m = map(int, user_state.day_end_time.split(":"))
            day_end_time_obj = time(h, m)
        except Exception:
            logger.warning(f"⚠️ Неверный формат day_end_time для chat_id={chat_id}: {user_state.day_end_time}")
            return
        
        # Условия для авто-закрытия:
        # 1. Если local_date == last_closed_date → день уже закрыт, ничего не делаем
        if user_state.last_closed_date == local_date:
            logger.debug(f"⏭️ День уже закрыт для chat_id={chat_id}, last_closed_date={user_state.last_closed_date}, current_date={local_date}")
            return
        
        # 2. Если last_closed_date is None и local_time >= day_end_time → закрываем первый раз
        # 3. Если local_date > last_closed_date и local_time >= day_end_time → закрываем новый день
        should_close = False
        if user_state.last_closed_date is None:
            # Первое закрытие дня
            if local_time >= day_end_time_obj:
                should_close = True
        elif local_date > user_state.last_closed_date:
            # Новый день, проверяем время
            if local_time >= day_end_time_obj:
                should_close = True
        
        if not should_close:
            logger.debug(f"⏭️ Условия для закрытия дня не выполнены для chat_id={chat_id}: last_closed_date={user_state.last_closed_date}, local_date={local_date}, local_time={local_time}, day_end_time={day_end_time_obj}")
            return
        
        # Время наступило и день ещё не закрыт - закрываем день
        logger.info(f"🔄 AUTO_DAY_CLOSE chat_id={chat_id} date={local_date} (время достигло day_end_time: {local_time} >= {day_end_time_obj})")
        
        # Закрываем день (close_day_for_user сам установит last_closed_date)
        await close_day_for_user(bot, chat_id, user_state)
        
        # Перезагружаем состояние после закрытия
        from state import load_user_state
        user_state = load_user_state(chat_id)
        if not user_state:
            logger.error(f"❌ Не удалось загрузить user_state после close_day_for_user для chat_id={chat_id}")
            return
        
        # Проверяем, что день действительно закрыт
        if user_state.last_closed_date != local_date:
            logger.warning(f"⚠️ После close_day_for_user last_closed_date не обновлён: ожидали {local_date}, получили {user_state.last_closed_date}")
            return
        
        # Вычисляем дату нового дня (следующий день после закрытого)
        from datetime import datetime, timedelta
        closed_date_obj = datetime.strptime(user_state.last_closed_date, "%Y-%m-%d").date()
        next_date_obj = closed_date_obj + timedelta(days=1)
        next_date = next_date_obj.isoformat()
        
        # ЗАЩИТА ОТ ДВОЙНОГО ОТКРЫТИЯ: проверяем, не открыт ли уже день для этой даты
        if user_state.last_opened_date == next_date:
            logger.info(f"⏭️ Новый день уже открыт для chat_id={chat_id}, last_opened_date={user_state.last_opened_date}, next_date={next_date}")
            return
        
        # Открываем новый день (start_new_day_for_user сам установит last_opened_date)
        await start_new_day_for_user(bot, chat_id, user_state)
        
        # Перезагружаем состояние после открытия
        user_state = load_user_state(chat_id)
        if not user_state:
            logger.error(f"❌ Не удалось загрузить user_state после start_new_day_for_user для chat_id={chat_id}")
            return
        
        # Проверяем, что last_opened_date обновлён (start_new_day_for_user должен был это сделать)
        if user_state.last_opened_date != next_date:
            logger.warning(f"⚠️ После start_new_day_for_user last_opened_date не обновлён: ожидали {next_date}, получили {user_state.last_opened_date}")
        
        logger.info(f"🔄 AUTO_NEW_DAY chat_id={chat_id} date={next_date} completed_daily={len([t for t in user_state.tasks if t.done])} pending_daily={len([t for t in user_state.tasks if not t.done])} tag_checklists={len(user_state.tag_checklists)}")
            
    except Exception as e:
        logger.error(f"❌ Ошибка при проверке конца дня для chat_id={chat_id}: {e}", exc_info=True)


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
        # ВАЖНО: close_day_for_user сам установит last_closed_date, не трогаем его здесь
        if user_state.last_closed_date != user_state.date:
            logger.info(f"🔄 Закрытие дня для chat_id={chat_id}: last_closed_date={user_state.last_closed_date}, date={user_state.date}")
            await close_day_for_user(bot, chat_id, user_state)
            # Перезагружаем состояние после закрытия
            from state import load_user_state
            user_state = load_user_state(chat_id)
            if not user_state:
                logger.error(f"❌ Не удалось загрузить user_state после close_day_for_user для chat_id={chat_id}")
                return
        
        # Открываем новый день
        # ВАЖНО: start_new_day_for_user сам установит date и last_opened_date, не трогаем их здесь
        if user_state.last_opened_date != current_date:
            logger.info(f"🔄 Открытие нового дня для chat_id={chat_id}: last_opened_date={user_state.last_opened_date}, current={current_date}")
            await start_new_day_for_user(bot, chat_id, user_state)
            
    except Exception as e:
        logger.error(f"❌ Ошибка при проверке смены дня для chat_id={chat_id}: {e}", exc_info=True)


async def start_new_day_for_user(bot, chat_id: int, user_state: UserState) -> None:
    """
    Создаёт новый день для пользователя:
    - Вычисляет новую дату на основе локального времени пользователя
    - Создаёт новый дневной чеклист из невыполненных задач (которые остались после close_day_for_user)
    - Создаёт теговые чеклисты для невыполненных задач
    
    ВАЖНО: предполагает, что в user_state к моменту вызова уже хранятся только невыполненные задачи
    (после close_day_for_user). Выполненные задачи уже "ушли" в текстовый отчёт.
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
        
        # ВАЖНО: устанавливаем last_opened_date сразу после обновления даты
        user_state.last_opened_date = today_date
        
        # ВАЖНО: сохраняем дату ДО создания чеклиста, чтобы create_checklist_for_user использовал правильную дату
        save_user_state(chat_id, user_state)
        
        # Проверяем, что работаем только с невыполненными задачами
        # (которые остались после close_day_for_user)
        pending_daily_count = len(user_state.tasks)
        pending_tag_count = sum(len(tag_state.tasks) for tag_state in user_state.tag_checklists.values())
        logger.info(f"📊 Невыполненные задачи для нового дня: дневных={pending_daily_count}, теговых чеклистов={len(user_state.tag_checklists)}")
        
        # Если нет невыполненных задач, добавляем автоматическую задачу
        if not user_state.tasks:
            first_task = TaskItem(item_id=1, text="улыбнуться себе в зеркало", done=False)
            user_state.tasks = [first_task]
            save_user_state(chat_id, user_state)
            logger.info(f"➕ Добавлена автоматическая задача для нового дня")
        
        # Создаём новый дневной чеклист из невыполненных задач
        # (checklist_message_id уже сброшен в close_day_for_user)
        # ВАЖНО: create_checklist_for_user теперь использует актуальную дату из user_state.date
        await create_checklist_for_user(bot, chat_id, user_state)
        
        # Создаём теговые чеклисты для невыполненных задач
        # (все задачи в tag_checklists уже невыполненные после close_day_for_user)
        for tag, tag_state in user_state.tag_checklists.items():
            if tag_state.tasks:
                # Восстанавливаем чеклист из уже существующих невыполненных задач
                await rebuild_tag_checklist_for_user(bot, chat_id, user_state, tag)
                logger.info(f"✅ Теговый чеклист '{tag}' восстановлен с {len(tag_state.tasks)} невыполненными задачами")
        
        # Логируем результат открытия дня
        # ВАЖНО: last_opened_date уже установлен выше, сразу после обновления user_state.date
        pending_daily_count = len([t for t in user_state.tasks if not t.done])
        completed_daily_count = len([t for t in user_state.tasks if t.done])
        pending_tag_count = sum(len([t for t in tag_state.tasks if not t.done]) for tag_state in user_state.tag_checklists.values())
        completed_tag_count = sum(len([t for t in tag_state.tasks if t.done]) for tag_state in user_state.tag_checklists.values())
        
        logger.info(f"AUTO_NEW_DAY chat_id={chat_id} date={today_date} completed_daily={completed_daily_count} pending_daily={pending_daily_count} completed_tag={completed_tag_count} pending_tag={pending_tag_count} tag_checklists={len(user_state.tag_checklists)} last_opened_date={user_state.last_opened_date}")
        logger.info(f"✅ Новый день создан для chat_id={chat_id}, дата={today_date}")
        
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
        
        # ЗАЩИТА ОТ ДВОЙНОГО ЗАКРЫТИЯ: вычисляем текущую дату пользователя
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        offset_minutes = getattr(user_state, "timezone_offset_minutes", 0) or 0
        user_now = now + timedelta(minutes=offset_minutes)
        current_local_date = user_now.date().isoformat()
        
        # Если день уже закрыт для этой даты, пропускаем
        if user_state.last_closed_date == current_local_date:
            logger.info(f"⏭️ День уже закрыт для chat_id={chat_id}, last_closed_date={user_state.last_closed_date}, current_date={current_local_date}")
            # Всё равно перепланируем job на следующий день
            from helpers_daily import schedule_user_midnight_job
            job_queue = getattr(context, 'job_queue', None) or (getattr(context.application, 'job_queue', None) if hasattr(context, 'application') else None)
            if job_queue:
                schedule_user_midnight_job(job_queue, chat_id, user_state)
            return
        
        # 1. Закрываем день
        await close_day_for_user(bot, chat_id, user_state)
        
        # Перезагружаем состояние после закрытия дня
        user_state = load_user_state(chat_id)
        if not user_state:
            logger.error(f"❌ handle_user_midnight: не удалось загрузить user_state после close_day_for_user для chat_id={chat_id}")
            return
        
        # Проверяем, что день действительно закрыт
        if user_state.last_closed_date != current_local_date:
            logger.warning(f"⚠️ После close_day_for_user last_closed_date не обновлён: ожидали {current_local_date}, получили {user_state.last_closed_date}")
        
        # Вычисляем дату нового дня (следующий день после закрытого)
        closed_date_obj = datetime.strptime(user_state.last_closed_date, "%Y-%m-%d").date()
        next_date_obj = closed_date_obj + timedelta(days=1)
        next_date = next_date_obj.isoformat()
        
        # ЗАЩИТА ОТ ДВОЙНОГО ОТКРЫТИЯ: проверяем, не открыт ли уже день для этой даты
        if user_state.last_opened_date == next_date:
            logger.info(f"⏭️ Новый день уже открыт для chat_id={chat_id}, last_opened_date={user_state.last_opened_date}, next_date={next_date}")
            # Всё равно перепланируем job на следующий день
            from helpers_daily import schedule_user_midnight_job
            job_queue = getattr(context, 'job_queue', None) or (getattr(context.application, 'job_queue', None) if hasattr(context, 'application') else None)
            if job_queue:
                schedule_user_midnight_job(job_queue, chat_id, user_state)
            return
        
        # 2. Открываем новый день
        await start_new_day_for_user(bot, chat_id, user_state)
        
        # Перезагружаем состояние после открытия дня
        user_state = load_user_state(chat_id)
        if not user_state:
            logger.error(f"❌ handle_user_midnight: не удалось загрузить user_state после start_new_day_for_user для chat_id={chat_id}")
            return
        
        # ВАЖНО: last_opened_date уже установлен в start_new_day_for_user, не трогаем его здесь
        
        logger.info(f"🔄 AUTO_NEW_DAY chat_id={chat_id} date={next_date} completed_daily={len([t for t in user_state.tasks if t.done])} pending_daily={len([t for t in user_state.tasks if not t.done])} tag_checklists={len(user_state.tag_checklists)}")
        
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
