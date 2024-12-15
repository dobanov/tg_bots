import re
import time
import os
import subprocess
import requests
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
    ]
)
logger = logging.getLogger(__name__)

# Ваш Telegram токен
TOKEN = "YOUR:TOKEN"
YOUTUBE_REGEX = r"^https://www\.youtube\.com/watch\?v=[a-zA-Z0-9_-]{1,15}$"

# Время задержки между запросами (3 минуты)
TIME_LIMIT = 180
last_message_time = {}  # Словарь для хранения времени последнего запроса
allowed_ids = []  # ID пользователей, которым разрешено использовать бота

# Максимальный размер файла Telegram (50 МБ)
MAX_FILE_SIZE = 52428700

# Функции для работы с Telegram API
def send_video(file_path, user_id, caption):
    """Отправляет видео пользователю через Telegram Bot API."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendVideo"
    try:
        with open(file_path, 'rb') as video_file:
            files = {'video': video_file}
            data = {'chat_id': user_id, 'caption': caption, 'supports_streaming': True}
            response = requests.post(url, files=files, data=data)
            if response.status_code == 200:
                logger.info(f"Видео {file_path} успешно отправлено пользователю {user_id}.")
            else:
                logger.error(f"Ошибка отправки видео пользователю {user_id}: {response.text}")
    except Exception as e:
        logger.exception(f"Ошибка при отправке видео {file_path}: {e}")

# Функции для работы с видео
def split_video(file_path, output_prefix):
    """Разбивает видео на части, каждая не превышает 50 МБ."""
    max_part_size = 50000000  # Чуть меньше 50 МБ
    total_size = os.path.getsize(file_path)
    try:
        with open(os.devnull, 'w') as devnull:
            duration = float(subprocess.check_output([
                'ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of',
                'default=noprint_wrappers=1:nokey=1', file_path
            ]).decode().strip())

            approx_part_time = duration * max_part_size / total_size
            part_time = max(10, int(approx_part_time))

            subprocess.run([
                'ffmpeg', '-i', file_path, '-c', 'copy', '-map', '0', '-segment_time',
                str(part_time), '-f', 'segment', '-reset_timestamps', '1',
                f"{output_prefix}_part_%03d.mp4"
            ], stdout=devnull, stderr=devnull)
        logger.info(f"Видео {file_path} успешно разбито на части.")
    except Exception as e:
        logger.exception(f"Ошибка при разбиении видео {file_path}: {e}")


def download_video(url, output_file):
    """Скачивает видео с YouTube с помощью yt-dlp."""
    try:
        with open(os.devnull, 'w') as devnull:
            subprocess.run(
                ['yt-dlp', '-f', 'best[height<=720][ext=mp4]', '-o', output_file, '--force-overwrites', url],
                stdout=devnull, stderr=devnull, check=True
            )
        logger.info(f"Видео по URL {url} успешно загружено в файл {output_file}.")
        return os.path.exists(output_file)
    except subprocess.CalledProcessError as e:
        logger.error(f"Ошибка при скачивании видео {url}: {e}")
        return False

def process_video(url, user_id):
    """Скачивает и отправляет видео, разбивая на части при необходимости."""
    output_file = "video.mp4"
    logger.info(f"Пользователь {user_id} запросил загрузку видео: {url}")

    if not download_video(url, output_file):
        logger.error(f"Не удалось загрузить видео: {url}")
        last_message_time.pop(user_id, None)  # Сброс таймера
        send_message(user_id, "Видео недоступно или не может быть загружено.")
        return

    try:
        if os.path.getsize(output_file) <= MAX_FILE_SIZE:
            send_video(output_file, user_id, caption=url)
        else:
            logger.info(f"Видео {output_file} превышает 50 МБ. Разбиваем на части...")
            split_video(output_file, "video_part")
            for part in sorted(os.listdir('.')):
                if part.startswith("video_part") and part.endswith(".mp4"):
                    send_video(part, user_id, caption=f"{url} (часть {part})")
                    os.remove(part)
        os.remove(output_file)
    except Exception as e:
        logger.exception(f"Ошибка при обработке видео {url}: {e}")
        last_message_time.pop(user_id, None)  # Сброс таймера
        send_message(user_id, "Произошла ошибка при обработке видео.")

# Команды бота
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    logger.info(f"Пользователь {user_id} вызвал команду /start.")
    await update.message.reply_text("Привет! Пришли мне URL видео на YouTube, и я обработаю его.")

def send_message(user_id, text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        response = requests.post(url, data={'chat_id': user_id, 'text': text})
        if response.status_code != 200:
            logger.error(f"Ошибка отправки сообщения пользователю {user_id}: {response.text}")
    except Exception as e:
        logger.exception(f"Ошибка при отправке сообщения пользователю {user_id}: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_message = update.message.text.strip()
    current_time = time.time()

    logger.info(f"Получено сообщение от пользователя {user_id}: {user_message}")

    if str(user_id) not in allowed_ids:
        await update.message.reply_text("Ты не авторизован для использования этого бота.")
        logger.warning(f"Несанкционированный доступ: {user_id}")
        return

    if not re.match(YOUTUBE_REGEX, user_message):
        await update.message.reply_text("Пришли корректный URL видео на YouTube.")
        return

    if user_id in last_message_time and current_time - last_message_time[user_id] < TIME_LIMIT:
        await update.message.reply_text("Подожди 3 минуты перед отправкой следующего URL.")
        return

    last_message_time[user_id] = current_time
    await update.message.reply_text("Видео обрабатывается, подожди...")
    process_video(user_message, user_id)

# Загрузка разрешённых ID
def load_allowed_ids():
    global allowed_ids
    try:
        with open("ids.txt", "r") as f:
            allowed_ids = [line.strip() for line in f]
        logger.info("Файл ids.txt успешно загружен.")
    except FileNotFoundError:
        logger.error("Файл ids.txt не найден.")

# Главная функция
def main():
    load_allowed_ids()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен. Нажми Ctrl+C для остановки.")
    app.run_polling()

if __name__ == "__main__":
    main()
