"""
==============================================================================
Автоматизация еженедельного отчета по лидам: Bitrix24 -> Google Sheets
==============================================================================

Бизнес-логика:
- Отчетный период: Среда (00:00) — Вторник (23:59)
- Правило А: Все лиды, созданные Ср-Вт, разбитые по дням
- Правило Б: Целевые лиды Ср-Пн + «хвосты» прошлого Вторника (Вт прошлой недели
  если они стали целевыми в текущем периоде), без Вт текущего периода

Автор: сгенерировано Antigravity AI
==============================================================================
"""

import os
import json
import logging
import requests
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo  # Python 3.9+
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Настройка логирования
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Константы — ЗАМЕНИТЕ ПОД СВОЙ БИТРИКС24
# ---------------------------------------------------------------------------

# ── Маппинг SOURCE_ID -> Категория ─────────────────────────────────────────
# Сгенерировано автоматически на основе вашего списка источников из CRM
SOURCE_MAP: dict[str, str] = {
    # Я.Директ
    "ADVERTISING":      "Я.Директ",

    # SEO
    "WEB":              "SEO",          # Веб-сайт
    "10":               "SEO",          # Сайт nat-advance.ru
    "11":               "SEO",          # Сайт barssport.com
    "UC_OKOQMS":        "SEO",          # Сайт na-corporate.ru

    # Вход. звонок
    "CALL":             "Вход. звонок", # Звонок
    "CALLBACK":         "Вход. звонок", # Обратный звонок
    "8":                "Вход. звонок", # Звонок на номер: 78005007990
    "12":               "Вход. звонок", # Звонок на номер: 78432126367
    "13":               "Вход. звонок", # Звонок на номер: 78432126361
    "14":               "Вход. звонок", # Звонок на номер: 79952225983
    "15":               "Вход. звонок", # Звонок на номер: 78432126364
    "16":               "Вход. звонок", # Звонок на номер: 74991134939
    "17":               "Вход. звонок", # Звонок на номер: 74993467174
    "18":               "Вход. звонок", # Звонок на номер: 74993502734
    "19":               "Вход. звонок", # Звонок на номер: 74994041625
    "20":               "Вход. звонок", # Звонок на номер: 74994041943
    "21":               "Вход. звонок", # Звонок на номер: 74994907608
    "22":               "Вход. звонок", # Звонок на номер: 74991133791
    "23":               "Вход. звонок", # Звонок на номер: 79315210658

    # Авито
    "4|AVITO":          "Авито",        # Avito - Avito
}

# Строки таблицы (фиксированный порядок — как в сквозной аналитике Битрикс24)
TABLE_ROWS: list[str] = [
    "ВКонтакте",
    "Я.Директ e-17479930",
    "Прочий трафик",
    "Email Маркетинг",
    "Карты",
    "Avito",
    "SEO barssport.com",
    "SEO nat-advance.ru",
    "No Cookie",
    "Входящий звонок"
]

# Статусы мусорных лидов, которые не должны учитываться при подсчете общего количества лидов (Правило А)
EXCLUDED_STATUSES: set[str] = {
    "3",  # Спам/Рассылка
    "4",  # Дубль
    "5",  # Тест
    "6",  # Клиент существует в базе
    "7",  # Неверный номер
}

# Статусы лидов, которые НЕ считаются целевыми (любые другие статусы считаются целевыми)
NON_TARGET_STATUSES: set[str] = {
    "3",     # Спам/Рассылка
    "4",     # Дубль
    "5",     # Тест
    "6",     # Клиент существует в базе
    "7",     # Неверный номер
    "8",     # Ребенок
    "9",     # 1 комплект
    "17",    # Не берет трубку
    "18",    # Не наш ассортимент
    "JUNK",  # Заявку не оставлял
}

# ── Временная зона ───────────────────────────────────────────────────────────
# Измените на вашу, если CRM работает в другой зоне
TZ = ZoneInfo("Europe/Moscow")

# ---------------------------------------------------------------------------
# Чтение переменных среды (GitHub Secrets)
# ---------------------------------------------------------------------------

def get_env(key: str) -> str:
    """Получает обязательную переменную среды или завершает программу с ошибкой."""
    value = os.environ.get(key)
    if not value:
        raise EnvironmentError(
            f"Переменная среды '{key}' не задана. "
            "Убедитесь, что она добавлена в GitHub Secrets."
        )
    return value


# ---------------------------------------------------------------------------
# Вычисление временных окон
# ---------------------------------------------------------------------------

def compute_date_windows(today: date | None = None) -> dict:
    """
    Вычисляет все временные окна для отчёта.

    Аргументы:
        today: Дата запуска (среда). Если None — берётся сегодня.

    Возвращает словарь с ключами:
        current_wed       — Среда текущего отчётного периода
        current_tue       — Вторник, которым закрывается период
        prev_tue_start    — Начало прошлого Вторника (дата 2 недели назад)
        prev_tue_end      — Конец прошлого Вторника
        days_of_week      — Упорядоченный список (date, label) по дням недели
    """
    if today is None:
        today = datetime.now(tz=TZ).date()

    # Если сегодня не среда — находим ближайшую прошедшую среду
    # weekday(): Пн=0, Вт=1, Ср=2, Чт=3, Пт=4, Сб=5, Вс=6
    days_since_wed = (today.weekday() - 2) % 7
    current_wed = today - timedelta(days=days_since_wed)

    # Вторник = Среда + 6 дней назад + 7 дней = Среда - 1 день
    current_tue = current_wed + timedelta(days=6)

    # Вторник прошлой недели = 7 дней до current_tue
    prev_tue = current_tue - timedelta(days=7)

    log.info("Отчётный период: %s (Ср) — %s (Вт)", current_wed, current_tue)
    log.info("Прошлый вторник (хвосты): %s", prev_tue)

    # Формируем список дней для колонок таблицы
    # Порядок: Ср, Чт, Пт, Сб+Вс (объединены), Пн, Вт
    days = []
    for offset, label in [
        (0, "Ср"),   # Среда
        (1, "Чт"),   # Четверг
        (2, "Пт"),   # Пятница
        (3, "Сб"),   # Суббота  ─┐ если Сб/Вс объединены в одну колонку,
        (4, "Вс"),   # Воскресенье ┘ скрипт суммирует их
        (5, "Пн"),   # Понедельник
        (6, "Вт"),   # Вторник
    ]:
        days.append((current_wed + timedelta(days=offset), label))

    return {
        "current_wed": current_wed,
        "current_tue": current_tue,
        "prev_tue": prev_tue,
        "days": days,  # [(date, label), ...]
    }


