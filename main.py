"""
리멤버 커뮤니티 스팸 감지기
- 관심사 11개 + 직무 40개 커뮤니티의 최신글을 확인하고,
  키워드 기반으로 사주/운세 스팸을 판별한 뒤, Slack으로 알림을 보냅니다.
- 새 글의 댓글까지 검사합니다.
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

# 관심사 커뮤니티 (11개)
INTEREST_COMMUNITIES = {
    61: "회사생활", 80: "이슈토론", 82: "재테크", 89: "자유주제",
    91: "술 이야기", 92: "취미생활", 93: "이직/커리어",
    109: "서류/면접 팁", 110: "연애", 111: "결혼생활", 116: "자랑거리",
}

# 직무 커뮤니티 (40개)
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


# ─────────────────────────────────────────────
# 1-2단계: 개별 글 페이지에서 댓글 가져오기
# ─────────────────────────────────────────────
class CommentParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.comments = []
        self._in_comment_area = False
        self._current_text = []
        self._capture = False

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class", "").lower()
        if any(k in cls for k in ["comment", "reply"]):
            self._in_comment_area = True
        if self._in_comment_area and tag in ("p", "span", "div"):
            if any(k in cls for k in ["content", "text", "body", "message"]):
                self._capture = True
                self._current_text = []

    def handle_endtag(self, tag):
        if self._capture and tag in ("p", "span", "div"):
            text = " ".join(self._current_text).strip()
            if text and len(text) > 3:
                self.comments.append(text)
            self._capture = False

    def handle_data(self, data):
        if self._capture:
            stripped = data.strip()
            if stripped:
                self._current_text.append(stripped)


def fetch_post_comments(post_url):
    req = urllib.request.Request(post_url, headers={"User-Agent": "SpamDetector/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8")
    except Exception as e:
        print(f"      ⚠️ 댓글 로드 실패: {e}")
        return []

    parser = CommentParser()
    parser.feed(html)
    return parser.comments  # 구조적 추출 실패 시 빈 리스트 반환 (오탐 방지)


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
# 2단계: 키워드 기반 스팸 판별
# ─────────────────────────────────────────────
def normalize_text(text):
    pat = r'([가-힣])\s*[.\u00b7\-_~,;:!?/\\|+\s\'"\u201c\u201d\u2018\u2019]+\s*([가-힣])'
    normalized = re.sub(pat, r'\1\2', text)
    for _ in range(3):
        normalized = re.sub(pat, r'\1\2', normalized)
    return normalized


FORTUNE_KEYWORDS = [
    # 핵심 운세/점술 키워드
    "운세", "사주", "타로", "신점", "궁합", "점술", "점괘",
    "관상", "손금", "작명", "역학", "명리", "풍수",
    "四柱", "ㅇㅅ", "ㅅㅈ",
    # 운세 특유 복합 표현
    "올해의흐름", "타고난기운",
    "연애운", "이성운", "재물운", "이직운", "취업운", "결혼운", "시험운",
    "올해운", "내년운", "금전운",
    "운의흐름", "운흐름", "올해운세", "내년운세",
    "올해의 운", "운의 흐름", "앞날의 운", "앞으로의 운",
    "사주풀이", "운명풀이", "운세풀이",
    # 스패머 특유 은어/오타
    "운대", "운때", "윤세", "운새",
    "누가 들어올지", "언제 들어올지", "인연은 있는지",
    "운이 들어와", "운이 들어올지",
    # 손금/재물 (운세 맥락에서 구체적)
    "생명선", "재물선", "애정선", "두뇌선",
    "재물", "생년월일",
]

SOLICITATION_PATTERNS = [
    r"\d+\s*명.{0,10}봐드",
    r"봐\s*[드볼줄줍랴]",
    r"보\s*[ㅏ-ㅣ]\s*드",
    r"풀어\s*[드볼줄줍보봐]",
    r"확인\s*드",               # "확인드릴게요" (단, "확인해보다"는 안 걸림)
    r"해석\s*드",               # "해석드려요" (단, "해석하다"는 안 걸림)
    r"댓글.{0,15}[주남달]",
    r"댓\s*글",
    r"오픈\s*채팅",
    r"카[카톡].{0,10}링크",
    r"open\.kakao\.com",
    r"chatgpt\.site",
    r"DM\s*주",
    r"무료.{0,10}상담",
    r"상담.{0,10}무료",
    r"신청.{0,10}받",
    r"받.{0,10}신청",
    r"보고\s*가",
    r"봐\s*가",
    r"보[고러]\s*[가와오]",
    r"알려\s*드",
    r"연락\s*[주줘]",
]

_NUM = r"(\d+|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|한두|두세|세네|네다섯|대여섯|서너|서넛)"

# 스팸 특유의 동사구
_SPAM_VERB = r"(봐\s*[드볼줄줍랴]|보\s*[ㅏ-ㅣ]\s*드|풀어\s*[드볼줄줍보봐]|해석\s*[해드]|확인\s*[해드]|파악\s*[해드]|풀이\s*[해드]|상담\s*[해드])"

STANDALONE_SPAM_PATTERNS = [
    _NUM + r"\s*(명|분)\s*(만|정도|정두)?\s*" + _SPAM_VERB,
    _NUM + r"\s*(명|분)\s*(만|정도|정두)?\s*보[고러]",
    r"(몇|몇몇)\s*(명|분)(들)?\s*(만)?\s*" + _SPAM_VERB,
    r"(몇|몇몇)\s*(명|분)(들)?\s*(만)?\s*보[고러]",
    r"(가볍게|간단히|간단하게).{0,15}(봐\s*[드볼줄줍랴]|보\s*[ㅏ-ㅣ]\s*드|풀어\s*[드볼])",
    r"신청.{0,15}받.{0,10}" + _NUM + r"\s*(명|분)",
    _NUM + r"\s*(명|분).{0,15}신청",
    r"신청.{0,15}받.{0,10}(몇|몇몇)\s*(명|분)",
    r"\d+\s*명\s*(만|만요)?\s*$",
    r"도움.{0,15}필요.{0,15}" + _SPAM_VERB,
    r"\d+\s*[-~,/]\s*\d+\s*(명|분).{0,10}" + _SPAM_VERB,
    r"(잘\s*맞나요|잘\s*맞더라고요|잘\s*맞네).{0,20}(어디|추천|알려)",
    r"(운세|사주|타로|점).{0,15}잘\s*보는\s*곳",
    r"취미로.{0,30}(제작|만들|봐|분석|풀이|해석|운|사주|타로|손금|관상|점|윤세|운새|보는)",
    r"(maeumgyeol|magickimm|chatgpt\.site)",
    r"(연애|결혼|이직|회사|직장|승진|풀리지\s*않는|안\s*풀리는|답답한).{0,60}(운대|운때|나만의\s*때|누가\s*들어올지|언제\s*들어올지|인연은\s*있는지|운.{0,3}흐름|운이\s*들어|앞날을\s*[보봐파]|윤세|운새|앞으로.{0,5}흐름|앞날.{0,5}운)",
    r"(연애|결혼|이직|회사|직장|승진|풀리지\s*않는|안\s*풀리는|답답한).{0,60}흐름.{0,20}" + _SPAM_VERB,
    r"(각자만의|나만의|본인만의|각자의|각자가|각자마다의).{0,30}(운대|운때|흐름|때가|운이).{0,20}(있|궁금|없)",
    # 카카오 오픈채팅 링크 포함 글 (낮은 임계치 적용)
    r"open\.kakao\.com.{0,200}(운|사주|타로|손금|관상|점|재물|궁합|윤세|운새|봐드|풀어|해석|선착)",
    r"(운|사주|타로|손금|관상|점|재물|궁합|윤세|운새|봐드|풀어|해석|선착).{0,200}open\.kakao\.com",
    # "운을/운이" + 동사 패턴 (스패머가 "운세" 대신 "운" 단독 사용)
    r"앞날.{0,10}운.{0,10}(해석|확인|봐|풀어|보[고러])",
    r"앞으로.{0,10}운.{0,10}(해석|확인|봐|풀어|보[고러])",
    r"올해.{0,10}운.{0,10}(해석|확인|봐|풀어|보[고러])",
    r"운.{0,5}(볼줄|봐줄|봐드|확인드|해석드)",
]

EXCLUDE_PATTERNS = [
    r"사주.*받았",
    r"타로.*갔",
]


def check_spam_keyword(text):
    normalized = normalize_text(text)
    combined = text + " " + normalized
    for pattern in EXCLUDE_PATTERNS:
        if re.search(pattern, combined):
            return {"is_spam": False, "reason": "일반 대화로 판단"}
    for pattern in STANDALONE_SPAM_PATTERNS:
        if re.search(pattern, combined):
            return {"is_spam": True, "reason": "단독 스팸 패턴 감지"}
    found_kw = [k for k in FORTUNE_KEYWORDS if k in combined]
    found_sol = [p for p in SOLICITATION_PATTERNS if re.search(p, combined)]
    if found_kw and found_sol:
        return {"is_spam": True, "reason": f"키워드 감지: [{', '.join(found_kw[:3])}] + 상담 유도"}
    if len(found_kw) >= 2:
        return {"is_spam": True, "reason": f"복수 키워드 감지: [{', '.join(found_kw[:3])}]"}
    return {"is_spam": False, "reason": "스팸 패턴 미감지"}


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
        {"type": "header", "text": {"type": "plain_text", "text": "🚨사주 빌런 출몰🚨"}},
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
    print(f"  ✅ Slack 알림: [{community}] [{spam_source}] {post['url']}")


# ─────────────────────────────────────────────
# 이미 확인한 글 관리
# ─────────────────────────────────────────────
import hashlib

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
    print(f"  📌 검사: 글 본문 (수정 감지 포함)\n")

    seen = load_seen_posts()
    print(f"  📋 기존 확인한 글: {len(seen)}개\n")

    all_posts = fetch_all_posts()
    print(f"\n  📊 총 수집: {len(all_posts)}개")

    # ── 시드 모드: seen이 비어있으면 현재 글을 전부 등록만 하고 종료 ──
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

    # ── 안전장치: 한꺼번에 20개 이상이면 비정상 (커뮤니티 추가/코드 변경 등) → 자동 시드 ──
    if len(posts_to_check) > 20:
        print(f"\n  🌱 자동 시드: 검사 대상이 {len(posts_to_check)}개로 비정상적으로 많습니다.")
        print(f"     (커뮤니티 추가 또는 코드 변경 감지)")
        print(f"     알림 없이 전부 확인 완료로 등록합니다.")
        for p in all_posts:
            seen[p["id"]] = _text_hash(p)
        save_seen_posts(seen)
        print(f"     다음 실행부터 새로 올라오는 글만 검사합니다.")
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

        result = check_spam_keyword(post["text"])
        if result["is_spam"]:
            spam_count += 1
            label = "수정→스팸" if check_type == "수정됨" else "글 스팸"
            print(f"\n  🚨 [{community}] {label}: {post['text'][:50]}...")
            try:
                reason = f"[{check_type}] {result['reason']}" if check_type == "수정됨" else result["reason"]
                send_slack_alert(post, reason)
            except Exception as e:
                print(f"     ⚠️ 슬랙 오류: {e}")

        seen[pid] = _text_hash(post)

    save_seen_posts(seen)
    print(f"\n✅ 완료! 검사 {len(posts_to_check)}개 (신규 {new_count} / 수정 {edit_count}) → 스팸 {spam_count}개")


if __name__ == "__main__":
    main()
