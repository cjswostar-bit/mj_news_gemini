
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
    allowed_methods=["GET"],
)
session.mount(
    "https://",
    HTTPAdapter(max_retries=retry)
)


# ==============================================
# 2. 기본 함수
# ==============================================
def now_kst():
    return datetime.now(KST)


def clean(value):
    text = BeautifulSoup(
        str(value or ""), "html.parser"
    ).get_text(" ", strip=True)

    return re.sub(
        r"\s+", " ", html.unescape(text)
    ).strip()


def article_key(title):
    normalized = re.sub(
        r"\s+", "", clean(title).casefold()
    )
    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


def load_history():
    history = {
        "keyword": {},
        "brief": {},
    }

    if not HISTORY_FILE.exists():
        return history

    try:
        raw = json.loads(
            HISTORY_FILE.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(raw, dict):
            for mode in history:
                if isinstance(raw.get(mode), dict):
                    history[mode] = raw[mode]

        elif isinstance(raw, list):
            stamp = now_kst().isoformat()

            for mode in history:
                history[mode] = {
                    url: stamp
                    for url in raw
                    if isinstance(url, str)
                }

    except (OSError, ValueError) as exc:
        logging.warning(
            "이력 읽기 실패: %s", exc
        )

    return history


def save_history(history):
    cutoff = now_kst() - timedelta(days=45)

    for mode in ("keyword", "brief"):
        kept = {}

        for key, value in history[mode].items():
            try:
                dt = datetime.fromisoformat(value)

                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=KST)

                if dt >= cutoff:
                    kept[key] = value

            except (TypeError, ValueError):
                continue

        history[mode] = dict(
            sorted(
                kept.items(),
                key=lambda x: x[1],
                reverse=True,
            )[:1500]
        )

    HISTORY_FILE.write_text(
        json.dumps(
            history,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ==============================================
# 3. 텔레그램 발송
# ==============================================
def telegram(method, data, files=None):
    url = (
        f"https://api.telegram.org/bot"
        f"{TOKEN}/{method}"
    )

    response = session.post(
        url,
        data=data,
        files=files,
        timeout=30,
    )
    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            result.get(
                "description",
                "텔레그램 전송 실패"
            )
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
        },
    )


# ==============================================
# 4. 뉴스 수집
# ==============================================
def read_feed(url):
    try:
        response = session.get(
            url, timeout=20
        )
        response.raise_for_status()

        feed = feedparser.parse(
            response.content
        )

        if feed.bozo and not feed.entries:
            raise ValueError("RSS 형식 오류")

        return feed.entries[:15]

    except (
        requests.RequestException,
        ValueError,
    ) as exc:
        logging.warning(
            "RSS 수집 실패 %s: %s",
            url, exc
        )
        return []


def published_at(entry):
    value = (
        entry.get("published_parsed")
        or entry.get("updated_parsed")
    )

    if value:
        try:
            return datetime(
                *value[:6],
                tzinfo=timezone.utc,
            ).astimezone(KST)
        except (ValueError, TypeError):
            pass

    return None


def collect_articles(urls, hours):
    cutoff = now_kst() - timedelta(
        hours=hours
    )
    articles = []

    for url in urls:
        for entry in read_feed(url):
            title = clean(
                entry.get("title")
            )
            link = str(
                entry.get("link", "")
            ).strip()

            if (
                not title
                or urlparse(link).scheme
                not in ("https", "http")
            ):
                continue

            date = published_at(entry)

            if date and date < cutoff:
                continue

            articles.append({
                "title": title,
                "link": link,
                "date": date,
                "summary": entry.get(
                    "summary", ""
                ),
                "key": article_key(title),
            })

    articles.sort(
        key=lambda item: (
            item["date"]
            or datetime.min.replace(
                tzinfo=KST
            )
        ),
        reverse=True,
    )

    return articles


