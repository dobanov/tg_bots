import logging
import asyncio
import re
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from datetime import datetime

# Настроим логирование
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)
# Отключаем логи для библиотеки httpx
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)  # Показывать только предупреждения и ошибки

TELEGRAM_TOKEN = '111:AAA'  # Токен бота
LIMIT = 3  # Количество отправляемых сообщений
POLL_INTERVAL_SECONDS = 14400 # Интервал ожидания в секундах
URLS_FILE = "urls.txt"  # Файл для хранения URL
OZON_URL_PATTERN = re.compile(r"^https:\/\/(?:www\.)?ozon\.ru\/(?:category|search|collection|brand)\/", re.IGNORECASE)

allowed_ids = []  # Список разрешенных ID пользователей
active_tasks = {}  # Список активных задач для пользователей

# Универсальная функция для работы с файлами
def read_file_lines(filename):
    try:
        with open(filename, "r") as f:
            return f.readlines()
    except FileNotFoundError:
        logger.error(f"Файл {filename} не найден.")
        return []

# Функция для загрузки разрешенных ID из файла
def load_allowed_ids():
    global allowed_ids
    allowed_ids = [line.strip() for line in read_file_lines("ids.txt")]
    logger.info("Файл ids.txt успешно загружен.")

# Удаление URL из файла
def remove_url(user_id):
    lines = read_file_lines(URLS_FILE)
    write_file_lines(URLS_FILE, [line for line in lines if not line.startswith(f"{user_id}|")])
    logger.info(f"URL для пользователя {user_id} удален.")

