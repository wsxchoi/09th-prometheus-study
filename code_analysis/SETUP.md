# Code Analysis 환경 설정 가이드

코드 분석을 위한 Git 및 VSCode 환경 설정 방법입니다.

## 1. 스터디 저장소 Fork & Clone

```bash
# 1) GitHub에서 스터디 저장소를 Fork합니다
# https://github.com/<org>/09th-prometheus-study → Fork

# 2) Fork한 저장소를 clone합니다
git clone https://github.com/<your-username>/09th-prometheus-study.git
cd 09th-prometheus-study
```

## 2. Prometheus 소스코드 Clone

코드 분석 시 Prometheus 소스코드를 직접 탐색해야 합니다. **스터디 저장소와 별도의 디렉토리**에 clone합니다.

```bash
# 스터디 저장소 밖에서 실행
cd ..
git clone https://github.com/prometheus/prometheus.git
cd prometheus

# 분석 기준 브랜치로 checkout
git checkout release-3.10
```

> 모든 코드 분석은 [v3.10.0 (release-3.10)](https://github.com/prometheus/prometheus/tree/release-3.10) 기준입니다.

## 3. VSCode 설정

### Workspace 구성

VSCode에서 두 저장소를 하나의 워크스페이스로 열면 코드 분석과 문서 작성을 동시에 할 수 있습니다.

1. **File → Add Folder to Workspace** 로 두 폴더를 추가합니다:
   - `09th-prometheus-study` (문서 작성용)
   - `prometheus` (코드 탐색용)

2. **File → Save Workspace As** 로 워크스페이스를 저장합니다

```
your-workspace/
├── 09th-prometheus-study/   ← 문서 작성 (origin: 내 fork)
│   └── code_analysis/
└── prometheus/              ← 코드 탐색 (release-3.10 브랜치)
    ├── cmd/
    ├── tsdb/
    └── ...
```

### 추천 VSCode 확장

| 확장 | 용도 |
|------|------|
| Go | Go 코드 탐색 시 정의 이동(F12), 참조 찾기 등 지원 |
| Markdown Preview Enhanced | 마크다운 미리보기 |

## 4. Git Remote 구성 (스터디 저장소)

스터디 저장소에서 origin(내 fork)과 upstream(원본)을 구분합니다.

```bash
cd 09th-prometheus-study

# remote 확인 — clone 직후에는 origin만 존재
git remote -v
# origin  https://github.com/<your-username>/09th-prometheus-study.git (fetch)
# origin  https://github.com/<your-username>/09th-prometheus-study.git (push)

# upstream 추가 (원본 스터디 저장소)
git remote add upstream https://github.com/<org>/09th-prometheus-study.git

# 확인
git remote -v
# origin    https://github.com/<your-username>/09th-prometheus-study.git (fetch)
# origin    https://github.com/<your-username>/09th-prometheus-study.git (push)
# upstream  https://github.com/<org>/09th-prometheus-study.git (fetch)
# upstream  https://github.com/<org>/09th-prometheus-study.git (push)
```

### Remote 요약

| Remote | 저장소 | 용도 |
|--------|--------|------|
| `origin` | 내 Fork | push 대상 (PR용 브랜치) |
| `upstream` | 원본 스터디 저장소 | 최신 변경사항 pull |

## 5. 작업 흐름

### 코드 분석 문서 작성

```bash
# 1) upstream의 최신 변경사항을 가져옵니다
git fetch upstream
git checkout main
git merge upstream/main

# 2) 작업 브랜치를 생성합니다
git checkout -b <your-name>

# 3) 문서를 작성합니다
#    - code_analysis/ 디렉토리의 해당 주차 md 파일을 편집
#    - 코드 경로는 백틱으로 감싸서 작성 (예: `tsdb/head.go`)

# 4) (선택) 파일 경로를 GitHub 링크로 자동 변환
python3 scripts/linkify.py code_analysis/Week<N>_<Topic>.md

# 5) 커밋 & push
git add code_analysis/
git commit -m "add Week<N> code analysis"
git push origin <your-name>

# 6) GitHub에서 upstream 저장소로 Pull Request 생성
```

### 다른 사람의 변경사항 반영

```bash
git fetch upstream
git checkout main
git merge upstream/main
git checkout <your-branch>
git rebase main
```
