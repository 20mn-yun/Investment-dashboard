# 맥미니 새로 세팅할 때 — git 에 없는 것 목록

이 문서는 **이름과 위치만** 적는다. 값(키·토큰·비밀번호)은 절대 적지 않는다(공개 저장소).
값은 기존 맥미니나 개인 보관처에서 옮긴다.

## 1. 저장소와 파이썬

- 저장소: `~/investment_dashboard` (git clone)
- 파이썬: `/Library/Frameworks/Python.framework/Versions/3.14/bin/python3` (launchd plist 들이 이 경로를 씀)
- 패키지: `python3 -m pip install -r requirements.txt`

## 2. `.env` (저장소 루트, git 제외)

키 이름만:

- `ANTHROPIC_API_KEY`
- `DART_API_KEY`
- `GEMINI_API_KEY`
- `TELEGRAM_TOKEN_GENERAL`, `TELEGRAM_TOKEN_EARNINGS`
- `TG_API_ID`, `TG_API_HASH`
- `KIS_APP_KEY`, `KIS_APP_SECRET`
- `DATA_GO_KR_API_KEY`
- `EIA_API_KEY`, `ECOS_API_KEY`, `FRED_API_KEY`, `ALPHAVANTAGE_API_KEY`
- `MCP_GOOGLE_CLIENT_ID`, `MCP_GOOGLE_CLIENT_SECRET`, `MCP_ALLOWED_EMAIL`

## 3. git 제외 설정·데이터 파일 (저장소 루트 기준)

옮겨야 하는 것(없으면 기능이 비거나 처음부터 다시 쌓임):

| 위치 | 내용 |
|---|---|
| `sessions/` | 텔레그램(Telethon) 로그인 세션 파일. 없으면 `setup_tg_session.py` 로 다시 로그인 |
| `report_index.db` | 텔레그램 리포트 색인 (없으면 전체 재색인) |
| `report_config.json` | 리포트 채널·관심 종목·전달 채널·AI 필터 설정 |
| `watchlist.json` | 통합 워치리스트 |
| `dart_monitor_config.json` | DART 모니터 설정, 텔레그램 chat id, 시총 기준 |
| `dart_seen.json`, `dart_sent.json`, `dart_daily_history.json` | DART 수집·발송 이력 |
| `tg_inbox_config.json`, `tg_inbox_corrections.json` | 텔레 인박스 채널·분류 보정 |
| `calendar_watchlist.json` | 캘린더 실적발표 종목 |
| `disparity_config.json` | 이격도 설정 |
| `indicators_manual.json` | 주요 지표 수동 입력값 |
| `stock_notes/` | 종목별 "나의 판단" 원본 |
| `portfolio_config.json`, `portfolio-sync-key.json` | 포트폴리오 동기화 설정·서비스 계정 키 (개발 중단 모듈) |

다시 만들어지는 것(옮기지 않아도 됨): `cache/` 대부분, `logs/`, `dart_corp_codes.json`·`dart_corp_map.json`·`dart_industry_*` 캐시, `tickers/*.json`.

## 4. 로그인이 필요한 도구

| 도구 | 실행 파일 | 로그인 정보 위치 |
|---|---|---|
| Claude Code CLI | `~/.local/bin/claude` | `~/.claude/.credentials.json` (`claude` 실행 후 로그인) |
| Codex CLI | `~/.local/bin/codex` | `~/.codex/auth.json` (`codex login`) |
| cloudflared 터널 | `~/.local/bin/cloudflared` | 토큰은 `~/Library/LaunchAgents/com.cloudflare.cloudflared.plist` 의 `--token` 인자 (Cloudflare 대시보드에서 다시 발급 가능) |
| Google Drive for desktop | 앱 | Drive 계정 로그인. `내 드라이브/Analysis` 등 폴더를 "오프라인 사용 가능"으로 |

launchd 에서 Drive(`~/Library/CloudStorage`)를 쓰려면 시스템 설정 > 개인정보 보호 > 전체 디스크 접근에 python3 를 추가해야 할 수 있다.

## 5. launchd 등록

- 저장소 `launchd/*.plist` 를 `~/Library/LaunchAgents/` 로 복사 (`launchd/disabled/` 는 폐기한 작업이라 제외)
- cloudflared plist 는 저장소에 없음(토큰 포함) — 위 4번 참고
- 등록: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<이름>.plist`
- plist 를 고친 뒤 반영: `launchctl bootout gui/$(id -u)/<라벨>` 후 다시 `bootstrap` (`kickstart` 로는 plist 변경이 반영되지 않음)
- 확인: `launchctl list | grep com.yun`

## 6. 전원·재부팅

- 주간 자동 재부팅: `sudo pmset repeat restart U 04:20:00` (확인 `pmset -g sched`) — macOS 26 가동 49.7일 TCP 포트 고갈 대비
- 정전 후 자동 켜짐: `sudo pmset -a autorestart 1`
- 잠자기 끔: `sudo pmset -a sleep 0` (확인 `pmset -g custom`)

## 7. 파일 디스크립터 한도

- 대시보드 plist(`launchd/com.yun.dashboard.plist`)에 `SoftResourceLimits`·`HardResourceLimits` 의 `NumberOfFiles` = 8192 가 들어 있다. 복사만 하면 적용됨
- 시스템 기본값 확인: `launchctl limit maxfiles`
