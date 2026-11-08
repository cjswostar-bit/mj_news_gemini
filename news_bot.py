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
session.headers.update({"User-Agent": "Mozilla/5.0"})

retry = Retry(
    total=2,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
session.mount("https://", HTTPAdapter(max_retries=retry))


# ==============================================
# 2. 공통 함수
# ==============================================
def now_kst():
    return datetime.now(KST)


def clean(value):
    text = BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def article_key(title):
    normalized = re.sub(r"\s+", "", clean(title).casefold())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def number(value, digits=2):
    if isinstance(value, (int, float)):
        return f"{value:,.{digits}f}"
    return "N/A"


def percent(value):
    if isinstance(value, (int, float)):
        return f"{value:+.2f}%"
    return "N/A"


def bp_text(value):
    if isinstance(value, (int, float)):
        return f"{value:+.1f} bp"
    return "N/A"


def get_json(url, params=None):
    response = session.get(url, params=params, timeout=15)
    response.raise_for_status()
    return response.json()


# ==============================================
# 3. 기사 이력 관리
# ==============================================
def load_history():
    history = {"keyword": {}, "brief": {}}

    if not HISTORY_FILE.exists():
        return history

    try:
        raw = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))

        if isinstance(raw, dict):
            for mode in history:
                if isinstance(raw.get(mode), dict):
                    history[mode] = raw[mode]

        elif isinstance(raw, list):
            stamp = now_kst().isoformat()
            for mode in history:
                history[mode] = {
                    item: stamp for item in raw if isinstance(item, str)
                }

    except (OSError, ValueError) as exc:
        logging.warning("이력 읽기 실패: %s", exc)

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
            sorted(kept.items(), key=lambda x: x[1], reverse=True)[:1500]
        )

    HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ==============================================
# 4. 텔레그램 전송
# ==============================================
def telegram(method, data, files=None):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"

    response = session.post(url, data=data, files=files, timeout=30)
    response.raise_for_status()

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "텔레그램 전송 실패"))

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
# 5. 뉴스 수집
# ==============================================
def read_feed(url):
    try:
        response = session.get(url, timeout=20)
        response.raise_for_status()

        feed = feedparser.parse(response.content)
        if feed.bozo and not feed.entries:
            raise ValueError("RSS 형식 오류")

        return feed.entries[:15]

    except (requests.RequestException, ValueError) as exc:
        logging.warning("RSS 수집 실패 %s: %s", url, exc)
        return []


def published_at(entry):
    value = entry.get("published_parsed") or entry.get("updated_parsed")

    if value:
        try:
            return datetime(*value[:6], tzinfo=timezone.utc).astimezone(KST)
        except (ValueError, TypeError):
            return None

    return None


def collect_articles(urls, hours):
    cutoff = now_kst() - timedelta(hours=hours)
    articles = []

    for url in urls:
        for entry in read_feed(url):
            title = clean(entry.get("title"))
            link = str(entry.get("link", "")).strip()

            if not title or urlparse(link).scheme not in ("http", "https"):
                continue

            date = published_at(entry)
            if date and date < cutoff:
                continue

            articles.append(
                {
                    "title": title,
                    "link": link,
                    "date": date,
                    "summary": entry.get("summary", ""),
                    "key": article_key(title),
                }
            )

    articles.sort(
        key=lambda item: item["date"] or datetime.min.replace(tzinfo=KST),
        reverse=True,
    )

    return articles


# ==============================================
# 6. 기사 요약 및 메시지
# ==============================================
def get_summary(article):
    text = clean(article["summary"])

    if len(text) < 45 or text.casefold().startswith(article["title"].casefold()[:45]):
        text = ""

    host = urlparse(article["link"]).netloc.lower()

    if not text and "news.google.com" not in host:
        try:
            response = session.get(article["link"], timeout=10)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            tag = soup.find("meta", attrs={"name": "description"})
            if not tag:
                tag = soup.find("meta", attrs={"property": "og:description"})

            if tag:
                text = clean(tag.get("content", ""))

        except requests.RequestException:
            pass

    if not text:
        return "요약 정보가 없습니다. 원문 링크에서 확인해 주세요."

    return text[:170] + ("…" if len(text) > 170 else "")


