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

        if body.startswith(title) and len(body) > len(title):
            body = body[len(title):].strip()

        return {
            "title": title,
            "body": body,
            "text": full_text, 
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
# 2단계: 키워드 기반 스팸 판별 (LLM급 포괄 탐지)
# ─────────────────────────────────────────────
def normalize_text(text: str) -> str:
    # 둥근 따옴표(상하따옴표) 및 일반 따옴표를 모두 필터링 대상에 추가
    normalized = re.sub(
        r'([가-힣])\s*[.\·\-_~,;:!?/\\|+\s\'"“”‘’]+\s*([가-힣])',
        r'\1\2',
        text
    )
    for _ in range(3):
        normalized = re.sub(
            r'([가-힣])\s*[.\·\-_~,;:!?/\\|+\s\'"“”‘’]+\s*([가-힣])',
            r'\1\2',
            normalized
        )
    return normalized


# 확장된 포괄적 키워드 (띄어쓰기 없는 버전 추가)
FORTUNE_KEYWORDS = [
    "운세", "사주", "타로", "신점", "궁합", "점술", "점괘",
    "관상", "손금", "작명", "역학", "명리", "풍수",
    "四柱", "ㅇㅅ", "ㅅㅈ",
    "올해의흐름", "타고난기운", "앞날을봐",
    "연애운", "이성운", "재물운", "이직운", "취업운", "결혼운", "시험운",
    "올해운", "내년운", "금전운",
    "운 흐름", "올해의 운", "기운", "고민거리", "방향성", "시기", 
    "운의 흐름", "운명", "생년월일", "풀이", "사주풀이", "운명풀이", "해석",
    "운의흐름", "운흐름", "올해운세", "내년운세", 
]

# 상담 유도 / 홍보 패턴 확장
SOLICITATION_PATTERNS = [
    r"\d+\s*명.*봐드",         
    r"봐\s*[드볼줄줍]",                 
    r"보\s*[ㅏ-ㅣ]\s*드",       
    r"풀어[드볼]",              
    r"풀이",                    
    r"댓글.*[주남달]",          
    r"댓\s*글",                 
    r"오픈\s*채팅",
    r"카[카톡].*링크",
    r"open\.kakao\.com",        
    r"DM\s*주",
    r"무료.*상담",
    r"상담.*무료",
    r"신청.*받",                
    r"받.*신청",                
    r"보고\s*가",               
    r"봐\s*가",                 
    r"보[고러]\s*[가와오]",      
    r"알려\s*드",               
    r"상담\s*[해드]",           
    r"연락\s*[주줘]",           
    r"해석\s*[해\s]*[드볼줄줍]", 
    r"확인\s*[해\s]*[드볼줄줍보봐]", # '봐' 추가 ('확인해봐요' 방어)
    r"분석\s*가능",             
]

_NUM = r"(\d+|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)"

# 단독 스팸 패턴
STANDALONE_SPAM_PATTERNS = [
    _NUM + r"\s*(명|분)\s*(만|정도|정두)?\s*봐\s*[드볼줄줍]",
    _NUM + r"\s*(명|분)\s*(만|정도|정두)?\s*보\s*[ㅏ-ㅣ]\s*드", 
    r"(몇|몇몇)\s*(명|분)(들)?\s*(만)?\s*봐\s*[드볼줄줍]",         
    r"(몇|몇몇)\s*(명|분)(들)?\s*(만)?\s*보\s*[ㅏ-ㅣ]\s*드",         
    _NUM + r"\s*(명|분)\s*(만|정도|정두)?\s*봐[볼줄]",
    _NUM + r"\s*(명|분)\s*(만|정도|정두)?\s*보[고러]",
    r"(몇|몇몇)\s*(명|분)(들)?\s*(만)?\s*봐[볼줄]",
    r"(몇|몇몇)\s*(명|분)(들)?\s*(만)?\s*보[고러]",
    r"(가볍게|간단히|간단하게).*봐\s*[드볼줄줍]",
    r"(가볍게|간단히|간단하게).*보\s*[ㅏ-ㅣ]\s*드",              
    r"(가볍게|간단히|간단하게).*봐[볼줄]",
    r"(가볍게|간단히|간단하게).*보[고러]",
    r"신청.*받.*" + _NUM + r"\s*(명|분)",
    _NUM + r"\s*(명|분).*신청",
    r"신청.*받.*(몇|몇몇)\s*(명|분)",
    r"\d+\s*명\s*(만|만요)?\s*$",
    r"도움.*필요.*봐",
    
    r"\d+\s*[-~,/]\s*\d+\s*(명|분)", 
    
    r"(잘\s*맞나요|잘\s*맞더라고요|잘\s*맞네).*(어디|추천|알려)",
    r"(운세|사주|타로|점).*잘\s*보는\s*곳",
    
    r"취미로.*(제작|만들|봐|분석|풀이|해석|운)", 
    r"(해석|풀이|분석|상담|고민).*(open\.kakao\.com|카카오톡|오픈채팅|오픈카톡)"
]

# 스패머가 악용하는 일반대화 제외 패턴 비활성화 유지
EXCLUDE_PATTERNS = [
    r"사주.*받았",              
    r"타로.*갔",                
]


def check_spam_keyword(text: str) -> dict:
    normalized = normalize_text(text)
    combined = text + " " + normalized

    for pattern in EXCLUDE_PATTERNS:
        if re.search(pattern, combined):
            return {"is_spam": False, "reason": "일반 대화로 판단"}

    for pattern in STANDALONE_SPAM_PATTERNS:
        if re.search(pattern, combined):
            return {
                "is_spam": True,
                "reason": "단독 스팸 패턴 감지 (취미 가장형, 인원수 제한 등)",
            }

    found_keywords = []
    for keyword in FORTUNE_KEYWORDS:
        if keyword in combined:
            found_keywords.append(keyword)

    found_solicitations = []
    for pattern in SOLICITATION_PATTERNS:
        if re.search(pattern, combined):
            found_solicitations.append(pattern)

    if found_keywords and found_solicitations:
        keyword_str = ", ".join(found_keywords[:3])
        return {
            "is_spam": True,
            "reason": f"키워드 감지: [{keyword_str}] + 상담 유도(또는 링크)",
        }

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
    print(f"  📌 검사 방식: 확장형 정규식 (LLM급 포괄 매칭)\n")

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
                print(f"    ⚠️ 슬랙 전송 오류: {e}")

        seen.add(post["id"])

    save_seen_posts(seen)
    print(f"\n✅ 완료! 새 글 {len(new_posts)}개 중 스팸 {spam_count}개 감지")


if __name__ == "__main__":
    main()
