import json
import urllib.request
import time
from datetime import datetime

API_URL = "https://apiv2.nobitex.ir/market/stats"

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "BTCIRT",
    "ETHIRT"
]

def get_market_data():
    url = API_URL
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))

        if data.get("status") != "ok":
            print("پاسخ بازار معتبر نیست.")
            return

        stats = data.get("stats", {})

        print("\nزمان بررسی:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        for symbol in SYMBOLS:
            market = stats.get(symbol)

            if not market:
                print(symbol, "- اطلاعات بازار موجود نیست")
                continue

            print(
                symbol,
                "| آخرین قیمت:", market.get("latest"),
                "| کمترین قیمت ۲۴ ساعت:", market.get("dayLow"),
                "| بیشترین قیمت ۲۴ ساعت:", market.get("dayHigh")
            )

    except Exception as error:
        print("خطا در دریافت اطلاعات:", error)

if __name__ == "__main__":
    get_market_data()
