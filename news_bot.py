
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

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
HISTORY_FILE = Path("history.json")
KST = ZoneInfo("Asia/Seoul")

# 정기 브리핑용 언론사 RSS
SOURCES = {
    "📈 경제/증시/코인": [
        "https://www.hankyung.com/feed/economy",
        "https://www.mk.co.kr/rss/30000001/",
    ],
    "⚖️ 정치/사회/시사": [
        "https://www.hani.co.kr/rss/",
        "https://www.khan.co.kr/rss/rssdata/total_news.xml",
        "https://www.sisain.co.kr/rss/S1N1.xml",
    ],
    "🌐 국제": [
        "https://news.google.com/rss/headlines/section/topic/WORLD?hl=ko&gl=KR&ceid=KR:ko",
    ],
}

# RSS / 시세 조회 시 일시적 오류 재시도
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; NewsBriefingBot/2.0)"
})

retry = Retry(
    total=2,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"]
)
session.mount("https://", HTTPAdapter(max_retries=retry))


# --------------------------------------------------
# 1. 기사 제목 및 텍스트 정리
# --------------------------------------------------
def clean(text):
    value = BeautifulSoup(
        str(text or ""), "html.parser"
    ).get_text(" ", strip=True)

    return re.sub(
        r"\s+", " ", html.unescape(value)
    ).strip()


def history_key(title):
    title = re.sub(
        r"\s*[-|]\s*[^-|]{1,35}$",
        "",
        clean(title)
    )
    normalized = re.sub(
        r"\s+", "", title
    ).casefold()

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


# --------------------------------------------------
# 2. 중복 기사 기록 관리
# 키워드와 정기 브리핑의 이력은 별도 관리
# --------------------------------------------------
def load_history():
    empty = {"keyword": {}, "brief": {}}

    if not HISTORY_FILE.exists():
        return empty

    try:
        raw = json.loads(
            HISTORY_FILE.read_text(encoding="utf-8")
        )

        if isinstance(raw, dict):
            for mode in empty:
                if isinstance(raw.get(mode), dict):
                    empty[mode] = raw[mode]

        elif isinstance(raw, list):
            # 기존 버전의 history.json도 읽을 수 있도록 처리
            timestamp = datetime.now(KST).isoformat()
            empty = {
                mode: {
                    url: timestamp
                    for url in raw
                    if isinstance(url, str)
                }
                for mode in empty
            }

    except (ValueError, OSError) as exc:
        logging.warning("이력 읽기 실패: %s", exc)

    return empty


