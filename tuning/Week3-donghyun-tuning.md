# Prometheus 커널 파라미터 튜닝 — WAL I/O 최적화 실습

> **담당:** 동현 | **범위:** 리눅스 커널 `sysctl` 파라미터 및 스토리지 튜닝
> 

> **환경:** Rocky Linux (RHEL 10 계열) · aarch64 · 2 CPU · 3.5 GiB RAM · NVMe SSD
> 

> **참고 자료:** [Prometheus TSDB (Part 2): WAL and Checkpoint — Ganesh Vernekar](https://ganeshvernekar.com/blog/prometheus-tsdb-wal-and-checkpoint/)
> 

---

# 0. 배경 지식: WAL과 Checkpoint 내부 동작

튜닝에 앞서, 프로메테우스가 **왜** 디스크에 지속적으로 쓰기를 발생시키는지 내부 구조를 먼저 이해해야 합니다.

## 0-1. WAL(Write-Ahead Log)의 역할

프로메테우스는 성능을 위해 수집한 데이터를 **메모리(Head Block)**에 먼저 적재합니다. 서버가 비정상 종료(crash)되어도 이 메모리 데이터를 복구할 수 있도록, 디스크에 순차적으로 기록해 두는 **백업 로그**가 WAL입니다.

- **Series(시리즈) 레코드:** "어떤 서버의 어떤 메트릭인가"를 나타내는 이름표(label set)입니다. 데이터가 처음 들어올 때 **단 한 번만** 기록하여 용량을 절약합니다.
- **Sample(샘플) 레코드:** 시간에 따른 실제 값(예: CPU 80%, 85%…)입니다. 계속해서 추가되며, 참조 무결성을 위해 반드시 시리즈 레코드보다 나중에 기록됩니다.
- **저장 형태:** WAL은 **128 MiB 크기의 세그먼트 파일**로 분할되어 순서대로 번호가 매겨집니다 (`000000`, `000001`, …).

## 0-2. Checkpoint — "오래된 WAL을 안전하게 지우는 방법"

디스크 용량 확보를 위해 오래된 WAL 세그먼트를 주기적으로 삭제해야 합니다. 그런데 맨 앞 파일을 통째로 지우면, **거기에 단 한 번만 기록해 둔 시리즈(이름표) 데이터까지 영구 유실**되는 문제가 발생합니다.

**Checkpoint는 이 딜레마를 해결합니다:**

1. 삭제 대상인 오래된 WAL 파일들을 순회합니다.
2. 쓸모없는 과거 샘플 값은 버리고, **아직 참조가 필요한 시리즈 정보와 Tombstone(삭제 마커)만 추출**합니다.
3. 추출한 필수 정보를 `checkpoint` 파일로 저장한 뒤, 원본 WAL 세그먼트를 안전하게 삭제합니다.

## 0-3. 재시작 복구(Replay) 과정

서버가 재부팅되면:

1. 가장 최근의 Checkpoint를 먼저 읽어 **뼈대(시리즈 정보)**를 복원합니다.
2. 이후 남은 WAL 파일들을 순차적으로 읽어 **최신 데이터(샘플)**를 복구합니다.

## 0-4. WAL의 디스크 I/O 특성 — 왜 커널 튜닝이 필요한가

| 특성 | 설명 |
| --- | --- |
| **32 KiB 페이지 단위 쓰기** | Write Amplification을 줄이기 위해 데이터를 메모리에 모았다가 32 KiB 페이지로 묶어서 디스크에 Flush합니다 |
| **Checksum** | 각 페이지마다 CRC32 체크섬을 기록하여 데이터 손상(corruption)을 감지합니다 |
| **순차 쓰기(Sequential Write)** | WAL은 오직 끝에 이어 쓰기(Append)만 수행하는 순차 I/O 구조입니다 |
| **지속적 스트리밍** | Scrape Interval마다 새로운 샘플이 유입되므로, 쓰기가 끊임없이 발생합니다 |

> **핵심:** 프로메테우스의 WAL은 `32 KiB 단위 · 순차 · 지속적 스트리밍 쓰기`라는 매우 뚜렷한 I/O 패턴을 가집니다. 커널 파라미터 튜닝은 바로 이 패턴을 공략합니다.
> 

---

# 1. 테스트 환경 구성

## 1-1. 시스템 모니터링 도구 설치

디스크 I/O 관찰을 위해 `iostat`이 포함된 `sysstat` 패키지를 설치합니다.

```
sudo dnf install epel-release -y
sudo dnf install sysstat -y
```

## 1-2. Prometheus 다운로드

```
# amd64
wget https://github.com/prometheus/prometheus/releases/download/v2.45.0/prometheus-2.45.0.linux-amd64.tar.gz
tar xvfz prometheus-2.45.0.linux-amd64.tar.gz
cd prometheus-2.45.0.linux-amd64

# ARM64 (aarch64)
wget https://github.com/prometheus/prometheus/releases/download/v2.45.0/prometheus-2.45.0.linux-arm64.tar.gz
tar xvfz prometheus-2.45.0.linux-arm64.tar.gz
cd prometheus-2.45.0.linux-arm64
```

## 1-3. Avalanche(부하 발생기) 실행

Avalanche는 수만 개의 Dummy 메트릭을 생성하여 프로메테우스에 강제로 Scrape하게 만드는 부하 발생기입니다.

**Docker:**

```
docker run -d -p 9001:9001 quay.io/freshtracks.io/avalanche \
  --metric-count=600 --series-count=200 --port=9001
```

**Podman (Rocky Linux 권장):**

```
sudo dnf install podman -y

podman run -d -p 9001:9001 quay.io/freshtracks.io/avalanche \
  --metric-count=600 --series-count=200 --port=9001

# 정상 동작 확인
curl localhost:9001/metrics | head -n 20
```

**macOS (네이티브 빌드 권장):**

```
sudo dnf install git golang -y
git clone https://github.com/prometheus-community/avalanche.git
cd avalanche
go build -o avalanche ./cmd/avalanche
nohup ./avalanche --metric-count=600 --series-count=200 --port=9001 > /dev/null 2>&1 &

curl localhost:9001/metrics | head -n 20
```
<img width="896" height="390" alt="스크린샷 2026-03-24 오후 9 29 18" src="https://github.com/user-attachments/assets/62259e04-ce9d-4b43-b803-1042cfbfd614" />


## 1-4. Prometheus 설정 (`prometheus.yml`)

Avalanche를 Scrape Target으로 등록합니다. **1초 간격 수집**으로 부하를 극대화합니다.

```
scrape_configs:
  - job_name: "prometheus"
    static_configs:
      - targets: ["localhost:9090"]

  - job_name: "avalanche"
    scrape_interval: 1s
    static_configs:
      - targets: ["localhost:9001"]
```

<img width="892" height="420" alt="스크린샷 2026-03-24 오후 9 30 50" src="https://github.com/user-attachments/assets/1cde285f-9e83-49fb-8cef-9ab039dbeee9" />


---

# 2. WAL 압축 튜닝 (Prometheus 자체 옵션)

커널 파라미터를 건드리기 전에, 프로메테우스 자체의 WAL 압축 옵션이 디스크 I/O에 미치는 영향을 먼저 측정합니다. **변경 전 상태(Baseline)**를 정확히 아는 것이 모든 튜닝의 기본입니다.

## 2-1. Baseline 측정 — WAL 압축 OFF

**터미널 1: Prometheus 실행 (압축 비활성화)**

```
./prometheus \
  --config.file=prometheus.yml \
  --storage.tsdb.path=./data-uncompressed \
  --no-storage.tsdb.wal-compression
```

**터미널 2: 디스크 I/O 모니터링**

```
iostat -dxz 1
```

> `-d`(디스크별), `-x`(확장 지표), `-z`(0인 라인 숨김), `1`(1초 간격) 옵션 조합입니다.  
> WAL 튜닝에서는 `wkB/s`, `w/s`, `w_await`, `%util` 4개를 우선적으로 보면 됩니다.

### 실습 결과 — `iostat` (압축 OFF)

<img width="1600" height="320" alt="스크린샷 2026-03-24 오후 9 34 49" src="https://github.com/user-attachments/assets/c94e7aa3-b17e-45a9-a11c-1ba2bf37cb7a" />

```
Device            r/s     rkB/s   rrqm/s  %rrqm r_await rareq-sz     w/s     wkB/s   wrqm/s  %wrqm w_await wareq-sz     d/s     dkB/s   drqm/s  %drqm d_await dareq-sz     f/s f_await  aqu-sz  %util
dm-0             9.41    251.11     0.00   0.00    0.14    26.68   21.09   1211.20     0.00   0.00   24.41    57.43    0.00      0.00     0.00   0.00    0.00     0.00    0.00    0.00    0.52   2.63
nvme0n1          9.85    291.45     0.68   6.46    0.17    29.60    9.72   1231.90    12.14  55.54   42.47   126.76    0.00      0.00     0.00   0.00    0.00     0.00    0.00    0.00    0.41   0.19
```

### Baseline 분석

| 지표 | 측정값 | 의미 |
| --- | --- | --- |
| `wkB/s` | **~1,211 KB/s (~1.2 MB/s)** | 압축 없이 Raw 데이터를 WAL에 그대로 밀어내고 있는 물리적 쓰기량 |
| `w/s` (IOPS) | **~10–21회** | WAL의 32 KiB 페이지 단위 포장 시스템이 작동 중. 데이터를 메모리에 모았다가 32 KiB 덩어리로 디스크에 전달 |
| `%util` | **0.19–2.63%** | NVMe 스토리지 성능이 우수하여 1.2 MB/s 수준의 쓰기는 부하로 느껴지지 않는 수준. 디스크 병목 제로 |

> **현실 시나리오:** 지금은 장비 성능이 충분하지만, 대규모 클러스터에서 초당 수백만 개의 메트릭이 쏟아지는 환경이라면 `wkB/s`가 수백 MB/s로 치솟으면서 스토리지(AWS EBS 등)의 IOPS 한계나 대역폭 제한에 걸리게 됩니다.
> 

## 2-2. 튜닝 적용 — WAL 압축 ON (Snappy)

**터미널 1: Prometheus 실행 (압축 활성화)**

```
./prometheus \
  --config.file=prometheus.yml \
  --storage.tsdb.path=./data-compressed \
  --storage.tsdb.wal-compression
```

### 실습 결과 — `iostat` (압축 ON)

> **주의:** `iostat` 실행 직후의 첫 번째 출력 블록은 부팅 이후의 **누적 평균**입니다. 실시간 비교를 위해 반드시 **두 번째 블록 이후**의 데이터를 기준으로 분석해야 합니다.
> 

**첫 번째 블록 (누적 평균 — 참고용):**

```
Device            r/s     rkB/s   rrqm/s  %rrqm r_await rareq-sz     w/s     wkB/s   wrqm/s  %wrqm w_await wareq-sz     d/s     dkB/s   drqm/s  %drqm d_await dareq-sz     f/s f_await  aqu-sz  %util
dm-0             8.22    224.40     0.00   0.00    0.14    27.30   18.36   1145.62     0.00   0.00   24.31    62.40    0.00      0.00     0.00   0.00    0.00     0.00    0.00    0.00    0.45   2.29
nvme0n1          8.61    259.37     0.59   6.40    0.17    30.13    8.59   1163.55    10.52  55.06   41.93   135.53    0.00      0.00     0.00   0.00    0.00     0.00    0.00    0.00    0.36   0.17
```

**두 번째 블록 (실시간 — 분석 대상):**

```
Device            r/s     rkB/s   rrqm/s  %rrqm r_await rareq-sz     w/s     wkB/s   wrqm/s  %wrqm w_await wareq-sz     d/s     dkB/s   drqm/s  %drqm d_await dareq-sz     f/s f_await  aqu-sz  %util
dm-0             0.00      0.00     0.00   0.00    0.00     0.00    2.00      6.50     0.00   0.00    0.00     3.25    0.00      0.00     0.00   0.00    0.00     0.00    0.00    0.00    0.00   0.00
nvme0n1          0.00      0.00     0.00   0.00    0.00     0.00    2.00      6.50     0.00   0.00    0.50     3.25    0.00      0.00     0.00   0.00    0.00     0.00    0.00    0.00    0.00   0.00
```

### 실습 결과 — `top` (압축 ON 상태의 CPU 사용량)

```
top - 21:37:58 up 33 min,  3 users,  load average: 0.40, 0.35, 0.20
Tasks: 232 total,   1 running, 231 sleeping,   0 stopped,   0 zombie
%Cpu(s): 14.4 us,  0.2 sy,  0.0 ni, 85.0 id,  0.0 wa,  0.0 hi,  0.5 si,  0.0 st
MiB Mem :   3575.8 total,    592.3 free,   1268.1 used,   1909.8 buff/cache
MiB Swap:   4024.0 total,   4024.0 free,      0.0 used.   2307.7 avail Mem

    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND
   9725 mr8356    20   0 1896100 203376  12368 S  22.6   5.6   1:12.98 avalanche
   9910 mr8356    20   0 1401096 579076  46456 S   6.6  15.8   0:04.70 prometheus
    827 root      20   0       0      0      0 S   0.3   0.0   0:00.70 xfsaild/dm-0
```

> 실습 중에는 `top -p $(pidof prometheus),$(pidof avalanche)`처럼 관심 프로세스만 보면 노이즈가 줄어듭니다.  
> CPU 비교 시에는 단일 스냅샷보다 10~30초 동안의 평균 추세로 판단하는 것이 안전합니다.

### WAL 압축 효과 분석

| 지표 | 압축 OFF (Baseline) | 압축 ON (Snappy) | 변화 |
| --- | --- | --- | --- |
| `wkB/s` | ~1,211 KB/s | **6.50 KB/s** | **약 1/200로 감소** |
| `w/s` (IOPS) | ~10–21회 | **2회** | IOPS 대폭 감소 |
| `%util` | 0.19–2.63% | **0.00%** | 디스크 부하 사실상 제로 |
| Prometheus CPU | 거의 0% | **6.6%** | CPU 오버헤드 발생 (트레이드오프) |

**왜 이렇게 극적으로 줄었는가?**

- Snappy 알고리즘이 시계열 데이터의 특성(반복되는 라벨, 유사한 값)을 매우 효과적으로 압축합니다.
- 압축률이 높아지면 32 KiB 페이지 하나를 채우는 데 더 오랜 시간이 걸리고, 결과적으로 디스크로 Flush하는 빈도가 급감합니다.

**트레이드오프:**

- CPU 자원을 6.6% 소비하는 대가로, 디스크 쓰기 부하를 1/200로 줄였습니다.
- **HDD / IOPS 제한 클라우드 블록 스토리지(EBS 등):** WAL 압축 **필수**
- **로컬 NVMe + CPU 코어 극한 부족 상황:** 압축을 끄는 것이 전체 시스템 안정성에 유리할 **수도** 있음

---

# 3. 리눅스 커널 파라미터 (`sysctl`) 튜닝

WAL 압축으로 디스크 쓰기 **총량**을 줄였다면, 커널 파라미터 튜닝은 그 쓰기가 **언제, 얼마나 한꺼번에** 디스크로 내려가는지를 제어합니다.

## 3-1. 배경 지식: Dirty Page와 Linux Page Cache

리눅스는 디스크에 쓸 데이터를 즉시 디스크로 보내지 않습니다. 대신 **메모리(Page Cache)**에 모아뒀다가 한 번에 Flush합니다. 이때 아직 디스크에 쓰여지지 않은 메모리 페이지를 **Dirty Page**라고 부릅니다.

```
              ┌─────────────┐
Prometheus    │  Page Cache  │     Disk
 (WAL 쓰기) ──→│ (Dirty Page) │──→  NVMe
              └─────────────┘
                ↑                    ↑
          메모리에 축적         Flush(Writeback)
```

커널은 Dirty Page를 디스크로 내보내는 **두 가지 기준**을 사용합니다:

| 기준 | 파라미터 | 기본값 | 동작 |
| --- | --- | --- | --- |
| **용량(Ratio)** | `vm.dirty_background_ratio` | 10% | 전체 RAM의 10%가 Dirty Page로 차면 **백그라운드 Writeback** 시작 |
| **용량(Hard Limit)** | `vm.dirty_ratio` | 30% | 전체 RAM의 30%가 차면 **프로세스를 블로킹**하고 강제 Flush |
| **시간(Timer)** | `vm.dirty_writeback_centisecs` | 500 (5초) | 용량과 무관하게 **5초마다** 깨어나서 오래된 Dirty Page를 검사 |
| **시간(Expire)** | `vm.dirty_expire_centisecs` | 3000 (30초) | Dirty Page가 생성된 지 **30초**가 지나면 Flush 대상으로 지정 |

## 3-2. 튜닝 전 현재값 확인

### 실습 — 현재 커널 파라미터 조회

```
mr8356@mr8356:~/prometheus-2.45.0.linux-arm64$ sysctl vm.dirty_background_ratio
vm.dirty_background_ratio = 10

mr8356@mr8356:~/prometheus-2.45.0.linux-arm64$ sysctl vm.dirty_ratio
vm.dirty_ratio = 30

mr8356@mr8356:~$ sysctl vm.dirty_writeback_centisecs vm.dirty_expire_centisecs
vm.dirty_writeback_centisecs = 500
vm.dirty_expire_centisecs = 3000
```

<img width="894" height="201" alt="스크린샷 2026-03-24 오후 9 52 56" src="https://github.com/user-attachments/assets/6807e89d-df77-4367-9f4c-1e4f88750401" />


### 실습 — Dirty Page 실시간 관찰

```
watch -n 1 'grep -i dirty /proc/meminfo'
```

> `watch`로 볼 때는 절대값 하나보다, 시간에 따른 패턴(완만한 증가 → 주기적 하락)을 보는 것이 핵심입니다.  
> 이 패턴이 보이면 Writeback 타이머가 정상 동작 중이라는 신호로 해석할 수 있습니다.

```
Every 1.0s: grep -i 'dirty' /proc/meminfo            mr8356: Tue Mar 24 21:55:40 2026

Dirty:             10604 kB
```

<img width="779" height="70" alt="스크린샷 2026-03-24 오후 9 55 30" src="https://github.com/user-attachments/assets/242b3f2e-f577-4511-90d8-60ea2ce0f9a6" />


### 분석: 기본값에서 10 MB 수준에서 빠지는 이유

파라미터 튜닝 전인데도 Dirty 수치가 10 MB 수준에서 주기적으로 빠지는 현상이 관찰됩니다. 이것은 **`dirty_background_ratio`(10% = ~350 MB)에 도달해서가 아니라, 시간 기준 Timer가 먼저 작동**했기 때문입니다.

**경합 분석:**

| 기준 | 임계값 (RAM 3.5 GiB 기준) | 실제 상황 |
| --- | --- | --- |
| `dirty_background_ratio` 10% | **~350 MB** | 압축 ON 상태에서 쓰기 속도가 6.5 KB/s이므로 350 MB 도달까지 약 15시간 소요 → 사실상 도달 불가 |
| `dirty_writeback_centisecs` 5초 | 5초마다 체크 | 데이터가 들어온 지 5초가 넘으면 양과 무관하게 디스크로 Flush → **이 기준이 먼저 발동** |

즉, **Timer가 Ratio를 이긴 상황**입니다. OS의 백그라운드 작업(로그, 메타데이터 등)이 섞여 10 MB 수준이 관찰된 것입니다.

## 3-3. 그런데 왜 Ratio를 낮춰야 하는가? — Worst Case 대비

> "어차피 타이머가 5초마다 내보내면 Ratio를 낮출 필요 없지 않나?"
> 

SRE는 **최악의 상황(Worst Case)**을 대비합니다.

**시나리오:** 갑자기 Avalanche 부하가 10배로 늘어나거나, 디스크 I/O 병목이 생겨서 5초 안에 메모리에 데이터가 수백 MB씩 쌓인다고 가정합니다.

| 상황 | `dirty_background_ratio` | 발생하는 일 |
| --- | --- | --- |
| **튜닝 전** (10%) | ~350 MB | 350 MB가 찰 때까지 커널이 방치 → 임계치 도달 시 **Burst I/O 폭탄** → 시스템 레이턴시 스파이크 |
| **튜닝 후** (3%) | ~100 MB | 부하가 급증해도 100 MB 선에서 강제 Writeback 시작 → **I/O 스파이크 크기를 1/3로 제한하는 안전벨트** |

마찬가지로 `dirty_ratio`(Hard Limit)를 30% → 10%로 낮추면, 프로세스가 블로킹되기 전에 더 일찍 강제 Flush를 시작하여 **Write Stall(쓰기 정체) 시간을 단축**합니다.

## 3-4. 튜닝 적용

### 실습 — 커널 파라미터 변경

```
# Dirty Page 백그라운드 Flush 임계치: 10% → 3%
sudo sysctl -w vm.dirty_background_ratio=3

# Dirty Page Hard Limit: 30% → 10%
sudo sysctl -w vm.dirty_ratio=10
```

### 실습 — 변경 확인

```
mr8356@mr8356:~/prometheus-2.45.0.linux-arm64$ sysctl vm.dirty_ratio
vm.dirty_ratio = 10

mr8356@mr8356:~/prometheus-2.45.0.linux-arm64$ sysctl vm.dirty_background_ratio
vm.dirty_background_ratio = 3
```

### 영구 적용 (`/etc/sysctl.d/`)

`sysctl -w`는 런타임에만 유효합니다. 재부팅 후에도 유지하려면:

```
sudo tee /etc/sysctl.d/99-prometheus-tuning.conf << 'EOF'
vm.dirty_background_ratio = 3
vm.dirty_ratio = 10
EOF

sudo sysctl --system
```

## 3-5. 파일 시스템 마운트 옵션: `noatime`

WAL은 오직 Append만 수행하는 순차 I/O입니다. 파일에 접근할 때마다 접근 시간(atime)을 기록하는 메타데이터 쓰기는 이 순차 흐름을 방해합니다.

**`/etc/fstab`에 `noatime` 옵션을 추가하여 불필요한 메타데이터 I/O를 제거합니다:**

```
# 예시: Prometheus 데이터 전용 볼륨이 있는 경우
UUID=xxxx-xxxx  /var/lib/prometheus  xfs  defaults,noatime  0 0
```

> `noatime` vs `relatime`: RHEL 계열은 기본이 `relatime`(수정 시간보다 접근 시간이 오래된 경우에만 갱신)입니다. Prometheus WAL처럼 쓰기 전용 워크로드에서는 `noatime`이 더 적합합니다.
> 

---

# 4. 전체 결과 요약

## 튜닝 항목별 효과 정리

| 튜닝 항목 | 변경 전 | 변경 후 | 효과 |
| --- | --- | --- | --- |
| **WAL 압축** (Prometheus) | OFF | ON (Snappy) | 디스크 쓰기량 **1/200 감소**, CPU 6.6% 증가 |
| **`vm.dirty_background_ratio`** | 10% (~350 MB) | 3% (~100 MB) | I/O 스파이크 크기 **1/3로 제한** |
| **`vm.dirty_ratio`** | 30% (~1 GB) | 10% (~350 MB) | Write Stall 발생 전 더 일찍 Flush 시작 |
| **마운트 옵션** | `relatime` (기본) | `noatime` | 불필요한 메타데이터 I/O 제거 |

## SRE 의사결정 매트릭스

| 환경 | WAL 압축 | Dirty Ratio 튜닝 | 근거 |
| --- | --- | --- | --- |
| **HDD / 클라우드 블록 스토리지** (EBS gp2 등) | **필수 ON** | **필수 적용** | IOPS 한계가 낮아 I/O 스파이크가 곧바로 서비스 지연으로 이어짐 |
| **로컬 NVMe + CPU 여유** | ON 권장 | 적용 권장 | 특별한 이유 없으면 켜두는 것이 이득 |
| **로컬 NVMe + CPU 극한 부족** | OFF 고려 | 적용 권장 | CPU 6.6% 절약이 더 중요한 극한 상황에 한해 |

---

# 5. 함께 알게 된  지식

### Linux Page Cache와 Writeback 메커니즘

리눅스의 모든 파일 I/O는 Page Cache를 거칩니다. `write()` 시스템 콜은 데이터를 커널의 Page Cache에 복사하고 즉시 반환합니다(buffered I/O). 이후 커널의 `pdflush`/`flush` 스레드가 비동기로 디스크에 기록합니다. 이 설계 덕분에 애플리케이션은 디스크 속도에 구애받지 않고 빠르게 쓰기를 완료할 수 있습니다.

### Write Amplification이란?

실제로 쓰려는 데이터보다 디스크에 기록되는 데이터가 더 많아지는 현상입니다. SSD/NVMe에서는 블록 단위 특성 때문에 1바이트를 수정해도 전체 블록을 다시 써야 하는 경우가 생깁니다. Prometheus가 32 KiB 단위로 묶어서 쓰는 것도 이를 최소화하기 위한 설계입니다.

### Snappy 압축 알고리즘

Google이 개발한 고속 압축 알고리즘입니다. 압축률보다 **속도**에 초점을 맞춘 설계로, CPU 오버헤드가 매우 낮습니다. 시계열 데이터는 라벨이 반복되고 값의 변화가 적어 Snappy의 LZ77 기반 압축에 매우 유리한 특성을 가집니다.

### iostat 핵심 컬럼 해석

- `w/s`: 초당 디스크 쓰기 요청 횟수 (Write IOPS)
- `wkB/s`: 초당 디스크에 기록되는 데이터 양 (KB/s)
- `w_await`: 쓰기 요청의 평균 대기 시간 (ms). 이 값이 높으면 디스크 병목
- `wareq-sz`: 평균 쓰기 요청 크기 (KB). Prometheus의 32 KiB 페이지가 반영됨
- `%util`: 디스크 대역폭 사용률. 100% 근접 시 병목

### 로그/지표 분석 빠른 체크리스트

1. **첫 블록 제외:** `iostat` 첫 출력은 누적 평균이므로 버리고, 두 번째부터 비교한다.
2. **기준선 확보:** 튜닝 전/후를 같은 부하(Avalanche 설정 동일)에서 최소 30초 이상 수집한다.
3. **핵심 4지표 비교:** `wkB/s`(총량), `w/s`(빈도), `w_await`(지연), `%util`(포화도)을 함께 본다.
4. **CPU 트레이드오프 확인:** `top`에서 `prometheus` CPU가 얼마나 증가했는지 반드시 같이 기록한다.
5. **Dirty 패턴 교차검증:** `/proc/meminfo`의 Dirty 증감 패턴이 `iostat` 쓰기 패턴과 시간적으로 맞는지 확인한다.

### iostat 첫 번째 출력 블록의 함정

`iostat`을 실행하면 첫 번째로 출력되는 블록은 **부팅 이후의 누적 평균**입니다. 실시간 분석에는 부적합하며, 반드시 두 번째 블록부터를 기준으로 판단해야 합니다. 이는 `/proc/diskstats`의 누적 카운터를 기반으로 계산되기 때문입니다.

### top에서 보이는 xfsaild/dm-0 프로세스

`xfsaild`는 XFS 파일 시스템의 AIL(Active Item List)을 관리하는 커널 스레드입니다. Journaling 파일 시스템의 로그를 디스크에 Flush하는 역할을 합니다. `dm-0`은 Device Mapper(LVM)의 첫 번째 논리 볼륨을 가리킵니다. Prometheus의 WAL 쓰기가 XFS 저널링과 맞물려 이 스레드의 활동이 관찰됩니다.