# ==============================================
# 5. 기사 요약과 날짜 표시
# ==============================================
def get_summary(article):
    text = clean(
        article["summary"]
    )

    if (
        len(text) < 45
        or text.casefold().startswith(
            article["title"].casefold()[:45]
        )
    ):
        text = ""

    host = urlparse(
        article["link"]
    ).netloc.lower()

    if (
        not text
        and "news.google.com" not in host
    ):
        try:
            response = session.get(
                article["link"],
                timeout=10,
            )
            response.raise_for_status()

            soup = BeautifulSoup(
                response.text,
                "html.parser",
            )

            tag = soup.find(
                "meta",
                attrs={
                    "name": "description"
                },
            )

            if not tag:
                tag = soup.find(
                    "meta",
                    attrs={
                        "property": "og:description"
                    },
                )

            text = (
                clean(tag.get("content", ""))
                if tag else ""
            )

        except requests.RequestException:
            pass

    if not text:
        return (
            "요약 정보가 없습니다. "
            "원문 링크에서 확인해 주세요."
        )

    return (
        text[:170]
        + ("…" if len(text) > 170 else "")
    )


def article_message(article, header=""):
    title = html.escape(
        article["title"]
    )
    summary = html.escape(
        get_summary(article)
    )
    link = html.escape(
        article["link"],
        quote=True,
    )

    if article["date"]:
        stamp = article["date"].strftime(
            "%m/%d %H:%M"
        )
    else:
        stamp = "날짜 미상"

    return (
        f"{header}\n"
        f"<b>📰 {title} [{stamp}]</b>\n"
        f"📝 {summary}\n"
        f'🔗 <a href="{link}">기사 보기</a>'
    )


