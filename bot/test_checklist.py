import asyncio
import os
from dotenv import load_dotenv
from telegram import Bot, InputChecklist, InputChecklistTask

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

async def test_checklist():
    """Тест создания чеклиста"""
    bot = Bot(BOT_TOKEN)
    
    # Получаем информацию о боте
    me = await bot.get_me()
    print(f"Бот: @{me.username}")
    
    # Создаем тестовый чеклист
    tasks = [
        InputChecklistTask(id=1, text="Проснулся? Улыбнулся?")
    ]
    
    checklist = InputChecklist(
        title="2025-11-28",
        tasks=tasks,
        others_can_add_tasks=False,
        others_can_mark_tasks_as_done=True
    )
    
    print("Попытка отправить чеклист...")
    
    # ПРИМЕЧАНИЕ: замените CHAT_ID на ваш реальный chat_id
    # Чтобы узнать свой chat_id, можно использовать @userinfobot в Telegram
    CHAT_ID = None  # Вставьте сюда ваш chat_id
    
    if CHAT_ID:
        try:
            message = await bot.send_checklist(
                chat_id=CHAT_ID,
                checklist=checklist
            )
            print(f"✅ Чеклист отправлен! message_id={message.message_id}")
        except Exception as e:
            print(f"❌ Ошибка: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("⚠️  Укажите CHAT_ID в скрипте для тестирования")
        print("Чтобы узнать chat_id, отправьте /start боту @userinfobot в Telegram")

if __name__ == "__main__":
    asyncio.run(test_checklist())

