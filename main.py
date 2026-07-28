"""
리멤버 커뮤니티 스팸 감지기
- 관심사 커뮤니티 11개의 최신글을 확인하고,
  LLM(Gemini)으로 사주/운세 스팸을 판별한 뒤, Slack으로 알림을 보냅니다.
"""

import os
import re
import json
import time
import urllib.request
from html.parser import HTMLParser


# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────
BASE_URL = "https://community.rememberapp.co.kr"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SEEN_FILE = "seen_posts.json"

# 관심사 커뮤니티 목록 (ID: 이름)
INTEREST_COMMUNITIES = {
    61: "회사생활",
    80: "이슈토론",
    82: "재테크",
    89: "자유주제",
    91: "술 이야기",
    92: "취미생활",
    93: "이직/커리어",
    109: "서류/면접 팁",
    110: "연애",
    111: "결혼생활",
    116: "자랑거리",
}


# ─────────────────────────────────────────────
# 1단계: 커뮤니티 페이지에서 글 목록 가져오기
# ─────────────────────────────────────────────
class PostParser(HTMLParser):
    """HTML에서 '최신글' 영역의 게시글을 추출하는 파서"""

    def __init__(self):
        super().__init__()
        self.posts = []
        self._current_href = None
        self._current_text = []
        self._in_post_link = False
        self._in_feed = False

    def handle_starttag(self, tag, attrs):
        if tag == "a" and self._in_feed:
            href = dict(attrs).get("href", "")
            if "/post/" in href:
                self._in_post_link = True
                self._current_href = href
                self._current_text = []

    def handle_endtag(self, tag):
        if tag == "a" and self._in_post_link:
            text = " ".join(self._current_text).strip()
            if self._current_href and text:
                match = re.search(r"/post/(\d+)", self._current_href)
                if match:
                    self.posts.append({
                        "id": match.group(1),
                        "url": f"{BASE_URL}/post/{match.group(1)}",
                        "text": text,
                    })
            self._in_post_link = False

    def handle_data(self, data):
        # "최신글" 또는 "새글피드" 텍스트를 만나면 이후부터 글을 수집
        if "최신글" in data or "새글피드" in data:
            self._in_feed = True
        if self._in_post_link:
            self._current_text.append(data.strip())


def fetch_community_posts(community_id: int, community_name: str) -> list[dict]:
    """특정 관심사 커뮤니티의 최신글을 가져옵니다."""
    url = f"{BASE_URL}/community/{community_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "SpamDetector/1.0"})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8")
    except Exception as e:
        print(f"    ⚠️ [{community_name}] 페이지 로드 실패: {e}")
        return []

    parser = PostParser()
    parser.feed(html)

    # 중복 제거 & 커뮤니티 이름 추가
    seen = set()
    unique = []
    for p in parser.posts:
        if p["id"] not in seen:
            seen.add(p["id"])
            p["community"] = community_name
            unique.append(p)
    return unique


def fetch_all_posts() -> list[dict]:
    """모든 관심사 커뮤니티의 최신글을 수집합니다."""
    all_posts = []
    seen_ids = set()

    for cid, cname in INTEREST_COMMUNITIES.items():
        posts = fetch_community_posts(cid, cname)
        count = 0
        for p in posts:
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"])
                all_posts.append(p)
                count += 1
        print(f"    📥 [{cname}] {count}개 수집")
        time.sleep(0.5)  # 서버 부담 줄이기

    return all_posts


