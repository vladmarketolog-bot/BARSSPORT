import os
import requests
from datetime import date

def run_tests():
    token = os.environ.get("YANDEX_DIRECT_TOKEN")
    if token:
        token = token.strip()
    client_login = os.environ.get("YANDEX_DIRECT_CLIENT_LOGIN")
    if client_login:
        client_login = client_login.strip()

    print("=" * 60)
    print("ЗАПУСК ДИАГНОСТИКИ API ЯНДЕКС.ДИРЕКТ")
    print("=" * 60)
    print(f"YANDEX_DIRECT_TOKEN configured: {bool(token)}")
    print(f"YANDEX_DIRECT_CLIENT_LOGIN configured: {bool(client_login)} (Value: {client_login})")
    
    if not token:
        print("Ошибка: YANDEX_DIRECT_TOKEN не задан!")
        return

    # Различные URL для тестирования
    urls = [
        "https://api.direct.yandex.com/json/v5/reports",
        "https://api.direct.yandex.com/json/v5/reports/",
        "https://api.direct.yandex.com/v5/reports",
        "https://api.direct.yandex.com/v5/reports/",
    ]

    # Различные типы авторизации
    auth_formats = [
        "Bearer",
        "OAuth"
    ]

    # Параметры отчета
    payload = {
        "params": {
            "SelectionCriteria": {
                "DateFrom": "2026-05-01",
                "DateTo": "2026-05-07"
            },
            "FieldNames": ["Cost"],
            "ReportName": "DiagReport_20260501_20260507",
            "ReportType": "ACCOUNT_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeDiscount": "NO",
            "IncludeVAT": "YES"
        }
    }

    for url in urls:
        for auth_type in auth_formats:
            print("-" * 60)
            print(f"ТЕСТ: URL: {url} | Auth: {auth_type}")
            
            headers = {
                "Authorization": f"{auth_type} {token}",
                "Accept-Language": "ru",
                "processingMode": "auto",
                "returnMoneyInMicros": "false",
                "skipReportHeader": "true",
                "skipReportSummary": "true",
            }
            if client_login:
                headers["Client-Login"] = client_login

            try:
                # Отправляем тестовый запрос
                resp = requests.post(url, json=payload, headers=headers, timeout=20)
                print(f"Результат: Status {resp.status_code}")
                
                # Печатаем заголовки ответа, важные для отладки (requestId, error_code и др.)
                for h_key in ["requestId", "error_code", "error_string", "Content-Type"]:
                    if h_key in resp.headers:
                        print(f"  Header {h_key}: {resp.headers[h_key]}")

                # Печатаем тело ответа (первые 500 символов)
                print(f"  Body snippet: {resp.text[:500]}")
                
            except Exception as e:
                print(f"  Исключение при запросе: {e}")

    print("=" * 60)
    print("ДИАГНОСТИКА ЗАВЕРШЕНА")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