#Получает URL и время последней отправки для пользователя.
def get_url_for_user(user_id):
    try:
        with open(URLS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line or "|" not in line:  # Пропускаем некорректные строки
                    logger.warning(f"Некорректная строка в файле: {line}")
                    continue
                try:
                    stored_user_id, stored_url, _ = line.split("|", 2)  # Игнорируем дату
                    if stored_user_id == user_id:
                        return stored_url, _  # Возвращаем URL и дату (если нужна)
                except ValueError as e:
                    logger.error(f"Ошибка при обработке строки: {line}. Детали: {e}")
    except FileNotFoundError:
        logger.error("Файл urls.txt не найден.")
    return None, None

# Восстановление задач из файла
async def restore_tasks():
    try:
        with open(URLS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line or "|" not in line:
                    continue
                try:
                    user_id, url, _ = line.split("|", 2)
                    if user_id in allowed_ids:
                        task = asyncio.create_task(send_results(user_id, url))
                        if user_id not in active_tasks:
                            active_tasks[user_id] = []
                        active_tasks[user_id].append((task, url))
                        logger.info(f"Восстановлено отслеживание для пользователя {user_id} по URL: {url}")
                except ValueError as e:
                    logger.error(f"Ошибка при восстановлении строки: {line}. Детали: {e}")
    except FileNotFoundError:
        logger.warning("Файл urls.txt не найден. Восстанавливать нечего.")

# Парсинг страницы
async def parse_page(url):
    options = Options()
    options.add_argument("--headless")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.5735.199 Safari/537.36")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    service = Service("/path/to/chromedriver/chromedriver")
    driver = webdriver.Chrome(service=service, options=options)

    results = []
    try:
        driver.get(url)
        await asyncio.sleep(5)
        soup = BeautifulSoup(driver.page_source, "html.parser")

        # Ищем все элементы с тегом <div> и атрибутом data-index
        items = soup.find_all("div", attrs={"data-index": True}, limit=LIMIT)

        for item in items:
            link_tag = item.find("a", attrs={"data-prerender": "true"})
            if link_tag and link_tag.has_attr("href"):
                full_url = "https://www.ozon.ru" + link_tag["href"].split("?")[0]
            else:
                continue
            price_tag = item.find("span", class_="tsHeadline500Medium")
            price = price_tag.text.strip() if price_tag else "Цена не найдена"

            # Ищем изображение с атрибутом loading равным "eager" или "lazy"
            img_tag = item.find("img", attrs={"loading": ("eager", "lazy")})
            img_url = img_tag["src"] if img_tag and img_tag.has_attr("src") else "Картинка не найдена"
            results.append((f"Ссылка: {full_url}\nЦена: {price}", img_url))
    except Exception as e:
        logger.error(f"Ошибка при парсинге страницы {url}: {e}")
    finally:
        driver.quit()
    return results

# Отправка результатов пользователю
async def send_results(user_id, url):
    while True:
        results = await parse_page(url)
        if not results:
            logger.warning(f"Нет данных для отправки пользователю {user_id} по URL: {url}")
        else:
            for item_message, img_url in results:
                await send_text_to_telegram(user_id, item_message, img_url)
        update_last_sent_time(user_id, url)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)  # Ожидание

# Функция для обновления времени последней отправки в файле
def update_last_sent_time(user_id, url):
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(URLS_FILE, "r") as f:
            lines = f.readlines()

        with open(URLS_FILE, "w") as f:
            for line in lines:
                line = line.strip()
                if not line or "|" not in line:
                    continue
                try:
                    stored_user_id, stored_url, _ = line.split("|", 2)
                    if stored_user_id == user_id and stored_url == url:
                        f.write(f"{user_id}|{url}|{current_time}\n")
                    else:
                        f.write(line + "\n")
                except ValueError:
                    logger.warning(f"Некорректная строка в файле: {line}")
    except FileNotFoundError:
        logger.error("Файл urls.txt не найден.")

# Отправка сообщения в Telegram
async def send_text_to_telegram(chat_id, message, img_url=None):
    send_photo_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        if img_url:
            photo_data = {"chat_id": chat_id, "photo": img_url, "caption": message}
            response_photo = requests.post(send_photo_url, data=photo_data)
            if response_photo.status_code == 200:
                logger.info(f"Успешно отправлено в чат ID {chat_id}")
            else:
                logger.error(f"Ошибка при отправке: {response_photo.text}")
        else:
            logger.warning(f"Пустая ссылка на изображение для чата ID {chat_id}")
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения в Telegram: {e}")

# Обработчик команды /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Привет! Отправьте мне URL страницы Ozon, чтобы я начал отслеживать.')

# Обработчик текстовых сообщений (получаем URL)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    url = update.message.text.strip()

    # Проверка доступа по ID
    if user_id not in allowed_ids:
        await update.message.reply_text("Ваш ID не в списке разрешенных. Доступ закрыт.")
        return

    # Проверка URL Ozon
    if OZON_URL_PATTERN.match(url):
        # Проверка, отслеживается ли уже URL
        if user_id in active_tasks:
            urls_tracked = [tracked_url for _, tracked_url in active_tasks[user_id]]
            if url in urls_tracked:
                await update.message.reply_text("Этот URL уже отслеживается.")
                return

        # Сохраняем URL, если он еще не отслеживается
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(URLS_FILE, "a") as f:
            f.write(f"{user_id}|{url}|{current_time}\n")
            logger.info(f"URL {url} добавлен для пользователя {user_id} с временем последней отправки {current_time}.")

        # Инициализация задач, если их нет
        if user_id not in active_tasks:
            active_tasks[user_id] = []

        # Запускаем задачу для нового URL
        task = asyncio.create_task(send_results(user_id, url))
        active_tasks[user_id].append((task, url))
        await update.message.reply_text("Я начал отслеживать эту страницу. Буду отправлять обновления!")
    else:
        await update.message.reply_text("Пожалуйста, отправьте корректный URL страницы Ozon (category, search, collection или brand).")

# Обработчик команды /list
async def list_urls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)

    # Проверяем, есть ли отслеживаемые URL для пользователя
    urls = []
    lines = read_file_lines(URLS_FILE)
    for line in lines:
        line = line.strip()
        if not line or "|" not in line:
            continue
        try:
            stored_user_id, stored_url, timestamp = line.split("|", 2)
            if stored_user_id == user_id:
                urls.append(f"URL: {stored_url}\nПоследняя отправка: {timestamp}")
        except ValueError:
            logger.warning(f"Некорректная строка: {line}")

    if urls:
        # Формируем и отправляем список
        message = "Ваши отслеживаемые URL:\n\n" + "\n\n".join(urls)
    else:
        # Сообщаем, что список пуст
        message = "У вас нет отслеживаемых URL."

    await update.message.reply_text(message)