# ─────────────────────────────────────────────
# 2단계: LLM으로 스팸 판별
# ─────────────────────────────────────────────
SPAM_DETECTION_PROMPT = """당신은 온라인 커뮤니티의 스팸 게시글 탐지 전문가입니다.

아래 게시글이 **사주·운세·점술·타로·신점·궁합 관련 홍보/스팸**인지 판별해 주세요.

## 주의사항
- 스패머는 탐지를 피하기 위해 의도적으로 단어를 변형합니다.
  예: "운세" → "운. 세", "운 세", "운·세", "ㅇㅅ" 등
  예: "사주" → "사. 주", "四柱", "saju" 등
- "올해의 흐름", "타고난 기운", "앞날을 봐드립니다" 등 우회 표현도 포함됩니다.
- 댓글로 카카오톡 오픈채팅 링크를 유도하는 경우가 많습니다.
- "몇 명 봐드립니다", "댓글 주시면 답 드립니다" 같은 무료 상담 유도 패턴에 주목하세요.

## 판별 기준
- 사주/운세/점술 관련 내용이면서 홍보·상담유도 성격이면 → 스팸
- 단순히 운세 이야기를 하는 일반 대화(예: "오늘 운세 봤는데 웃기더라")는 → 스팸 아님

## 응답 형식
반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트는 절대 포함하지 마세요.
{"is_spam": true 또는 false, "reason": "판단 근거를 한 줄로"}

## 게시글
{post_text}
"""


def check_spam_with_llm(post_text: str) -> dict:
    """Gemini API를 호출하여 스팸 여부를 판별합니다."""
    prompt = SPAM_DETECTION_PROMPT.replace("{post_text}", post_text)

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    )

    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 256},
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    text = data["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"```json\s*|```", "", text).strip()
    return json.loads(text)


# ─────────────────────────────────────────────
# 3단계: Slack 알림 보내기
# ─────────────────────────────────────────────
def send_slack_alert(post: dict, reason: str):
    """스팸으로 판별된 글을 Slack으로 알림합니다."""
    preview = post["text"][:200] + ("..." if len(post["text"]) > 200 else "")
    community = post.get("community", "알 수 없음")

    message = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🚨 사주/운세 스팸 감지"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*커뮤니티:*\n{community}"},
                    {"type": "mrkdwn", "text": f"*판별 사유:*\n{reason}"},
                ],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*글 내용 미리보기:*\n{preview}"},
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "📄 글 확인하기"},
                        "url": post["url"],
                    }
                ],
            },
        ]
    }

    payload = json.dumps(message).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)
    print(f"  ✅ Slack 알림 전송 완료: [{community}] {post['url']}")


# ─────────────────────────────────────────────
# 이미 확인한 글 관리
# ─────────────────────────────────────────────
def load_seen_posts() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen_posts(seen: set):
    recent = sorted(seen, key=int, reverse=True)[:2000]
    with open(SEEN_FILE, "w") as f:
        json.dump(recent, f)


# ─────────────────────────────────────────────
# 메인 실행
# ─────────────────────────────────────────────
def main():
    print("🔍 리멤버 커뮤니티 스팸 감지 시작...")
    print(f"  📌 검사 대상: 관심사 커뮤니티 {len(INTEREST_COMMUNITIES)}개\n")

    seen = load_seen_posts()
    print(f"  📋 기존에 확인한 글: {len(seen)}개\n")

    # 모든 관심사 커뮤니티에서 글 수집
    all_posts = fetch_all_posts()
    print(f"\n  📊 총 수집: {len(all_posts)}개")

    # 새 글만 필터링
    new_posts = [p for p in all_posts if p["id"] not in seen]
    print(f"  🆕 새 글: {len(new_posts)}개")

    if not new_posts:
        print("  ℹ️  새 글이 없습니다. 종료합니다.")
        save_seen_posts(seen)
        return

    # 각 새 글에 대해 스팸 판별
    spam_count = 0
    for post in new_posts:
        seen.add(post["id"])
        community = post.get("community", "")
        print(f"\n  🔎 [{community}] 검사 중: {post['text'][:50]}...")

        try:
            result = check_spam_with_llm(post["text"])
            print(f"     결과: is_spam={result['is_spam']}, reason={result['reason']}")

            if result["is_spam"]:
                spam_count += 1
                send_slack_alert(post, result["reason"])
        except Exception as e:
            print(f"     ⚠️ 판별 오류: {e}")

    save_seen_posts(seen)
    print(f"\n✅ 완료! 새 글 {len(new_posts)}개 중 스팸 {spam_count}개 감지")


if __name__ == "__main__":
    main()
