"""
스팸 감지기 사전 테스트 - 관심사 커뮤니티 11개 전체 수집
"""

import re
import json
import time
import urllib.request
from html.parser import HTMLParser

BASE_URL = "https://community.rememberapp.co.kr"

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
                    self.posts.append({
                        "id": match.group(1),
                        "text": text,
                    })
            self._in_post_link = False

    def handle_data(self, data):
        if "최신글" in data or "새글피드" in data:
            self._in_feed = True
        if self._in_post_link:
            self._current_text.append(data.strip())


def test_crawling():
    print("=" * 60)
    print("테스트 1: 관심사 커뮤니티 11개 전체 수집")
    print("=" * 60)

    try:
        all_posts = []
        seen_ids = set()
        community_counts = {}

        for cid, cname in INTEREST_COMMUNITIES.items():
            url = f"{BASE_URL}/community/{cid}"
            req = urllib.request.Request(url, headers={"User-Agent": "SpamDetector/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8")

            parser = PostParser()
            parser.feed(html)

            count = 0
            for p in parser.posts:
                if p["id"] not in seen_ids:
                    seen_ids.add(p["id"])
                    p["community"] = cname
                    all_posts.append(p)
                    count += 1

            community_counts[cname] = count
            print(f"  📥 [{cname}] {count}개")
            time.sleep(0.3)

        print(f"\n  📊 총 수집: {len(all_posts)}개 (중복 제거 후)")

        if not all_posts:
            print("  ⚠️ 수집된 글이 없습니다.")
            return False

        id_min = min(int(p["id"]) for p in all_posts)
        id_max = max(int(p["id"]) for p in all_posts)
        print(f"  📊 Post ID 범위: {id_min} ~ {id_max}")

        # ID 내림차순 정렬
        sorted_posts = sorted(all_posts, key=lambda p: int(p["id"]), reverse=True)

        print(f"\n{'='*60}")
        print(f"  전체 글 목록 (ID 내림차순)")
        print(f"{'='*60}")
        for i, post in enumerate(sorted_posts, 1):
            preview = post["text"][:55] + ("..." if len(post["text"]) > 55 else "")
            print(f"  {i:3d}. [{post['community']:<7s}] [ID:{post['id']}] {preview}")

        return True

    except Exception as e:
        print(f"❌ 실패: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_gemini_api():
    print(f"\n{'='*60}")
    print("테스트 2: Gemini API 키 확인")
    print(f"{'='*60}")

    api_key = input("\nGemini API 키 (건너뛰려면 Enter): ").strip()
    if not api_key:
        print("⏭️  건너뜀")
        return None

    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={api_key}"
        )

        test_post = "간단히 열 명 정도 봐드릴게요. 연애나 결혼, 이런저런 이직 등등 고민이 있다면 올해의 운. 세를 풀어보세요. 댓글주시면 답 드릴게여!"

        prompt = f"""아래 게시글이 사주/운세 관련 스팸인지 판별해주세요.
JSON으로만 응답하세요: {{"is_spam": true/false, "reason": "판단 근거"}}

게시글: {test_post}"""

        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 256},
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")

        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        text = data["candidates"][0]["content"]["parts"][0]["text"]
        text = re.sub(r"```json\s*|```", "", text).strip()
        result = json.loads(text)

        print(f"\n✅ API 정상 작동!")
        print(f'  테스트: "{test_post[:50]}..."')
        print(f"  결과: {'🚨 스팸' if result['is_spam'] else '✅ 정상'}")
        print(f"  사유: {result['reason']}")
        return True

    except Exception as e:
        print(f"❌ 실패: {e}")
        return False


if __name__ == "__main__":
    print("🔧 스팸 감지기 사전 테스트\n")
    crawl_ok = test_crawling()
    api_ok = test_gemini_api()

    print(f"\n{'='*60}")
    print("최종 결과")
    print(f"{'='*60}")
    print(f"  커뮤니티 크롤링: {'✅ 통과' if crawl_ok else '❌ 실패'}")
    if api_ok is None:
        print(f"  Gemini API:     ⏭️  건너뜀")
    else:
        print(f"  Gemini API:     {'✅ 통과' if api_ok else '❌ 실패'}")