# Обработчик команды /help
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Привет! Вот список доступных команд:\n\n"
        "/start - Начать работу с ботом.\n"
        "/stop - Остановить отслеживание текущего URL.\n"
        "/list - Показать список ваших отслеживаемых URL.\n"
        "/tasks - Показать список активных отслеживаемых задач.\n"
        "/remove - Удалить url из отслеживания.\n"
        "/help - Показать это сообщение.\n\n"
        "Просто отправьте ссылку на страницу Ozon, чтобы начать её отслеживать!"
    )
    await update.message.reply_text(help_text)

# Обработчик команды /stop
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)

    if user_id in active_tasks:
        for task, _ in active_tasks[user_id]:
            task.cancel()  # Останавливаем задачу
        del active_tasks[user_id]
        remove_url(user_id)  # Удаляем URL из файла
        await update.message.reply_text("Я больше не буду отслеживать ваш URL.")
    else:
        await update.message.reply_text("У вас нет активных задач для остановки.")

# Обработчик команды /tasks
async def tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)

    # Ищем все задачи для пользователя
    user_tasks = active_tasks.get(user_id, [])

    if user_tasks:
        total_tasks = len(user_tasks)
        message_lines = [f"Активные задачи для пользователя {user_id} (всего: {total_tasks}):"]
        for index, (task, url) in enumerate(user_tasks, start=1):
            message_lines.append(f"{index}. Отслеживание URL: {url}")
        message = "\n".join(message_lines)
    else:
        message = "У вас нет активных задач."

    await update.message.reply_text(message)

# Обработчик команды /remove
async def remove_url_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if not context.args:
        await update.message.reply_text("Пожалуйста, укажите URL для удаления. Пример: /remove https://www.ozon.ru/...")
        return

    url_to_remove = context.args[0].strip()
    removed = False

    # Удаление URL из файла
    try:
        with open(URLS_FILE, "r") as f:
            lines = f.readlines()
        with open(URLS_FILE, "w") as f:
            for line in lines:
                stored_user_id, stored_url, _ = line.strip().split("|", 2)
                if stored_user_id == user_id and stored_url == url_to_remove:
                    removed = True
                else:
                    f.write(line)
        if removed:
            # Остановка задачи отслеживания
            if user_id in active_tasks:
                for task, url in active_tasks[user_id]:
                    if url == url_to_remove:
                        task.cancel()
                        active_tasks[user_id].remove((task, url))
            await update.message.reply_text(f"URL {url_to_remove} удален из отслеживания.")
        else:
            await update.message.reply_text("Указанный URL не найден в вашем списке отслеживания.")
    except FileNotFoundError:
        logger.error("Файл urls.txt не найден.")
        await update.message.reply_text("Ошибка: файл с URL не найден.")

# Настройка и запуск бота
def main():
    load_allowed_ids()
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).read_timeout(30).write_timeout(30).connect_timeout(30).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("list", list_urls))
    application.add_handler(CommandHandler("tasks", tasks))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("remove", remove_url_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def async_main():
        await restore_tasks()
        logger.info("Бот запущен и восстановил задачи.")
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        try:
            await asyncio.Event().wait()
        finally:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()

    try:
        asyncio.run(async_main())
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")

if __name__ == "__main__":
    main()
