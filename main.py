"""
리멤버 커뮤니티 스팸 감지기
- 관심사 11개 + 직무 40개 커뮤니티의 최신글을 확인하고,
  LLM(Gemini)으로 스팸을 판별한 뒤, Slack으로 알림을 보냅니다.
"""

import os
import re
import json
import time
import hashlib
import urllib.request
from html.parser import HTMLParser


# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────
BASE_URL = "https://community.rememberapp.co.kr"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SEEN_FILE = "seen_posts.json"

INTEREST_COMMUNITIES = {
    61: "회사생활", 80: "이슈토론", 82: "재테크", 89: "자유주제",
    91: "술 이야기", 92: "취미생활", 93: "이직/커리어",
    109: "서류/면접 팁", 110: "연애", 111: "결혼생활", 116: "자랑거리",
}

JOB_COMMUNITIES = {
    2: "CEO/법인대표", 5: "재무/회계", 7: "금융/투자", 8: "부동산 개발",
    9: "법률전문가", 10: "인사/HR", 11: "바이오/헬스케어", 13: "대학 교수",
    15: "유치원/학교 교사", 16: "영업/세일즈", 19: "마케팅", 20: "IT 직군",
    21: "IT 엔지니어", 24: "AI/빅데이터", 26: "R&D", 29: "건설/건축",
    30: "생산/제조", 31: "무역/유통/물류", 32: "커머스/MD", 33: "공직/공무원",
    34: "방송/언론", 35: "미디어/문화/예술", 38: "사회복지사", 39: "종교인",
    40: "통/번역사", 44: "고객대응/CS", 45: "비서", 47: "정보보안",
    53: "데이터분석", 55: "ESG/CSR", 56: "PE/VC", 59: "자영업",
    60: "전략/기획", 62: "디자이너", 65: "은행 근무자", 66: "식음료/외식 업계",
    76: "단체/협회", 77: "기업교육/산업강사", 87: "프리랜서/1인기업",
    90: "기획/PM/PO", 107: "총무/GA", 112: "컨설팅", 113: "홍보/PR",
    114: "설계/엔지니어링", 115: "의료/보건",
}

ALL_COMMUNITIES = {**INTEREST_COMMUNITIES, **JOB_COMMUNITIES}


# ─────────────────────────────────────────────
# 1단계: 커뮤니티 페이지에서 글 목록 가져오기
# ─────────────────────────────────────────────
class PostParser(HTMLParser):
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
                    parsed = self._parse_post_parts(self._current_text, text)
                    parsed["id"] = match.group(1)
                    parsed["url"] = f"{BASE_URL}/post/{match.group(1)}"
                    self.posts.append(parsed)
            self._in_post_link = False

    def handle_data(self, data):
        if "최신글" in data or "새글피드" in data:
            self._in_feed = True
        if self._in_post_link:
            stripped = data.strip()
            if stripped:
                self._current_text.append(stripped)

    @staticmethod
    def _parse_post_parts(parts, full_text):
        title = parts[0] if parts else full_text
        author = ""
        time_str = ""
        views = ""
        likes = ""
        comments = ""
        body = full_text

        meta_re = re.compile(
            r'^(.*?)\s+(\S+)\s+'
            r'(방금|\d+분\s*전|\d+시간\s*전|\d+일\s*전|\d{1,2}월\s*\d{1,2}일)'
            r'\s*(?:조회수)*\s*(\d[\d,]*)\s*(?:좋아요)*\s*(\d[\d,]*)\s*(?:댓글)*\s*(\d[\d,]*)\s*$',
            re.DOTALL
        )
        m = meta_re.match(full_text)
        if m:
            body = m.group(1).strip()
            author = m.group(2)
            time_str = m.group(3)
            views = m.group(4)
            likes = m.group(5)
            comments = m.group(6)
        if body.startswith(title) and len(body) > len(title):
            body = body[len(title):].strip()
        return {"title": title, "body": body, "text": full_text,
                "author": author, "time": time_str,
                "views": views, "likes": likes, "comments": comments}