def to_bitrix_dt(d: date, end_of_day: bool = False) -> str:
    """
    Конвертирует дату Python в строку формата, ожидаемого Битрикс24 API.
    Битрикс24 принимает ISO 8601: "2026-05-27T00:00:00+03:00"
    """
    t = datetime(d.year, d.month, d.day, tzinfo=TZ)
    if end_of_day:
        t = t.replace(hour=23, minute=59, second=59)
    return t.isoformat()


# ---------------------------------------------------------------------------
# Запросы к Битрикс24 API
# ---------------------------------------------------------------------------

def bitrix_request(webhook_url: str, method: str, params: dict) -> list[dict]:
    """
    Выполняет запрос к REST API Битрикс24 с автоматической пагинацией.

    Битрикс24 возвращает максимум 50 записей за раз.
    Функция итерирует по страницам и возвращает полный список.
    """
    results = []
    start = 0

    while True:
        payload = {**params, "start": start}
        url = f"{webhook_url.rstrip('/')}/{method}"

        try:
            resp = requests.post(url, json=payload, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("Ошибка запроса к Битрикс24: %s", e)
            raise

        data = resp.json()

        if "error" in data:
            raise RuntimeError(
                f"Ошибка API Битрикс24: {data.get('error_description', data['error'])}"
            )

        batch = data.get("result", [])
        results.extend(batch)

        total = int(data.get("total", 0))
        start += len(batch)

        log.debug("Получено %d / %d лидов", len(results), total)

        if start >= total or not batch:
            break

    return results


def fetch_leads(
    webhook_url: str,
    date_from: date,
    date_to: date,
    extra_filter: dict | None = None,
) -> list[dict]:
    """
    Запрашивает список лидов из Битрикс24 за указанный период.

    Аргументы:
        webhook_url  — URL входящего вебхука
        date_from    — Начало периода (включительно)
        date_to      — Конец периода (включительно)
        extra_filter — Дополнительные фильтры (например, по статусу)

    Возвращает список лидов (каждый — словарь с полями API).
    """
    # Поля, которые запрашиваем у API
    # SOURCE_ID — источник лида (для маппинга)
    # STATUS_ID — статус лида (для фильтрации целевых)
    # DATE_CREATE — дата создания
    # DATE_MODIFY — дата последнего изменения (нужна для хитрой логики Вт)
    # UTM_SOURCE — UTM-метка источника (запасной вариант)
    # UF_CRM_TRACKING_SOURCE_TXT — Источник сквозной аналитики (приоритетный вариант)
    fields = ["ID", "SOURCE_ID", "STATUS_ID", "DATE_CREATE", "DATE_MODIFY", "UTM_SOURCE", "UF_CRM_TRACKING_SOURCE_TXT"]

    filter_params: dict = {
        ">=DATE_CREATE": to_bitrix_dt(date_from),
        "<=DATE_CREATE": to_bitrix_dt(date_to, end_of_day=True),
    }

    if extra_filter:
        filter_params.update(extra_filter)

    params = {
        "filter": filter_params,
        "select": fields,
        "order": {"DATE_CREATE": "ASC"},
    }

    log.info(
        "Запрос лидов: %s — %s | доп. фильтр: %s",
        date_from, date_to, extra_filter or "нет",
    )

    leads = bitrix_request(webhook_url, "crm.lead.list", params)
    log.info("Получено %d лидов", len(leads))
    return leads


# ---------------------------------------------------------------------------
# Бизнес-логика: агрегация лидов
# ---------------------------------------------------------------------------

def get_source_category(lead: dict) -> str:
    """
    Определяет категорию строки таблицы по Источнику сквозной аналитики (UF_CRM_TRACKING_SOURCE_TXT).
    """
    tracking_src = str(lead.get("UF_CRM_TRACKING_SOURCE_TXT") or "").strip().lower()

    if tracking_src:
        if "я.директ" in tracking_src or "direct" in tracking_src or "yandex" in tracking_src:
            return "Я.Директ e-17479930"
        if "seo barssport.com" in tracking_src or "barssport.com" in tracking_src:
            return "SEO barssport.com"
        if "seo nat-advance.ru" in tracking_src or "nat-advance.ru" in tracking_src:
            return "SEO nat-advance.ru"
        if "google ads" in tracking_src or "google_ads" in tracking_src:
            return "Прочий трафик"  # Google Ads удален из таблицы, направляем в Прочий трафик
        if "вконтакте" in tracking_src or "vk" in tracking_src:
            return "ВКонтакте"
        if "звонок" in tracking_src or "call" in tracking_src:
            return "Входящий звонок"
        if "avito" in tracking_src or "авито" in tracking_src:
            return "Avito"
        if "email" in tracking_src or "рассылка" in tracking_src:
            return "Email Маркетинг"
        if "карты" in tracking_src or "maps" in tracking_src:
            return "Карты"
        if "no cookie" in tracking_src:
            return "No Cookie"
        if "прочий" in tracking_src or "other" in tracking_src:
            return "Прочий трафик"

        # Если не подошло под ключевые слова, но совпадает с одной из строк таблицы
        for row in TABLE_ROWS:
            if row.lower() == tracking_src:
                return row

    # Резервный вариант по SOURCE_ID (для старых или ручных лидов)
    source_id = lead.get("SOURCE_ID", "") or ""
    utm_source = lead.get("UTM_SOURCE", "") or ""

    if source_id in SOURCE_MAP:
        mapped = SOURCE_MAP[source_id]
        # Сопоставляем старые названия с новыми
        mapping_old_to_new = {
            "Я.Директ": "Я.Директ e-17479930",
            "SEO": "SEO barssport.com",
            "Вход. звонок": "Входящий звонок",
            "Авито": "Avito"
        }
        return mapping_old_to_new.get(mapped, "Прочий трафик")

    # Если SOURCE_ID не в маппинге — проверяем UTM_SOURCE
    utm_lower = utm_source.lower()
    if "yandex" in utm_lower or "direct" in utm_lower or "ya" in utm_lower:
        return "Я.Директ e-17479930"
    if "avito" in utm_lower:
        return "Avito"
    if "seo" in utm_lower or "organic" in utm_lower or "google" in utm_lower:
        return "SEO barssport.com"

    return "Прочий трафик"


def aggregate_leads(windows: dict, all_leads: list[dict], tail_leads: list[dict]) -> dict:
    """
    Агрегирует лиды по правилам А и Б.

    Правило А: Считаем все лиды Ср-Вт по дням и источникам.
    Правило Б: Целевые лиды Ср-Пн + «хвосты» прошлого Вт.

    Возвращает структуру:
    {
        "daily": {
            "Я.Директ": {"Ср": 6, "Чт": 5, "Пт": 5, "Сб": 0, "Вс": 0, "Пн": 18, "Вт": 7, "Итого": 41},
            ...
        },
        "targeted": {"Я.Директ": 25, "SEO": 15, ...},
    }
    """
    days = windows["days"]          # [(date, label), ...]
    current_wed = windows["current_wed"]
    current_tue = windows["current_tue"]
    prev_tue = windows["prev_tue"]

    # Словарь date -> label для быстрого поиска
    date_to_label: dict[date, str] = {d: lbl for d, lbl in days}

    # Инициализируем структуру результата
    daily: dict[str, dict[str, int]] = {}
    targeted: dict[str, int] = {}

    for row in TABLE_ROWS:
        daily[row] = {lbl: 0 for _, lbl in days}
        daily[row]["Итого"] = 0
        targeted[row] = 0

    # ── Правило А: Дневная разбивка всех лидов ──────────────────────────────
    for lead in all_leads:
        # Исключаем мусорные лиды из общей статистики по дням
        status = lead.get("STATUS_ID", "")
        if status in EXCLUDED_STATUSES:
            continue

        # Дата создания из API: "2026-05-27T14:30:00+03:00"
        raw_date = lead.get("DATE_CREATE", "")
        try:
            created_dt = datetime.fromisoformat(raw_date).astimezone(TZ)
            created_date = created_dt.date()
        except (ValueError, TypeError):
            log.warning("Неверный формат DATE_CREATE у лида %s: %s", lead.get("ID"), raw_date)
            continue

        category = get_source_category(lead)
        if category not in daily:
            continue  # Неизвестный источник — пропускаем

        label = date_to_label.get(created_date)
        if label:
            daily[category][label] += 1
            daily[category]["Итого"] += 1

    # ── Правило Б: Целевые лиды Ср-Пн (без Вт!) ────────────────────────────
    # Граница: от current_wed до (current_tue - 1 день) = Понедельника
    current_mon = current_tue - timedelta(days=1)

    for lead in all_leads:
        raw_date = lead.get("DATE_CREATE", "")
        status = lead.get("STATUS_ID", "")

        try:
            created_date = datetime.fromisoformat(raw_date).astimezone(TZ).date()
        except (ValueError, TypeError):
            continue

        # Лид создан в Вт текущего периода — ПРОПУСКАЕМ (переносится на след. период)
        if created_date == current_tue:
            continue

        # Лид создан в Ср-Пн и является целевым (все статусы кроме NON_TARGET_STATUSES)
        if current_wed <= created_date <= current_mon and status not in NON_TARGET_STATUSES:
            category = get_source_category(lead)
            if category in targeted:
                targeted[category] += 1

    # ── Правило Б: «Хвосты» прошлого Вторника ───────────────────────────────
    # tail_leads — лиды, созданные во Вт прошлой недели и уже ставшие целевыми
    # (API вернул их без фильтра по статусу, фильтруем в Python по дате изменения и целевому статусу)
    for lead in tail_leads:
        status = lead.get("STATUS_ID", "")
        raw_modify = lead.get("DATE_MODIFY", "")

        # Проверяем, что статус стал целевым в текущем отчётном периоде
        # (дата изменения >= current_wed)
        try:
            modify_date = datetime.fromisoformat(raw_modify).astimezone(TZ).date()
        except (ValueError, TypeError):
            modify_date = None

        if status in NON_TARGET_STATUSES:
            continue

        # Если дата изменения не попала в текущий период — пропускаем
        if modify_date and not (current_wed <= modify_date <= current_tue):
            continue

        category = get_source_category(lead)
        if category in targeted:
            targeted[category] += 1
            log.info(
                "Хвост Вт: лид ID=%s добавлен в целевые (%s)",
                lead.get("ID"), category,
            )

    log.info("Агрегация завершена. Целевые: %s", targeted)
    return {"daily": daily, "targeted": targeted}


# ---------------------------------------------------------------------------
# Google Sheets: вспомогательные функции, поиск нужного блока и запись данных
# ---------------------------------------------------------------------------

def get_sheets_service(service_account_json: str):
    """Создаёт авторизованный клиент Google Sheets API."""
    creds_dict = json.loads(service_account_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    service = build("sheets", "v4", credentials=creds)
    return service.spreadsheets()


def get_or_create_sheet_tab(sheets, spreadsheet_id: str, tab_name: str):
    """
    Проверяет, существует ли вкладка с именем tab_name.
    Если нет — создаёт её.
    Возвращает sheet_id вкладки.
    """
    meta = sheets.get(spreadsheetId=spreadsheet_id).execute()
    existing_tabs = [sheet["properties"]["title"] for sheet in meta.get("sheets", [])]
    log.info("Доступные вкладки в таблице: %s", existing_tabs)
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["title"] == tab_name:
            log.info("Вкладка '%s' найдена.", tab_name)
            return sheet["properties"]["sheetId"]

    # Вкладка не найдена — создаём
    log.info("Вкладка '%s' не найдена. Создаём...", tab_name)
    body = {
        "requests": [{
            "addSheet": {
                "properties": {"title": tab_name}
            }
        }]
    }
    resp = sheets.batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()
    return resp["replies"][0]["addSheet"]["properties"]["sheetId"]


def find_week_block_row(sheets, spreadsheet_id: str, tab_name: str, target_wed: date) -> int | None:
    """
    Ищет начальную строку блока для текущей недели в таблице Google Sheets.
    """
    allowed_date_strs = [
        target_wed.strftime("%d.%m"),                           # "06.05"
        f"{target_wed.day}.{target_wed.month:02d}",             # "6.05" (без ведущего нуля)
        target_wed.strftime("%d.%m.%Y"),                        # "06.05.2026"
        f"{target_wed.day}.{target_wed.month:02d}.{target_wed.year}" # "6.05.2026"
    ]

    range_notation = f"'{tab_name}'!A1:M200"
    try:
        result = sheets.values().get(
            spreadsheetId=spreadsheet_id,
            range=range_notation,
        ).execute()
        rows = result.get("values", [])
        for row_idx, row in enumerate(rows):
            for cell in row:
                cell_str = str(cell).strip()
                if cell_str in allowed_date_strs:
                    log.info(
                        "Дата недели найдена в строке %d (значение: '%s')",
                        row_idx + 1, cell_str,
                    )
                    return row_idx + 1  # 1-indexed
    except Exception as e:
        log.warning("Ошибка при чтении листа '%s': %s", tab_name, e)

    log.warning(
        "Блок для недели %s не найден на листе '%s'. Проверяем все остальные листы...",
        target_wed, tab_name,
    )

    try:
        meta = sheets.get(spreadsheetId=spreadsheet_id).execute()
        for sheet in meta.get("sheets", []):
            title = sheet["properties"]["title"]
            if title == tab_name:
                continue
            res = sheets.values().get(
                spreadsheetId=spreadsheet_id,
                range=f"'{title}'!A1:M200",
            ).execute()
            r_rows = res.get("values", [])
            for r_idx, r in enumerate(r_rows):
                for cell in r:
                    cell_str = str(cell).strip()
                    if cell_str in allowed_date_strs:
                        log.info(
                            "💡 НАЙДЕНО на листе '%s' в строке %d (значение: '%s')",
                            title, r_idx + 1, cell_str,
                        )
    except Exception as e:
        log.warning("Не удалось выполнить поиск по всем листам: %s", e)

    return None


def hex_to_rgb(hex_str: str) -> dict:
    """Конвертирует HEX цвет в RGB формат для Google Sheets API (значения от 0.0 до 1.0)."""
    hex_str = hex_str.lstrip('#')
    if len(hex_str) == 3:
        hex_str = ''.join([c*2 for c in hex_str])
    r = int(hex_str[0:2], 16) / 255.0
    g = int(hex_str[2:4], 16) / 255.0
    b = int(hex_str[4:6], 16) / 255.0
    return {"red": r, "green": g, "blue": b}


def make_border(hex_color: str = "#CBD5E1") -> dict:
    """Создаёт объект границы для Google Sheets API."""
    return {
        "style": "SOLID",
        "color": hex_to_rgb(hex_color)
    }


def create_month_template(sheets, spreadsheet_id: str, tab_name: str, sheet_id: int, current_wed: date):
    """
    Создаёт шаблон со всеми неделями месяца на указанной вкладке с профессиональным дизайном и легендой.
    """
    log.info("Шаблон не найден. Создаём структуру недель для листа '%s'...", tab_name)

    # 1. Находим все Среды в текущем месяце
    year = current_wed.year
    month = current_wed.month

    wednesdays = []
    d = date(year, month, 1)
    while d.month == month:
        if d.weekday() == 2:  # Wednesday
            wednesdays.append(d)
        d += timedelta(days=1)

    log.info("Найдено %d недель в месяце: %s", len(wednesdays), [w.strftime("%d.%m") for w in wednesdays])

    # Справка-легенда занимает первые 5 строк (0..4).
    # Индекс 5 - пустая строка-разделитель.
    # Каждая неделя занимает 28 строк (с отступами).
    week_height = 2 + len(TABLE_ROWS) + 1 + 1 + 1 + len(TABLE_ROWS) + 1 + 1 + 1
    num_rows = 6 + len(wednesdays) * week_height
    values = [["" for _ in range(14)] for _ in range(num_rows)]
    
    # Список запросов batchUpdate (для слияний и форматирования)
    requests_list = []

    # ── Справка по логике отчёта (Легенда) ─────────────────────────────────
    values[0][0] = "Справка по логике отчёта (Сквозная аналитика)"
    values[1][0] = "• Период отчёта: Среда (00:00) — Вторник (23:59). Лиды за выходные (Сб и Вс) автоматически суммируются в колонку Понедельника."
    values[2][0] = "• Исключено из общего количества лидов (Дубли, Спам, Тесты, Неверные номера, Клиент существует в базе)."
    values[3][0] = "• Исключено из целевых лидов (Все вышеперечисленное + Ребенок, 1 комплект, Не берет трубку, Не наш ассортимент, Заявку не оставлял)."
    values[4][0] = "• Расчет стоимости лида: Стоимость рассчитывается автоматически по формуле сразу после ручного ввода расходов в колонку 'Израсходованный бюджет'."

    def add_merge(s_r, e_r, s_c, e_c):
        requests_list.append({
            "mergeCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": s_r,
                    "endRowIndex": e_r + 1,
                    "startColumnIndex": s_c,
                    "endColumnIndex": e_c + 1,
                },
                "mergeType": "MERGE_ALL",
            }
        })

    def format_range(s_r, e_r, s_c, e_c, bg_hex=None, fg_hex=None, size=9, bold=False, italic=False, align="CENTER", num_pattern=None):
        cell_format = {}
        fields = []

        if bg_hex:
            cell_format["backgroundColor"] = hex_to_rgb(bg_hex)
            fields.append("backgroundColor")

        text_fmt = {}
        if fg_hex:
            text_fmt["foregroundColor"] = hex_to_rgb(fg_hex)
        text_fmt["fontSize"] = size
        text_fmt["bold"] = bold
        text_fmt["italic"] = italic
        cell_format["textFormat"] = text_fmt
        fields.append("textFormat")

        cell_format["horizontalAlignment"] = align
        cell_format["verticalAlignment"] = "MIDDLE"
        fields.extend(["horizontalAlignment", "verticalAlignment"])

        if num_pattern:
            cell_format["numberFormat"] = {
                "type": "CURRENCY" if "₽" in num_pattern else ("PERCENT" if "%" in num_pattern else "NUMBER"),
                "pattern": num_pattern
            }
            fields.append("numberFormat")

        border = make_border("#CBD5E1")
        cell_format["borders"] = {
            "top": border,
            "bottom": border,
            "left": border,
            "right": border
        }
        fields.append("borders")

        requests_list.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": s_r,
                    "endRowIndex": e_r + 1,
                    "startColumnIndex": s_c,
                    "endColumnIndex": e_c + 1,
                },
                "cell": {
                    "userEnteredFormat": cell_format
                },
                "fields": f"userEnteredFormat({','.join(fields)})"
            }
        })

    # Слияния и стили для Легенды
    add_merge(0, 0, 0, 12)
    add_merge(1, 1, 0, 12)
    add_merge(2, 2, 0, 12)
    add_merge(3, 3, 0, 12)
    add_merge(4, 4, 0, 12)
    
    format_range(0, 0, 0, 12, bg_hex="#1E293B", fg_hex="#FFFFFF", size=11, bold=True)
    format_range(1, 4, 0, 12, bg_hex="#F8FAFC", size=9, align="LEFT")
    format_range(4, 4, 0, 12, bg_hex="#F8FAFC", size=9, align="LEFT", italic=True)

    for idx, wed in enumerate(wednesdays):
        start_row = 6 + idx * week_height

        # Вычисляем даты
        thu = wed + timedelta(days=1)
        fri = wed + timedelta(days=2)
        mon = wed + timedelta(days=5)
        tue = wed + timedelta(days=6)

        d_wed = wed.strftime("%d.%m")
        d_thu = thu.strftime("%d.%m")
        d_fri = fri.strftime("%d.%m")
        d_mon = mon.strftime("%d.%m")
        d_tue = tue.strftime("%d.%m")

        # ── Первая таблица: Данные по лидам ─────────────────────────────────
        # Заголовки (строка start_row)
        values[start_row][0] = "Источник"
        values[start_row][1] = "Период"
        values[start_row][6] = "Итого за неделю"
        values[start_row][7] = "Кол-во целевых"
        values[start_row][8] = "Израсходованный бюджет"
        values[start_row][9] = "Общая цена лида"
        values[start_row][10] = "Цена целевого лида"
        values[start_row][11] = "Динамика количества целевого лида относительной прошлой недели, %"
        values[start_row][12] = "Динамика стоимости целевого лида относительной прошлой недели, %"

        # Подзаголовки дат (строка start_row + 1)
        values[start_row+1][1] = d_wed
        values[start_row+1][2] = d_thu
        values[start_row+1][3] = d_fri
        values[start_row+1][4] = d_mon
        values[start_row+1][5] = d_tue

        # Источники
        for s_idx, src in enumerate(TABLE_ROWS):
            r_idx = start_row + 2 + s_idx
            values[r_idx][0] = src

            # Формулы цены лида: Общая = Бюджет/Лиды, Целевая = Бюджет/Целевые
            r_num = r_idx + 1  # 1-indexed row number in sheet
            values[r_idx][9] = f'=IF(G{r_num}>0; I{r_num}/G{r_num}; "")'
            values[r_idx][10] = f'=IF(H{r_num}>0; I{r_num}/H{r_num}; "")'

        # Строка ИТОГО
        itog_idx = start_row + 2 + len(TABLE_ROWS)
        r_total = itog_idx + 1
        values[itog_idx][0] = "ИТОГО"
        for col_idx in range(1, 9):
            col_letter = chr(65 + col_idx)  # 65 = 'A'
            start_r = r_total - len(TABLE_ROWS)
            end_r = r_total - 1
            values[itog_idx][col_idx] = f'=SUM({col_letter}{start_r}:{col_letter}{end_r})'

        # ── Вторая таблица: Комментарии и планы ─────────────────────────────
        comments_header_idx = itog_idx + 2
        values[comments_header_idx][0] = "Источник"
        values[comments_header_idx][1] = "Комментарий по результату недели"
        values[comments_header_idx][2] = "План мероприятий на следующую неделю"

        for s_idx, src in enumerate(TABLE_ROWS):
            r_c = comments_header_idx + 1 + s_idx
            values[r_c][0] = src

        proposal_idx = comments_header_idx + 1 + len(TABLE_ROWS) + 1
        values[proposal_idx][0] = "Предложение на тест новой площадки:"

        # ── Слияния заголовков ──
        add_merge(start_row, start_row + 1, 0, 0)   # Источник
        add_merge(start_row, start_row + 1, 6, 6)   # Итого за неделю
        add_merge(start_row, start_row + 1, 7, 7)   # Кол-во целевых
        add_merge(start_row, start_row + 1, 8, 8)   # Бюджет
        add_merge(start_row, start_row + 1, 9, 9)   # Общая цена лида
        add_merge(start_row, start_row + 1, 10, 10) # Цена целевого лида
        add_merge(start_row, start_row + 1, 11, 11) # Динамика %
        add_merge(start_row, start_row + 1, 12, 12) # Динамика %
        add_merge(start_row, start_row, 1, 5)        # Period

        # ── Стили заголовков первой таблицы ──
        format_range(start_row, start_row, 0, 12, bg_hex="#1E293B", fg_hex="#FFFFFF", size=10, bold=True)
        format_range(start_row + 1, start_row + 1, 0, 12, bg_hex="#334155", fg_hex="#FFFFFF", size=9, bold=True)

        # ── Стили строк данных ──
        for s_idx, src in enumerate(TABLE_ROWS):
            r_idx = start_row + 2 + s_idx
            bg = "#FFFFFF" if s_idx % 2 == 0 else "#F8FAFC"

            format_range(r_idx, r_idx, 0, 12, bg_hex=bg, size=9)
            format_range(r_idx, r_idx, 0, 0, bg_hex=bg, size=9, bold=True, align="LEFT")
            format_range(r_idx, r_idx, 1, 5, bg_hex=bg, size=9, num_pattern="#,##0")
            format_range(r_idx, r_idx, 6, 6, bg_hex=bg, size=9, bold=True, num_pattern="#,##0")
            format_range(r_idx, r_idx, 7, 7, bg_hex="#ECFDF5", size=9, bold=True, num_pattern="#,##0")
            format_range(r_idx, r_idx, 8, 8, bg_hex="#FEF3C7", size=9, align="RIGHT", num_pattern="#,##0\" ₽\"")
            format_range(r_idx, r_idx, 9, 10, bg_hex=bg, size=9, align="RIGHT", num_pattern="#,##0\" ₽\"")
            format_range(r_idx, r_idx, 11, 12, bg_hex=bg, size=9, num_pattern="0.0%")

        # ── Стили строки ИТОГО ──
        format_range(itog_idx, itog_idx, 0, 12, bg_hex="#E2E8F0", size=9, bold=True)
        format_range(itog_idx, itog_idx, 0, 0, bg_hex="#E2E8F0", size=9, bold=True, align="LEFT")
        format_range(itog_idx, itog_idx, 1, 7, bg_hex="#E2E8F0", size=9, bold=True, num_pattern="#,##0")
        format_range(itog_idx, itog_idx, 8, 8, bg_hex="#E2E8F0", size=9, bold=True, align="RIGHT", num_pattern="#,##0\" ₽\"")

        # ── Стили второй таблицы (Комментарии и планы) ──
        add_merge(comments_header_idx, comments_header_idx, 1, 6)
        add_merge(comments_header_idx, comments_header_idx, 7, 12)
        for s_idx in range(len(TABLE_ROWS)):
            r_c = comments_header_idx + 1 + s_idx
            add_merge(r_c, r_c, 1, 6)
            add_merge(r_c, r_c, 7, 12)
        add_merge(proposal_idx, proposal_idx, 0, 12)

        format_range(comments_header_idx, comments_header_idx, 0, 12, bg_hex="#475569", fg_hex="#FFFFFF", size=9, bold=True)
        format_range(comments_header_idx, comments_header_idx, 0, 0, bg_hex="#475569", fg_hex="#FFFFFF", size=9, bold=True, align="LEFT")

        for s_idx, src in enumerate(TABLE_ROWS):
            r_c = comments_header_idx + 1 + s_idx
            bg = "#FFFFFF" if s_idx % 2 == 0 else "#F8FAFC"
            format_range(r_c, r_c, 0, 12, bg_hex=bg, size=9)
            format_range(r_c, r_c, 0, 0, bg_hex=bg, size=9, bold=True, align="LEFT")

        format_range(proposal_idx, proposal_idx, 0, 12, bg_hex="#F8FAFC", size=9, align="LEFT", italic=True)

    # ── 3. Установка высоты строк ─────────────────────────────────────────
    for r in range(6):
        requests_list.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": r,
                    "endIndex": r + 1
                },
                "properties": {"pixelSize": 24},
                "fields": "pixelSize"
            }
        })

    for idx in range(len(wednesdays)):
        start_row = 6 + idx * week_height
        requests_list.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": start_row,
                    "endIndex": start_row + 1
                },
                "properties": {"pixelSize": 35},
                "fields": "pixelSize"
            }
        })
        requests_list.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": start_row + 1,
                    "endIndex": start_row + 2
                },
                "properties": {"pixelSize": 24},
                "fields": "pixelSize"
            }
        })
        for r_offset in range(len(TABLE_ROWS) + 1):  # Data + ИТОГО
            r_idx = start_row + 2 + r_offset
            requests_list.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": r_idx,
                        "endIndex": r_idx + 1
                    },
                    "properties": {"pixelSize": 24},
                    "fields": "pixelSize"
                }
            })
        c_header = start_row + 2 + len(TABLE_ROWS) + 2
        requests_list.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": c_header,
                    "endIndex": c_header + 1
                },
                "properties": {"pixelSize": 28},
                "fields": "pixelSize"
            }
        })
        for r_offset in range(len(TABLE_ROWS) + 1):  # Comments + Proposal
            r_idx = c_header + 1 + r_offset
            requests_list.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": r_idx,
                        "endIndex": r_idx + 1
                    },
                    "properties": {"pixelSize": 24},
                    "fields": "pixelSize"
                }
            })

    # ── 4. Установка ширины столбцов ──────────────────────────────────────
    col_widths = {
        0: 180,  # Источник
        1: 70, 2: 70, 3: 70, 4: 70, 5: 70,  # Ср, Чт, Пт, Пн, Вт
        6: 120,  # Итого за неделю
        7: 120,  # Кол-во целевых
        8: 160,  # Израсходованный бюджет
        9: 140,  # Общая цена лида
        10: 140, # Цена целевого лида
        11: 140, # Динамика целевых %
        12: 140, # Динамика стоимости %
    }

    for col_idx, width in col_widths.items():
        requests_list.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": col_idx,
                    "endIndex": col_idx + 1
                },
                "properties": {
                    "pixelSize": width
                },
                "fields": "pixelSize"
            }
        })

    # 5. Записываем значения на лист
    range_name = f"'{tab_name}'!A1"
    body = {"values": values}
    sheets.values().update(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        valueInputOption="USER_ENTERED",
        body=body,
    ).execute()

    # 6. Отправляем слияния и форматирование
    if requests_list:
        sheets.batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests_list},
        ).execute()

    log.info("Шаблон на лист '%s' успешно записан.", tab_name)


