import os
import logging
from datetime import date
from script import (
    get_env,
    get_sheets_service,
    update_dashboard,
    process_week
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

def backfill_week(sheets, webhook_url: str, spreadsheet_id: str, target_wed: date):
    """Делегирует обработку конкретной исторической недели функции process_week."""
    process_week(sheets, webhook_url, spreadsheet_id, target_wed)

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
