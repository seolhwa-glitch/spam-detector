"""
리멤버 커뮤니티 스팸 감지기
- 관심사 커뮤니티 11개의 최신글을 확인하고,
  키워드 기반으로 사주/운세 스팸을 판별한 뒤, Slack으로 알림을 보냅니다.
- LLM API 불필요 (무료, 무제한)
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
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SEEN_FILE = "seen_posts.json"

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
        """텍스트 조각들을 제목/본문/메타데이터로 분리합니다."""
        title = parts[0] if parts else full_text
        author = ""
        time_str = ""
        views = ""
        likes = ""
        comments = ""
        body = full_text

        # 끝에서부터 메타데이터 추출 시도
        # 패턴: ... 닉네임 시간표현 조회수 좋아요 댓글수
        # "조회수", "좋아요", "댓글" 라벨 텍스트를 제거하면서 숫자만 추출
        meta_re = re.compile(
            r'^(.*?)\s+'
            r'(\S+)\s+'
            r'(방금|\d+분\s*전|\d+시간\s*전|\d+일\s*전|\d{1,2}월\s*\d{1,2}일)'
            r'\s*(?:조회수)*\s*(\d[\d,]*)'
            r'\s*(?:좋아요)*\s*(\d[\d,]*)'
            r'\s*(?:댓글)*\s*(\d[\d,]*)\s*$',
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

        # 본문에서 제목 중복 제거
        if body.startswith(title) and len(body) > len(title):
            body = body[len(title):].strip()

        return {
            "title": title,
            "body": body,
            "text": full_text,    # 전체 텍스트 (스팸 판별용)
            "author": author,
            "time": time_str,
            "views": views,
            "likes": likes,
            "comments": comments,
        }


def fetch_community_posts(community_id: int, community_name: str) -> list[dict]:
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


def fetch_all_posts() -> list[dict]:
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
        time.sleep(0.5)
    return all_posts


# ─────────────────────────────────────────────
# 2단계: 키워드 기반 스팸 판별
# ─────────────────────────────────────────────
def normalize_text(text: str) -> str:
    """스패머의 글자 분리 트릭을 무력화합니다.
    "운. 세" → "운세", "사 . 주" → "사주" 등"""
    # 한글 글자 사이의 특수문자/공백 제거
    # 예: 운. 세 → 운세, 사 주 → 사주, 타·로 → 타로
    normalized = re.sub(
        r'([가-힣])\s*[.\·\-_~,;:!?\s]+\s*([가-힣])',
        r'\1\2',
        text
    )
    # 여러 번 반복 (3글자 이상 분리된 경우 대비: 운. 세. 보. 기)
    for _ in range(3):
        normalized = re.sub(
            r'([가-힣])\s*[.\·\-_~,;:!?\s]+\s*([가-힣])',
            r'\1\2',
            normalized
        )
    return normalized


# 사주/운세/점술 관련 키워드
FORTUNE_KEYWORDS = [
    "운세", "사주", "타로", "신점", "궁합", "점술", "점괘",
    "관상", "손금", "작명", "역학", "명리", "풍수",
    "四柱", "ㅇㅅ", "ㅅㅈ",
    "올해의흐름", "타고난기운", "앞날을봐",
    "연애운", "이성운", "재물운", "이직운", "취업운", "결혼운", "시험운",
    "올해운", "내년운", "금전운",
]

# 상담 유도 / 홍보 패턴
SOLICITATION_PATTERNS = [
    r"\d+\s*명.*봐드",          # "5명 봐드릴게요", "열 명 봐드립니다"
    r"봐드릴[게께겠]",           # "봐드릴게요", "봐드릴께요"
    r"봐드립니다",
    r"봐드려요",
    r"풀어[드볼]",              # "풀어드릴게요", "풀어볼게요"
    r"댓글.*[주남달]",          # "댓글 주시면", "댓글 남겨주세요", "댓글 달아주세요"
    r"오픈\s*채팅",
    r"카[카톡].*링크",
    r"DM\s*주",
    r"무료.*상담",
    r"상담.*무료",
]

# 이 패턴만으로도 스팸 확정 (운세 키워드 없어도)
STANDALONE_SPAM_PATTERNS = [
    r"\d+\s*명\s*(만|정도|정두)?\s*봐드",     # "5명 봐드릴게요", "5명만 봐드려요", "5명 정두 봐드려요"
    r"(몇|몇몇)\s*(명|분)\s*(만)?\s*봐드",    # "몇 분만 봐드릴게여", "몇명 봐드려요"
    r"(가볍게|간단히|간단하게).*봐드",          # "가볍게 봐드릴게요", "간단히 봐드려요"
    r"도움.*필요.*봐드",                       # "도움 필요하신분? 봐드릴게여"
]

# 일반 대화 제외 패턴 (이게 있으면 스팸이 아닌 것으로 판단)
EXCLUDE_PATTERNS = [
    r"운세.*봤[는더]",          # "운세 봤는데", "운세 봤더니"
    r"운세.*믿",               # "운세 믿으세요?"
    r"사주.*받았",             # "사주 받았는데"
    r"타로.*갔",               # "타로 갔다왔는데"
]


def check_spam_keyword(text: str) -> dict:
    """키워드 기반으로 사주/운세 스팸 여부를 판별합니다."""
    normalized = normalize_text(text)
    combined = text + " " + normalized  # 원본 + 정규화 텍스트 모두 검사

    # 제외 패턴 먼저 체크 (일반 대화)
    for pattern in EXCLUDE_PATTERNS:
        if re.search(pattern, combined):
            return {"is_spam": False, "reason": "일반 대화로 판단"}

    # 단독 스팸 패턴 체크 ("N명 봐드릴게요" 류 — 운세 키워드 없어도 스팸)
    for pattern in STANDALONE_SPAM_PATTERNS:
        if re.search(pattern, combined):
            return {
                "is_spam": True,
                "reason": "상담 유도 패턴 감지 (N명 봐드릴게요 류)",
            }

    # 사주/운세 키워드 매칭
    found_keywords = []
    for keyword in FORTUNE_KEYWORDS:
        if keyword in combined:
            found_keywords.append(keyword)

    # 상담 유도 패턴 매칭
    found_solicitations = []
    for pattern in SOLICITATION_PATTERNS:
        if re.search(pattern, combined):
            found_solicitations.append(pattern)

    # 판별: 운세 키워드 + 유도 패턴 둘 다 있으면 스팸
    if found_keywords and found_solicitations:
        keyword_str = ", ".join(found_keywords[:3])
        return {
            "is_spam": True,
            "reason": f"키워드 감지: [{keyword_str}] + 상담 유도 패턴",
        }

    # 운세 키워드만 2개 이상이면 의심 스팸
    if len(found_keywords) >= 2:
        keyword_str = ", ".join(found_keywords[:3])
        return {
            "is_spam": True,
            "reason": f"복수 키워드 감지: [{keyword_str}]",
        }

    return {"is_spam": False, "reason": "스팸 패턴 미감지"}


# ─────────────────────────────────────────────
# 3단계: Slack 알림 보내기
# ─────────────────────────────────────────────
def send_slack_alert(post: dict, reason: str):
    community = post.get("community", "알 수 없음")
    title = post.get("title", "")
    body = post.get("body", "")
    author = post.get("author", "")
    views = post.get("views", "0")
    likes = post.get("likes", "0")
    comments = post.get("comments", "0")

    preview = body[:200] + ("..." if len(body) > 200 else "") if body else "(본문 없음)"

    message = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🚨사주 빌런 출몰🚨"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*커뮤니티 :*  {community}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*제목 :*  {title}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*내용 :*\n{preview}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*작성자 :*  {author}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*조회 | 좋아요 | 댓글수 :*  {views} | {likes} | {comments}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*판별 사유 :*  {reason}"},
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🔗 바로가기"},
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
    print(f"  📌 검사 대상: 관심사 커뮤니티 {len(INTEREST_COMMUNITIES)}개")
    print(f"  📌 검사 방식: 키워드 기반 (API 불필요)\n")

    seen = load_seen_posts()
    print(f"  📋 기존에 확인한 글: {len(seen)}개\n")

    all_posts = fetch_all_posts()
    print(f"\n  📊 총 수집: {len(all_posts)}개")

    new_posts = [p for p in all_posts if p["id"] not in seen]
    print(f"  🆕 새 글: {len(new_posts)}개")

    if not new_posts:
        print("  ℹ️  새 글이 없습니다. 종료합니다.")
        save_seen_posts(seen)
        return

    spam_count = 0
    for post in new_posts:
        community = post.get("community", "")
        result = check_spam_keyword(post["text"])

        if result["is_spam"]:
            spam_count += 1
            print(f"\n  🚨 [{community}] 스팸 감지: {post['text'][:50]}...")
            print(f"     사유: {result['reason']}")
            try:
                send_slack_alert(post, result["reason"])
            except Exception as e:
                print(f"     ⚠️ 슬랙 전송 오류: {e}")

        # 키워드 방식은 실패할 일이 없으므로 바로 확인 완료 처리
        seen.add(post["id"])

    save_seen_posts(seen)
    print(f"\n✅ 완료! 새 글 {len(new_posts)}개 중 스팸 {spam_count}개 감지")


if __name__ == "__main__":
    main()
