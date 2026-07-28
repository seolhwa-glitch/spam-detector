# 🚨 리멤버 커뮤니티 스팸 감지기

리멤버 커뮤니티에 올라오는 **사주/운세 관련 스팸 글**을 자동으로 감지하여 Slack으로 알림을 보냅니다.

## 작동 방식

```
10분마다 자동 실행 (GitHub Actions)
    ↓
커뮤니티 최신 글 확인
    ↓
Gemini API로 스팸 여부 판별
    ↓
스팸이면 Slack 알림 전송
```

## 초기 설정 방법

### 1. 이 저장소를 GitHub에 Push

```bash
git init
git remote add origin https://github.com/[회사계정]/spam-detector.git
git add .
git commit -m "init: 스팸 감지기"
git push -u origin main
```

### 2. GitHub Secrets 등록

GitHub 저장소 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret 이름 | 값 |
|---|---|
| `GEMINI_API_KEY` | Gemini API 키 (`AIza...`) |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook URL |

### 3. GitHub Actions 활성화

저장소의 **Actions** 탭에 가면 "스팸 감지" 워크플로우가 보입니다.
- 10분마다 자동 실행됩니다.
- **"Run workflow"** 버튼으로 수동 테스트도 가능합니다.

## 파일 구조

```
├── .github/workflows/check-spam.yml   ← 자동 실행 설정
├── main.py                            ← 메인 로직
├── requirements.txt                   ← 의존성 (없음)
├── seen_posts.json                    ← 확인한 글 목록 (자동 생성)
└── README.md
```

## 커스터마이징

- **실행 주기 변경**: `check-spam.yml`의 `cron: '*/10 * * * *'` 수정
  - `*/5 * * * *` → 5분마다
  - `*/30 * * * *` → 30분마다
- **스팸 판별 기준 변경**: `main.py`의 `SPAM_DETECTION_PROMPT` 수정
- **알림 메시지 변경**: `main.py`의 `send_slack_alert()` 함수 수정
