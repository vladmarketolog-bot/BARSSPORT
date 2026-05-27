"""
Скрипт для разового заполнения исторических данных (Бэкфилл) за Апрель и Май 2026 года.
Запускается на GitHub Actions, чтобы иметь доступ к секретам.
"""

import os
import logging
from datetime import date, timedelta
from script import (
    get_env,
    compute_date_windows,
    fetch_leads,
    aggregate_leads,
    get_sheets_service,
    get_or_create_sheet_tab,
    find_week_block_row,
    create_month_template,
    build_update_requests,
    write_to_google_sheets,
    update_dashboard,
    fetch_yandex_direct_cost,
    parse_direct_csv_files,
    fetch_yandex_direct_cost_browser
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

def backfill_week(sheets, webhook_url, spreadsheet_id, target_wed: date):
    log.info("=" * 60)
    log.info("ОБРАБОТКА НЕДЕЛИ С: %s", target_wed.strftime("%d.%m.%Y"))
    log.info("=" * 60)

    # 1. Вычисляем временные окна для конкретной среды
    windows = compute_date_windows(target_wed)
    current_wed = windows["current_wed"]
    current_tue = windows["current_tue"]
    prev_tue    = windows["prev_tue"]

    # 2. Запрашиваем лиды из CRM за этот период
    all_leads = fetch_leads(webhook_url, current_wed, current_tue)

    # «Хвосты» прошлой недели
    tail_leads = fetch_leads(webhook_url, prev_tue, prev_tue)

    # 3. Агрегируем данные
    aggregated = aggregate_leads(windows, all_leads, tail_leads)

    # Получаем расходы Яндекс.Директ для этой исторической недели
    # Приоритет 0: CSV-файл в репозитории
    # Приоритет 1: API Яндекс.Директ
    # Приоритет 2: браузерная автоматизация
    yandex_token    = os.environ.get("YANDEX_DIRECT_TOKEN", "").strip()
    yandex_login    = os.environ.get("YANDEX_DIRECT_CLIENT_LOGIN", "").strip()
    yandex_browser_login    = os.environ.get("YANDEX_LOGIN", "").strip()
    yandex_browser_password = os.environ.get("YANDEX_PASSWORD", "").strip()
    yandex_secret_answer    = os.environ.get("YANDEX_SECRET_ANSWER", "").strip() or None
    aggregated["budgets"] = {}

    yandex_cost = None

    # Попытка 0: CSV-файл(ы)
    csv_cost = parse_direct_csv_files(current_wed, current_tue)
    if csv_cost is not None:
        yandex_cost = csv_cost

    # Попытка 1: API
    if yandex_cost is None and yandex_token:
        try:
            log.info("Пробуем получить расходы через API для недели %s...", current_wed)
            yandex_cost = fetch_yandex_direct_cost(yandex_token, yandex_login or None, current_wed, current_tue)
        except Exception as e:
            log.warning("API Яндекс.Директ недоступен для недели %s: %s.", current_wed, e)
            yandex_cost = None

    # Попытка 2: Браузер
    if yandex_cost is None and yandex_browser_login and yandex_browser_password:
        try:
            log.info("Получаем расходы через браузер для недели %s...", current_wed)
            yandex_cost = fetch_yandex_direct_cost_browser(
                yandex_browser_login,
                yandex_browser_password,
                yandex_secret_answer,
                current_wed,
                current_tue,
            )
        except Exception as e:
            log.error("Ошибка браузерного импорта для недели %s: %s", current_wed, e)

    if yandex_cost is not None:
        aggregated["budgets"]["Я.Директ e-17479930"] = yandex_cost
        log.info("Расходы Яндекс.Директ записаны: %.2f руб.", yandex_cost)
    else:
        log.warning("Расходы Яндекс.Директ не получены для недели %s", current_wed)

    # 4. Название вкладки (по дате Среды)
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
        # Если блок не найден, создаём шаблон месяца
        create_month_template(sheets, spreadsheet_id, tab_name, sheet_id, current_wed)
        header_row = find_week_block_row(sheets, spreadsheet_id, tab_name, current_wed)

    if header_row is None:
        log.error("❌ Не удалось найти или создать блок для недели %s", current_wed)
        return

    # 5. Записываем данные в Google Sheets
    requests_list = build_update_requests(sheet_id, header_row, windows, aggregated)
    write_to_google_sheets(sheets, spreadsheet_id, tab_name, sheet_id, requests_list)

def main():
    webhook_url          = get_env("BITRIX24_WEBHOOK_URL")
    service_account_json = get_env("GOOGLE_SERVICE_ACCOUNT_JSON")
    spreadsheet_id       = get_env("SPREADSHEET_ID")

    sheets = get_sheets_service(service_account_json)

    # Запуск диагностики Yandex Direct API
    try:
        import test_yandex_api
        test_yandex_api.run_tests()
    except Exception as e:
        log.error("Не удалось запустить диагностику Yandex API: %s", e)

    # Определяем все среды для Апреля и Мая 2026 года
    backfill_weeks = [
        # --- Апрель 2026 ---
        date(2026, 4, 1),
        date(2026, 4, 8),
        date(2026, 4, 15),
        date(2026, 4, 22),
        date(2026, 4, 29),
        # --- Май 2026 ---
        date(2026, 5, 6),
        date(2026, 5, 13),
        date(2026, 5, 20),
        date(2026, 5, 27) # Текущую тоже перезапишем для надежности
    ]

    for wed in backfill_weeks:
        try:
            backfill_week(sheets, webhook_url, spreadsheet_id, wed)
        except Exception as e:
            log.error("Ошибка при обработке недели %s: %s", wed, e, exc_info=True)

    # Обновляем аналитический дашборд после завершения бэкфилла
    try:
        log.info("Обновление аналитического дашборда...")
        update_dashboard(sheets, spreadsheet_id)
    except Exception as e:
        log.error("Ошибка при обновлении дашборда: %s", e, exc_info=True)

    log.info("Бэкфилл успешно завершен для всех указанных недель!")

if __name__ == "__main__":
    main()
