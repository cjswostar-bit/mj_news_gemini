
import argparse
import hashlib
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================
# 1. 기본 설정
# ==============================================
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
HISTORY_FILE = Path("history.json")
KST = ZoneInfo("Asia/Seoul")

SOURCES = {
    "경제/증시/코인": [
        "https://www.hankyung.com/feed/economy",
        "https://www.mk.co.kr/rss/30000001/",
    ],
    "정치/사회/시사": [
        "https://www.hani.co.kr/rss/",
        "https://www.khan.co.kr/rss/rssdata/total_news.xml",
        "https://www.sisain.co.kr/rss/S1N1.xml",
    ],
    "국제": [
        "https://news.google.com/rss/headlines/section/topic/WORLD?hl=ko&gl=KR&ceid=KR:ko",
    ],
}

# 시세 조회용 종목 코드
MARKETS = {
    "SP500": "^GSPC",
    "NASDAQ": "^IXIC",
    "KOSPI": "^KS11",
    "KOSDAQ": "^KQ11",
    "USD/KRW": "KRW=X",
    "WTI": "CL=F",
    "BRENT": "BZ=F",
    "US10Y": "^TNX",
}

COINS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "XRP": "ripple",
    "ENA": "ethena",
}

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0"
})

retry = Retry(
    total=2,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"]
)
session.mount(
    "https://",
    HTTPAdapter(max_retries=retry)
)


# ==============================================
# 2. 기본 보조 함수
# ==============================================
def now_kst():
    return datetime.now(KST)


def clean(text):
    value = BeautifulSoup(
        str(text or ""), "html.parser"
    ).get_text(" ", strip=True)

    return re.sub(
        r"\s+", " ", html.unescape(value)
    ).strip()


def article_key(title):
    normalized = clean(title).casefold()
    normalized = re.sub(
        r"\s+", "", normalized
    )
    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


def fmt(value, digits=2):
    if value is None:
        return "N/A"
    return f"{value:,.{digits}f}"


def change_text(value, unit="%"):
    if value is None:
        return "N/A"
    return f"{value:+.2f}{unit}"


def get_json(url, params=None):
    response = session.get(
        url,
        params=params,
        timeout=15
    )
    response.raise_for_status()
    return response.json()