# ==============================================
# 6. 시세 데이터 조회
# ==============================================
def get_json(url, params=None):
    response = session.get(
        url,
        params=params,
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


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
                {
                    "range": "10d",
                    "interval": "1d",
                },
            )

            info = data["chart"]["result"][0]
            closes = info["indicators"]["quote"][0]["close"]
            stamps = info.get(
                "timestamp", []
            )

            valid = [
                (float(v), t)
                for v, t in zip(
                    closes, stamps
                )
                if v is not None
            ]

            if not valid:
                raise ValueError(
                    "유효한 종가가 없습니다"
                )

            price, stamp = valid[-1]

            previous = (
                valid[-2][0]
                if len(valid) > 1
                else None
            )

            change = (
                (price / previous - 1) * 100
                if previous
                else None
            )

            exchange_tz = info.get(
                "meta", {}
            ).get(
                "exchangeTimezoneName",
                "UTC",
            )

            try:
                market_tz = ZoneInfo(
                    exchange_tz
                )
            except (KeyError, ValueError):
                market_tz = timezone.utc

            market_day = datetime.fromtimestamp(
                stamp,
                tz=market_tz,
            ).strftime("%m/%d")

            result[name] = {
                "price": price,
                "previous": previous,
                "change": change,
                "date": market_day,
            }

        except (
            requests.RequestException,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            logging.warning(
                "시세 조회 실패 %s: %s",
                name, exc
            )
            result[name] = {}

    return result


# ==============================================
# 7. 코인 시세: 업비트 KRW
# ==============================================
def get_krw_coins():
    result = {}

    try:
        data = get_json(
            "https://api.upbit.com/v1/ticker",
            {
                "markets": ",".join(
                    "KRW-" + symbol
                    for symbol in COINS
                )
            },
        )

        for item in data:
            symbol = item.get(
                "market", ""
            ).replace("KRW-", "")

            if symbol not in COINS:
                continue

            timestamp = (
                item.get("timestamp")
                or item.get("trade_timestamp")
            )

            observed = (
                datetime.fromtimestamp(
                    timestamp / 1000,
                    tz=KST,
                ).strftime("%H:%M")
                if timestamp
                else None
            )

            result[symbol] = {
                "price": item.get(
                    "trade_price"
                ),
                "observed": observed,
            }

    except (
        requests.RequestException,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        logging.warning(
            "업비트 시세 조회 실패: %s",
            exc
        )

    return result


# ==============================================
# 8. 코인 시세: CoinGecko USD / 24h
# ==============================================
def get_usd_coins():
    result = {}

    try:
        data = get_json(
            "https://api.coingecko.com/api/v3/simple/price",
            {
                "ids": ",".join(
                    COINS.values()
                ),
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_last_updated_at": "true",
            },
        )

        for symbol, coin_id in COINS.items():
            item = data.get(
                coin_id, {}
            )

            ts = item.get(
                "last_updated_at"
            )

            observed = (
                datetime.fromtimestamp(
                    ts, tz=KST
                ).strftime("%H:%M")
                if ts else None
            )

            result[symbol] = {
                "price": item.get("usd"),
                "change_24h": item.get(
                    "usd_24h_change"
                ),
                "observed": observed,
            }

    except (
        requests.RequestException,
        ValueError,
        TypeError,
    ) as exc:
        logging.warning(
            "달러 코인 시세 조회 실패: %s",
            exc
        )

    return result


# ==============================================
# 9. 시세 이미지 디자인 설정
# ==============================================
BG = "#F0F4FA"
WHITE = "#FFFFFF"
NAVY = "#162840"
TEXT = "#14263E"
MUTED = "#66788F"
LINE = "#DDE5EF"

# 상승은 빨간색, 하락은 파란색
UP = "#D63848"
DOWN = "#1769C2"
NEUTRAL = "#596B81"


def fnt(size, bold=False):
    directory = (
        "/usr/share/fonts/truetype/nanum/"
    )
    filename = (
        "NanumGothicBold.ttf"
        if bold
        else "NanumGothic.ttf"
    )

    try:
        return ImageFont.truetype(
            directory + filename,
            size,
        )
    except OSError:
        return ImageFont.load_default()


def put(
    draw, x, y, value,
    size=28, color=TEXT,
    bold=False, right=None,
):
    value = str(value)
    selected = fnt(size, bold)

    if right is not None:
        box = draw.textbbox(
            (0, 0),
            value,
            font=selected,
        )
        x = right - (
            box[2] - box[0]
        )

    draw.text(
        (x, y),
        value,
        font=selected,
        fill=color,
    )


def number(value, digits=2):
    if isinstance(
        value, (int, float)
    ):
        return f"{value:,.{digits}f}"
    return "N/A"


def percent(value):
    if isinstance(
        value, (int, float)
    ):
        return f"{value:+.2f}%"
    return "N/A"


def trend_color(value):
    if value is None:
        return NEUTRAL

    if value > 0:
        return UP

    if value < 0:
        return DOWN

    return NEUTRAL


# ==============================================
# 10. 카드 및 숫자 그리기
# ==============================================
def card(
    draw, x, y,
    width, height,
    title, subtitle=None,
):
    draw.rounded_rectangle(
        (
            x, y,
            x + width,
            y + height,
        ),
        radius=22,
        fill=WHITE,
        outline=LINE,
        width=2,
    )

    draw.rounded_rectangle(
        (
            x + 22, y + 24,
            x + 29, y + 61,
        ),
        radius=3,
        fill="#2D6CDF",
    )

    put(
        draw,
        x + 43, y + 20,
        title,
        35, NAVY, True,
    )

    if subtitle:
        put(
            draw,
            x + 25, y + 69,
            subtitle,
            20, MUTED,
        )

    draw.line(
        (
            x + 25, y + 103,
            x + width - 25, y + 103,
        ),
        fill=LINE,
        width=2,
    )


def stock_row(
    draw, x, y, width,
    label, data,
    digits=2, suffix="",
):
    date = data.get("date")

    put(
        draw,
        x + 26, y,
        label,
        26, MUTED, True,
    )

    put(
        draw, 0, y + 3,
        f"{date} 종가"
        if date
        else "조회 불가",
        19, MUTED,
        right=x + width - 27,
    )

    price = data.get("price")

    price_text = (
        number(price, digits) + suffix
        if price is not None
        else "N/A"
    )

    put(
        draw,
        x + 26, y + 32,
        price_text,
        39, TEXT, True,
    )

    change = data.get("change")

    put(
        draw, 0, y + 42,
        percent(change),
        25,
        trend_color(change),
        True,
        right=x + width - 27,
    )


def coin_row(
    draw, x, y, width,
    symbol, krw, usd,
):
    a = krw.get(symbol, {})
    b = usd.get(symbol, {})

    k = a.get("price")
    u = b.get("price")
    change = b.get(
        "change_24h"
    )

    put(
        draw,
        x + 26, y,
        symbol,
        30, TEXT, True,
    )

    put(
        draw, 0, y + 2,
        percent(change),
        27,
        trend_color(change),
        True,
        right=x + width - 27,
    )

    krw_text = number(
        k,
        0 if k is not None
        and k >= 1000
        else 2,
    )

    usd_text = number(
        u,
        2 if u is not None
        and u >= 1
        else 4,
    )

    put(
        draw,
        x + 26, y + 39,
        f"₩ {krw_text}",
        29, NAVY, True,
    )

    put(
        draw, 0, y + 39,
        f"$ {usd_text}",
        28, NAVY, True,
        right=x + width - 27,
    )


# ==============================================
# 11. 새로운 시세 대시보드 생성
# ==============================================
def draw_dashboard(path="dashboard.png"):
    width, height = 1280, 1510

    img = Image.new(
        "RGB",
        (width, height),
        BG,
    )
    draw = ImageDraw.Draw(img)

    # 상단 제목 영역
    draw.rounded_rectangle(
        (28, 26, 1252, 155),
        radius=24,
        fill=NAVY,
    )

    put(
        draw, 59, 43,
        "DAILY MARKET",
        44, WHITE, True,
    )

    put(
        draw, 59, 101,
        "시장 시세 브리핑",
        27, "#BDD3F5", True,
    )

    put(
        draw, 0, 102,
        now_kst().strftime(
            "%Y-%m-%d %H:%M KST"
        ),
        23, WHITE,
        right=1215,
    )

    # 시세 조회
    market = get_market_data()
    krw = get_krw_coins()
    usd = get_usd_coins()

    # 카드 좌표
    lx = 28
    rx = 654
    cw = 598

    # ------------------------------------------
    # 1행: 미국 증시 / 한국 증시
    # ------------------------------------------
    card(
        draw,
        lx, 178, cw, 298,
        "미국 증시",
        "미국 거래소 기준 최근 종가",
    )

    card(
        draw,
        rx, 178, cw, 298,
        "한국 증시",
        "한국 거래소 기준 최근 종가",
    )

    stock_row(
        draw, lx, 294, cw,
        "S&P 500",
        market.get("SP500", {}),
    )

    stock_row(
        draw, lx, 387, cw,
        "NASDAQ",
        market.get("NASDAQ", {}),
    )

    stock_row(
        draw, rx, 294, cw,
        "KOSPI",
        market.get("KOSPI", {}),
    )

    stock_row(
        draw, rx, 387, cw,
        "KOSDAQ",
        market.get("KOSDAQ", {}),
    )

    # ------------------------------------------
    # 2행: 가상자산 / 환율
    # ------------------------------------------
    card(
        draw,
        lx, 495, cw, 468,
        "가상자산",
        "원화: 업비트  |  달러·24h: CoinGecko",
    )

    card(
        draw,
        rx, 495, cw, 468,
        "달러 환율",
        "USD/KRW · 최근 종가",
    )

    for i, symbol in enumerate(COINS):
        coin_row(
            draw,
            lx,
            616 + 87 * i,
            cw,
            symbol,
            krw,
            usd,
        )

    fx = market.get(
        "USD/KRW", {}
    )

    put(
        draw,
        rx + 27, 623,
        "1 USD",
        28, MUTED, True,
    )

    value = fx.get("price")

    put(
        draw,
        rx + 27, 682,
        (
            number(value) + " KRW"
            if value is not None
            else "N/A"
        ),
        48, NAVY, True,
    )

    put(
        draw,
        rx + 27, 761,
        "전일 대비",
        26, MUTED,
    )

    put(
        draw,
        rx + 27, 805,
        percent(
            fx.get("change")
        ),
        35,
        trend_color(
            fx.get("change")
        ),
        True,
    )

    put(
        draw,
        rx + 27, 860,
        (
            "가격 기준: "
            + (
                fx.get("date")
                or "조회 불가"
            )
        ),
        22, MUTED,
    )

    # ------------------------------------------
    # 3행: 국제 유가 / 미국 국채금리
    # ------------------------------------------
    card(
        draw,
        lx, 984, cw, 297,
        "국제 유가",
        "WTI · Brent / USD per barrel",
    )

    card(
        draw,
        rx, 984, cw, 297,
        "미국 10년물 국채금리",
        "금리 수준 및 전일 대비 bp",
    )

    stock_row(
        draw,
        lx, 1100, cw,
        "WTI",
        market.get("WTI", {}),
        suffix=" USD",
    )

    stock_row(
        draw,
        lx, 1193, cw,
        "Brent",
        market.get("BRENT", {}),
        suffix=" USD",
    )

    bond = market.get(
        "US10Y", {}
    )

    put(
        draw,
        rx + 28, 1112,
        (
            "기준: "
            + (
                bond.get("date")
                or "조회 불가"
            )
        ),
        23, MUTED,
    )

    current = bond.get("price")
    previous = bond.get("previous")

    put(
        draw,
        rx + 28, 1162,
        (
            number(current, 3) + "%"
            if current is not None
            else "N/A"
        ),
        50, NAVY, True,
    )

    bp = (
        (current - previous) * 100
        if current is not None
        and previous is not None
        else None
    )

    put(
        draw, 0, 1186,
        (
            f"{bp:+.1f} bp"
            if bp is not None
            else "N/A"
        ),
        29,
        trend_color(bp),
        True,
        right=rx + cw - 28,
    )

    # ------------------------------------------
    # 하단 색상 범례 및 데이터 출처
    # ------------------------------------------
    draw.line(
        (44, 1323, 1236, 1323),
        fill=LINE,
        width=2,
    )

    put(
        draw,
        48, 1345,
        "상승",
        25, UP, True,
    )

    put(
        draw,
        122, 1345,
        "/ 하락",
        25, DOWN, True,
    )

    put(
        draw,
        48, 1392,
        (
            "주식·환율·유가·금리: 최근 종가"
            "  |  코인: 조회 시점 가격"
        ),
        23, MUTED,
    )

    put(
        draw,
        48, 1434,
        (
            "출처: Yahoo Finance · Upbit · "
            "CoinGecko  |  N/A = 조회 실패"
        ),
        21, MUTED,
    )

    img.save(path)
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
                    ),
                },
                files={"photo": photo},
            )

    finally:
        Path(path).unlink(
            missing_ok=True
        )


