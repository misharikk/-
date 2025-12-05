#!/bin/bash

# Скрипт проверки статуса бота
# Проверяет, запущен ли systemd-сервис bot.service

SERVICE="bot.service"

# Цвета для вывода
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "Проверка статуса сервиса: $SERVICE"
echo ""

if systemctl is-active --quiet "$SERVICE"; then
    echo -e "${GREEN}✅ Бот запущен (systemd: $SERVICE)${NC}"
    echo ""
    echo "Статус сервиса:"
    systemctl status "$SERVICE" --no-pager -l | head -n 10
else
    echo -e "${RED}❌ Бот не запущен (systemd: $SERVICE)${NC}"
    echo ""
    echo -e "${YELLOW}Последние логи из journald:${NC}"
    sudo journalctl -u "$SERVICE" -n 20 --no-pager
    echo ""
    echo -e "${YELLOW}Последние строки из лог-файлов:${NC}"
    if [ -f /var/log/bot/bot.log ]; then
        echo "--- /var/log/bot/bot.log (последние 10 строк) ---"
        sudo tail -n 10 /var/log/bot/bot.log
    else
        echo "⚠️  Файл /var/log/bot/bot.log не найден"
    fi
    if [ -f /var/log/bot/bot.err.log ]; then
        echo ""
        echo "--- /var/log/bot/bot.err.log (последние 10 строк) ---"
        sudo tail -n 10 /var/log/bot/bot.err.log
    else
        echo "⚠️  Файл /var/log/bot/bot.err.log не найден"
    fi
fi


