#!/usr/bin/env python3
"""MONEY CLUB — меню управления ботом entropy-arb.

Запуск: команда `moneyclub` (ставится install.sh) или `venv/bin/python club.py`.

Тикер и площадка хеджа зафиксированы: SNDK, Entropy ↔ Lighter Robinhood chain.
Бот работает в фоновой tmux-сессии, поэтому его не останавливает ни закрытие
терминала, ни обрыв SSH. Пользователь tmux не видит: всё через пункты меню.
"""
from __future__ import annotations

import getpass
import os
import re
import shlex
import signal
import subprocess
import sys
import time

APP_DIR = os.path.dirname(os.path.realpath(__file__))
os.chdir(APP_DIR)
sys.path.insert(0, APP_DIR)

try:
    import yaml
    from rich.console import Console
except ImportError:
    print("Не найдены библиотеки меню (rich / PyYAML).\n"
          "Запустите установку ещё раз той же командой, которой ставили бота.")
    sys.exit(1)

# ---------------------------------------------------------------- константы

SYMBOL = "SNDK"
HEDGE = "lighter-rh"
PAIR_LABEL = "SNDK · Entropy ↔ RH"

VENV_PY = os.path.join(APP_DIR, "venv", "bin", "python")
PY = VENV_PY if os.path.exists(VENV_PY) else sys.executable

CONFIG = "config.yaml"
CONFIG_EXAMPLE = "config.example.yaml"
ENV_FILE = ".env"
ENV_EXAMPLE = ".env.example"
LOG_DIR = "logs"
STDERR_LOG = os.path.join(LOG_DIR, "stderr.log")

TMUX_SOCKET = "moneyclub"
TMUX_SESSION = "moneyclub"
TMUX_CONF = os.path.join(APP_DIR, ".moneyclub-tmux.conf")

DEFAULT_THRESHOLDS = {"midline_bps": -7.0, "upper_bps": 4.0, "lower_bps": 4.5}
DEFAULT_POSITION_USD = 25.0
DEFAULT_ORDER_USD = 20.0

BANNER = r"""
 ███╗   ███╗ ██████╗ ███╗   ██╗███████╗██╗   ██╗
 ████╗ ████║██╔═══██╗████╗  ██║██╔════╝╚██╗ ██╔╝
 ██╔████╔██║██║   ██║██╔██╗ ██║█████╗   ╚████╔╝
 ██║╚██╔╝██║██║   ██║██║╚██╗██║██╔══╝    ╚██╔╝
 ██║ ╚═╝ ██║╚██████╔╝██║ ╚████║███████╗   ██║
 ╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝   ╚═╝
        ██████╗██╗     ██╗   ██╗██████╗
       ██╔════╝██║     ██║   ██║██╔══██╗
       ██║     ██║     ██║   ██║██████╔╝
       ██║     ██║     ██║   ██║██╔══██╗
       ╚██████╗███████╗╚██████╔╝██████╔╝
        ╚═════╝╚══════╝ ╚═════╝ ╚═════╝
"""
BANNER_COMPACT = "\n  M O N E Y   C L U B\n"

TMUX_CONF_TEXT = """\
# Создаётся автоматически меню MONEY CLUB — вручную не редактировать.
set -g default-terminal "screen-256color"
set -g escape-time 0
set -g mouse off
set -g history-limit 2000
set -g status on
set -g status-style "bg=default,fg=white"
set -g status-left-length 80
set -g status-right ""
set -g status-left " MONEY CLUB · дашборд · Q — выйти в меню (бот продолжит работать) "
set -g window-status-format ""
set -g window-status-current-format ""
# выход из дашборда одной клавишей, на любой раскладке
bind-key -n q detach-client
bind-key -n Q detach-client
bind-key -n й detach-client
bind-key -n Й detach-client
"""

console = Console(highlight=False)


class Back(Exception):
    """Возврат на уровень выше (Ctrl+C или «0» внутри подменю)."""


# ------------------------------------------------------------------ ввод

def ask(prompt: str = "  › ") -> str:
    try:
        return input(prompt).strip()
    except KeyboardInterrupt:
        print()
        raise Back
    except EOFError:
        print()
        raise SystemExit(0)


def confirm(question: str, default: bool = True) -> bool:
    hint = "Enter — да, 0 — нет" if default else "1 — да, Enter — нет"
    while True:
        a = ask(f"  {question} [{hint}]: ").lower()
        if a == "":
            return default
        if a in ("1", "д", "да", "y", "yes"):
            return True
        if a in ("0", "н", "нет", "n", "no"):
            return False
        console.print("  [dim]Введите 1 (да) или 0 (нет).[/dim]")


def pause() -> None:
    ask("\n  Нажмите Enter, чтобы вернуться в меню… ")


def ask_number(label: str, current: float, money: bool = False) -> float:
    """Число с клавиатуры; Enter — оставить текущее. Запятая тоже подходит."""
    while True:
        a = ask(f"  {label} [сейчас {fmt(current, money)}, Enter — оставить]: ")
        if a == "":
            return current
        try:
            return float(a.replace(",", ".").replace(" ", ""))
        except ValueError:
            console.print("  [dim]Нужно число, например 4.5 или -7[/dim]")


def fmt(v, whole_as_int: bool = False) -> str:
    v = float(v)
    if v == 0:
        v = 0.0
    if whole_as_int and v.is_integer():
        return str(int(v))
    s = f"{v:.4f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def usd(v) -> str:
    return "$" + fmt(v, whole_as_int=True)