def article_message(article, header=""):
    title = html.escape(article["title"])
    summary = html.escape(get_summary(article))
    link = html.escape(article["link"], quote=True)

    if article["date"]:
        stamp = article["date"].strftime("%m/%d %H:%M")
    else:
        stamp = "날짜 미상"

    return (
        f"{header}\n"
        f"<b>📰 {title} [{stamp}]</b>\n"
        f"📝 {summary}\n"
        f'🔗 <a href="{link}">기사 보기</a>'
    )


# ==============================================
# 7. 시세 데이터 조회
# ==============================================
def get_market_data():
    result = {}

    for name, symbol in MARKETS.items():
        try:
            url = (
                "https://query1.finance.yahoo.com/v8/finance/chart/"
                + quote(symbol, safe="")
            )

            data = get_json(url, {"range": "10d", "interval": "1d"})

            info = data["chart"]["result"][0]
            closes = info["indicators"]["quote"][0]["close"]
            timestamps = info.get("timestamp", [])

            valid = [
                (float(v), ts)
                for v, ts in zip(closes, timestamps)
                if v is not None
            ]

            if not valid:
                raise ValueError("유효한 가격이 없습니다.")

            price, last_ts = valid[-1]
            previous = valid[-2][0] if len(valid) > 1 else None
            change = ((price / previous - 1) * 100) if previous else None

            exchange_tz = info.get("meta", {}).get("exchangeTimezoneName", "UTC")
            try:
                tz_obj = ZoneInfo(exchange_tz)
            except Exception:
                tz_obj = timezone.utc

            basis_date = datetime.fromtimestamp(last_ts, tz=tz_obj).strftime("%m/%d")

            result[name] = {
                "price": price,
                "previous": previous,
                "change": change,
                "date": basis_date,
            }

        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            logging.warning("시세 조회 실패 %s: %s", name, exc)
            result[name] = {}

    return result


def get_krw_coins():
    result = {}

    try:
        data = get_json(
            "https://api.upbit.com/v1/ticker",
            {"markets": ",".join("KRW-" + symbol for symbol in COINS)},
        )

        for item in data:
            symbol = item.get("market", "").replace("KRW-", "")
            if symbol not in COINS:
                continue

            result[symbol] = {
                "price": item.get("trade_price"),
            }

    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        logging.warning("업비트 조회 실패: %s", exc)

    return result


def get_usd_coins():
    result = {}

    try:
        data = get_json(
            "https://api.coingecko.com/api/v3/simple/price",
            {
                "ids": ",".join(COINS.values()),
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_last_updated_at": "true",
            },
        )

        for symbol, coin_id in COINS.items():
            item = data.get(coin_id, {})

            result[symbol] = {
                "price": item.get("usd"),
                "change_24h": item.get("usd_24h_change"),
            }

    except (requests.RequestException, ValueError, TypeError) as exc:
        logging.warning("CoinGecko 조회 실패: %s", exc)

    return result


# ==============================================
# 8. 시세 이미지 디자인
# ==============================================
BG = "#F1F4F8"
WHITE = "#FFFFFF"
NAVY = "#132B49"
TEXT = "#10233D"
MUTED = "#6B778C"
LINE = "#D9E2EC"
ACCENT = "#2D78FF"
UP = "#E23B42"
DOWN = "#1E78E8"
NEUTRAL = "#6A7A90"


def trend_color(value):
    if value is None:
        return NEUTRAL
    if value > 0:
        return UP
    if value < 0:
        return DOWN
    return NEUTRAL



