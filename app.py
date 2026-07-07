import json
import os
import time
import requests
from flask import Flask, render_template, request
from datetime import datetime

app = Flask(__name__)

# ============================================================
# [설정] API 키
# ============================================================
# Alpha Vantage: https://www.alphavantage.co/support/#api-key 에서 무료 발급
ALPHA_VANTAGE_API_KEY = "?apikey=5CstjxCIoXVdNYb9QZ8M76EJc6mG9rij"
ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"

# 공공데이터포털(금융위원회_주식배당정보): https://www.data.go.kr/data/15043284/openapi.do 에서
# "활용신청" 후 발급받은 서비스키(디코딩 키)를 넣으세요.
DATA_GO_KR_SERVICE_KEY = "80f4084a7b39918394ff574877bd9c6546c28fcf43fb482055393232aca98738"
DATA_GO_KR_BASE_URL = "http://apis.data.go.kr/1160100/service/GetStocDiviInfoService/getDiviInfo"

# 캐시 유지 시간 (초). 무료 API 호출 한도를 아끼기 위해 하루 단위로 캐싱합니다.
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24시간
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
os.makedirs(CACHE_DIR, exist_ok=True)


# ============================================================
# [공통] 파일 캐시 유틸
# ============================================================
def _cache_path(name):
    return os.path.join(CACHE_DIR, f"{name}.json")


def load_cache(name):
    path = _cache_path(name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if time.time() - payload.get("_cached_at", 0) > CACHE_TTL_SECONDS:
            return None  # 만료됨
        return payload.get("data")
    except Exception:
        return None


def save_cache(name, data):
    path = _cache_path(name)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"_cached_at": time.time(), "data": data}, f, ensure_ascii=False)
    except Exception as e:
        print(f"캐시 저장 실패({name}): {e}")


# ============================================================
# [해외 - 미국] Alpha Vantage
# ============================================================
# 무료 API는 "시총 랭킹 스크리너"를 제공하지 않으므로, 관심종목 리스트를 직접 관리합니다.
# 필요에 맞게 티커를 추가/삭제하세요.
US_WATCHLIST = [
    "AAPL", "MSFT", "JNJ", "KO", "PG",
    "XOM", "CVX", "VZ", "T", "PFE",
    "IBM", "MMM", "ABBV", "HD", "MCD",
]


def _av_request(function, symbol):
    params = {
        "function": function,
        "symbol": symbol,
        "apikey": ALPHA_VANTAGE_API_KEY,
    }
    res = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=10)
    res.raise_for_status()
    data = res.json()

    # Alpha Vantage는 오류/한도초과 시에도 200 OK + 메시지 JSON을 반환하므로 별도 체크 필요
    if "Note" in data or "Information" in data:
        raise RuntimeError(data.get("Note") or data.get("Information"))
    if "Error Message" in data:
        raise RuntimeError(data["Error Message"])
    return data


def _fetch_us_dividend_stock(symbol):
    """OVERVIEW(펀더멘털/배당) + GLOBAL_QUOTE(현재가) 2회 호출로 종목 하나의 정보를 구성"""
    overview = _av_request("OVERVIEW", symbol)
    if not overview or overview.get("Symbol") is None:
        return None

    quote_raw = _av_request("GLOBAL_QUOTE", symbol)
    quote = quote_raw.get("Global Quote", {}) if quote_raw else {}
    price = float(quote.get("05. price", 0) or 0)

    market_cap = float(overview.get("MarketCapitalization", 0) or 0)
    div_per_share = float(overview.get("DividendPerShare", 0) or 0)
    # Alpha Vantage DividendYield는 이미 소수(예: 0.024 = 2.4%) 형태로 제공됨
    div_yield = float(overview.get("DividendYield", 0) or 0) * 100

    ex_date_raw = overview.get("ExDividendDate", "None")
    ex_date_str = "무배당" if div_per_share == 0 else "배당락일 미정"
    if ex_date_raw and ex_date_raw not in ("None", "0000-00-00", "-"):
        try:
            ex_date_str = datetime.strptime(ex_date_raw, "%Y-%m-%d").strftime("%Y-%m-%d")
        except Exception:
            ex_date_str = ex_date_raw

    status = "미지급"
    if div_yield > 2.0:
        status = "성장"
    elif div_yield > 0:
        status = "안정"

    return {
        "ticker": symbol,
        "name": overview.get("Name", symbol),
        "sector": overview.get("Sector", "N/A"),
        "price": f"${price:,.2f}",
        "market_cap": f"${market_cap / 1_000_000_000:,.1f}B" if market_cap else "N/A",
        "latest_div": f"${div_per_share:,.2f}",
        "div_yield": f"{round(div_yield, 2)}%",
        "ex_date": ex_date_str,
        "status": status,
    }