# ------------------------------------------------------------ экран

def clear() -> None:
    console.clear()


def header(title: str = "") -> None:
    clear()
    console.print(BANNER if console.width >= 52 else BANNER_COMPACT,
                  markup=False, highlight=False)
    if title:
        console.print(f"  {title}\n", style="bold")


def menu(items, back_label: str = "Назад") -> str:
    for key, label in items:
        console.print(f"  {key}  {label}", markup=False)
    console.print(f"  0  {back_label}", markup=False)
    console.print()
    return ask()


# ------------------------------------------------------------- файлы

def ensure_files() -> bool:
    """Создаёт недостающие файлы. Возвращает True при самом первом запуске."""
    os.makedirs(LOG_DIR, exist_ok=True)
    if not os.path.exists(CONFIG):
        with open(CONFIG_EXAMPLE, encoding="utf-8") as src, \
                open(CONFIG, "w", encoding="utf-8") as dst:
            dst.write(src.read())
    first = not os.path.exists(ENV_FILE)
    if first:
        _atomic_write(ENV_FILE, _env_template(), mode=0o600)
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass
    old = ""
    if os.path.exists(TMUX_CONF):
        with open(TMUX_CONF, encoding="utf-8") as fh:
            old = fh.read()
    if old != TMUX_CONF_TEXT:
        _atomic_write(TMUX_CONF, TMUX_CONF_TEXT)
    return first


def _atomic_write(path: str, text: str, mode: int = None) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


# -------------------------------------------------------------- config.yaml