def fnt(size, bold=False):
    # Render와 GitHub Actions에서 동일한 한글 폰트 사용
    filename = (
        "NanumGothic-Bold.ttf"
        if bold
        else "NanumGothic-Regular.ttf"
    )

    local_font = Path("fonts") / filename

    if local_font.exists():
        return ImageFont.truetype(
            str(local_font), size
        )

    # 기존 GitHub Actions 환경 지원
    font_dir = "/usr/share/fonts/truetype/nanum/"
    old_name = (
        "NanumGothicBold.ttf"
        if bold
        else "NanumGothic.ttf"
    )

    try:
        return ImageFont.truetype(
            font_dir + old_name,
            size
        )
    except OSError:
        return ImageFont.load_default()



def put(draw, x, y, value, size=24, color=TEXT, bold=False, right=None):
    value = str(value)
    selected = fnt(size, bold)

    if right is not None:
        box = draw.textbbox((0, 0), value, font=selected)
        x = right - (box[2] - box[0])

    draw.text((x, y), value, font=selected, fill=color)


def draw_card(draw, x, y, w, h, title, subtitle=None):
    draw.rounded_rectangle(
        (x, y, x + w, y + h),
        radius=20,
        fill=WHITE,
        outline=LINE,
        width=2,
    )

    draw.rounded_rectangle(
        (x + 18, y + 16, x + 28, y + 58),
        radius=4,
        fill=ACCENT,
    )

    put(draw, x + 40, y + 10, title, 32, TEXT, True)

    if subtitle:
        put(draw, x + 22, y + 62, subtitle, 18, MUTED)

    draw.line((x + 20, y + 95, x + w - 20, y + 95), fill=LINE, width=2)


def draw_stock_row(draw, x, y, w, label, data):
    basis = data.get("date")

    put(draw, x + 24, y, label, 24, MUTED, True)
    put(draw, 0, y + 1, f"{basis} 종가" if basis else "조회 불가", 18, MUTED, right=x + w - 24)

    price = data.get("price")
    change = data.get("change")

    put(draw, x + 24, y + 30, number(price, 2) if price is not None else "N/A", 44, TEXT, True)
    put(draw, 0, y + 42, percent(change), 27, trend_color(change), True, right=x + w - 24)


