import os
import json
import re
import argparse
import datetime
import requests
import feedparser
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
HISTORY_FILE = "history.json"

# 언론사별 RSS 출처
BRIEFING_RSS_SOURCES = {
    "📈 경제/증시/코인": [
        "https://www.hankyung.com/feed/economy",
        "https://www.mk.co.kr/rss/30000001/"
    ],
    "⚖️ 정치/사회/시사": [
        "https://www.hani.co.kr/rss/",
        "https://www.khan.co.kr/rss/rssdata/total_news.xml",
        "https://www.sisain.co.kr/rss/S1N1.xml"
    ],
    "🌐 국제": [
        "https://news.google.com/rss/headlines/section/topic/WORLD?hl=ko&gl=KR&ceid=KR:ko"
    ]
}

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_history(history):
    history_list = list(history)[-1000:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history_list, f, ensure_ascii=False, indent=2)

def send_telegram_text(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
    requests.post(url, data=payload)

def send_telegram_photo(photo_path, caption=""):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    with open(photo_path, "rb") as photo:
        payload = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}
        files = {"photo": photo}
        requests.post(url, data=payload, files=files)

def clean_html(text):
    soup = BeautifulSoup(text, "html.parser")
    cleaned = soup.get_text()
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def create_dashboard_image(output_path="dashboard.png"):
    """실시간 시세 대시보드 이미지 생성"""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    width, height = 800, 300
    img = Image.new("RGB", (width, height), color="#1e1e2e")
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf", 24)
        font_sub = ImageFont.truetype("/usr/share/fonts/truetype/nanum/NanumGothic.ttf", 16)
    except:
        font_title = font_sub = ImageFont.load_default()

    draw.text((40, 30), f"📊 Daily Market Dashboard ({now_str})", fill="#ffffff", font=font_title)
    draw.line([(40, 75), (760, 75)], fill="#45475a", width=2)

    # 코인 시세 (업비트 API)
    try:
        res = requests.get("https://api.upbit.com/v1/ticker?markets=KRW-BTC").json()
        btc_price = f"{res[0]['trade_price']:,} KRW"
        btc_change = f"{res[0]['signed_change_rate'] * 100:+.2f}%"
    except:
        btc_price, btc_change = "N/A", "0.00%"

    draw.rectangle([(40, 100), (370, 240)], fill="#252538", outline="#585b70", width=1)
    draw.text((60, 120), "🪙 Bitcoin (BTC)", fill="#cdd6f4", font=font_sub)
    draw.text((60, 155), btc_price, fill="#ffffff", font=font_title)
    color = "#f38ba8" if "-" in btc_change else "#a6e3a1"
    draw.text((60, 195), f"24h: {btc_change}", fill=color, font=font_sub)

    # 주요 시세 링크 안내 카드
    draw.rectangle([(400, 100), (760, 240)], fill="#252538", outline="#585b70", width=1)
    draw.text((420, 120), "📈 Quick Market Links", fill="#cdd6f4", font=font_sub)
    draw.text((420, 155), "• KOSPI / KOSDAQ Index", fill="#a6adc8", font=font_sub)
    draw.text((420, 185), "• USD/KRW Exchange Rate", fill="#a6adc8", font=font_sub)

    img.save(output_path)
    return output_path

# --- 1. 30분 단위 특정 키워드 감시 ---
def run_keyword_monitor():
    history = load_history()
    keywords = ["농협", "농협금융"]
    new_history = set(history)

    for kw in keywords:
        rss_url = f"https://news.google.com/rss/search?q={kw}&hl=ko&gl=KR&ceid=KR:ko"
        feed = feedparser.parse(rss_url)

        for entry in feed.entries[:3]:
            link = entry.link
            title = clean_html(entry.title)

            if link in history:
                continue

            # [키워드 알림] 대신 [{kw}] 형태로 전송
            msg = f"🚨 <b>[{kw}]</b>\n\n" \
                  f"📌 <a href='{link}'>{title}</a>"

            send_telegram_text(msg)
            new_history.add(link)

    save_history(new_history)

# --- 2. 종합 뉴스 브리핑 ---
def run_daily_briefing():
    history = load_history()
    new_history = set(history)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # 1) 대시보드 이미지 생성 및 전송
    image_path = create_dashboard_image()
    send_telegram_photo(image_path, caption=f"📰 <b>종합 뉴스 브리핑 ({now_str})</b>")

    # 2) 카테고리별 중복 제거 뉴스 브리핑 전송
    briefing_text = ""
    has_new_articles = False

    for category, rss_urls in BRIEFING_RSS_SOURCES.items():
        category_articles = []
        for url in rss_urls:
            feed = feedparser.parse(url)
            for entry in feed.entries[:3]:
                link = entry.link
                title = clean_html(entry.title)

                if link in history:
                    continue

                category_articles.append(f"• <a href='{link}'>{title}</a>")
                new_history.add(link)
                has_new_articles = True

        if category_articles:
            briefing_text += f"<b>[ {category} ]</b>\n" + "\n".join(category_articles[:4]) + "\n\n"

    if has_new_articles:
        send_telegram_text(briefing_text)
        save_history(new_history)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["keyword", "brief"], required=True)
    args = parser.parse_args()

    if args.mode == "keyword":
        run_keyword_monitor()
    elif args.mode == "brief":
        run_daily_briefing()