def read_config() -> dict:
    with open(CONFIG, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def cfg_values() -> dict:
    c = read_config()
    thr = c.get("thresholds") or {}
    return {
        "midline_bps": float(thr.get("midline_bps", 0.0)),
        "upper_bps": float(thr.get("upper_bps", 0.0)),
        "lower_bps": float(thr.get("lower_bps", 0.0)),
        "pos_entropy": float((c.get("entropy") or {}).get("max_position_usd", 0)),
        "pos_hedge": float((c.get("hedge") or {}).get("max_position_usd", 0)),
        "order": float((c.get("sizing") or {}).get("max_order_notional_usd", 0)),
        "min_order": float((c.get("sizing") or {}).get("min_order_notional_usd", 10)),
        "csv": (c.get("recorder") or {}).get("csv", "logs/minutes.csv"),
    }


def _replace_yaml_value(text: str, section: str, key: str, value: str) -> str:
    """Меняет значение section.key, сохраняя комментарии и выравнивание."""
    lines = text.split("\n")
    in_sec = False
    for i, line in enumerate(lines):
        if re.match(r"^[A-Za-z_]\w*\s*:", line):
            in_sec = re.match(rf"^{re.escape(section)}\s*:", line) is not None
            continue
        if not in_sec:
            continue
        m = re.match(rf"^(\s+{re.escape(key)}\s*:\s*)([^#]*?)(\s*#.*)?$", line)
        if not m:
            continue
        new = m.group(1) + value
        comment = (m.group(3) or "").lstrip()
        if comment:
            hash_col = len(line) - len(comment)
            new += " " * max(2, hash_col - len(new)) + comment
        lines[i] = new
        return "\n".join(lines)
    raise KeyError(f"в config.yaml не найден параметр {section}.{key}")


def save_config(changes) -> None:
    """changes: [(section, key, value_str)]. Проверяет файл так же, как бот
    при старте; если что-то не так — откатывает изменения."""
    with open(CONFIG, encoding="utf-8") as fh:
        original = fh.read()
    text = original
    for section, key, value in changes:
        text = _replace_yaml_value(text, section, key, value)
    _atomic_write(CONFIG, text)
    try:
        from entropy_arb.config import load_config
        load_config(CONFIG, "/nonexistent-env", symbol=SYMBOL, hedge_venue=HEDGE)
    except Exception as e:
        _atomic_write(CONFIG, original)
        raise RuntimeError(f"настройки не сохранены: {e}") from e


# --------------------------------------------------------------------- .env

ENV_KEYS = [
    "HL_PRIVATE_KEY",
    "HL_ACCOUNT_ADDRESS",
    "LIGHTER_ACCOUNT_INDEX",
    "LIGHTER_API_KEY_INDEX",
    "LIGHTER_API_PRIVATE_KEY",
]
SECRET_KEYS = {"HL_PRIVATE_KEY", "LIGHTER_API_PRIVATE_KEY"}

KEY_INFO = {
    "HL_PRIVATE_KEY": (
        "Приватный ключ API (agent) кошелька Hyperliquid",
        "app.hyperliquid.xyz/API → ключ, который показали один раз при создании. "
        "Формат: 0x + 64 символа."),
    "HL_ACCOUNT_ADDRESS": (
        "Адрес ОСНОВНОГО кошелька (не агента)",
        "Адрес кошелька, на котором лежат деньги на Hyperliquid. "
        "Формат: 0x + 40 символов."),
    "LIGHTER_ACCOUNT_INDEX": (
        "Номер аккаунта на Lighter (Robinhood chain)",
        "Число — номер вашего аккаунта на бирже."),
    "LIGHTER_API_KEY_INDEX": (
        "Индекс API-ключа Lighter",
        "Короткое число, которое вы выбрали при создании ключа "
        "(например 4). Это НЕ Public Key."),
    "LIGHTER_API_PRIVATE_KEY": (
        "Приватный ключ API Lighter",
        "robinhoodchain.lighter.xyz/apikeys → Private Key (обычно 80 символов)."),
}

HEX = re.compile(r"^[0-9a-fA-F]+$")


def read_env() -> dict:
    out = {}
    if not os.path.exists(ENV_FILE):
        return out
    with open(ENV_FILE, encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k.strip()] = v
    return out


def _env_template() -> str:
    """Шаблон .env; если .env.example потерялся — минимальный встроенный."""
    if os.path.exists(ENV_EXAMPLE):
        with open(ENV_EXAMPLE, encoding="utf-8") as fh:
            return fh.read()
    return "# MONEY CLUB — ключи. Заполняются через меню.\n" + \
        "".join(f"{k}=\n" for k in ENV_KEYS)


def write_env(updates: dict) -> None:
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = _env_template()
    lines = text.split("\n")
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in updates:
            lines[i] = f"{m.group(1)}={updates[m.group(1)]}"
            done.add(m.group(1))
    for k, v in updates.items():
        if k not in done:
            lines.append(f"{k}={v}")
    _atomic_write(ENV_FILE, "\n".join(lines).rstrip("\n") + "\n", mode=0o600)


def validate_key(name: str, raw: str):
    """Возвращает (нормализованное_значение, ошибка_или_None, мягкое_предупреждение)."""
    v = raw.strip().strip("\"'").replace(" ", "")
    if name == "HL_PRIVATE_KEY":
        if HEX.match(v) and len(v) == 64:
            v = "0x" + v
        if not (v.startswith("0x") and len(v) == 66 and HEX.match(v[2:])):
            return v, (f"Приватный ключ должен быть 0x + 64 символа, а у вас "
                       f"{len(v)} символов."), None
        return v, None, None
    if name == "HL_ACCOUNT_ADDRESS":
        if HEX.match(v) and len(v) == 40:
            v = "0x" + v
        if not (v.startswith("0x") and len(v) == 42 and HEX.match(v[2:])):
            hint = (" Похоже, вы вставили приватный ключ вместо адреса."
                    if len(v) in (64, 66) else "")
            return v, (f"Адрес должен быть 0x + 40 символов, а у вас "
                       f"{len(v)} символов.{hint}"), None
        return v, None, None
    if name == "LIGHTER_ACCOUNT_INDEX":
        if not v.isdigit():
            return v, "Номер аккаунта — это только цифры.", None
        return str(int(v)), None, None
    if name == "LIGHTER_API_KEY_INDEX":
        if not v.isdigit():
            hint = (" Похоже, это Public Key. Нужен индекс — короткое число, "
                    "которое вы выбрали при создании ключа (например 4)."
                    if len(v) > 10 else "")
            return v, "Индекс API-ключа — это короткое число." + hint, None
        n = int(v)
        if n > 255:
            return v, ("Индекс API-ключа не бывает больше 255. Возможно, это "
                       "номер аккаунта, а не индекс ключа."), None
        warn = (f"Индексы 0–2 у Lighter заняты сайтом и приложением; "
                f"обычно для бота берут 3 и выше." if n < 3 else None)
        return str(n), None, warn
    if name == "LIGHTER_API_PRIVATE_KEY":
        body = v[2:] if v.startswith("0x") else v
        if not body or not HEX.match(body):
            return v, "Приватный ключ Lighter состоит из символов 0-9 и a-f.", None
        warn = (None if len(body) == 80 else
                f"Обычно ключ Lighter — 80 символов, а у вас {len(body)}. "
                f"Проверьте, что это Private Key, а не Public Key.")
        return v, None, warn
    return v, None, None


def keys_state():
    """{имя: (заполнен, корректен)}, all_ok"""
    env = read_env()
    state = {}
    for k in ENV_KEYS:
        v = env.get(k, "")
        if not v:
            state[k] = (False, False)
            continue
        _, err, _ = validate_key(k, v)
        state[k] = (True, err is None)
    return state, all(ok for _, ok in state.values())


def mask(name: str, value: str) -> str:
    if not value:
        return "не заполнен"
    if name in SECRET_KEYS:
        return f"••••••••{value[-4:]}  ({len(value)} симв.)"
    if name == "HL_ACCOUNT_ADDRESS" and len(value) > 12:
        return f"{value[:6]}…{value[-4:]}"
    return value


def agent_address(private_key: str):
    try:
        from eth_account import Account
        return Account.from_key(private_key).address
    except Exception:
        return None


# ------------------------------------------------------------ процесс бота

class Bot:
    def __init__(self, pid: int, args: list, cwd: str):
        self.pid, self.args, self.cwd = pid, args, cwd
        self.record = "--record-only" in args
        self.ours = os.path.realpath(cwd) == APP_DIR

    @property
    def mode_label(self) -> str:
        return "ТЕСТОВАЯ ЗАПИСЬ" if self.record else "БОЕВОЙ РЕЖИМ"


def find_bots():
    """Все запущенные main.py — и наши, и запущенные вручную из другой папки
    (иначе можно случайно запустить второй бот на тех же ключах)."""
    bots = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == os.getpid():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        args = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if len(args) < 2 or not os.path.basename(args[0]).startswith("python"):
            continue
        if not any(os.path.basename(a) == "main.py" for a in args[1:]):
            continue
        if "--symbol" not in args:
            continue
        try:
            cwd = os.readlink(f"/proc/{d}/cwd")
        except OSError:
            cwd = "?"
        bots.append(Bot(int(d), args, cwd))
    return bots


def is_halted(bot: Bot) -> bool:
    """Бот жив, но после серии ошибок перестал торговать (HALTED)."""
    path = os.path.join(bot.cwd, "logs", "engine.log")
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 512 * 1024))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return False
    start = max(tail.rfind("RECORD-ONLY —"), tail.rfind("LIVE — real orders"))
    return start >= 0 and tail.find("HALTED after", start) >= 0