def draw_coin_box(draw, x, y, w, h, symbol, krw_data, usd_data):
    draw.rounded_rectangle(
        (x, y, x + w, y + h),
        radius=16,
        fill=WHITE,
        outline=LINE,
        width=2,
    )

    krw = krw_data.get(symbol, {})
    usd = usd_data.get(symbol, {})

    change = usd.get("change_24h")

    put(draw, x + 24, y + 12, symbol, 30, TEXT, True)
    put(draw, 0, y + 13, percent(change), 24, trend_color(change), True, right=x + w - 20)

    krw_price = krw.get("price")
    usd_price = usd.get("price")

    if krw_price is not None:
        if krw_price >= 1000:
            krw_text = f"₩ {number(krw_price, 0)}"
        else:
            krw_text = f"₩ {number(krw_price, 2)}"
    else:
        krw_text = "₩ N/A"

    if usd_price is not None:
        if usd_price >= 1:
            usd_text = f"$ {number(usd_price, 2)}"
        else:
            usd_text = f"$ {number(usd_price, 4)}"
    else:
        usd_text = "$ N/A"

    put(draw, x + 24, y + 55, krw_text, 26, NAVY, True)
    put(draw, 0, y + 55, usd_text, 25, NAVY, True, right=x + w - 20)

    draw.line((x + w // 2, y + 51, x + w // 2, y + h - 20), fill=LINE, width=2)


def draw_dashboard(path="dashboard.png"):
    width, height = 1280, 1570
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    # 헤더
    draw.rounded_rectangle((18, 16, 1262, 140), radius=26, fill=NAVY)

    put(draw, 55, 35, "DAILY MARKET", 48, WHITE, True)
    put(draw, 55, 92, "시장 시세 브리핑", 26, "#D5E4FA", True)
    put(draw, 0, 90, now_kst().strftime("%Y-%m-%d %H:%M KST"), 20, WHITE, right=1215)

    market = get_market_data()
    krw = get_krw_coins()
    usd = get_usd_coins()

    left_x = 18
    right_x = 649
    card_w = 613

    # 1행: 미국증시 / 한국증시
    draw_card(draw, left_x, 165, card_w, 315, "미국 증시", "미국 거래소 기준 최근 종가")
    draw_card(draw, right_x, 165, card_w, 315, "한국 증시", "한국 거래소 기준 최근 종가")

    draw_stock_row(draw, left_x, 255, card_w, "S&P 500", market.get("SP500", {}))
    draw_stock_row(draw, left_x, 360, card_w, "NASDAQ", market.get("NASDAQ", {}))

    draw_stock_row(draw, right_x, 255, card_w, "KOSPI", market.get("KOSPI", {}))
    draw_stock_row(draw, right_x, 360, card_w, "KOSDAQ", market.get("KOSDAQ", {}))

    # 2행: 가상자산 (가로 전체)
    draw_card(draw, 18, 500, 1244, 350, "가상자산", "원화: 업비트  |  달러·24h: CoinGecko")

    crypto_box_w = 592
    crypto_box_h = 98
    gap_x = 18
    gap_y = 16

    draw_coin_box(draw, 42, 598, crypto_box_w, crypto_box_h, "BTC", krw, usd)
    draw_coin_box(draw, 42 + crypto_box_w + gap_x, 598, crypto_box_w, crypto_box_h, "ETH", krw, usd)
    draw_coin_box(draw, 42, 598 + crypto_box_h + gap_y, crypto_box_w, crypto_box_h, "XRP", krw, usd)
    draw_coin_box(draw, 42 + crypto_box_w + gap_x, 598 + crypto_box_h + gap_y, crypto_box_w, crypto_box_h, "ENA", krw, usd)

    # 3행: 환율 / 금리
    draw_card(draw, left_x, 870, card_w, 285, "달러 환율", "USD/KRW · 최근 종가")
    draw_card(draw, right_x, 870, card_w, 285, "미국 10년물 국채금리", "금리 수준 및 전일 대비 bp")

    fx = market.get("USD/KRW", {})
    fx_price = fx.get("price")
    fx_change = fx.get("change")
    fx_basis = fx.get("date")

    put(draw, left_x + 26, 976, "1 USD", 24, MUTED, True)
    put(draw, left_x + 26, 1025, f"{number(fx_price, 2)} KRW" if fx_price is not None else "N/A", 43, NAVY, True)

    draw.line((left_x + 370, 1012, left_x + 370, 1090), fill=LINE, width=2)

    put(draw, left_x + 395, 985, "전일 대비", 22, MUTED, True)
    put(draw, left_x + 395, 1037, percent(fx_change), 32, trend_color(fx_change), True)

    put(draw, left_x + 26, 1095, f"가격 기준: {fx_basis}" if fx_basis else "가격 기준: 조회 불가", 21, MUTED)

    bond = market.get("US10Y", {})
    bond_price = bond.get("price")
    bond_previous = bond.get("previous")
    bond_basis = bond.get("date")

    put(draw, right_x + 28, 985, f"기준: {bond_basis}" if bond_basis else "기준: 조회 불가", 22, MUTED)
    put(draw, right_x + 28, 1040, f"{number(bond_price, 3)}%" if bond_price is not None else "N/A", 48, NAVY, True)

    draw.line((right_x + 385, 1018, right_x + 385, 1090), fill=LINE, width=2)

    if bond_price is not None and bond_previous is not None:
        bp_change = (bond_price - bond_previous) * 100
    else:
        bp_change = None

    put(draw, 0, 1048, bp_text(bp_change), 31, trend_color(bp_change), True, right=right_x + card_w - 28)

    # 4행: 유가 한줄 전체
    draw_card(draw, 18, 1175, 1244, 225, "국제 유가", "WTI · Brent / USD per barrel")

    oil_left_x = 44
    oil_y = 1280
    half_w = 580

    wti = market.get("WTI", {})
    brent = market.get("BRENT", {})

    # WTI
    put(draw, oil_left_x, oil_y, "WTI", 24, MUTED, True)
    put(draw, 0, oil_y + 1, f"{wti.get('date')} 종가" if wti.get("date") else "조회 불가", 18, MUTED, right=oil_left_x + half_w - 24)
    put(draw, oil_left_x, oil_y + 42, f"{number(wti.get('price'), 2)} USD" if wti.get("price") is not None else "N/A", 46, NAVY, True)
    put(draw, 0, oil_y + 52, percent(wti.get("change")), 28, trend_color(wti.get("change")), True, right=oil_left_x + half_w - 24)

    # 중앙 구분선
    draw.line((641, 1293, 641, 1375), fill=LINE, width=2)

    # Brent
    brent_x = 685
    put(draw, brent_x, oil_y, "Brent", 24, MUTED, True)
    put(draw, 0, oil_y + 1, f"{brent.get('date')} 종가" if brent.get("date") else "조회 불가", 18, MUTED, right=1230)
    put(draw, brent_x, oil_y + 42, f"{number(brent.get('price'), 2)} USD" if brent.get("price") is not None else "N/A", 46, NAVY, True)
    put(draw, 0, oil_y + 52, percent(brent.get("change")), 28, trend_color(brent.get("change")), True, right=1230)

    # 하단 설명
    put(draw, 26, 1435, "상승", 24, UP, True)
    put(draw, 92, 1435, "/ 하락", 24, DOWN, True)

    put(draw, 26, 1478, "주식·환율·유가·금리: 최근 종가  |  코인: 조회 시점 가격", 20, MUTED)
    put(draw, 26, 1510, "출처: Yahoo Finance · Upbit · CoinGecko  |  N/A = 조회 실패", 18, MUTED)

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
                    "caption": "📊 주요 시세 브리핑 | " + now_kst().strftime("%m/%d %H:%M"),
                },
                files={"photo": photo},
            )
    finally:
        Path(path).unlink(missing_ok=True)


