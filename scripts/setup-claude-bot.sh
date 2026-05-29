#!/bin/bash
#
# setup-claude-bot.sh — скрипт настройки пользователя claude-bot для запуска CLI-агентов
#
# Использование:
#   sudo ./scripts/setup-claude-bot.sh [--workdir /path] [--username claude-bot]
#

set -e

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Параметры по умолчанию
WORKDIR="/srv/git_projects"
USERNAME="claude-bot"
CLAUDE_VERSION="latest"
SHARED_GROUP="cli-proxy-workgroup"

# Переменные окружения для Anthropic
ANTHROPIC_BASE_URL=""
ANTHROPIC_API_KEY=""
ANTHROPIC_MODEL="coder-model"

# Парсинг аргументов
while [[ $# -gt 0 ]]; do
    case $1 in
        --workdir)
            WORKDIR="$2"
            shift 2
            ;;
        --username)
            USERNAME="$2"
            shift 2
            ;;
        --version)
            CLAUDE_VERSION="$2"
            shift 2
            ;;
        -h|--help)
            echo "Использование: $0 [--workdir /path] [--username claude-bot]"
            echo ""
            echo "Опции:"
            echo "  --workdir    Директория для работы (по умолчанию: /srv/git_projects)"
            echo "  --username   Имя пользователя (по умолчанию: claude-bot)"
            echo "  --version    Версия claude (по умолчанию: latest)"
            exit 0
            ;;
        *)
            echo -e "${RED}Неизвестная опция: $1${NC}"
            exit 1
            ;;
    esac
done

echo -e "${GREEN}=== Настройка пользователя ${USERNAME} для CLI-агентов ===${NC}"
echo ""

# Проверка запуска от root
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}Ошибка: скрипт должен быть запущен от root${NC}"
    exit 1
fi

# 1. Создание пользователя (если не существует)
echo -e "${YELLOW}[1/5] Проверка пользователя ${USERNAME}...${NC}"
if id "$USERNAME" &>/dev/null; then
    echo -e "${GREEN}✓ Пользователь ${USERNAME} уже существует${NC}"
else
    echo -e "${YELLOW}Создание пользователя ${USERNAME}...${NC}"
    useradd -m -s /bin/bash "$USERNAME"
    echo -e "${GREEN}✓ Пользователь создан${NC}"
fi
echo ""

# 2. Права на workdir (общая группа для root и claude-bot)
echo -e "${YELLOW}[2/6] Настройка прав на ${WORKDIR}...${NC}"

# Создаём общую группу
if getent group "$SHARED_GROUP" &>/dev/null; then
    echo -e "${GREEN}✓ Группа ${SHARED_GROUP} уже существует${NC}"
else
    echo "Создание группы ${SHARED_GROUP}..."
    groupadd "$SHARED_GROUP"
    echo -e "${GREEN}✓ Группа создана${NC}"
fi

# Добавляем пользователей в группу
usermod -aG "$SHARED_GROUP" root 2>/dev/null || true
usermod -aG "$SHARED_GROUP" "$USERNAME" 2>/dev/null || true
echo -e "${GREEN}✓ Пользователи добавлены в группу ${SHARED_GROUP}${NC}"

if [[ -d "$WORKDIR" ]]; then
    # Устанавливаем группу и setgid бит
    chgrp -R "$SHARED_GROUP" "$WORKDIR"
    chmod -R g+rwxs "$WORKDIR"
    echo -e "${GREEN}✓ Права на ${WORKDIR} установлены (setgid, группа ${SHARED_GROUP})${NC}"
else
    echo -e "${YELLOW}Директория ${WORKDIR} не существует, создаём...${NC}"
    mkdir -p "$WORKDIR"
    chgrp -R "$SHARED_GROUP" "$WORKDIR"
    chmod -R g+rwxs "$WORKDIR"
    echo -e "${GREEN}✓ Директория создана и права установлены${NC}"
fi
echo ""

# 3. Проверка наличия curl (нужен для установки claude)
echo -e "${YELLOW}[3/6] Проверка зависимостей...${NC}"
if ! command -v curl &>/dev/null; then
    echo -e "${RED}Ошибка: curl не найден. Установите: apt install curl${NC}"
    exit 1
fi
echo -e "${GREEN}✓ curl установлен${NC}"
echo ""

# 4. Установка claude для пользователя
echo -e "${YELLOW}[4/6] Установка claude для ${USERNAME}...${NC}"

# Домашняя директория пользователя
USER_HOME=$(eval echo ~"$USERNAME")

