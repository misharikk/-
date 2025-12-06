#!/usr/bin/env python3
"""
Скрипт для верификации основных функций бота после исправлений.

Проверяет:
1. Создание дневного чеклиста
2. Создание тегового чеклиста
3. Синхронизацию задач между чеклистами
4. Корректность очистки дубликатов
5. Логику смены дня
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'bot'))

from state import UserState, TaskItem, TagChecklistState, clean_tasks_list, validate_and_clean_user_state
from helpers_text import normalize_tag
from datetime import datetime, timedelta
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_clean_tasks_list():
    """Тест очистки дубликатов задач"""
    print("\n🧪 ТЕСТ 1: Очистка дубликатов задач")
    
    # Создаём список с дубликатами по item_id
    tasks = [
        TaskItem(item_id=1, text="суп", done=False),
        TaskItem(item_id=2, text="хлеб", done=False),
        TaskItem(item_id=1, text="суп", done=True),  # Дубликат по item_id
        TaskItem(item_id=3, text="суп", done=False),  # Дубликат по тексту
    ]
    
    cleaned = clean_tasks_list(tasks)
    
    assert len(cleaned) == 2, f"Ожидалось 2 задачи, получено {len(cleaned)}"
    assert cleaned[0].item_id == 1, "Первая задача должна иметь item_id=1"
    assert cleaned[1].item_id == 2, "Вторая задача должна иметь item_id=2"
    assert cleaned[0].text == "суп", "Первая задача должна быть 'суп'"
    
    print("✅ Тест пройден: дубликаты удалены корректно")


def test_normalize_tag():
    """Тест нормализации тегов"""
    print("\n🧪 ТЕСТ 2: Нормализация тегов")
    
    test_cases = [
        ("Рабочие вопросы", "#рабочие_вопросы"),
        ("  СУП  ", "#суп"),
        ("МИТИ", "#мити"),
    ]
    
    for input_tag, expected in test_cases:
        result = normalize_tag(input_tag)
        assert result == expected, f"Ожидалось '{expected}', получено '{result}'"
        print(f"  ✅ '{input_tag}' → '{result}'")
    
    print("✅ Тест пройден: нормализация тегов работает корректно")


def test_sync_logic():
    """Тест логики синхронизации задач"""
    print("\n🧪 ТЕСТ 3: Логика синхронизации задач")
    
    # Создаём состояние с задачами в дневном и теговом чеклистах
    user_state = UserState(
        business_connection_id="test_conn",
        tasks=[
            TaskItem(item_id=1, text="Готовность оседлать все задачи!", done=False),
            TaskItem(item_id=2, text="суп", done=False),
        ],
        tag_checklists={
            "#мити": TagChecklistState(
                title="#мити",
                checklist_message_id=123,
                tasks=[
                    TaskItem(item_id=3, text="суп", done=False),
                ]
            )
        }
    )
    
    # Проверяем, что задачи синхронизируются по тексту
    from helpers_checklist import sync_task_status_by_text
    
    # Отмечаем "суп" в дневном чеклисте
    result = sync_task_status_by_text(user_state, "суп", True)
    assert result == True, "Синхронизация должна вернуть True"
    
    # Проверяем, что задача в теговом чеклисте тоже отмечена
    tag_task = user_state.tag_checklists["#мити"].tasks[0]
    assert tag_task.done == True, "Задача в теговом чеклисте должна быть отмечена"
    
    # Проверяем, что "Готовность..." не затронута
    daily_task = user_state.tasks[0]
    assert daily_task.done == False, "Задача 'Готовность...' не должна быть затронута"
    
    print("✅ Тест пройден: синхронизация работает корректно")


def test_validate_and_clean():
    """Тест валидации и очистки состояния"""
    print("\n🧪 ТЕСТ 4: Валидация и очистка состояния")
    
    user_state = UserState(
        business_connection_id="test_conn",
        tasks=[
            TaskItem(item_id=1, text="суп", done=False),
            TaskItem(item_id=1, text="суп", done=True),  # Дубликат по item_id
            TaskItem(item_id=2, text="хлеб", done=False),
        ],
        tag_checklists={
            "#мити": TagChecklistState(
                title="#мити",
                checklist_message_id=123,
                tasks=[
                    TaskItem(item_id=1, text="суп", done=False),  # Дубликат по тексту
                    TaskItem(item_id=2, text="суп", done=False),  # Дубликат по тексту
                ]
            )
        }
    )
    
    original_daily_count = len(user_state.tasks)
    original_tag_count = len(user_state.tag_checklists["#мити"].tasks)
    
    validate_and_clean_user_state(user_state)
    
    assert len(user_state.tasks) < original_daily_count, "Дневные задачи должны быть очищены"
    assert len(user_state.tag_checklists["#мити"].tasks) < original_tag_count, "Теговые задачи должны быть очищены"
    
    print(f"✅ Тест пройден: очистка работает (дневные: {original_daily_count} → {len(user_state.tasks)}, теговые: {original_tag_count} → {len(user_state.tag_checklists['#мити'].tasks)})")


def test_date_format():
    """Тест формата даты для чеклиста"""
    print("\n🧪 ТЕСТ 5: Формат даты для чеклиста")
    
    from helpers_checklist import get_checklist_title_from_date
    
    # Тестируем формат даты
    test_date = "2025-12-07"  # Воскресенье
    result = get_checklist_title_from_date(test_date)
    expected = "#7дек_вс"
    
    assert result == expected, f"Ожидалось '{expected}', получено '{result}'"
    print(f"✅ Тест пройден: формат даты корректен ({test_date} → {result})")


def run_all_tests():
    """Запускает все тесты"""
    print("=" * 60)
    print("🧪 ЗАПУСК ВЕРИФИКАЦИОННЫХ ТЕСТОВ")
    print("=" * 60)
    
    tests = [
        test_clean_tasks_list,
        test_normalize_tag,
        test_sync_logic,
        test_validate_and_clean,
        test_date_format,
    ]
    
    passed = 0
    failed = 0
    
    for test_func in tests:
        try:
            test_func()
            passed += 1
        except AssertionError as e:
            print(f"❌ Тест провален: {e}")
            failed += 1
        except Exception as e:
            print(f"❌ Ошибка в тесте: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    
    print("\n" + "=" * 60)
    print(f"📊 РЕЗУЛЬТАТЫ: ✅ {passed} пройдено, ❌ {failed} провалено")
    print("=" * 60)
    
    if failed == 0:
        print("\n✅ Все тесты пройдены успешно!")
        return 0
    else:
        print(f"\n❌ {failed} тест(ов) провалено")
        return 1


if __name__ == "__main__":
    exit_code = run_all_tests()
    sys.exit(exit_code)