def get_us_dividend_list():
    cached = load_cache("us_dividends")
    if cached is not None:
        return cached

    results = []
    for symbol in US_WATCHLIST:
        try:
            stock = _fetch_us_dividend_stock(symbol)
            if stock:
                results.append(stock)
        except Exception as e:
            # 한도초과(Note) 등 하나가 실패해도 나머지는 계속 진행
            print(f"[Alpha Vantage] {symbol} 조회 실패: {e}")
            continue

    if results:
        save_cache("us_dividends", results)
        return results
    return None  # 전부 실패한 경우에만 None (오류 화면 노출용)


# ============================================================
# [국내] 공공데이터포털 - 금융위원회 주식배당정보
# ============================================================
# ※ 이 API는 종목명(likeStkNm) 또는 법인등록번호(crno)로 조회하는 방식입니다.
#    파라미터명은 활용신청 승인 후 제공되는 Swagger 문서를 기준으로 다시 확인해 주세요.
KR_WATCHLIST = [
    "삼성전자", "SK텔레콤", "KT&G", "POSCO홀딩스", "하나금융지주",
    "KB금융", "신한지주", "우리금융지주", "삼성카드", "기업은행",
]


def _fetch_kr_dividend_stock(stock_name):
    params = {
        "serviceKey": DATA_GO_KR_SERVICE_KEY,
        "numOfRows": 1,
        "pageNo": 1,
        "resultType": "json",
        "likeStkNm": stock_name,
    }
    res = requests.get(DATA_GO_KR_BASE_URL, params=params, timeout=10)
    res.raise_for_status()
    data = res.json()

    header = data.get("response", {}).get("header", {})
    if header.get("resultCode") not in ("00", 0, "0"):
        raise RuntimeError(f"{stock_name}: {header.get('resultMsg')}")

    items = data.get("response", {}).get("body", {}).get("items", {})
    if not items:
        return None
    item = items.get("item") if isinstance(items, dict) else items
    if isinstance(item, list):
        item = item[0] if item else None
    if not item:
        return None

    # 필드명은 공식 문서 기준 예상 명칭이며, 실제 응답 필드와 다르면 아래 get() 키를 맞춰주세요.
    div_amount = float(item.get("stockGeneralDividendAmount", 0) or 0)
    div_rate = item.get("dividendRate", "0")
    base_date = item.get("baseDate", "-")
    pay_date = item.get("moneyDividendPaymentDate", "-")
    stock_kind = item.get("stockKind", "-")

    return {
        "name": item.get("stockIssuingCorpName", stock_name),
        "stock_kind": stock_kind,
        "base_date": base_date,
        "pay_date": pay_date,
        "div_amount": f"{div_amount:,.0f}원" if div_amount else "-",
        "div_rate": f"{div_rate}%" if div_rate not in (None, "0", "") else "-",
    }


def get_kr_dividend_list():
    cached = load_cache("kr_dividends")
    if cached is not None:
        return cached

    results = []
    for name in KR_WATCHLIST:
        try:
            stock = _fetch_kr_dividend_stock(name)
            if stock:
                results.append(stock)
        except Exception as e:
            print(f"[공공데이터포털] {name} 조회 실패: {e}")
            continue

    if results:
        save_cache("kr_dividends", results)
        return results
    return None


# ============================================================
# 라우트
# ============================================================
PAGE_SIZE = 5


def paginate(full_list, page):
    if full_list is None:
        return None
    start = page * PAGE_SIZE
    return full_list[start:start + PAGE_SIZE]


@app.route("/")
def index():
    market = request.args.get("market", "us")  # 'us' 또는 'kr'
    page = request.args.get("page", 0, type=int)

    us_full = get_us_dividend_list()
    kr_full = get_kr_dividend_list()

    us_stocks = paginate(us_full, page) if market == "us" else None
    kr_stocks = paginate(kr_full, page) if market == "kr" else None

    us_total = len(us_full) if us_full else 0
    kr_total = len(kr_full) if kr_full else 0

    return render_template(
        "index.html",
        market=market,
        current_page=page,
        us_stocks=us_stocks,
        kr_stocks=kr_stocks,
        us_total=us_total,
        kr_total=kr_total,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5001)