def build_update_requests(
    sheet_id: int,
    header_row: int,
    windows: dict,
    aggregated: dict,
) -> list[dict]:
    """
    Формирует список batchUpdate requests для записи данных в Google Sheets.
    """
    # ── Маппинг столбцов (0-indexed) ────────────────────────────────────────
    COL_SOURCE   = 0   # Столбец A — "Источник"
    COL_WED      = 1   # Столбец B — Среда
    COL_THU      = 2   # Столбец C — Четверг
    COL_FRI      = 3   # Столбец D — Пятница
    COL_MON      = 4   # Столбец E — Понедельник (Сб+Вс+Пн)
    COL_TUE      = 5   # Столбец F — Вторник
    COL_TOTAL    = 6   # Столбец G — "Итого за неделю"
    COL_TARGETED = 7   # Столбец H — "Кол-во целевых"
    COL_CR       = 8   # Столбец I — "Конверсия в целевой, %"
    COL_BUDGET   = 9   # Столбец J — "Израсходованный бюджет" (ручной ввод)
    COL_SHARE    = 10  # Столбец K — "Доля бюджета, %"
    COL_AVG_LEAD = 11  # Столбец L — "Общая цена лида" (формула)
    COL_TGT_LEAD = 12  # Столбец M — "Цена целевого лида" (формула)

    daily = aggregated["daily"]
    targeted = aggregated["targeted"]

    requests_list = []

    def cell_update(row_0idx: int, col_0idx: int, value) -> dict:
        return {
            "updateCells": {
                "rows": [{
                    "values": [{
                        "userEnteredValue": (
                            {"numberValue": value}
                            if isinstance(value, (int, float))
                            else {"stringValue": str(value)}
                        )
                    }]
                }],
                "fields": "userEnteredValue",
                "start": {
                    "sheetId": sheet_id,
                    "rowIndex": row_0idx,
                    "columnIndex": col_0idx,
                },
            }
        }

    def formula_update(row_0idx: int, col_0idx: int, formula: str) -> dict:
        return {
            "updateCells": {
                "rows": [{
                    "values": [{
                        "userEnteredValue": {"formulaValue": formula}
                    }]
                }],
                "fields": "userEnteredValue",
                "start": {
                    "sheetId": sheet_id,
                    "rowIndex": row_0idx,
                    "columnIndex": col_0idx,
                },
            }
        }

    # Суммируем по каждому источнику
    for row_offset, source_name in enumerate(TABLE_ROWS):
        # header_row - это 1-indexed строка подзаголовков (с датами).
        # Data-строки начинаются сразу под ней (т.е. на индексе header_row в 0-indexed системе).
        data_row_0idx = header_row + row_offset

        requests_list.append(cell_update(data_row_0idx, COL_SOURCE, source_name))

        row_data = daily.get(source_name, {})

        # Подготовка данных по дням
        val_wed = row_data.get("Ср", 0)
        val_thu = row_data.get("Чт", 0)
        val_fri = row_data.get("Пт", 0)
        val_mon = row_data.get("Сб", 0) + row_data.get("Вс", 0) + row_data.get("Пн", 0)
        val_tue = row_data.get("Вт", 0)

        requests_list.append(cell_update(data_row_0idx, COL_WED, val_wed))
        requests_list.append(cell_update(data_row_0idx, COL_THU, val_thu))
        requests_list.append(cell_update(data_row_0idx, COL_FRI, val_fri))
        requests_list.append(cell_update(data_row_0idx, COL_MON, val_mon))
        requests_list.append(cell_update(data_row_0idx, COL_TUE, val_tue))

        # Итого за неделю по лидам (сумма дней)
        total_leads = val_wed + val_thu + val_fri + val_mon + val_tue
        requests_list.append(cell_update(data_row_0idx, COL_TOTAL, total_leads))

        # Кол-во целевых
        tgt = targeted.get(source_name, 0)
        requests_list.append(cell_update(data_row_0idx, COL_TARGETED, tgt))

        # Формулы (с разделителем ; для русской локали)
        r = data_row_0idx + 1
        r_total = (header_row + len(TABLE_ROWS)) + 1

        # Конверсия в целевой, % (Col I)
        requests_list.append(formula_update(
            data_row_0idx, COL_CR,
            f'=IF(G{r}>0; H{r}/G{r}; "")'
        ))

        # Доля бюджета, % (Col K)
        requests_list.append(formula_update(
            data_row_0idx, COL_SHARE,
            f'=IF(J{r_total}>0; J{r}/J{r_total}; "")'
        ))

        # Общая цена лида (Col L)
        requests_list.append(formula_update(
            data_row_0idx, COL_AVG_LEAD,
            f'=IF(G{r}>0; J{r}/G{r}; "")'
        ))

        # Цена целевого лида (Col M)
        requests_list.append(formula_update(
            data_row_0idx, COL_TGT_LEAD,
            f'=IF(H{r}>0; J{r}/H{r}; "")'
        ))

    # Для строки ИТОГО
    itog_row_0idx = header_row + len(TABLE_ROWS)
    requests_list.append(cell_update(itog_row_0idx, COL_SOURCE, "ИТОГО"))

    # ИТОГО считает через формулы SUM и IF, прописанные на шаге создания шаблона,
    # поэтому нам не нужно вручную перезаписывать ячейки строки ИТОГО.

    log.info("Подготовлено %d requests для Google Sheets", len(requests_list))
    return requests_list