# ==============================================
# 3. 중복 기사 이력
# ==============================================
def load_history():
    result = {
        "keyword": {},
        "brief": {}
    }

    if not HISTORY_FILE.exists():
        return result

    try:
        raw = json.loads(
            HISTORY_FILE.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(raw, dict):
            for mode in result:
                if isinstance(raw.get(mode), dict):
                    result[mode] = raw[mode]

        elif isinstance(raw, list):
            stamp = now_kst().isoformat()
            for mode in result:
                result[mode] = {
                    item: stamp
                    for item in raw
                    if isinstance(item, str)
                }

    except (OSError, ValueError) as exc:
        logging.warning(
            "이력 불러오기 실패: %s", exc
        )

    return result


def save_history(history):
    cutoff = now_kst() - timedelta(days=45)

    for mode in ("keyword", "brief"):
        cleaned = {}

        for key, date_text in history[mode].items():
            try:
                dt = datetime.fromisoformat(
                    date_text
                )
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=KST)
                if dt >= cutoff:
                    cleaned[key] = date_text
            except (ValueError, TypeError):
                continue

        history[mode] = dict(
            sorted(
                cleaned.items(),
                key=lambda x: x[1],
                reverse=True
            )[:1500]
        )

    HISTORY_FILE.write_text(
        json.dumps(
            history,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


# ==============================================
# 4. 텔레그램 전송
# ==============================================
def telegram(method, data, files=None):
    url = (
        "https://api.telegram.org/bot"
        f"{TOKEN}/{method}"
    )

    response = session.post(
        url,
        data=data,
        files=files,
        timeout=30
    )
    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            result.get("description")
        )

    return result


def send_text(message):
    return telegram(
        "sendMessage",
        {
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    )


# ==============================================
# 5. 뉴스 수집
# ==============================================
def read_feed(url):
    try:
        response = session.get(
            url,
            timeout=20
        )
        response.raise_for_status()

        feed = feedparser.parse(
            response.content
        )

        if feed.bozo and not feed.entries:
            raise ValueError("RSS 형식 오류")

        return feed.entries[:15]

    except (requests.RequestException, ValueError) as exc:
        logging.warning(
            "RSS 실패 %s: %s", url, exc
        )
        return []


def published_at(entry):
    value = (
        entry.get("published_parsed")
        or entry.get("updated_parsed")
    )

    if not value:
        return None

    try:
        return datetime(
            *value[:6],
            tzinfo=timezone.utc
        ).astimezone(KST)
    except (ValueError, TypeError):
        return None


def parse_article(entry):
    title = clean(entry.get("title"))
    link = str(entry.get("link", "")).strip()

    if (
        not title
        or urlparse(link).scheme
        not in ("http", "https")
    ):
        return None

    return {
        "title": title,
        "link": link,
        "summary": entry.get("summary", ""),
        "date": published_at(entry),
        "key": article_key(title),
    }


def collect_articles(urls, age_hours=48):
    items = []
    cutoff = now_kst() - timedelta(
        hours=age_hours
    )

    for url in urls:
        for entry in read_feed(url):
            article = parse_article(entry)

            if not article:
                continue

            date = article["date"]
            if date and date < cutoff:
                continue

            items.append(article)

    items.sort(
        key=lambda a: (
            a["date"]
            or datetime.min.replace(tzinfo=KST)
        ),
        reverse=True
    )

    return items


# ==============================================
# 6. 뉴스 요약 및 메시지
# ==============================================
def get_summary(article):
    summary = clean(article["summary"])

    if (
        len(summary) < 45
        or summary.casefold().startswith(
            article["title"].casefold()[:45]
        )
    ):
        summary = ""

    host = urlparse(
        article["link"]
    ).netloc.lower()

    if (
        not summary
        and "news.google.com" not in host
    ):
        try:
            response = session.get(
                article["link"],
                timeout=10
            )
            response.raise_for_status()
            soup = BeautifulSoup(
                response.text,
                "html.parser"
            )

            meta = soup.find(
                "meta",
                attrs={"name": "description"}
            )

            if not meta:
                meta = soup.find(
                    "meta",
                    attrs={
                        "property": "og:description"
                    }
                )

            if meta:
                summary = clean(
                    meta.get("content", "")
                )

        except requests.RequestException:
            pass

    if not summary:
        return (
            "요약 정보가 없습니다. "
            "원문 링크에서 확인해 주세요."
        )

    return (
        summary[:170]
        + ("…" if len(summary) > 170 else "")
    )


def article_message(article, header=""):
    title = html.escape(article["title"])
    summary = html.escape(
        get_summary(article)
    )
    link = html.escape(
        article["link"],
        quote=True
    )

    if article["date"]:
        date_text = article["date"].strftime(
            "%m/%d %H:%M"
        )
    else:
        date_text = "날짜 미상"

    return (
        f"{header}\n"
        f"<b>📰 {title} [{date_text}]</b>\n"
        f"📝 {summary}\n"
        f'🔗 <a href="{link}">기사 보기</a>'
    )


# ==============================================
# 7. 주요 증시, 환율, 유가, 금리
# Yahoo 차트 데이터의 최근 종가 사용
# ==============================================
def get_market_data():
    result = {}

    for name, symbol in MARKETS.items():
        try:
            url = (
                "https://query1.finance.yahoo.com"
                "/v8/finance/chart/"
                + quote(symbol, safe="")
            )

            data = get_json(
                url,
                params={
                    "range": "10d",
                    "interval": "1d",
                }
            )

            info = data["chart"]["result"][0]
            quotes = info["indicators"]["quote"][0]
            closes = quotes.get("close", [])
            times = info.get("timestamp", [])

            valid = [
                (float(price), timestamp)
                for price, timestamp
                in zip(closes, times)
                if price is not None
            ]

            if not valid:
                raise ValueError("가격 없음")

            price, timestamp = valid[-1]

            previous = (
                valid[-2][0]
                if len(valid) >= 2 else None
            )

            change = None
            if previous and previous != 0:
                change = (
                    (price / previous - 1) * 100
                )

            market_date = datetime.fromtimestamp(
                timestamp,
                tz=timezone.utc
            ).astimezone(KST)

            result[name] = {
                "price": price,
                "change": change,
                "date": market_date.strftime(
                    "%m/%d"
                ),
                "previous": previous,
            }

        except (
            requests.RequestException,
            KeyError,
            IndexError,
            ValueError,
            TypeError
        ) as exc:
            logging.warning(
                "%s 시세 조회 실패: %s",
                name, exc
            )
            result[name] = {}

    return result


# ==============================================
# 8. 가상자산 원화 시세: 업비트
# ==============================================
def get_krw_coins():
    result = {}

    try:
        data = get_json(
            "https://api.upbit.com/v1/ticker",
            params={
                "markets": ",".join(
                    f"KRW-{coin}"
                    for coin in COINS
                )
            }
        )

        for item in data:
            symbol = item["market"].replace(
                "KRW-", ""
            )

            result[symbol] = {
                "price": item.get("trade_price"),
                "change": (
                    item.get("signed_change_rate")
                    * 100
                    if item.get(
                        "signed_change_rate"
                    ) is not None
                    else None
                )
            }

    except (
        requests.RequestException,
        ValueError,
        KeyError,
        TypeError
    ) as exc:
        logging.warning(
            "업비트 조회 실패: %s", exc
        )

    return result


# ==============================================
# 9. 가상자산 달러 시세: CoinGecko
# ==============================================
def get_usd_coins():
    result = {}

    try:
        data = get_json(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": ",".join(COINS.values()),
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            }
        )

        for symbol, coin_id in COINS.items():
            item = data.get(coin_id, {})

            result[symbol] = {
                "price": item.get("usd"),
                "change": item.get(
                    "usd_24h_change"
                )
            }

    except (
        requests.RequestException,
        ValueError,
        TypeError
    ) as exc:
        logging.warning(
            "CoinGecko 조회 실패: %s",
            exc
        )

    return result


# ==============================================
# 10. 대시보드 디자인 보조 함수
# ==============================================
def font(size, bold=False):
    root = "/usr/share/fonts/truetype/nanum/"
    name = (
        "NanumGothicBold.ttf"
        if bold else "NanumGothic.ttf"
    )

    try:
        return ImageFont.truetype(
            root + name, size
        )
    except OSError:
        return ImageFont.load_default()


def draw_label(draw, xy, text, size=23, color="#CBD5E1", bold=False):
    draw.text(
        xy, str(text),
        font=font(size, bold),
        fill=color
    )


def draw_card(draw, x, y, w, h, title):
    draw.rounded_rectangle(
        (x, y, x+w, y+h),
        radius=18,
        fill="#222B40",
        outline="#38465F",
        width=2
    )

    draw_label(
        draw,
        (x+24, y+19),
        title,
        27,
        "#FFFFFF",
        True
    )

    draw.line(
        (x+24, y+66, x+w-24, y+66),
        fill="#3B4961",
        width=2
    )


def color_change(value):
    if value is None:
        return "#94A3B8"
    if value > 0:
        return "#FB7185"
    if value < 0:
        return "#60A5FA"
    return "#CBD5E1"


def draw_market_row(draw, x, y, name, data, digits=2, suffix=""):
    value = data.get("price")
    change = data.get("change")

    draw_label(
        draw, (x, y),
        name, 24, "#CBD5E1", True
    )

    price_text = (
        fmt(value, digits) + suffix
        if value is not None else "N/A"
    )

    draw_label(
        draw, (x+155, y),
        price_text, 25, "#FFFFFF", True
    )

    draw_label(
        draw, (x+375, y+2),
        change_text(change),
        21, color_change(change), True
    )


def draw_coin_row(draw, x, y, symbol, krw, usd):
    a = krw.get(symbol, {})
    b = usd.get(symbol, {})

    krw_price = a.get("price")
    usd_price = b.get("price")

    krw_text = (
        fmt(
            krw_price,
            0 if krw_price >= 1000 else 2
        )
        if krw_price is not None else "N/A"
    )

    usd_text = (
        fmt(
            usd_price,
            2 if usd_price >= 1 else 4
        )
        if usd_price is not None else "N/A"
    )

    draw_label(
        draw, (x, y),
        symbol, 24, "#FFFFFF", True
    )

    draw_label(
        draw, (x+100, y),
        krw_text, 23, "#E2E8F0"
    )

    draw_label(
        draw, (x+345, y),
        "$" + usd_text, 23, "#E2E8F0"
    )


# ==============================================
# 11. 시세 대시보드 이미지 생성
# ==============================================
def draw_dashboard(path="dashboard.png"):
    W, H = 1320, 1080

    image = Image.new(
        "RGB", (W, H), "#111827"
    )
    draw = ImageDraw.Draw(image)

    now = now_kst().strftime(
        "%Y-%m-%d %H:%M KST"
    )

    draw_label(
        draw, (45, 30),
        "DAILY MARKET DASHBOARD",
        37, "#FFFFFF", True
    )

    draw_label(
        draw, (47, 84),
        now, 22, "#94A3B8"
    )

    draw.line(
        (45, 125, 1275, 125),
        fill="#46536A", width=2
    )

    market = get_market_data()
    krw = get_krw_coins()
    usd = get_usd_coins()

    # 카드 배치
    left, right = 45, 675
    card_w = 600

    # 1행: 미국증시 / 한국증시
    draw_card(
        draw, left, 150,
        card_w, 245, "미국 증시"
    )
    draw_card(
        draw, right, 150,
        card_w, 245, "한국 증시"
    )

    draw_market_row(
        draw, left+24, 245,
        "S&P 500",
        market.get("SP500", {})
    )
    draw_market_row(
        draw, left+24, 315,
        "NASDAQ",
        market.get("NASDAQ", {})
    )

    draw_market_row(
        draw, right+24, 245,
        "KOSPI",
        market.get("KOSPI", {})
    )
    draw_market_row(
        draw, right+24, 315,
        "KOSDAQ",
        market.get("KOSDAQ", {})
    )

    # 2행: 코인 / 환율
    draw_card(
        draw, left, 420,
        card_w, 325, "가상자산"
    )
    draw_card(
        draw, right, 420,
        card_w, 325, "환율"
    )

    draw_label(
        draw, (left+124, 498),
        "KRW (업비트)",
        18, "#94A3B8"
    )
    draw_label(
        draw, (left+369, 498),
        "USD (CoinGecko)",
        18, "#94A3B8"
    )

    for i, symbol in enumerate(COINS):
        draw_coin_row(
            draw,
            left+24,
            540 + i*46,
            symbol, krw, usd
        )

    fx = market.get("USD/KRW", {})
    fx_price = fx.get("price")

    draw_label(
        draw, (right+27, 525),
        "USD / KRW",
        26, "#CBD5E1", True
    )

    draw_label(
        draw, (right+27, 585),
        (
            fmt(fx_price, 2) + " KRW"
            if fx_price is not None
            else "N/A"
        ),
        40, "#FFFFFF", True
    )

    draw_label(
        draw, (right+27, 665),
        "전일 대비 "
        + change_text(fx.get("change")),
        24,
        color_change(fx.get("change")),
        True
    )

    # 3행: 유가 / 금리
    draw_card(
        draw, left, 770,
        card_w, 235, "국제 유가"
    )
    draw_card(
        draw, right, 770,
        card_w, 235, "미국 국채금리"
    )

    draw_market_row(
        draw, left+24, 860,
        "WTI",
        market.get("WTI", {}),
        2, " USD"
    )
    draw_market_row(
        draw, left+24, 930,
        "Brent",
        market.get("BRENT", {}),
        2, " USD"
    )

    rate = market.get("US10Y", {})
    rate_value = rate.get("price")

    draw_label(
        draw, (right+27, 862),
        "10년물",
        28, "#CBD5E1", True
    )
    draw_label(
        draw, (right+205, 857),
        (
            fmt(rate_value, 3) + "%"
            if rate_value is not None
            else "N/A"
        ),
        35, "#FFFFFF", True
    )

    # 금리는 퍼센트 변동률보다
    # 전일 대비 bp 변화가 직관적
    previous = rate.get("previous")
    if rate_value is not None and previous is not None:
        bp = (rate_value - previous) * 100
        bp_text = f"{bp:+.1f}bp"
    else:
        bp = None
        bp_text = "N/A"

    draw_label(
        draw, (right+27, 942),
        "전일 대비 " + bp_text,
        23, color_change(bp), True
    )

    # 출처 및 주의 문구
    draw_label(
        draw, (45, 1022),
        "증시/환율/유가/금리: 최근 종가 | 코인: 조회 시점 현재가",
        18, "#94A3B8"
    )
    draw_label(
        draw, (45, 1048),
        "출처: Yahoo Finance / Upbit / CoinGecko | N/A = 조회 실패",
        17, "#94A3B8"
    )

    image.save(path)
    return path


def send_dashboard():
    path = draw_dashboard()

    try:
        with open(path, "rb") as photo:
            telegram(
                "sendPhoto",
                {
                    "chat_id": CHAT_ID,
                    "caption": (
                        "📊 주요 시세 브리핑 | "
                        + now_kst().strftime(
                            "%m/%d %H:%M"
                        )
                    )
                },
                files={"photo": photo}
            )
    finally:
        Path(path).unlink(
            missing_ok=True
        )


# ==============================================
# 12. 농협 키워드 뉴스 발송
# ==============================================
def run_keyword(history):
    keywords = [
        "농협",
        "농협금융"
    ]

    urls = [
        (
            "https://news.google.com/rss/search"
            f"?q={quote(word)}"
            "&hl=ko&gl=KR&ceid=KR:ko"
        )
        for word in keywords
    ]

    articles = collect_articles(
        urls, age_hours=48
    )

    seen = set(history["keyword"])
    count = 0

    for article in articles:
        key = article["key"]

        if (
            key in seen
            or article["link"] in seen
        ):
            continue

        try:
            send_text(
                article_message(
                    article,
                    "🚨 <b>[농협 관련 뉴스]</b>"
                )
            )
        except Exception:
            logging.exception(
                "키워드 기사 전송 실패"
            )
            continue

        history["keyword"][
            key
        ] = now_kst().isoformat()

        seen.add(key)
        count += 1

        if count >= 5:
            break

    logging.info(
        "키워드 발송: %d건", count
    )


# ==============================================
# 13. 정기 뉴스 브리핑
# ==============================================
def run_brief(history):
    try:
        send_dashboard()
    except Exception:
        logging.exception(
            "대시보드 전송 실패"
        )

    total = 0

    for category, urls in SOURCES.items():
        articles = collect_articles(
            urls, age_hours=72
        )

        selected = []
        seen = set(history["brief"])

        for article in articles:
            key = article["key"]

            if (
                key in seen
                or article["link"] in seen
            ):
                continue

            selected.append(article)
            seen.add(key)

            if len(selected) >= 4:
                break

        if not selected:
            continue

        header = (
            "📰 <b>["
            + html.escape(category)
            + "]</b>"
        )

        # 개별 발송하여 성공한 기사만 기록
        for i, article in enumerate(selected):
            try:
                send_text(
                    article_message(
                        article,
                        header if i == 0 else ""
                    )
                )

                history["brief"][
                    article["key"]
                ] = now_kst().isoformat()

                total += 1

            except Exception:
                logging.exception(
                    "브리핑 기사 전송 실패"
                )

    logging.info(
        "종합 뉴스 발송: %d건", total
    )


# ==============================================
# 14. 프로그램 실행
# ==============================================
def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--mode",
        choices=["keyword", "brief"],
        required=True
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s"
    )

    if not TOKEN or not CHAT_ID:
        raise RuntimeError(
            "GitHub Secrets의 "
            "TELEGRAM_TOKEN 또는 CHAT_ID를 "
            "확인하세요."
        )

    history = load_history()

    try:
        if args.mode == "keyword":
            run_keyword(history)
        else:
            run_brief(history)
    finally:
        save_history(history)


if __name__ == "__main__":
    main()
