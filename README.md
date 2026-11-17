# 개인정보 입법동향 모니터링

국회 의원발의 법률안 중 개인정보 관련 법안을 매일 자동으로 골라 공개 웹페이지로 보여줍니다.

- **방문자**: 주소만 알면 누구나 같은 화면을 봅니다. 읽기만 가능합니다.
- **호스트(저장소 주인)**: 분류 기준, 수동 포함/제외, 화면·기능을 고칠 수 있습니다.

```
GitHub Actions (매일 06:17) ─ 국회 API 수집 → 제안이유 수집 → Claude 분류 → data/bills.json 저장
GitHub Pages ─ index.html이 data/bills.json을 읽어 표시
```

API 키는 GitHub Secrets에만 저장되고 웹페이지에는 노출되지 않습니다.

---

## 처음 설정하기 (한 번만)

### 1. 키 준비
- **국회 API 키**: 열린국회정보(open.assembly.go.kr) 가입 → 마이페이지 → 인증키 발급
- **Anthropic API 키**: console.anthropic.com → API Keys (크레딧 충전 필요)

### 2. 저장소 만들기
1. github.com 가입 후 **New repository** → 이름 예: `bill-monitor`, **Public** 선택
2. **uploading an existing file**을 눌러 이 폴더의 파일을 폴더 구조 그대로 올립니다.
   (`.github` 폴더는 숨김 폴더라 끌어다 놓을 때 빠지기 쉬우니 꼭 확인하세요.)

### 3. 키 등록
저장소 **Settings → Secrets and variables → Actions → New repository secret**
- `ASSEMBLY_API_KEY` : 국회 API 키
- `ANTHROPIC_API_KEY` : Anthropic API 키

### 4. 웹페이지 켜기
**Settings → Pages → Source: Deploy from a branch → Branch: main / (root) → Save**
잠시 후 `https://<아이디>.github.io/bill-monitor/` 주소가 생깁니다.

### 5. 첫 수집 실행
**Actions 탭 → 법안 데이터 갱신 → Run workflow**
- 첫 실행은 22대 국회 법안을 최신순으로 최대 1,500건 분류합니다(약 1~2시간).
  나머지는 다음 날부터 매일 이어서 분류되며, 화면에 "분류 대기 N건"으로 표시됩니다.
- 이후에는 새로 발의된 법안만 분류하므로 몇 분이면 끝납니다.

---

## 호스트가 고치는 곳

| 하고 싶은 일 | 파일 |
|---|---|
| 개인정보 관련 판단 기준 추가·수정 | `config/criteria.md` |
| 특정 법안 강제 포함/제외 | `config/overrides.json` |
| 사이트 제목, 수집 시작일, 모델, 하루 분류 한도 | `config/settings.json` |
| 화면 디자인·기능 | `index.html` |

GitHub 웹에서 파일을 열고 연필 아이콘으로 고친 뒤 저장(Commit)하면 됩니다.
`config/`를 고치면 갱신 작업이 자동으로 다시 실행됩니다.

**수동 포함/제외 예시** (`config/overrides.json`)
```json
{
  "include": { "2212345": "신용정보 제3자 제공 조항 포함 — 담당자 확인" },
  "exclude": { "2209876": "제명에만 '정보' 포함, 개인정보 무관" }
}
```

> ⚠️ `criteria.md`나 모델을 바꾸면 기존 분류가 모두 무효화되어 처음부터 다시 분류합니다(전체 약 1만 건 기준 수 달러 수준, 하루 한도에 따라 며칠에 나눠 진행).
> 특정 법안 몇 건만 바로잡을 때는 `overrides.json`을 쓰세요.

---

## 비용
- GitHub Pages·Actions: 공개 저장소는 무료
- Anthropic API: 법안 1건당 Haiku 호출 1회. 첫 전체 분류 수 달러, 이후 하루 수 센트 수준

## 문제가 생기면
- Actions 탭에서 실패한 실행을 열어 로그를 확인하세요.
- 화면에 "제목 기준" 표시가 많이 보이면 의안정보시스템에서 제안이유를 가져오지 못한 것입니다.
  (`scripts/update.py`의 `fetch_summary` 함수 확인 필요)