def tmux_env() -> dict:
    env = dict(os.environ)
    env.pop("TMUX", None)          # чтобы работало и изнутри другого tmux
    loc = env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or ""
    if "utf" not in loc.lower():
        env["LC_ALL"] = "C.UTF-8"  # иначе tmux покажет кириллицу как «___»
    env["PYTHONUTF8"] = "1"
    return env


def tmux(*args, **kw):
    return subprocess.run(["tmux", "-u", "-L", TMUX_SOCKET, "-f", TMUX_CONF,
                           *args], env=tmux_env(), **kw)


def tmux_has_session() -> bool:
    return tmux("has-session", "-t", TMUX_SESSION,
                capture_output=True).returncode == 0


def live_deps_ok() -> bool:
    r = subprocess.run([PY, "-c", "import lighter, hyperliquid, eth_account"],
                       capture_output=True)
    return r.returncode == 0


def explain_error(text: str) -> str:
    t = text.lower()
    if "credentials" in t:
        return "Не хватает ключей. Откройте Настройки → Ключи."
    if "no module named" in t or "requirements-live" in t:
        return ("Не установлены библиотеки боевого режима. Запустите установку "
                "ещё раз той же командой.")
    if "not found on" in t:
        return "Тикер не найден на одной из бирж."
    if "config error" in t:
        return "Ошибка в настройках (config.yaml)."
    if any(x in t for x in ("clientresponseerror", "clientconnectorerror",
                            "cannot connect", "timeout", "connection",
                            "forbidden", "service unavailable", "bad gateway")):
        return ("Нет связи с биржей или биржа отклонила запрос. "
                "Попробуйте ещё раз через минуту.")
    if "private key" in t or "invalid" in t or "signature" in t:
        return "Похоже, неверный ключ. Проверьте Настройки → Ключи."
    return "Бот завершился с ошибкой."


def launch_bot(record: bool) -> bool:
    """Запускает бота в фоновой tmux-сессии и проверяет, что он поднялся."""
    if tmux_has_session():
        tmux("kill-session", "-t", TMUX_SESSION, capture_output=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    offset = os.path.getsize(STDERR_LOG) if os.path.exists(STDERR_LOG) else 0
    flags = f"--symbol {SYMBOL} --hedge {HEDGE} --ru"
    if record:
        flags += " --record-only"
    cmd = (f"cd {shlex.quote(APP_DIR)} && exec {shlex.quote(PY)} main.py "
           f"{flags} 2>>{shlex.quote(STDERR_LOG)}")
    r = tmux("new-session", "-d", "-s", TMUX_SESSION, "-x", "200", "-y", "50",
             cmd, capture_output=True, text=True)
    if r.returncode != 0:
        console.print(f"\n  [red]✘ Не удалось запустить tmux:[/red] "
                      f"{r.stderr.strip()}")
        return False
    alive = False
    with console.status("  Запускаю бота…"):
        for _ in range(20):             # ~10 секунд: загрузка рынков и ключей
            time.sleep(0.5)
            alive = any(b.ours for b in find_bots())
            if not alive:
                break
    if alive:
        console.print("\n  [green]✔ Бот запущен[/green] — "
                      f"{'тестовая запись' if record else 'боевой режим'}.")
        return True
    err = ""
    if os.path.exists(STDERR_LOG):
        with open(STDERR_LOG, "rb") as fh:
            fh.seek(offset)
            err = fh.read().decode("utf-8", "replace").strip()
    console.print(f"\n  [red]✘ Бот не запустился.[/red] {explain_error(err)}")
    if err:
        # из трейсбека показываем только суть — последнюю строку с ошибкой
        lines = [l.strip() for l in err.splitlines() if l.strip()]
        errs = [l for l in lines if re.match(r"^[\w.]*(Error|Exception)\b", l)
                or l.startswith(("config error", "startup error"))]
        console.print("\n  Сообщение бота:", style="dim")
        console.print(f"    {(errs or lines)[-1]}", style="dim", markup=False,
                      soft_wrap=True)
        console.print(f"  [dim]Подробности: {STDERR_LOG}[/dim]")
    return False


def stop_bots(bots) -> bool:
    for b in bots:
        try:
            os.kill(b.pid, signal.SIGINT)   # мягкая остановка, как Ctrl+C
        except ProcessLookupError:
            pass
        except PermissionError:
            console.print(f"  [red]Нет прав остановить процесс {b.pid}.[/red]")
    pids = {b.pid for b in bots}

    def alive():
        return {p for p in pids if os.path.exists(f"/proc/{p}")}

    with console.status("  Останавливаю бота (дожидаюсь текущих ордеров)…"):
        deadline = time.time() + 30
        while alive() and time.time() < deadline:
            time.sleep(0.5)
        for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 2)):
            if not alive():
                break
            for p in alive():
                try:
                    os.kill(p, sig)
                except OSError:
                    pass
            deadline = time.time() + wait
            while alive() and time.time() < deadline:
                time.sleep(0.3)
    if tmux_has_session():
        tmux("kill-session", "-t", TMUX_SESSION, capture_output=True)
    if alive():
        console.print("  [red]✘ Не удалось остановить бота.[/red]")
        return False
    console.print("  [green]✔ Бот остановлен.[/green]")
    return True