def save_history(history):
    cutoff = datetime.now(KST) - timedelta(days=45)

    for mode in ("keyword", "brief"):
        retained = {}

        for key, saved_at in history[mode].items():
            try:
                saved_time = datetime.fromisoformat(saved_at)

                if saved_time >= cutoff:
                    retained[key] = saved_at

            except (TypeError, ValueError):
                continue

        history[mode] = dict(
            sorted(
                retained.items(),
                key=lambda item: item[1],
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


# --------------------------------------------------
# 3. 텔레그램 메시지 전송
# --------------------------------------------------
def telegram(method, data, files=None):
    url = (
        f"https://api.telegram.org/bot"
        f"{TOKEN}/{method}"
    )

    response = session.post(
        url,
        data=data,
        files=files,
        timeout=25
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
            "disable_web_page_preview": "true"
        }
    )


# --------------------------------------------------
# 4. RSS 뉴스 수집
# --------------------------------------------------
def read_feed(url):
    try:
        response = session.get(
            url,
            timeout=20
        )
        response.raise_for_status()

        parsed = feedparser.parse(
            response.content
        )

        if parsed.bozo and not parsed.entries:
            raise ValueError("RSS 형식 오류")

        return parsed.entries[:15]

    except (
        requests.RequestException,
        ValueError
    ) as exc:
        logging.warning(
            "RSS 수집 실패 (%s): %s",
            url,
            exc
        )
        return []


def published_at(entry):
    structured = (
        entry.get("published_parsed")
        or entry.get("updated_parsed")
    )

    if structured:
        try:
            return datetime(
                *structured[:6],
                tzinfo=timezone.utc
            ).astimezone(KST)

        except (TypeError, ValueError):
            pass

    return None


def article_from_entry(entry):
    title = clean(
        entry.get("title")
    )
    link = entry.get(
        "link", ""
    ).strip()

    if (
        not title
        or urlparse(link).scheme
        not in ("https", "http")
    ):
        return None

    return {
        "title": title,
        "link": link,
        "summary_html": entry.get(
            "summary", ""
        ),
        "date": published_at(entry),
        "key": history_key(title)
    }


# --------------------------------------------------
# 5. 기사 요약 추출
# RSS 요약 우선 → 기사 메타 설명
# --------------------------------------------------
def summary_for(article):
    summary = clean(
        article["summary_html"]
    )

    title_start = clean(
        article["title"]
    ).casefold()[:45]

    # 기사 제목과 출처만 있는 RSS 설명은 제외
    if (
        len(summary) < 45
        or summary.casefold().startswith(title_start)
    ):
        summary = ""

    host = urlparse(
        article["link"]
    ).netloc.lower()

    # 일반 언론사 페이지의 설명 태그 확인
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

            tag = soup.find(
                "meta",
                attrs={"name": "description"}
            )

            if not tag:
                tag = soup.find(
                    "meta",
                    attrs={
                        "property": "og:description"
                    }
                )

            if tag:
                summary = clean(
                    tag.get("content", "")
                )

        except requests.RequestException:
            pass

    summary = summary.strip()

    if not summary:
        return (
            "요약을 제공하지 않는 기사입니다. "
            "링크에서 본문을 확인해 주세요."
        )

    return (
        summary[:170].rstrip()
        + ("…" if len(summary) > 170 else "")
    )


def article_message(article, header):
    title = html.escape(
        article["title"]
    )
    summary = html.escape(
        summary_for(article)
    )
    link = html.escape(
        article["link"],
        quote=True
    )

    return (
        f"{header}\n"
        f"<b>📰 {title}</b>\n"
        f"📝 {summary}\n"
        f'🔗 <a href="{link}">기사 보기</a>'
    )


# --------------------------------------------------
# 6. 비트코인 / 이더리움 시세 이미지
# --------------------------------------------------
def draw_dashboard(path="dashboard.png"):
    image = Image.new(
        "RGB",
        (800, 280),
        "#1e1e2e"
    )
    draw = ImageDraw.Draw(image)

    font_dir = (
        "/usr/share/fonts/truetype/nanum/"
    )

    try:
        big = ImageFont.truetype(
            font_dir + "NanumGothicBold.ttf",
            24
        )
        small = ImageFont.truetype(
            font_dir + "NanumGothic.ttf",
            17
        )
    except OSError:
        big = small = ImageFont.load_default()

    now_str = datetime.now(KST).strftime(
        "%Y-%m-%d %H:%M KST"
    )

    draw.text(
        (35, 24),
        "Market Dashboard  " + now_str,
        font=big,
        fill="white"
    )

    draw.line(
        (35, 70, 765, 70),
        fill="#585b70",
        width=2
    )

    prices = {}

    try:
        response = session.get(
            "https://api.upbit.com/v1/ticker",
            params={
                "markets": "KRW-BTC,KRW-ETH"
            },
            timeout=12
        )
        response.raise_for_status()

        prices = {
            item["market"]: item
            for item in response.json()
        }

    except (
        requests.RequestException,
        ValueError,
        KeyError,
        TypeError
    ) as exc:
        logging.warning(
            "시세 조회 실패: %s",
            exc
        )

    markets = [
        ("KRW-BTC", "Bitcoin (BTC)"),
        ("KRW-ETH", "Ethereum (ETH)")
    ]

    for idx, (market, label) in enumerate(markets):
        left = 35 + idx * 375

        draw.rounded_rectangle(
            (
                left, 95,
                left + 355, 230
            ),
            radius=12,
            fill="#29293d"
        )

        draw.text(
            (left + 20, 112),
            label,
            font=small,
            fill="#cdd6f4"
        )

        item = prices.get(market)

        if item:
            price = (
                f"{item['trade_price']:,.0f} KRW"
            )
            change = (
                "전일 대비 "
                f"{item['signed_change_rate'] * 100:+.2f}%"
            )
        else:
            price = "조회 실패"
            change = "시세 제공 일시 중단"

        draw.text(
            (left + 20, 149),
            price,
            font=big,
            fill="white"
        )

        draw.text(
            (left + 20, 193),
            change,
            font=small,
            fill="#a6e3a1"
        )

    image.save(path)
    return path


def send_dashboard():
    path = draw_dashboard()

    try:
        with open(path, "rb") as image:
            telegram(
                "sendPhoto",
                {
                    "chat_id": CHAT_ID,
                    "caption": (
                        "📊 종합 뉴스 브리핑 | "
                        + datetime.now(KST).strftime(
                            "%m/%d %H:%M"
                        )
                    )
                },
                files={"photo": image}
            )

    finally:
        Path(path).unlink(
            missing_ok=True
        )


# --------------------------------------------------
# 7. 기사 수집 및 날짜별 정렬
# --------------------------------------------------
def collect(urls, max_age_hours):
    articles = []

    cutoff = (
        datetime.now(KST)
        - timedelta(hours=max_age_hours)
    )

    for url in urls:
        for entry in read_feed(url):
            article = article_from_entry(entry)

            if (
                article
                and (
                    article["date"] is None
                    or article["date"] >= cutoff
                )
            ):
                articles.append(article)

    articles.sort(
        key=lambda a: (
            a["date"]
            or datetime.min.replace(tzinfo=KST)
        ),
        reverse=True
    )

    return articles


# --------------------------------------------------
# 8. 농협 관련 키워드 뉴스
# --------------------------------------------------
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

    articles = collect(
        urls,
        max_age_hours=48
    )

    seen = set(
        history["keyword"]
    )

    sent = 0

    for article in articles:
        if (
            article["key"] in seen
            or article["link"] in seen
        ):
            continue

        send_text(
            article_message(
                article,
                "🚨 <b>[농협 관련 뉴스]</b>"
            )
        )

        history["keyword"][
            article["key"]
        ] = datetime.now(KST).isoformat()

        seen.add(
            article["key"]
        )

        sent += 1

        # 한 번 실행할 때 최대 5건
        if sent >= 5:
            break

    logging.info(
        "키워드 뉴스 발송: %s건",
        sent
    )


# --------------------------------------------------
# 9. 정기 종합 뉴스 브리핑
# --------------------------------------------------
def run_brief(history):
    try:
        send_dashboard()

    except (
        requests.RequestException,
        ValueError,
        RuntimeError
    ) as exc:
        logging.warning(
            "대시보드 발송 실패: %s",
            exc
        )

    total = 0

    for category, urls in SOURCES.items():
        selected = []
        seen = set(
            history["brief"]
        )

        articles = collect(
            urls,
            max_age_hours=72
        )

        for article in articles:
            if (
                article["key"] in seen
                or article["link"] in seen
            ):
                continue

            selected.append(article)
            seen.add(
                article["key"]
            )

            # 카테고리별 최대 4건
            if len(selected) >= 4:
                break

        if not selected:
            continue

        header = (
            "<b>["
            + html.escape(category)
            + "]</b>"
        )

        message = "\n\n".join(
            article_message(
                article,
                header if i == 0 else ""
            )
            for i, article in enumerate(selected)
        )

        # 전송 성공 이후에만 이력 반영
        send_text(message)

        timestamp = datetime.now(
            KST
        ).isoformat()

        for article in selected:
            history["brief"][
                article["key"]
            ] = timestamp

        total += len(selected)

    logging.info(
        "정기 브리핑 발송: %s건",
        total
    )


# --------------------------------------------------
# 10. 실행 설정
# --------------------------------------------------
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
            "TELEGRAM_TOKEN / CHAT_ID를 확인하세요."
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
