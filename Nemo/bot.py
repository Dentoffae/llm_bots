import logging
import logging.handlers
import os
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

import database

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o-mini")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты полезный AI-ассистент. Отвечай по делу. "
    "Поле 'message' форматируй HTML-тегами для Telegram: "
    "используй <b>жирный</b> для заголовков и ключевых слов, "
    "<i>курсив</i> для пояснений, "
    "маркированные списки оформляй через символ '•' с переносом строки, "
    "нумерованные — цифрой с точкой. "
    "Разбивай ответ на смысловые абзацы через пустую строку. "
    "Не используй теги <ul>, <ol>, <li> — только plain-текст с HTML-разметкой. "
    "Поле 'theses' — список ключевых намерений пользователя для базы данных, "
    "формат: 'Пользователь хочет [действие]'.",
)
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "20"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "3000"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.7"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR = Path(__file__).parent / "logs"

# Инструкция форматирования — добавляется к каждому промпту, не перекрывается .env
FORMAT_INSTRUCTIONS = (
    "\n\n---\n"
    "ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА ФОРМАТИРОВАНИЯ поля 'message':\n"
    "• Используй HTML-теги: <b>текст</b> — жирный для заголовков, <i>текст</i> — курсив для пояснений.\n"
    "• Маркированные списки: каждый пункт с новой строки, начиная с символа '•'.\n"
    "• Нумерованные списки: '1. ', '2. ' и т.д.\n"
    "• Разделяй смысловые блоки пустой строкой.\n"
    "• НЕ пиши сплошным текстом — всегда структурируй ответ.\n"
    "• НЕ используй теги <ul>, <ol>, <li>, <br> — Telegram их не поддерживает."
)


class NemoResponse(BaseModel):
    theses: list[str]
    message: str


# Тексты кнопок — используются и как метки, и как триггеры в handle_message
BTN_THESES = "📋 Мои тезисы"
BTN_CLEAR  = "🗑 Очистить историю"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_THESES), KeyboardButton(BTN_CLEAR)]],
    resize_keyboard=True,
    is_persistent=True,
)


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(LOG_LEVEL)

    # Rotating file handler — max 5 MB × 3 backups
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "nemo.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(file_handler)

    # Silence noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram.vendor").setLevel(logging.WARNING)

    return logging.getLogger(__name__)


logger = setup_logging()
client = OpenAI(api_key=OPENAI_API_KEY)

# Per-chat conversation history: {chat_id: deque([{"role": ..., "content": ...}, ...])}
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORY))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Telegram error: %s", context.error, exc_info=context.error)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    history[chat_id].clear()
    logger.info("START — chat_id=%s history cleared", chat_id)
    await update.message.reply_text(
        "Привет! Я Немо 🐠 Я помню наши разговоры, так что смело продолжай любую тему!\n"
        "Просто напиши мне сообщение.",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Отправь мне любое сообщение — я отвечу с учётом нашего диалога.\n"
        f"Я помню последние {MAX_HISTORY} сообщений.\n\n"
        "Команды:\n"
        "/start   — приветствие и очистка истории\n"
        "/theses  — показать тезисы из базы данных\n"
        "/clear   — очистить историю и тезисы\n"
        "/help    — это сообщение"
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    history[chat_id].clear()
    database.clear_theses(user_id)
    logger.info("CLEAR — chat_id=%s user_id=%s history+db cleared", chat_id, user_id)
    await update.message.reply_text(
        "История диалога очищена! Начнём с чистого листа.",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_theses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    database.init_user_table(user_id)
    theses = database.get_all_theses(user_id)
    logger.info("THESES CMD — user_id=%s запросил тезисы (%d шт.)", user_id, len(theses))

    if not theses:
        await update.message.reply_text(
            "В базе данных пока нет тезисов. Напиши мне что-нибудь!",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    lines = [f"📋 <b>Тезисы из базы данных</b> ({len(theses)} записей):\n"]
    for i, thesis in enumerate(theses, start=1):
        lines.append(f"{i}. {thesis}")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_text = update.message.text
    chat_id = update.effective_chat.id
    user = update.effective_user

    # Обработка нажатий кнопок клавиатуры
    if user_text == BTN_THESES:
        await cmd_theses(update, context)
        return
    if user_text == BTN_CLEAR:
        await cmd_clear(update, context)
        return

    logger.info(
        "IN  | chat_id=%-12s user=%-20s | %s",
        chat_id,
        f"{user.full_name} (id={user.id})",
        user_text,
    )

    await update.message.chat.send_action("typing")

    # Инициализируем таблицу пользователя при первом обращении
    database.init_user_table(user.id)

    # Загружаем накопленные тезисы из БД
    db_theses = database.get_all_theses(user.id)

    history[chat_id].append({"role": "user", "content": user_text})

    # История без последнего сообщения пользователя — оно передаётся отдельным user-блоком
    context_history = list(history[chat_id])[:-1]

    system_prompt_with_context = SYSTEM_PROMPT + FORMAT_INSTRUCTIONS

    if db_theses:
        system_prompt_with_context += (
            f"\n\nТезисы из базы данных о пользователе ({len(db_theses)} записей):\n"
            + "\n".join(f"- {t}" for t in db_theses)
        )

    if context_history:
        system_prompt_with_context += (
            f"\n\nИстория диалога ({len(context_history)} сообщений):\n"
            + "\n".join(
                f"{'Пользователь' if m['role'] == 'user' else 'Ассистент'}: {m['content']}"
                for m in context_history
            )
        )

    logger.debug(
        "Запрос OpenAI | model=%s | history=%d | db_theses=%d | max_tokens=%d | temperature=%s",
        MODEL_NAME, len(context_history), len(db_theses), MAX_TOKENS, TEMPERATURE,
    )

    try:
        response = client.beta.chat.completions.parse(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt_with_context},
                {"role": "user", "content": user_text},
            ],
            response_format=NemoResponse,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
        )
        parsed: NemoResponse = response.choices[0].message.parsed
        answer = parsed.message
        theses = parsed.theses
        usage = response.usage
    except Exception as exc:
        logger.exception("OpenAI request failed: %s", exc)
        history[chat_id].pop()
        await update.message.reply_text(
            "Извини, что-то пошло не так. Пожалуйста, попробуй ещё раз.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    history[chat_id].append({"role": "assistant", "content": answer})

    # Сохраняем свежие тезисы в БД
    database.save_theses(user.id, theses)

    logger.info(
        "OUT | chat_id=%-12s tokens=(%s+%s=%s)",
        chat_id,
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.total_tokens,
    )

    if theses:
        logger.info(
            "THESES | chat_id=%s\n%s",
            chat_id,
            "\n".join(f"  [{i + 1}] {t}" for i, t in enumerate(theses)),
        )

    await update.message.reply_text(answer, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN не задан в .env")
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY не задан в .env")

    logger.info("Starting Nemo bot (model=%s, max_history=%s)", MODEL_NAME, MAX_HISTORY)
    logger.info("Logs directory: %s", LOG_DIR.resolve())

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("theses", cmd_theses))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Nemo bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
