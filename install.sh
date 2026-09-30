#!/usr/bin/env bash
# MONEY CLUB — установка одной командой на чистый сервер Ubuntu:
#
#   curl -fsSL https://raw.githubusercontent.com/ВАШ_ГИТХАБ/money-club/main/install.sh | bash
#
# Можно запускать повторно: это обновит код, а ключи (.env) и настройки
# (config.yaml) останутся как были.
#
# Всё обёрнуто в функцию main и вызывается в самом конце — если скачивание
# оборвётся на середине, недокачанный скрипт просто не выполнится.

main() {
  set -euo pipefail

  # ↓↓↓ ЕДИНСТВЕННОЕ, ЧТО НУЖНО ПОМЕНЯТЬ: адрес вашего репозитория ↓↓↓
  local REPO_URL="${MONEYCLUB_REPO:-https://github.com/gorkarik/money-club.git}"
  local APP_DIR="${MONEYCLUB_DIR:-$HOME/money-club}"

  say()  { printf '\n  \033[1m%s\033[0m\n' "$*"; }
  ok()   { printf '  ✔ %s\n' "$*"; }
  fail() { printf '\n  ✘ %s\n\n' "$*" >&2; exit 1; }

  printf '\n  M O N E Y   C L U B  —  установка\n'

  case "$REPO_URL" in
    *ВАШ_ГИТХАБ*) fail "В install.sh не указан адрес репозитория (REPO_URL)." ;;
  esac

  # ---- права: root работает напрямую, обычный пользователь — через sudo
  local SUDO=""
  if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || fail "Нужен root или sudo."
    SUDO="sudo"
  fi
  command -v apt-get >/dev/null 2>&1 || fail "Поддерживается только Ubuntu/Debian."

  # ---- 1. системные пакеты (ставим только то, чего нет)
  say "[1/5] Системные пакеты"
  local need=()
  command -v git  >/dev/null 2>&1 || need+=(git)
  command -v tmux >/dev/null 2>&1 || need+=(tmux)
  command -v python3 >/dev/null 2>&1 || need+=(python3)
  python3 -c "import ensurepip, venv" >/dev/null 2>&1 || need+=(python3-venv python3-pip)
  if [ ${#need[@]} -gt 0 ]; then
    # на свежем сервере apt часто занят автообновлениями — ждём до 5 минут
    local APT=(env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
               apt-get -o DPkg::Lock::Timeout=300 -y -q)
    $SUDO "${APT[@]}" update >/dev/null
    $SUDO "${APT[@]}" install "${need[@]}" >/dev/null
    ok "установлено: ${need[*]}"
  else
    ok "всё уже есть"
  fi

  # ---- 2. код бота
  say "[2/5] Код бота"
  if [ -d "$APP_DIR/.git" ]; then
    if git -C "$APP_DIR" pull --ff-only -q; then
      ok "обновлён ($APP_DIR)"
    else
      printf '  ! не удалось обновить — продолжаю с текущей версией\n'
    fi
  elif [ -e "$APP_DIR" ]; then
    fail "Папка $APP_DIR уже существует и это не установка MONEY CLUB. Переименуйте её и запустите снова."
  else
    git clone -q --depth 1 "$REPO_URL" "$APP_DIR" || fail "Не удалось скачать $REPO_URL (репозиторий должен быть публичным)."
    ok "скачан в $APP_DIR"
  fi
  cd "$APP_DIR"

  # ---- 3. python-окружение и библиотеки
  say "[3/5] Библиотеки (1–3 минуты)"
  if [ ! -x venv/bin/python ]; then
    python3 -m venv venv
  fi
  venv/bin/python -m pip install -q --upgrade pip >/dev/null
  if venv/bin/python -m pip install -q -r requirements-live.txt; then
    ok "установлены, включая боевой режим"
  else
    venv/bin/python -m pip install -q -r requirements.txt \
      || fail "Не удалось установить библиотеки."
    printf '  ! библиотеки боевого режима не встали — тестовая запись работает,\n'
    printf '    боевой режим меню предложит доустановить при старте\n'
  fi

  # ---- 4. файлы настроек (существующие не трогаем)
  say "[4/5] Настройки"
  mkdir -p logs
  if [ ! -f config.yaml ]; then
    cp config.example.yaml config.yaml
    ok "config.yaml создан (пороги -7.0 / 4.0 / 4.5, лимит \$25)"
  else
    ok "config.yaml уже есть — оставлен как был"
  fi
  if [ ! -f .env ]; then
    ok "ключи введёте в меню"
  else
    chmod 600 .env
    ok ".env уже есть — ключи сохранены"
  fi

  # ---- 5. команда moneyclub и автозапуск меню при входе по SSH
  say "[5/5] Команда moneyclub"
  local LAUNCHER
  LAUNCHER="$(mktemp)"
  cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
# MONEY CLUB — открыть меню
export PYTHONUTF8=1
cd "$APP_DIR" && exec "$APP_DIR/venv/bin/python" "$APP_DIR/club.py" "\$@"
EOF
  chmod 755 "$LAUNCHER"
  $SUDO mv "$LAUNCHER" /usr/local/bin/moneyclub
  ok "меню открывается командой: moneyclub"

  local RC="$HOME/.bashrc"
  if ! grep -q '>>> moneyclub >>>' "$RC" 2>/dev/null; then
    cat >> "$RC" <<'EOF'

# >>> moneyclub >>>  (меню при входе по SSH; отключить: удалите этот блок)
if [ -n "$SSH_TTY" ] && [ -z "$TMUX" ] && [ -z "$MONEYCLUB_NOAUTO" ] \
   && [[ $- == *i* ]] && command -v moneyclub >/dev/null 2>&1; then
  moneyclub
fi
# <<< moneyclub <<<
EOF
    ok "меню будет открываться само при входе на сервер"
  fi

  printf '\n  Готово.\n\n'
  sleep 1

  # запуск меню: при "curl | bash" клавиатура доступна только через /dev/tty
  if (exec </dev/tty) 2>/dev/null; then
    exec /usr/local/bin/moneyclub </dev/tty
  else
    printf '  Откройте меню командой: moneyclub\n\n'
  fi
}

main "$@"
