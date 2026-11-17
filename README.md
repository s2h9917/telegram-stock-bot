# 📈 텔레그램 증시 자동 채널봇 (무료)

| 기능 | 파일 | 실행 시각(평일, KST) |
|---|---|---|
| ① 오늘자 종목 리포트 요약 (텍스트/이미지) | `bot/reports.py` | 07:20, 08:40 |
| ② 기준 유튜브 채널 새 영상·라이브 링크 공유 | `bot/youtube_watch.py` | 10분마다 |
| ③ 장전·장중·장마감 증시 브리핑 | `bot/market_brief.py` | 07:50, 10:50, 15:50 |
| 관리자 화면 | `admin/app.py` (Streamlit) | 상시 |

- 자동 실행: GitHub Actions (공개 저장소는 무료·무제한)
- AI 요약: Google Gemini API 무료 등급 (키가 없어도 요약 없이 동작)
- 시간은 `.github/workflows/*.yml` 의 cron(UTC)에서 바꿉니다. GitHub 예약 실행은 5~20분 늦을 수 있어 실제 목표보다 조금 일찍 잡아 두었습니다.

## GitHub Secrets (Settings → Secrets and variables → Actions)
| 이름 | 필수 | 설명 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | BotFather에서 받은 토큰 |
| `TELEGRAM_CHAT_ID` | | 비우면 config.json의 chat_id 사용 |
| `GEMINI_API_KEY` | 권장 | aistudio.google.com 무료 키 |
| `YOUTUBE_API_KEY` | 선택 | 라이브 시작/종료 구분용 |

## 내 PC에서 테스트
```bash
pip install -r requirements.txt
DRY_RUN=1 python -m bot.reports --force          # 텔레그램에 보내지 않고 화면 출력
DRY_RUN=1 python -m bot.market_brief --slot pre --force
python -m bot.youtube_watch
streamlit run admin/app.py                        # 관리자 화면 (admin/.streamlit/secrets.toml 필요)
```
Windows PowerShell에서는 `DRY_RUN=1` 대신 먼저 `$env:DRY_RUN="1"` 을 입력하세요.

자세한 단계별 설명은 함께 받은 가이드 문서를 참고하세요.
