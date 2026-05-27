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

# Строки таблицы (фиксированный порядок — как в шаблоне)
TABLE_ROWS: list[str] = ["Я.Директ", "SEO", "Вход. звонок", "Авито"]

# ── Маппинг статусов «целевых» лидов ────────────────────────────────────────
# Согласно выбору пользователя, целевым считается только статус "Качественный лид" (CONVERTED)
TARGET_STATUSES: set[str] = {
    "CONVERTED",    # «Качественный лид»
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
    fields = ["ID", "SOURCE_ID", "STATUS_ID", "DATE_CREATE", "DATE_MODIFY", "UTM_SOURCE"]

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
    Определяет категорию строки таблицы по полям SOURCE_ID и UTM_SOURCE лида.
    Если источник не распознан — возвращает "Прочее" (не записывается в таблицу).
    """
    source_id = lead.get("SOURCE_ID", "") or ""
    utm_source = lead.get("UTM_SOURCE", "") or ""

    # Сначала проверяем SOURCE_ID (он приоритетнее)
    if source_id in SOURCE_MAP:
        return SOURCE_MAP[source_id]

    # Если SOURCE_ID не в маппинге — проверяем UTM_SOURCE
    # (удобно для Я.Директ, который может передавать utm_source=yandex)
    utm_lower = utm_source.lower()
    if "yandex" in utm_lower or "direct" in utm_lower or "ya" in utm_lower:
        return "Я.Директ"
    if "avito" in utm_lower:
        return "Авито"
    if "seo" in utm_lower or "organic" in utm_lower or "google" in utm_lower:
        return "SEO"

    return "Прочее"


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

        # Лид создан в Ср-Пн и является целевым
        if current_wed <= created_date <= current_mon and status in TARGET_STATUSES:
            category = get_source_category(lead)
            if category in targeted:
                targeted[category] += 1

    # ── Правило Б: «Хвосты» прошлого Вторника ───────────────────────────────
    # tail_leads — лиды, созданные во Вт прошлой недели и уже ставшие целевыми
    # (API вернул их с фильтром по статусу + дате создания = prev_tue)
    for lead in tail_leads:
        status = lead.get("STATUS_ID", "")
        raw_modify = lead.get("DATE_MODIFY", "")

        # Проверяем, что статус стал целевым в текущем отчётном периоде
        # (дата изменения >= current_wed)
        try:
            modify_date = datetime.fromisoformat(raw_modify).astimezone(TZ).date()
        except (ValueError, TypeError):
            modify_date = None

        if status not in TARGET_STATUSES:
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
# Google Sheets: поиск нужного блока и запись данных
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
    Если нет — создаёт её (опционально).
    Возвращает sheet_id вкладки.
    """
    meta = sheets.get(spreadsheetId=spreadsheet_id).execute()
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

    Логика: сканирует столбец "Период" (обычно столбец B или C) в поисках
    даты, совпадающей с current_wed (форматы "27.05", "27.05.2026" и т.д.).

    Возвращает номер строки (1-indexed) заголовка блока или None.
    """
    # Читаем весь лист (столбцы A-M, до 200 строк)
    range_notation = f"'{tab_name}'!A1:M200"
    result = sheets.values().get(
        spreadsheetId=spreadsheet_id,
        range=range_notation,
    ).execute()

    rows = result.get("values", [])
    target_str_short = target_wed.strftime("%d.%m")       # "27.05"
    target_str_full  = target_wed.strftime("%d.%m.%Y")    # "27.05.2026"
    target_str_day   = str(target_wed.day)                # "27"

    for row_idx, row in enumerate(rows):
        for cell in row:
            cell_str = str(cell).strip()
            if cell_str in (target_str_short, target_str_full, target_str_day):
                log.info(
                    "Дата недели найдена в строке %d (значение: '%s')",
                    row_idx + 1, cell_str,
                )
                return row_idx + 1  # 1-indexed

    log.warning(
        "Блок для недели %s не найден на листе '%s'. "
        "Возможно, нужно добавить даты в шаблон вручную.",
        target_wed, tab_name,
    )
    return None


def build_update_requests(
    sheet_id: int,
    header_row: int,
    windows: dict,
    aggregated: dict,
) -> list[dict]:
    """
    Формирует список batchUpdate requests для записи данных в Google Sheets.

    Структура шаблона (согласно image_242328.png):
    Строка header_row   : Заголовки дат (Ср, Чт, Пт, Сб/Вс, Пн, Вт)
    Строка header_row+1 : Подзаголовки (дд.мм)
    Строка header_row+2+: Данные по источникам (Я.Директ, SEO, Вход. звонок, Авито)
    Последняя строка    : ИТОГО

    ВАЖНО: Номера столбцов (0-indexed) — измените, если ваш шаблон отличается!
    """
    # ── Маппинг столбцов (0-indexed) ────────────────────────────────────────
    # Измените согласно реальной структуре вашей таблицы
    COL_SOURCE   = 0   # Столбец A — "Источник"
    COL_WED      = 1   # Столбец B — Среда
    COL_THU      = 2   # Столбец C — Четверг
    COL_FRI      = 3   # Столбец D — Пятница
    COL_SAT_SUN  = 4   # Столбец E — Сб+Вс (объединены)
    COL_MON      = 5   # Столбец F — Понедельник
    COL_TUE      = 6   # Столбец G — Вторник
    COL_TOTAL    = 7   # Столбец H — "Итого за неделю"
    COL_TARGETED = 8   # Столбец I — "Кол-во целевых"
    COL_BUDGET   = 9   # Столбец J — "Израсходованный бюджет" (ручной ввод)
    COL_AVG_LEAD = 10  # Столбец K — "Общая цена лида" (формула)
    COL_TGT_LEAD = 11  # Столбец L — "Цена целевого лида" (формула)

    # ── Строки данных (источников) ────────────────────────────────────────────
    # header_row — строка заголовка "Период" (даты)
    # Данные начинаются со следующей строки (+ смещение подзаголовка "дд.мм")
    # По умолчанию: заголовок + 1 строка с датами + 4 строки данных + 1 итог
    DATA_ROW_OFFSET = 2   # Сколько строк после header_row начинаются данные
    ITOG_ROW_OFFSET = 6   # Строка ИТОГО (относительно header_row)

    days = windows["days"]  # [(date, label), ...]
    daily = aggregated["daily"]
    targeted = aggregated["targeted"]

    requests_list = []

    def cell_update(row_0idx: int, col_0idx: int, value) -> dict:
        """Вспомогательная функция: создаёт request на обновление одной ячейки."""
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
                    "rowIndex": row_0idx,   # 0-indexed!
                    "columnIndex": col_0idx,
                },
            }
        }

    def formula_update(row_0idx: int, col_0idx: int, formula: str) -> dict:
        """Вспомогательная функция: вставляет формулу в ячейку."""
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

    # Маппинг label -> column index
    label_to_col = {
        "Ср":  COL_WED,
        "Чт":  COL_THU,
        "Пт":  COL_FRI,
        "Сб":  COL_SAT_SUN,   # Сб и Вс суммируются в одну колонку
        "Вс":  COL_SAT_SUN,
        "Пн":  COL_MON,
        "Вт":  COL_TUE,
    }

    # Промежуточные суммы по дням для строки ИТОГО
    day_totals: dict[str, int] = {lbl: 0 for _, lbl in days}
    total_targeted = 0

    # ── Записываем данные по каждому источнику ───────────────────────────────
    for row_offset, source_name in enumerate(TABLE_ROWS):
        data_row_0idx = (header_row - 1) + DATA_ROW_OFFSET + row_offset  # 0-indexed

        # Вставляем название источника (на случай, если ячейка пуста)
        requests_list.append(cell_update(data_row_0idx, COL_SOURCE, source_name))

        row_data = daily.get(source_name, {})
        row_total = 0

        # Обработка Сб+Вс вместе (суммируем)
        sat_sun_val = row_data.get("Сб", 0) + row_data.get("Вс", 0)

        for _, label in days:
            col = label_to_col.get(label)
            if col is None:
                continue

            if label in ("Сб", "Вс"):
                value = sat_sun_val
                # Записываем только один раз (при "Сб")
                if label == "Вс":
                    continue
            else:
                value = row_data.get(label, 0)

            requests_list.append(cell_update(data_row_0idx, col, value))
            row_total += value
            day_totals[label] = day_totals.get(label, 0) + value

        # Суббота+воскресенье для итогов (считаем один раз)
        day_totals["Сб"] = day_totals.get("Сб", 0) + sat_sun_val

        # Итого за строку
        requests_list.append(cell_update(data_row_0idx, COL_TOTAL, row_data.get("Итого", 0)))

        # Кол-во целевых
        tgt = targeted.get(source_name, 0)
        requests_list.append(cell_update(data_row_0idx, COL_TARGETED, tgt))
        total_targeted += tgt

        # Формулы цены лида: =Бюджет/Кол-во (с защитой от деления на 0)
        # Используем нотацию R1C1 для batchUpdate
        # Budget_cell = та же строка, столбец J (COL_BUDGET)
        # В A1-нотации (для формул): Бюджет = столбец J, лиды = столбец H

        # Пересчитываем в A1-нотацию (1-indexed, col A=1)
        r = data_row_0idx + 1   # 1-indexed row
        budget_cell = f"J{r}"   # Ячейка бюджета
        total_cell  = f"H{r}"   # Ячейка итого лидов
        tgt_cell    = f"I{r}"   # Ячейка целевых лидов

        # Формула "Общая цена лида" = Бюджет / Итого
        requests_list.append(formula_update(
            data_row_0idx, COL_AVG_LEAD,
            f"=IF({total_cell}>0,{budget_cell}/{total_cell},\"\")",
        ))

        # Формула "Цена целевого лида" = Бюджет / Целевые
        requests_list.append(formula_update(
            data_row_0idx, COL_TGT_LEAD,
            f"=IF({tgt_cell}>0,{budget_cell}/{tgt_cell},\"\")",
        ))

    # ── Строка ИТОГО ─────────────────────────────────────────────────────────
    itog_row_0idx = (header_row - 1) + ITOG_ROW_OFFSET

    requests_list.append(cell_update(itog_row_0idx, COL_SOURCE, "ИТОГО"))

    for _, label in days:
        col = label_to_col.get(label)
        if col is None or label == "Вс":
            continue
        requests_list.append(cell_update(itog_row_0idx, col, day_totals.get(label, 0)))

    # Итого лидов
    grand_total = sum(daily.get(src, {}).get("Итого", 0) for src in TABLE_ROWS)
    requests_list.append(cell_update(itog_row_0idx, COL_TOTAL, grand_total))

    # Итого целевых
    requests_list.append(cell_update(itog_row_0idx, COL_TARGETED, total_targeted))

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

    # «Хвосты» прошлого Вт: лиды, созданные во Вт прошлой недели с целевым статусом
    # Битрикс24 не позволяет фильтровать по дате изменения статуса напрямую,
    # поэтому забираем все лиды за прошлый Вт с нужным статусом и
    # затем фильтруем по DATE_MODIFY в Python (в функции aggregate_leads).
    tail_filter = {"STATUS_ID": list(TARGET_STATUSES)}
    # Если TARGET_STATUSES содержит несколько статусов — API принимает список
    # для оператора IN: {"@STATUS_ID": [...]}
    # Исправляем фильтр под Битрикс24-синтаксис:
    tail_filter_bitrix = {"@STATUS_ID": list(TARGET_STATUSES)}

    tail_leads = fetch_leads(webhook_url, prev_tue, prev_tue, extra_filter=tail_filter_bitrix)

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
    tab_name = f"{MONTH_NAMES_RU[current_tue.month]} {current_tue.year}"
    log.info("Целевая вкладка: '%s'", tab_name)

    # Получаем или создаём вкладку
    sheet_id = get_or_create_sheet_tab(sheets, spreadsheet_id, tab_name)

    # Ищем блок текущей недели по дате Среды
    header_row = find_week_block_row(sheets, spreadsheet_id, tab_name, current_wed)

    if header_row is None:
        log.error(
            "❌ Не удалось найти блок недели для даты %s на листе '%s'.\n"
            "   Убедитесь, что в шаблоне Google Sheets есть строка с датой '%s'.\n"
            "   Скрипт завершает работу без записи данных.",
            current_wed, tab_name, current_wed.strftime("%d.%m"),
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