# ==============================================
# 12. 농협 키워드 뉴스
# ==============================================
def run_keyword(history):
    urls = [
        (
            "https://news.google.com/rss/search"
            f"?q={quote(word)}"
            "&hl=ko&gl=KR&ceid=KR:ko"
        )
        for word in (
            "농협",
            "농협금융",
        )
    ]

    articles = collect_articles(
        urls, 48
    )

    seen = set(
        history["keyword"]
    )
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
                    "🚨 <b>[농협 관련 뉴스]</b>",
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
        "키워드 뉴스 발송: %d건",
        count,
    )


# ==============================================
# 13. 정기 종합 뉴스 브리핑
# ==============================================
def run_brief(history):
    try:
        send_dashboard()
    except Exception:
        logging.exception(
            "대시보드 전송 실패"
        )

    count = 0

    for category, urls in SOURCES.items():
        selected = []
        seen = set(
            history["brief"]
        )

        for article in collect_articles(
            urls, 72
        ):
            if (
                article["key"] in seen
                or article["link"] in seen
            ):
                continue

            selected.append(article)
            seen.add(
                article["key"]
            )

            if len(selected) >= 4:
                break

        for i, article in enumerate(selected):
            try:
                header = (
                    f"📰 <b>["
                    f"{html.escape(category)}"
                    f"]</b>"
                    if i == 0
                    else ""
                )

                send_text(
                    article_message(
                        article, header
                    )
                )

                history["brief"][
                    article["key"]
                ] = now_kst().isoformat()

                count += 1

            except Exception:
                logging.exception(
                    "종합 브리핑 기사 전송 실패"
                )

    logging.info(
        "종합 브리핑 뉴스 발송: %d건",
        count,
    )


# ==============================================
# 14. 프로그램 실행
# ==============================================
def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--mode",
        choices=[
            "keyword",
            "brief",
        ],
        required=True,
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(levelname)s: "
            "%(message)s"
        ),
    )

    if not TOKEN or not CHAT_ID:
        raise RuntimeError(
            "GitHub Secrets의 "
            "TELEGRAM_TOKEN / CHAT_ID를 "
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