def fetch_community_posts(community_id, community_name):
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
    seen = set()
    unique = []
    for p in parser.posts:
        if p["id"] not in seen:
            seen.add(p["id"])
            p["community"] = community_name
            unique.append(p)
    return unique


def fetch_all_posts():
    all_posts = []
    seen_ids = set()
    total = len(ALL_COMMUNITIES)
    for i, (cid, cname) in enumerate(ALL_COMMUNITIES.items(), 1):
        posts = fetch_community_posts(cid, cname)
        count = 0
        for p in posts:
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"])
                all_posts.append(p)
                count += 1
        print(f"    📥 [{cname}] {count}개 ({i}/{total})")
        time.sleep(0.3)
    return all_posts


# ─────────────────────────────────────────────
# 2단계: LLM(Gemini)으로 스팸 판별
# ─────────────────────────────────────────────
SPAM_PROMPT = """당신은 직장인 커뮤니티의 스팸 게시글 탐지 전문가입니다.

아래 게시글이 **스팸(홍보/광고/도배)**인지 판별해 주세요.

## 스팸의 예시
- 사주/운세/타로/점술 관련 홍보 (단어를 "운. 세"처럼 변형하기도 함)
- 외부 링크(카카오 오픈채팅, 블로그, 사이트 등)로 유도하는 홍보
- 상품/서비스/앱/강의/코칭 등 상업적 홍보 및 판매
- 설문조사/서베이 참여 유도 (무조건 스팸)
- 서비스/앱/사이트/커뮤니티 등의 가입 유도
- "N명 봐드릴게요", "댓글 남겨주세요" 등 상담 유도
- 같은 내용을 여러 게시판에 반복 게시(도배)
- 채용 공고가 아닌 불법/비정상 구인 홍보

## 스팸이 아닌 예시
- 직장/이직/연애/결혼/재테크 등에 대한 진솔한 고민 상담
- 정보 공유, 의견 교환, 질문/답변
- 일상적인 대화, 감정 표현

## 응답 형식
반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트는 절대 포함하지 마세요.
{"is_spam": true 또는 false, "reason": "판단 근거를 한 줄로"}

## 게시글
제목: {title}
본문: {body}
"""


def check_spam_llm(post):
    """Gemini API를 호출하여 스팸 여부를 판별합니다."""
    title = post.get("title", "")
    body = post.get("body", "") or post.get("text", "")
    prompt = SPAM_PROMPT.replace("{title}", title).replace("{body}", body)

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    )

    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 256},
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload,
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
def send_slack_alert(post, reason):
    community = post.get("community", "알 수 없음")
    title = post.get("title", "")
    body = post.get("body", "")
    author = post.get("author", "")
    views = post.get("views", "0")
    likes = post.get("likes", "0")
    comments_count = post.get("comments", "0")
    preview = body[:200] + ("..." if len(body) > 200 else "") if body else "(본문 없음)"

    message = {"blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": "🚨스팸 감지🚨"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*커뮤니티 :*  {community}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*제목 :*  {title}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*내용 :*\n{preview}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*작성자 :*  {author}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*조회 | 좋아요 | 댓글수 :*  {views} | {likes} | {comments_count}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*판별 사유 :*  {reason}"}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "🔗 바로가기"}, "url": post["url"]}
        ]},
    ]}
    payload = json.dumps(message).encode("utf-8")
    req = urllib.request.Request(SLACK_WEBHOOK_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10)
    print(f"  ✅ Slack 알림: [{community}] {post['url']}")


# ─────────────────────────────────────────────
# 이미 확인한 글 관리
# ─────────────────────────────────────────────
def _text_hash(post):
    """제목만 해시 (조회수/시간/댓글수 변화에 영향 안 받음)"""
    title = post.get("title", "")
    return hashlib.md5(title.encode("utf-8")).hexdigest()[:12]

