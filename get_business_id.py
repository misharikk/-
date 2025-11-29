#!/usr/bin/env python3
"""
Скрипт для получения Business Connection ID
"""
import asyncio
import os
from dotenv import load_dotenv
from telegram import Bot

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

async def get_business_connection_id():
    """Попытка получить Business Connection ID разными способами"""
    bot = Bot(BOT_TOKEN)
    
    print("🔍 Поиск Business Connection ID...")
    print("=" * 50)
    
    # Способ 1: Через get_me (базовая информация)
    try:
        me = await bot.get_me()
        print(f"✅ Бот: @{me.username}")
        print(f"   ID: {me.id}")
    except Exception as e:
        print(f"❌ Ошибка получения информации о боте: {e}")
    
    print("\n📋 Инструкция для получения Business Connection ID:")
    print("=" * 50)
    print("1. Откройте Telegram на Premium аккаунте")
    print("2. Перейдите: Настройки → Telegram для бизнеса → Чат-боты")
    print("3. Найдите бота @tasker3000_bot в списке")
    print("4. Нажмите на него - должен быть показан Connection ID")
    print("\nИЛИ")
    print("\n5. Откройте @BotFather")
    print("6. Отправьте /mybots")
    print("7. Выберите @tasker3000_bot")
    print("8. Проверьте все сообщения от BotFather - там должен быть Connection ID")
    print("\nИЛИ")
    print("\n9. Попробуйте использовать Bot API напрямую через браузер:")
    print(f"   https://api.telegram.org/bot{BOT_TOKEN}/getUpdates")
    print("   (НЕ ДЕЛАЙТЕ ЭТО, если не уверены - это покажет все обновления)")
    print("\n" + "=" * 50)
    print("\n💡 После получения ID отправьте боту:")
    print("   /set_business_connection <ваш_connection_id>")

if __name__ == "__main__":
    asyncio.run(get_business_connection_id())