# ==============================================
# 9. 키워드 뉴스
# ==============================================
def run_keyword(history):
    urls = [
        (
            "https://news.google.com/rss/search"
            f"?q={quote(word)}&hl=ko&gl=KR&ceid=KR:ko"
        )
        for word in ("농협", "농협금융")
    ]

    articles = collect_articles(urls, 48)

    seen = set(history["keyword"])
    count = 0

    for article in articles:
        key = article["key"]

        if key in seen or article["link"] in seen:
            continue

        try:
            send_text(article_message(article, "🚨 <b>[농협 관련 뉴스]</b>"))
        except Exception:
            logging.exception("키워드 기사 전송 실패")
            continue

        history["keyword"][key] = now_kst().isoformat()
        seen.add(key)
        count += 1

        if count >= 5:
            break

    logging.info("키워드 뉴스 발송: %d건", count)


# ==============================================
# 10. 종합 뉴스 브리핑
# ==============================================
def run_brief(history):
    try:
        send_dashboard()
    except Exception:
        logging.exception("대시보드 전송 실패")

    count = 0

    for category, urls in SOURCES.items():
        selected = []
        seen = set(history["brief"])

        for article in collect_articles(urls, 72):
            if article["key"] in seen or article["link"] in seen:
                continue

            selected.append(article)
            seen.add(article["key"])

            if len(selected) >= 4:
                break

        for i, article in enumerate(selected):
            try:
                header = f"📰 <b>[{html.escape(category)}]</b>" if i == 0 else ""
                send_text(article_message(article, header))
                history["brief"][article["key"]] = now_kst().isoformat()
                count += 1
            except Exception:
                logging.exception("종합 브리핑 기사 전송 실패")

    logging.info("종합 브리핑 뉴스 발송: %d건", count)


# ==============================================
# 11. 실행
# ==============================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["keyword", "brief"], required=True)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if not TOKEN or not CHAT_ID:
        raise RuntimeError("GitHub Secrets의 TELEGRAM_TOKEN / CHAT_ID를 확인하세요.")

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
