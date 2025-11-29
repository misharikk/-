#!/bin/bash
# Скрипт для безопасного запуска бота

cd "$(dirname "$0")"

echo "🛑 Остановка всех запущенных экземпляров бота..."
pkill -9 -f "python.*main.py" 2>/dev/null
sleep 2

echo "🧹 Очистка webhook..."
python3 -c "
import os
from dotenv import load_dotenv
from telegram import Bot
import asyncio

load_dotenv()
bot = Bot(os.getenv('BOT_TOKEN'))
asyncio.run(bot.delete_webhook(drop_pending_updates=True))
print('✅ Webhook очищен')
"

echo "🚀 Запуск бота..."
nohup python3 main.py > bot_output.log 2>&1 &

sleep 3

if ps aux | grep -q "[p]ython3 main.py"; then
    echo "✅ Бот успешно запущен!"
    echo ""
    echo "📋 Последние логи:"
    tail -5 bot.log 2>/dev/null | tail -3
    echo ""
    echo "Для просмотра логов в реальном времени: tail -f bot.log"
else
    echo "❌ Бот не запустился. Проверьте ошибки:"
    tail -10 bot_output.log
fi