# --------------------------------------------------------------- статус

def status_lines():
    bots = find_bots()
    if not bots:
        run = "[dim]○ ОСТАНОВЛЕН[/dim]"
    elif len(bots) > 1:
        run = f"[red]⚠ ЗАПУЩЕНО НЕСКОЛЬКО БОТОВ ({len(bots)}) — нажмите Стоп[/red]"
    else:
        b = bots[0]
        if is_halted(b):
            run = "[red]⚠ АВАРИЙНЫЙ СТОП — проверьте позиции, затем Стоп и Старт[/red]"
        elif not b.ours:
            run = "[yellow]● РАБОТАЕТ · запущен не из меню[/yellow]"
        else:
            run = f"[green]● РАБОТАЕТ[/green] · {b.mode_label}"
    _, keys_ok = keys_state()
    try:
        v = cfg_values()
        info = (f"ключи {'✔' if keys_ok else '✘ не настроены'} · "
                f"лимит {usd(v['pos_entropy'])} · "
                f"центр {fmt(v['midline_bps'])} "
                f"(+{fmt(v['upper_bps'])} / −{fmt(v['lower_bps'])})")
    except Exception:
        info = "[red]config.yaml не читается — Настройки → Пороги → по умолчанию[/red]"
    return run, info


def main_screen() -> None:
    header()
    run, info = status_lines()
    console.print(f"  {PAIR_LABEL}    {run}")
    console.print(f"  [dim]{info}[/dim]\n")


# ================================================================ ДЕЙСТВИЯ

def action_start() -> None:
    bots = find_bots()
    if bots:
        header("Старт")
        where = "" if bots[0].ours else f" (запущен вручную из {bots[0].cwd})"
        console.print(f"  Бот уже работает{where}. Второй запускать нельзя — "
                      f"он торговал бы теми же ключами.\n"
                      f"  Сначала остановите его: пункт 2 — Стоп.")
        pause()
        return
    header("Старт")
    choice = menu([("1", "Боевой режим — реальная торговля"),
                   ("2", "Тестовая запись — без денег и без ключей")])
    if choice == "1":
        start_live()
    elif choice == "2":
        header("Старт · тестовая запись")
        console.print("  Бот будет только записывать стаканы обеих бирж раз в "
                      "минуту — это данные для анализа.\n  Сделок нет, ключи "
                      "не нужны.\n")
        if confirm("Запустить?"):
            if launch_bot(record=True):
                offer_dashboard()
            else:
                pause()


def start_checklist():
    """Печатает проверку перед боевым стартом, возвращает (ключи_ок, либы_ок)."""
    state, keys_ok = keys_state()
    v = cfg_values()
    with console.status("  Проверяю…"):
        deps_ok = live_deps_ok()
    console.print("  Проверка перед стартом:\n")
    if keys_ok:
        console.print("   [green]✔[/green] Ключи заполнены")
    else:
        bad = [k for k, (_, ok) in state.items() if not ok]
        console.print(f"   [red]✘[/red] Ключи не заполнены или с ошибкой: "
                      f"{', '.join(bad)}")
    if v["midline_bps"] != 0:
        console.print(f"   [green]✔[/green] Калибровка: центр "
                      f"{fmt(v['midline_bps'])}, +{fmt(v['upper_bps'])} / "
                      f"−{fmt(v['lower_bps'])}")
    else:
        console.print("   [yellow]![/yellow] Калибровка не выполнена (центр = 0) "
                      "— рекомендуем пункт 4, Анализ")
    same = v["pos_entropy"] == v["pos_hedge"]
    console.print(f"   [green]✔[/green] Лимит позиции {usd(v['pos_entropy'])}"
                  f"{'' if same else ' / ' + usd(v['pos_hedge']) + ' (разные!)'}"
                  f", одна сделка до {usd(v['order'])}")
    if deps_ok:
        console.print("   [green]✔[/green] Библиотеки боевого режима установлены")
    else:
        console.print("   [red]✘[/red] Не установлены библиотеки боевого режима")
    console.print()
    return keys_ok, deps_ok


def start_live() -> None:
    while True:
        header("Старт · боевой режим")
        keys_ok, deps_ok = start_checklist()
        if not deps_ok:
            if confirm("Установить библиотеки сейчас? (1–3 минуты)"):
                install_live_deps()
                continue
            return
        if not keys_ok:
            if confirm("Перейти к настройке ключей?"):
                keys_menu()
                continue
            return
        if confirm("Запустить боевой режим?"):
            if launch_bot(record=False):
                offer_dashboard()
            else:
                pause()
        return


def install_live_deps() -> None:
    console.print()
    subprocess.call([PY, "-m", "pip", "install", "-r", "requirements-live.txt"])
    console.print()


def offer_dashboard() -> None:
    console.print()
    if confirm("Открыть дашборд?"):
        open_dashboard(skip_intro=False)


def action_stop() -> None:
    header("Стоп")
    bots = find_bots()
    if not bots:
        console.print("  Бот не запущен.")
        pause()
        return
    for b in bots:
        where = "" if b.ours else f" — запущен вручную из {b.cwd}"
        console.print(f"  Сейчас работает: {b.mode_label}{where}")
    console.print("\n  [dim]Открытые позиции останутся открытыми (они "
                  "захеджированы). При следующем старте бот их подхватит.[/dim]\n")
    if confirm("Остановить бота?"):
        stop_bots(bots)
        pause()