def write_to_google_sheets(
    sheets,
    spreadsheet_id: str,
    tab_name: str,
    sheet_id: int,
    requests_list: list[dict],
):
    """Отправляет данные в Google Sheets через batchUpdate."""
    if not requests_list:
        log.warning("Нет данных для записи в Google Sheets.")
        return

    body = {"requests": requests_list}
    sheets.batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()
    log.info(
        "✅ Данные успешно записаны в таблицу '%s', вкладка '%s'.",
        spreadsheet_id, tab_name,
    )


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------

def main():
    log.info("=" * 60)
    log.info("Запуск скрипта еженедельного отчёта по лидам")
    log.info("=" * 60)

    # 1. Читаем переменные среды
    webhook_url          = get_env("BITRIX24_WEBHOOK_URL")
    service_account_json = get_env("GOOGLE_SERVICE_ACCOUNT_JSON")
    spreadsheet_id       = get_env("SPREADSHEET_ID")

    # 2. Вычисляем временные окна
    windows = compute_date_windows()

    current_wed = windows["current_wed"]
    current_tue = windows["current_tue"]
    prev_tue    = windows["prev_tue"]

    # 3. Запрашиваем лиды из Битрикс24
    # Основной пул: Ср прошлой — Вт текущей
    all_leads = fetch_leads(webhook_url, current_wed, current_tue)

    # «Хвосты» прошлого Вт: лиды, созданные во Вт прошлой недели.
    # Так как целевые статусы теперь определяются через исключение (все кроме NON_TARGET_STATUSES),
    # забираем все лиды за прошлый Вт и затем фильтруем по DATE_MODIFY и статусу в Python.
    tail_leads = fetch_leads(webhook_url, prev_tue, prev_tue)

    # 4. Агрегируем данные
    aggregated = aggregate_leads(windows, all_leads, tail_leads)

    # 5. Работаем с Google Sheets
    sheets = get_sheets_service(service_account_json)

    # Название вкладки = текущий месяц на русском (напр. "Май 2026")
    MONTH_NAMES_RU = {
        1: "Январь", 2: "Февраль", 3: "Март",
        4: "Апрель", 5: "Май",     6: "Июнь",
        7: "Июль",   8: "Август",  9: "Сентябрь",
        10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
    }
    tab_name = f"{MONTH_NAMES_RU[current_wed.month]} {current_wed.year}"
    log.info("Целевая вкладка: '%s'", tab_name)

    # Получаем или создаём вкладку
    sheet_id = get_or_create_sheet_tab(sheets, spreadsheet_id, tab_name)

    # Ищем блок текущей недели по дате Среды
    header_row = find_week_block_row(sheets, spreadsheet_id, tab_name, current_wed)

    if header_row is None:
        create_month_template(sheets, spreadsheet_id, tab_name, sheet_id, current_wed)
        header_row = find_week_block_row(sheets, spreadsheet_id, tab_name, current_wed)

    if header_row is None:
        log.error(
            "❌ Не удалось найти или создать блок недели для даты %s на листе '%s'.\n"
            "   Скрипт завершает работу без записи данных.",
            current_wed, tab_name
        )
        return

    # 6. Формируем и отправляем запросы к Sheets API
    requests_list = build_update_requests(sheet_id, header_row, windows, aggregated)
    write_to_google_sheets(sheets, spreadsheet_id, tab_name, sheet_id, requests_list)

    log.info("=" * 60)
    log.info("Скрипт завершён успешно.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
