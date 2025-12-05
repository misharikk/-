#!/bin/bash
cd "$(dirname "$0")/.."

echo "🛑 Остановка всех экземпляров бота..."
# Убиваем все процессы Python, связанные с main.py
pkill -9 -f "python.*bot/main.py" 2>/dev/null
pkill -9 -f "python.*main.py" 2>/dev/null
pkill -9 -f "main.py" 2>/dev/null
killall -9 Python 2>/dev/null

# Ждем 3 секунды
sleep 3

# Проверяем, что все остановлено
if ps aux | grep -q "[p]ython.*bot/main.py"; then
    echo "❌ Все еще есть запущенные процессы!"
    ps aux | grep "[p]ython.*bot/main.py"
    echo "Попытка убить вручную..."
    ps aux | grep "[p]ython.*bot/main.py" | awk '{print $2}' | xargs kill -9 2>/dev/null
    sleep 2
fi

echo "🧹 Очистка webhook..."
python3 -c "
import os
import sys
sys.path.insert(0, '.')
from dotenv import load_dotenv
from telegram import Bot
import asyncio
load_dotenv()
bot = Bot(os.getenv('BOT_TOKEN'))
asyncio.run(bot.delete_webhook(drop_pending_updates=True))
print('✅ Webhook очищен')
"

echo "⏳ Ожидание 10 секунд для освобождения Telegram API..."
sleep 10

echo "🚀 Запуск бота..."
nohup python3 bot/main.py > bot_output.log 2>&1 &
BOT_PID=$!
echo $BOT_PID > bot.pid

sleep 5

if ps -p $BOT_PID > /dev/null 2>&1; then
    echo "✅ Бот успешно запущен! PID: $BOT_PID"
    echo ""
    echo "📋 Последние логи:"
    tail -5 bot.log 2>/dev/null | tail -3 || tail -5 bot_output.log | grep -E "(Запуск|version|INFO)" | head -3
    echo ""
    echo "Для просмотра логов в реальном времени: tail -f bot.log"
else
    echo "❌ Бот не запустился. Проверьте ошибки:"
    tail -15 bot_output.log
fi

