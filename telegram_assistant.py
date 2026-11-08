
import os
import re
import hmac
import logging
from urllib.parse import quote

from flask import Flask, request, jsonify

from news_bot import (
    collect_articles,
    article_message,
    send_text,
    send_dashboard,
)

app = Flask(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

logging.basicConfig(level=logging.INFO)


# 서버 작동 확인용
@app.route("/", methods=["GET"])
def home():
    return "Personal AI Assistant is running!"


# 사용자가 질문하면 실행되는 뉴스 검색
def search_news(keyword):
    rss_url = (
        "https://news.google.com/rss/search?q="
        + quote(keyword)
        + "&hl=ko&gl=KR&ceid=KR:ko"
    )

    articles = collect_articles(
        [rss_url],
        hours=72
    )

    # 검색 결과 중 최대 5건
    selected = []
    seen = set()

    for article in articles:
        if article["key"] in seen:
            continue

        selected.append(article)
        seen.add(article["key"])

        if len(selected) >= 5:
            break

    if not selected:
        send_text(
            "검색 조건에 맞는 최근 뉴스가 없습니다.\n"
            "다른 키워드로 검색해 주세요."
        )
        return

    send_text(
        f"🔎 <b>[{escape_html(keyword)} 뉴스 검색]</b>\n"
        f"최근 기사 {len(selected)}건을 찾았습니다."
    )

    for article in selected:
        send_text(article_message(article))


def escape_html(value):
    import html
    return html.escape(value)


# 사용자 입력을 검색어로 변환
def find_keyword(text):
    text = text.strip()

    for word in [
        "알려줘",
        "찾아줘",
        "검색해줘",
        "보여줘",
        "있어?",
        "있니?",
    ]:
        text = text.replace(word, "")

    text = re.sub(
        r"(관련|최신|뉴스|기사)",
        " ",
        text
    )

    text = re.sub(
        r"\s+", " ",
        text
    ).strip(" ?!.")

    return text


# 텔레그램 질문 처리
def handle_message(text):
    text = text.strip()

    if text in [
        "/start",
        "/help",
        "안녕",
        "도움말",
    ]:
        send_text(
            "🤖 <b>개인 AI 비서</b>\n\n"
            "현재 사용 가능한 기능입니다.\n\n"
            "📰 농협금융 뉴스 알려줘\n"
            "🔎 이더리움 뉴스 찾아줘\n"
            "🌐 오늘 주요 뉴스\n"
            "📊 시세 보여줘\n\n"
            "앞으로 생활·영어·투자 기능도 "
            "추가할 예정입니다."
        )
        return

    # 기존 시세 대시보드 활용
    if "시세" in text or "대시보드" in text:
        send_dashboard()
        return

    # 주요 뉴스 요청
    if (
        "주요 뉴스" in text
        or "오늘 뉴스" in text
        or text == "뉴스"
    ):
        search_news("경제 금융 증시")
        return

    keyword = find_keyword(text)

    if keyword:
        search_news(keyword)
    else:
        send_text(
            "검색할 주제를 입력해 주세요.\n"
            "예: 농협금융 뉴스 알려줘"
        )


# 텔레그램 Webhook
@app.route("/telegram", methods=["POST"])
def telegram_webhook():

    # 인증 정보가 없다면 요청 거부
    if not WEBHOOK_SECRET or not CHAT_ID:
        return jsonify({
            "error": "Server not configured"
        }), 503

    # Telegram에서 설정한 비밀값 확인
    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        ""
    )

    if not hmac.compare_digest(
        received_secret,
        WEBHOOK_SECRET
    ):
        return jsonify({
            "error": "Unauthorized"
        }), 403

    update = request.get_json(silent=True) or {}
    message = update.get("message", {})

    chat = message.get("chat", {})
    incoming_chat_id = str(chat.get("id", ""))

    # 본인 채팅만 허용
    if incoming_chat_id != str(CHAT_ID):
        return jsonify({"ok": True}), 200

    text = message.get("text", "")

    if not text:
        return jsonify({"ok": True}), 200

    try:
        handle_message(text)

    except Exception:
        logging.exception(
            "메시지 처리 중 오류 발생"
        )

    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))

    app.run(
        host="0.0.0.0",
        port=port
    )