def open_dashboard(skip_intro: bool = False) -> None:
    bots = find_bots()
    if not bots:
        header("Дашборд")
        console.print("  Бот не запущен — смотреть пока нечего.\n"
                      "  Запустите его: пункт 1 — Старт.")
        pause()
        return
    if not tmux_has_session() or not any(b.ours for b in bots):
        header("Дашборд")
        console.print("  Бот запущен не из меню, поэтому его дашборд открывается "
                      "только там,\n  где его запускали. Чтобы видеть дашборд "
                      "отсюда: пункт 2 — Стоп, затем пункт 1 — Старт.")
        pause()
        return
    if not skip_intro:
        header("Дашборд")
        console.print("  Сейчас откроется дашборд бота.\n")
        console.print("  [bold]Выйти обратно в меню — клавиша Q[/bold] "
                      "(бот продолжит работать).")
        console.print("  [dim]Закрыть окно терминала тоже можно — бот работает "
                      "на сервере.[/dim]\n")
        ask("  Нажмите Enter, чтобы открыть… ")
    tmux("attach-session", "-r", "-t", TMUX_SESSION)


# ------------------------------------------------------------ анализ

def action_analysis() -> None:
    v = cfg_values()
    csv_path = v["csv"]
    header("Анализ / калибровка")
    if not os.path.exists(csv_path):
        console.print("  Данных пока нет. Бот записывает их в любом режиме — "
                      "запустите его\n  (например, тестовую запись) и "
                      "вернитесь через несколько часов.")
        pause()
        return
    console.print("  За какой период посчитать?\n")
    periods = {"1": (24, "последние 24 часа"), "2": (48, "последние 48 часов"),
               "3": (168, "последнюю неделю"), "4": (0, "всё время")}
    choice = menu([(k, p[1].capitalize()) for k, p in periods.items()])
    if choice not in periods:
        return
    hours, label = periods[choice]
    with console.status("  Считаю…"):
        r = subprocess.run([PY, "tools/analyze.py", "--csv", csv_path,
                            "--hours", str(hours)],
                           capture_output=True, text=True, timeout=300)
    out = (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip()
    header("Анализ / калибровка")
    line = "─" * 22
    print(f"  {line} скопируйте отсюда {line}\n")
    print(f"MONEY CLUB · анализ {PAIR_LABEL} · период: {label}")
    print(f"Текущие настройки: midline {fmt(v['midline_bps'])} | upper "
          f"{fmt(v['upper_bps'])} | lower {fmt(v['lower_bps'])} | лимит позиции "
          f"{usd(v['pos_entropy'])} | сделка до {usd(v['order'])}")
    print(out)
    print(f"\n  {line}──── до сюда ────{line}\n")
    console.print("  [bold]Выделите блок выше, скопируйте и отправьте Клоду "
                  "(claude.ai)[/bold]\n  с вопросом: «стоит ли обновить "
                  "калибровку бота?»\n")
    m = re.search(r"thresholds:\s*\n\s*midline_bps:\s*([-+\d.]+)\s*\n\s*"
                  r"upper_bps:\s*([-+\d.]+)\s*\n\s*lower_bps:\s*([-+\d.]+)", out)
    if not m or r.returncode != 0:
        pause()
        return
    sug = {"midline_bps": float(m.group(1)), "upper_bps": float(m.group(2)),
           "lower_bps": float(m.group(3))}
    cur = {k: v[k] for k in sug}
    if all(abs(sug[k] - cur[k]) < 1e-9 for k in sug):
        console.print("  Рекомендованные пороги совпадают с текущими — менять "
                      "нечего.")
        pause()
        return
    console.print(f"  Сейчас:       центр {fmt(cur['midline_bps'])}, "
                  f"+{fmt(cur['upper_bps'])} / −{fmt(cur['lower_bps'])}")
    console.print(f"  Рекомендация: центр {fmt(sug['midline_bps'])}, "
                  f"+{fmt(sug['upper_bps'])} / −{fmt(sug['lower_bps'])}\n")
    if confirm("Применить рекомендованные пороги?", default=False):
        apply_changes([("thresholds", k, fmt(sug[k])) for k in sug])
    pause()


# ------------------------------------------------------------ настройки

def apply_changes(changes) -> None:
    """Сохраняет изменения. Если бот работает — останавливает его, сохраняет и
    запускает снова в том же режиме (настройки на ходу бот не подхватывает)."""
    bots = find_bots()
    restart = None
    if bots:
        console.print("  Бот работает — новые настройки вступят в силу только "
                      "после перезапуска.")
        if not confirm("Остановить бота, сохранить и запустить снова?"):
            console.print("  Изменения не сохранены.")
            return
        restart = bots[0].record if all(b.ours for b in bots) else None
        if not stop_bots(bots):
            console.print("  Изменения не сохранены.")
            return
    try:
        save_config(changes)
        console.print("  [green]✔ Сохранено.[/green]")
    except Exception as e:
        console.print(f"  [red]✘ {e}[/red]")
    if restart is not None:
        launch_bot(record=restart)


def action_settings() -> None:
    bots = find_bots()
    resume_mode = None
    if bots:
        header("Настройки")
        console.print("  Настройки меняются только при остановленном боте.\n")
        if not confirm("Остановить бота сейчас?"):
            return
        if all(b.ours for b in bots):
            resume_mode = bots[0].record
        if not stop_bots(bots):
            pause()
            return
    try:
        while True:
            header("Настройки")
            choice = menu([("1", "Пороги входа (центр / выше / ниже)"),
                           ("2", "Лимиты (позиция и размер сделки)"),
                           ("3", "Ключи (.env)")])
            try:
                if choice == "1":
                    thresholds_menu()
                elif choice == "2":
                    limits_menu()
                elif choice == "3":
                    keys_menu()
                elif choice == "0":
                    break
            except Back:
                continue
    except Back:
        pass
    if resume_mode is not None:
        header("Настройки")
        mode = "тестовой записи" if resume_mode else "боевом режиме"
        console.print(f"  До входа в настройки бот работал в {mode}.\n")
        if confirm("Запустить его снова?"):
            if resume_mode:
                launch_bot(record=True)
                pause()
            else:
                start_live()


def thresholds_menu() -> None:
    while True:
        v = cfg_values()
        header("Настройки · пороги входа")
        mid, up, lo = v["midline_bps"], v["upper_bps"], v["lower_bps"]
        console.print(f"  Центр (midline):      {fmt(mid)} bps — обычный "
                      f"уровень премии")
        console.print(f"  Выше центра (upper):  +{fmt(up)} bps → продажа Entropy "
                      f"при премии ≥ {fmt(mid + up)}")
        console.print(f"  Ниже центра (lower):  −{fmt(lo)} bps → покупка Entropy "
                      f"при премии ≤ {fmt(mid - lo)}\n")
        d = DEFAULT_THRESHOLDS
        choice = menu([("1", "Изменить"),
                       ("2", f"Вернуть по умолчанию ({fmt(d['midline_bps'])} / "
                             f"{fmt(d['upper_bps'])} / {fmt(d['lower_bps'])})")])
        if choice == "0":
            return
        if choice == "1":
            console.print()
            new_mid = ask_number("Центр midline_bps", mid)
            while True:
                new_up = ask_number("Выше центра upper_bps", up)
                if new_up > 0:
                    break
                console.print("  [dim]Должно быть больше 0.[/dim]")
            while True:
                new_lo = ask_number("Ниже центра lower_bps", lo)
                if new_lo > 0:
                    break
                console.print("  [dim]Должно быть больше 0.[/dim]")
            new = {"midline_bps": new_mid, "upper_bps": new_up,
                   "lower_bps": new_lo}
        elif choice == "2":
            new = dict(d)
        else:
            continue
        console.print(f"\n  Будет: центр {fmt(new['midline_bps'])}, "
                      f"+{fmt(new['upper_bps'])} / −{fmt(new['lower_bps'])}")
        if confirm("Сохранить?"):
            apply_changes([("thresholds", k, fmt(val)) for k, val in new.items()])
            pause()


def limits_menu() -> None:
    while True:
        v = cfg_values()
        header("Настройки · лимиты")
        console.print(f"  Лимит позиции:     {usd(v['pos_entropy'])} на каждой "
                      f"бирже")
        if v["pos_entropy"] != v["pos_hedge"]:
            console.print(f"  [yellow]! На хедже сейчас {usd(v['pos_hedge'])} — "
                          f"лимиты разные, сохраните заново, чтобы выровнять[/yellow]")
        console.print(f"  Одна сделка:       до {usd(v['order'])} "
                      f"(минимум {usd(v['min_order'])})\n")
        choice = menu([("1", "Лимит позиции"), ("2", "Размер одной сделки"),
                       ("3", f"Вернуть по умолчанию ({usd(DEFAULT_POSITION_USD)} / "
                             f"{usd(DEFAULT_ORDER_USD)})")])
        if choice == "0":
            return
        if choice == "1":
            console.print("\n  [dim]Лимит ставится сразу на обе биржи. На "
                          "каждой бирже депозит должен быть больше лимита.[/dim]")
            while True:
                pos = ask_number("Лимит позиции, $", v["pos_entropy"], money=True)
                if pos > 0:
                    break
                console.print("  [dim]Должно быть больше 0.[/dim]")
            if pos < v["min_order"] and not confirm(
                    f"Лимит меньше минимальной сделки {usd(v['min_order'])} — "
                    f"бот не сможет торговать. Всё равно сохранить?",
                    default=False):
                continue
            changes = [("entropy", "max_position_usd", fmt(pos, True)),
                       ("hedge", "max_position_usd", fmt(pos, True))]
            if v["order"] > pos:
                console.print(f"  [dim]Сделка ({usd(v['order'])}) больше нового "
                              f"лимита — уменьшаю её до {usd(pos)}.[/dim]")
                changes.append(("sizing", "max_order_notional_usd",
                                fmt(pos, True)))
        elif choice == "2":
            console.print()
            while True:
                order = ask_number("Одна сделка до, $", v["order"], money=True)
                if order > 0:
                    break
                console.print("  [dim]Должно быть больше 0.[/dim]")
            if order < v["min_order"] and not confirm(
                    f"Это меньше минимальной сделки {usd(v['min_order'])} — "
                    f"бот не сможет торговать. Всё равно сохранить?",
                    default=False):
                continue
            changes = [("sizing", "max_order_notional_usd", fmt(order, True))]
        elif choice == "3":
            changes = [
                ("entropy", "max_position_usd", fmt(DEFAULT_POSITION_USD, True)),
                ("hedge", "max_position_usd", fmt(DEFAULT_POSITION_USD, True)),
                ("sizing", "max_order_notional_usd", fmt(DEFAULT_ORDER_USD, True))]
        else:
            continue
        apply_changes(changes)
        pause()


def keys_menu() -> None:
    while True:
        env = read_env()
        state, all_ok = keys_state()
        header("Настройки · ключи")
        for i, k in enumerate(ENV_KEYS, 1):
            filled, ok = state[k]
            mark = "[green]✔[/green]" if ok else ("[red]✘[/red]" if filled
                                                   else "[dim]·[/dim]")
            console.print(f"  {i}  {mark} {k:<24} "
                          f"[dim]{mask(k, env.get(k, ''))}[/dim]")
        console.print()
        console.print("  6  Ввести все ключи по порядку", markup=False)
        console.print("  0  Назад\n", markup=False)
        choice = ask()
        if choice == "0":
            return
        if choice == "6":
            for k in ENV_KEYS:
                if not edit_key(k):
                    break
            check_agent_address()
            header("Настройки · ключи")
            _, all_ok = keys_state()
            console.print("  [green]✔ Все ключи заполнены.[/green]" if all_ok
                          else "  [yellow]Не все ключи заполнены.[/yellow]")
            pause()
        elif choice.isdigit() and 1 <= int(choice) <= len(ENV_KEYS):
            edit_key(ENV_KEYS[int(choice) - 1])
            check_agent_address()


def edit_key(name: str) -> bool:
    """Спрашивает один ключ. False — пользователь прервал ввод."""
    title, help_text = KEY_INFO[name]
    current = read_env().get(name, "")
    header("Настройки · ключи")
    console.print(f"  [bold]{title}[/bold]  ({name})")
    console.print(f"  [dim]{help_text}[/dim]\n")
    if current:
        console.print(f"  Сейчас: {mask(name, current)}")
    secret = name in SECRET_KEYS
    if secret:
        console.print("  [dim]Вставьте ключ и нажмите Enter. Символы на экране "
                      "не отображаются — так задумано.[/dim]")
    console.print("  [dim]Enter без ввода — оставить как есть.[/dim]\n")
    while True:
        try:
            raw = (getpass.getpass("  › ") if secret else ask("  › ")).strip()
        except KeyboardInterrupt:
            print()
            return False
        except EOFError:
            raise SystemExit(0)
        if raw == "":
            return True
        value, err, warn = validate_key(name, raw)
        if err:
            console.print(f"  [red]✘ {err}[/red]\n  Попробуйте ещё раз "
                          f"(Enter — оставить как есть).")
            continue
        console.print(f"  Принято: {mask(name, value)}")
        if warn:
            console.print(f"  [yellow]! {warn}[/yellow]")
            if not confirm("Сохранить всё равно?", default=False):
                continue
        write_env({name: value})
        return True


def check_agent_address() -> None:
    env = read_env()
    pk, addr = env.get("HL_PRIVATE_KEY", ""), env.get("HL_ACCOUNT_ADDRESS", "")
    if validate_key("HL_PRIVATE_KEY", pk)[1] or \
            validate_key("HL_ACCOUNT_ADDRESS", addr)[1]:
        return
    derived = agent_address(pk)
    if derived and derived.lower() == addr.lower():
        header("Настройки · ключи")
        console.print("  [yellow]! HL_ACCOUNT_ADDRESS совпадает с адресом самого "
                      "агента.[/yellow]\n  Нужен адрес ОСНОВНОГО кошелька, на "
                      "котором лежат деньги, а не адрес API-кошелька.\n")
        if confirm("Ввести адрес заново?"):
            edit_key("HL_ACCOUNT_ADDRESS")


# ================================================================== main

def first_run_prompt() -> None:
    header("Добро пожаловать")
    console.print("  Бот установлен. Для боевого режима нужны ключи Hyperliquid "
                  "и Lighter.\n  [dim]Для тестовой записи ключи не нужны — "
                  "их можно ввести позже: Настройки → Ключи.[/dim]\n")
    try:
        if confirm("Ввести ключи сейчас?"):
            keys_menu()
    except Back:
        pass


def main() -> None:
    if subprocess.run(["which", "tmux"], capture_output=True).returncode != 0:
        print("Не найден tmux. Запустите установку ещё раз той же командой.")
        sys.exit(1)
    if ensure_files():
        first_run_prompt()
    actions = {"1": action_start, "2": action_stop, "3": open_dashboard,
               "4": action_analysis, "5": action_settings}
    while True:
        try:
            main_screen()
            choice = menu([("1", "Старт"), ("2", "Стоп"), ("3", "Дашборд"),
                           ("4", "Анализ / калибровка"), ("5", "Настройки")],
                          back_label="Выход")
        except Back:
            choice = "0"
        if choice == "0":
            clear()
            console.print("  Бот продолжает работать на сервере, если был "
                          "запущен.\n  Открыть меню снова — команда [bold]"
                          "moneyclub[/bold]\n")
            return
        fn = actions.get(choice)
        if fn is None:
            continue
        try:
            fn()
        except Back:
            continue
        except SystemExit:
            raise
        except Exception as e:
            console.print(f"\n  [red]✘ Ошибка: {e}[/red]", markup=True)
            try:
                pause()
            except Back:
                pass


if __name__ == "__main__":
    main()
