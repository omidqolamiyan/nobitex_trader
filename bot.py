import json
import urllib.request
import urllib.parse
from datetime import datetime

API_URL = "https://apiv2.nobitex.ir/market/stats"

MARKETS = [
    ("BTC/USDT", "btc", "usdt"),
    ("ETH/USDT", "eth", "usdt"),
    ("BTC/IRT", "btc", "rls"),
    ("ETH/IRT", "eth", "rls"),
]


def get_market_data(src, dst):
    params = urllib.parse.urlencode({
        "srcCurrency": src,
        "dstCurrency": dst
    })

    url = f"{API_URL}?{params}"

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "NobitexMarketBot/1.0"}
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))

    if data.get("status") != "ok":
        raise ValueError(f"پاسخ نامعتبر API: {data}")

    market_key = f"{src}-{dst}"
    market = data.get("stats", {}).get(market_key)

    if not market:
        raise ValueError(f"اطلاعات بازار {market_key} پیدا نشد")

    return market


def main():
    print(
        "\nزمان بررسی:",
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    for name, src, dst in MARKETS:
        try:
            market = get_market_data(src, dst)

            print(
                f"{name} | "
                f"آخرین قیمت: {market.get('latest')} | "
                f"کمترین ۲۴ ساعت: {market.get('dayLow')} | "
                f"بیشترین ۲۴ ساعت: {market.get('dayHigh')} | "
                f"تغییر ۲۴ ساعت: {market.get('dayChange')}%"
            )

        except Exception as error:
            print(f"{name} | خطا در دریافت اطلاعات: {error}")


if __name__ == "__main__":
    main()