# Проверка, установлен ли уже claude
if su - "$USERNAME" -c "command -v claude" &>/dev/null; then
    echo -e "${GREEN}✓ claude уже установлен у пользователя ${USERNAME}${NC}"
    su - "$USERNAME" -c "claude --version" || true
else
    echo "Установка claude..."
    su - "$USERNAME" -c "curl -fsSL https://claude.ai/install.sh | bash"

    # Проверяем установку
    if su - "$USERNAME" -c "command -v claude" &>/dev/null; then
        echo -e "${GREEN}✓ claude установлен${NC}"
        su - "$USERNAME" -c "claude --version" || true
    else
        echo -e "${RED}Ошибка при установке claude${NC}"
        exit 1
    fi
fi
echo ""

# 5. Проверка ~/.local/bin в PATH
echo -e "${YELLOW}[5/6] Настройка PATH...${NC}"
if su - "$USERNAME" -c 'echo $PATH' | grep -q "$USER_HOME/.local/bin"; then
    echo -e "${GREEN}✓ ~/.local/bin уже в PATH${NC}"
else
    echo "Добавление ~/.local/bin в PATH..."
    echo '' >> "$USER_HOME/.bashrc"
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$USER_HOME/.bashrc"
    echo -e "${GREEN}✓ ~/.local/bin добавлен в PATH${NC}"
fi
echo ""

# 6. Настройка переменных окружения для Anthropic (опционально)
echo -e "${YELLOW}[6/6] Настройка переменных окружения и итоговая проверка...${NC}"
if [[ -n "$ANTHROPIC_BASE_URL" ]] || [[ -n "$ANTHROPIC_API_KEY" ]] || [[ -n "$ANTHROPIC_MODEL" ]]; then
    {
        echo ""
        echo "# Anthropic API settings (added by setup-claude-bot.sh)"
        echo "export ANTHROPIC_BASE_URL=\"$ANTHROPIC_BASE_URL\""
        echo "export ANTHROPIC_API_KEY=\"$ANTHROPIC_API_KEY\""
        echo "export ANTHROPIC_MODEL=\"$ANTHROPIC_MODEL\""
    } >> "$USER_HOME/.bashrc"
    echo -e "${GREEN}✓ Переменные окружения добавлены в ~/.bashrc${NC}"
else
    echo -e "${GREEN}✓ Переменные окружения не установлены (используются значения по умолчанию)${NC}"
fi
echo ""

# Итоговая проверка
echo -e "${GREEN}=== Итоговая проверка ===${NC}"
echo -e "Пользователь: ${USERNAME}"
echo -e "Workdir: ${WORKDIR}"
echo -e "Общая группа: ${SHARED_GROUP}"
echo -e "Домашняя директория: ${USER_HOME}"
echo ""

echo "Проверка claude..."
if su - "$USERNAME" -c "claude --version" 2>/dev/null; then
    echo -e "${GREEN}✓ claude работает${NC}"
else
    echo -e "${RED}✗ claude не отвечает${NC}"
fi

echo ""
echo "Проверка прав на workdir..."
if su - "$USERNAME" -c "test -w '$WORKDIR' && echo 'OK'"; then
    echo -e "${GREEN}✓ Запись в workdir доступна${NC}"
else
    echo -e "${RED}✗ Запись в workdir недоступна${NC}"
fi

echo ""
echo "Проверка ~/.local/bin..."
if su - "$USERNAME" -c "test -x '$USER_HOME/.local/bin/claude' && echo 'OK'"; then
    echo -e "${GREEN}✓ claude найден в ~/.local/bin${NC}"
else
    echo -e "${YELLOW}! claude не найден в ~/.local/bin${NC}"
fi

echo ""
echo "Проверка группы ${SHARED_GROUP}..."
if groups "$USERNAME" | grep -q "$SHARED_GROUP"; then
    echo -e "${GREEN}✓ ${USERNAME} состоит в группе ${SHARED_GROUP}${NC}"
else
    echo -e "${YELLOW}! ${USERNAME} не состоит в группе ${SHARED_GROUP}${NC}"
fi

echo ""
echo -e "${GREEN}=== Настройка завершена ===${NC}"
echo ""
echo "Важно: После запуска скрипта требуется перезагрузка или перелогин для"
echo "применения изменений в группах. Для root выполните:"
echo "  newgrp ${SHARED_GROUP}"
echo ""
echo "Для запуска бота используйте:"
echo "  python ${WORKDIR}/cli-proxy/bot.py"