def load_seen_posts():
    """seen_posts.json: {id: hash} 형태"""
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(pid): "" for pid in data}
        if data and isinstance(list(data.values())[0], dict):
            return {pid: entry.get("hash", "") for pid, entry in data.items()}
        return data
    return {}

def save_seen_posts(seen):
    sorted_ids = sorted(seen.keys(), key=lambda x: int(x) if x.isdigit() else 0, reverse=True)[:5000]
    trimmed = {pid: seen[pid] for pid in sorted_ids}
    with open(SEEN_FILE, "w") as f:
        json.dump(trimmed, f)


# ─────────────────────────────────────────────
# 메인 실행
# ─────────────────────────────────────────────
def main():
    print("🔍 리멤버 커뮤니티 스팸 감지 시작...")
    print(f"  📌 관심사 {len(INTEREST_COMMUNITIES)}개 + 직무 {len(JOB_COMMUNITIES)}개 = {len(ALL_COMMUNITIES)}개")
    print(f"  📌 검사: LLM(Gemini) 기반\n")

    seen = load_seen_posts()
    print(f"  📋 기존 확인한 글: {len(seen)}개\n")

    all_posts = fetch_all_posts()
    print(f"\n  📊 총 수집: {len(all_posts)}개")

    # ── 시드 모드 ──
    if not seen:
        for p in all_posts:
            seen[p["id"]] = _text_hash(p)
        save_seen_posts(seen)
        print(f"\n  🌱 시드 모드: {len(all_posts)}개 글을 확인 완료로 등록했습니다.")
        print(f"     다음 실행부터 새로 올라오는 글만 검사합니다.")
        return

    posts_to_check = []
    for p in all_posts:
        pid = p["id"]
        current_hash = _text_hash(p)

        if pid not in seen:
            p["_check_type"] = "신규"
            posts_to_check.append(p)
        elif seen[pid] != current_hash:
            p["_check_type"] = "수정됨"
            posts_to_check.append(p)

    # ── 자동 시드 안전장치 ──
    if len(posts_to_check) > 20:
        print(f"\n  🌱 자동 시드: 검사 대상이 {len(posts_to_check)}개로 비정상적으로 많습니다.")
        for p in all_posts:
            seen[p["id"]] = _text_hash(p)
        save_seen_posts(seen)
        print(f"     알림 없이 등록 완료. 다음 실행부터 새 글만 검사합니다.")
        return

    new_count = sum(1 for p in posts_to_check if p["_check_type"] == "신규")
    edit_count = sum(1 for p in posts_to_check if p["_check_type"] == "수정됨")
    print(f"  🆕 새 글: {new_count}개")
    print(f"  ✏️  수정된 글: {edit_count}개")

    if not posts_to_check:
        print("  ℹ️  검사할 글이 없습니다.")
        save_seen_posts(seen)
        return

    spam_count = 0
    for post in posts_to_check:
        community = post.get("community", "")
        check_type = post["_check_type"]
        pid = post["id"]

        try:
            result = check_spam_llm(post)
            print(f"  🔎 [{community}] {check_type}: is_spam={result['is_spam']}, reason={result.get('reason','')}")

            if result["is_spam"]:
                spam_count += 1
                try:
                    reason = f"[{check_type}] {result['reason']}" if check_type == "수정됨" else result["reason"]
                    send_slack_alert(post, reason)
                except Exception as e:
                    print(f"     ⚠️ 슬랙 오류: {e}")

            # LLM 검사 성공 → 확인 완료 처리
            seen[pid] = _text_hash(post)

        except Exception as e:
            # LLM 검사 실패 → seen에 추가하지 않음 (다음 실행에서 재시도)
            print(f"  ⚠️ [{community}] LLM 오류 (다음 실행에서 재시도): {e}")

        time.sleep(1)  # API 호출 간격

    save_seen_posts(seen)
    print(f"\n✅ 완료! 검사 {len(posts_to_check)}개 (신규 {new_count} / 수정 {edit_count}) → 스팸 {spam_count}개")


if __name__ == "__main__":
    main()
