# Memory Mapping of Head Chunks — OS & Memory Management

> Memory-Mapping (mmap), Memory Footprint (메모리 풋프린트), Offset (오프셋), Bitwise Operation (비트 연산)

---

## Memory-Mapping (mmap)

### 개념 정의

- `mmap()`은 파일의 내용을 프로세스의 가상 주소 공간에 직접 매핑하는 OS 시스템 콜이다.
- 일반 파일 I/O(`read()`)와 달리, 커널 스페이스에서 유저 스페이스로의 **데이터 복사 없이** 파일에 접근할 수 있다.

---

### read() vs mmap 비교 (kernel vs user)

```
  [read() 방식]
  유저 공간                 커널 공간               디스크
  ┌────────────┐       ┌──────────────┐      ┌──────────┐
  │ 유저 버퍼   │◄─②복사─│ Page Cache   │◄─①──│ 청크 파일 │
  │ (힙 메모리) │       │ (커널 버퍼)   │      │          │
  └────────────┘       └──────────────┘      └──────────┘
  ※ 복사 발생, 힙 메모리 직접 할당 필요

  [mmap 방식]
  유저 공간                 커널 공간               디스크
  ┌────────────┐       ┌──────────────┐      ┌──────────┐
  │ mmap 영역  │──────>│ Page Cache   │◄────  │ 청크 파일 │
  │(가상 주소)  │ 같은   │              │ 필요할│          │
  └────────────┘ 물리   └──────────────┘ 때만 └──────────┘
                 페이지 공유!
  ※ 복사 없음, OS가 페이지 테이블을 조작하여 같은 물리 메모리를 공유
```

> **Page Cache**: OS가 디스크에서 읽은 데이터를 물리 메모리에 캐싱하는 영역이다. `read()`는 이 캐시를 유저 버퍼로 복사하지만, mmap은 직접 참조한다.

---

### Demand Paging

mmap으로 128MiB를 매핑해도 전체가 메모리에 올라가지 않는다. **접근하는 부분만** 로드된다.

> `mmapFile[1234]`에서 파일 선택이 빠져있는 이유: 실제로는 **참조(ref)에서 파일 번호를 먼저 추출**하여 해당 mmap 슬라이스를 찾은 뒤, 오프셋으로 접근한다. 즉 `mmapSlices[93][1234]`가 정확한 표현이지만, Demand Paging 설명을 위해 단일 파일 접근으로 단순화한 것이다.

```
  ① 프로그램: data := mmapFile[1234]  (평범한 메모리 접근)
                    │
                    ▼
  ② CPU(MMU)가 페이지 테이블 조회 > valid=0 발견
     > Page Fault (CPU 하드웨어가 자동 발생, 프로그램이 요청한 것 아님)
                    │ -> 여기서 Demand Paging 등장
                    ▼
  ③ OS가 디스크에서 해당 4KB 페이지만 물리 메모리에 로드
     > 페이지 테이블 업데이트 (valid=1)
                    │
                    ▼
  ④ 프로그램 재개 > 데이터 정상 반환 (Page Fault 발생을 인지하지 못함)
```

청크는 OS 페이지(4KB)보다 훨씬 작아, 한번의 Page Fault로 인접한 ~22개 청크가 함께 로드된다(공간 지역성). 메모리 부족 시 OS가 자동으로 페이지를 내리고, 다음 접근 시 다시 로드한다.

> **대략적인 청크의 사이즈**: 청크 하나의 디스크 포맷은 `series_ref(8B) + mint(8B) + maxt(8B) + encoding(1B) + len(uvarint, ~1~2B) + data(압축 데이터, ~150B) + CRC32(4B)` 이므로 대략 180B 전후이다.

---

### 읽기 전용 매핑과 읽기/쓰기 매핑

mmap에는 **읽기 전용**과 **읽기/쓰기** 두 가지 모드가 있다.

```
  [읽기/쓰기 mmap]
  프로그램이 mmap 영역을 수정할 수 있다.
  > 수정된 페이지 = dirty page 발생
  > msync()로 디스크에 반영해야 함
  > 여러 스레드가 동시에 쓰면 동기화(락) 필요
  예시: 데이터베이스 엔진이 인덱스 파일을 직접 수정할 때

  [읽기 전용 mmap]  ← TSDB가 사용하는 방식
  프로그램은 읽기만 가능, 수정 불가.
  > dirty page 미발생 (수정이 없으니 Page Cache = 디스크 항상 일치)
  > 동기화 불필요 (수정 주체가 없으므로 동시 읽기에 락 불필요)
  > 메모리 해제 간단 (디스크에 반영할 것 없이 그냥 버리면 됨)
```

TSDB의 플러시된 청크는 불변(immutable)이므로 읽기 전용 mmap만 사용한다.

---

## Memory Footprint (메모리 풋프린트)

### 개념 정의

- 프로세스가 실제 점유하는 메모리의 총량이다.
- 블로그 원문에서도 직접 사용하는 용어: *"reducing the memory footprint of the Head block"*

---

### 힙(user) vs mmap(kernel) 영역 — Go로 메모리 관리할 때와의 차이점

| 구분 | 힙 (Heap) | mmap 영역 |
| --- | --- | --- |
| 할당 주체 | 프로그램이 직접 | OS가 파일 매핑으로 |
| 메모리 관리 | Go GC가 추적/해제 | OS가 자동 관리 |
| 풋프린트 영향 | 직접적 | 간접적 |

mmap 이전에는 청크 데이터(120~200B/개)가 전부 **Go 힙**에 있었다. mmap 도입 후 청크 데이터가 OS 관리 영역으로 이동하면서, **Go GC가 추적할 메모리가 줄고 GC 오버헤드도 감소**했다.

```
  [도입 전] 힙: 1.5GB (청크 800MB 포함) > GC가 1.5GB 추적
  [도입 후] 힙: 800MB (참조 100MB만)    > GC가 800MB만 추적
```

---

## Offset (오프셋)

### 개념 정의

- **기준점으로부터의 바이트 단위 거리**이다.
- 파일 오프셋 = "파일 시작에서 몇 바이트 떨어져 있는가"
- 오프셋만 알면 O(1)로 원하는 위치에 접근할 수 있다.

---

### 파일 오프셋 예시

```
  chunks_head/000001:
  ┌────────┬──────────────┬──────────────┬──────────────┐
  │ 헤더   │    청크 1     │    청크 2     │    청크 3     │
  │ (8B)   │  (~180B)     │  (~180B)     │  (~180B)     │
  └────────┴──────────────┴──────────────┴──────────────┘
  offset: 0      8              188             368
```

mmap에서는 오프셋이 곧 **배열 인덱스**이므로, `seek()` 시스템 콜 없이 `mmapSlice[1234]`처럼 직접 접근한다.

---

## Bitwise Operation (비트 연산)

### 개념 정의

- 데이터를 비트 단위로 조작하는 연산 (AND, OR, SHIFT 등)
- CPU ALU에서 1클럭 수준으로 처리되는 매우 빠른 연산이다.
- 하나의 정수에 여러 값을 압축 저장(패킹)하거나, 특정 비트만 추출(마스킹)하는 데 쓰인다.

---

### 핵심 연산

**Left Shift (`<<`)**: 값을 상위 비트에 배치, 하위를 0으로 비움

```
  93 << 32:
  [  93이 상위 32비트에 위치  ][  하위 32비트는 전부 0 (빈자리)  ]
```

**OR (`|`)**: 빈자리(0)에 값을 채워넣기 (`0 | x = x`)

```
  (93 << 32) | 1234:
  [── 93 (상위 32비트) ──][── 1234 (하위 32비트) ──]
```

**Right Shift (`>>`)**: 상위 비트를 하위로 내려서 추출

```
  reference >> 32  >  93 (하위 32비트는 밀리면서 사라짐)
```

**AND (`&`) + 마스크**: 원하는 비트만 남기고 나머지 제거 (`x & 0 = 0`, `x & 1 = x`)

```
  reference & 0xFFFFFFFF:
  상위 32비트: & 0 > 사라짐
  하위 32비트: & 1 > 남음  >  1234
```

---

### 비트 패킹

하나의 64비트 정수에 두 값을 담는 기법이다.

```
  패킹:   reference = (fileNum << 32) | offset
  언패킹: fileNum = reference >> 32
          offset  = reference & 0xFFFFFFFF

  ┌──────── 64비트 정수 (8바이트) ──────────────┐
  │ 상위 32비트: 파일 번호 │ 하위 32비트: 오프셋  │
  └───────────────────────┴────────────────────┘
```

구조체(~16B)보다 비트 패킹(8B)이 공간 효율적이고, 64비트 값은 CPU에서 원자적으로 읽기/쓰기가 가능하여 동시성에도 안전하다.

---

## TSDB와의 연결

### 전체 그림

```
  Prometheus가 exporter(node_exporter 등)를 scrape하여 시계열 샘플 수집
     │
     ▼
  ① WAL에 먼저 기록 (내구성 보장, 크래시 시 복구용)
     │
     ▼
  ② Head Block(메모리)의 활성 청크에 샘플 추가 (순차 처리, 동시 아님) — 압축도 진행
     │
     ▼ (청크가 가득 참, ~120개 샘플)
  ③ chunks_head/에 플러시 > mmap 참조(24B)만 메모리에 유지
```

```
  data/
  ├── wal/              ← 크래시 복구용 원본 기록
  ├── chunks_head/      ← 가득 찬 불변 청크 저장, mmap 접근
  + Head Block (메모리)  ← 활성 청크, mmap 참조, 인덱스, 심볼
```

---

### mmap × Prometheus

원문: *"the file looks like yet another byte slice and accessing the slice at some index to get the chunk data while the OS maps the slice in the memory to the disk under the hood."*

시작 시 모든 chunks_head 파일을 mmap하여 `파일번호 > 바이트 슬라이스` 맵을 만든다. 쿼리 시 `mmapSlices[fileNum][offset]`으로 접근하면 OS가 Demand Paging으로 필요한 페이지만 로드한다.

불변 청크이므로 읽기 전용 mmap만 사용 > 동기화/dirty page 문제 없음.

---

### Memory Footprint × Prometheus

원문: *"it can take anywhere between 120 to 200 bytes. Now this is replaced with 24 bytes."*

```
  도입 전: 120~200 B/청크 (압축 데이터 전체가 힙에)
  도입 후:      24 B/청크 (참조 8B + mint 8B + maxt 8B)
```

원문: *"In the real world, we can see a 15-50% reduction in the memory footprint depending on ... 'churn'."*

Head 블록에는 인덱스, 심볼, 활성 청크 등 mmap 대상이 아닌 메모리도 있으므로, 청크 단위 85% 절감이 전체로는 15~50%가 된다. Churn(시리즈 생성/소멸 빈도)이 낮을수록 효과가 크다.

또한 쿼리 시 mmap 페이지가 물리 메모리에 로드되므로, 피크 메모리의 절대적 감소는 아니다.

---

### Offset × Prometheus

원문: *"the last 4 bytes tell the offset in the file where the chunk starts"*

```
  chunks_head/000001:
  ┌────────┬────────┬────────┬────────┐
  │ 헤더8B │ 청크1   │ 청크2   │ 청크3   │
  └────────┴────────┴────────┴────────┘
  offset: 0    8       188      368
```

이 오프셋이 참조의 하위 32비트에 인코딩되어, 파일 내 청크 위치를 정확히 가리킨다.

---

### Bitwise Operation × Prometheus

원문: *"the reference of that chunk would be (93 << 32) | 1234"*

```
  파일 93번, 오프셋 1234:

  93 << 32:  [  93  ][ 0000...0000 ]   ← 상위에 배치, 하위 비움
  | 1234:    [  93  ][    1234     ]   ← 하위에 오프셋 채움

  디코딩:
  >> 32          > 93    (파일 번호)
  & 0xFFFFFFFF   > 1234  (오프셋)
```

8B 정수 하나로 "어떤 파일의 어느 위치"를 표현한다.

---

### 쿼리 흐름 — 4개 개념의 결합

```
  PromQL 쿼리: "최근 5분간 http_requests_total"
       │
  ①  Head에서 mint/maxt로 청크 선택           [Footprint: 24B로 유지]
       │
  ②  ref >> 32 > 파일번호, ref & mask > 오프셋 [Bitwise + Offset]
       │
  ③  mmapSlices[fileNum][offset]으로 접근       [mmap: 배열처럼 접근]
       │
  ④  OS가 Page Fault > 해당 4KB만 로드          [Demand Paging]
       │
  ⑤  encoding + data 디코딩 > 샘플 반환
```

---

## 도입 전 vs 후

### 메모리

```
  도입 전: 청크 120~200B × N개 > 힙에 상주, GC 부하 높음
  도입 후: 참조 24B × N개만 힙 > 나머지 OS 관리, GC 부하 낮음
  절감: 실제 환경 15~50%
```

### 시작 속도

원문: *"(1) decoding of WAL records from disk and (2) rebuilding the compressed chunks from individual samples, are the slow parts"*

```
  도입 전: WAL 디코딩(T1) + 전체 청크 재구축(T2)
  도입 후: chunks_head 순회(T0, 매우 빠름) + WAL 디코딩(T1) + 일부만 재구축(T2')
  절감: 15~30%
```

Head 블록 메모리에는 청크 데이터 외에 인메모리 인덱스, 심볼 테이블, 활성 청크, 시리즈 메타데이터 등 mmap 대상이 아닌 것들도 있어서, 청크 단위 85% 절감이 전체로는 희석된다. Churn(시리즈 생성/소멸 빈도)에 따라 차이가 난다:

- **Churn 높음** > 시리즈당 청크 수 적음 > 인덱스/심볼 비중이 큼 > ~15%
- **Churn 낮음** > 시리즈당 청크 수 많음 > 청크 데이터 비중이 큼 > ~50%

WAL 디코딩은 피할 수 없지만(인덱스가 없어 전부 읽어야 함), 이미 완성된 청크의 재구축을 건너뛰는 것이 핵심이다.

---

## 관련 코드

- [`tsdb/chunks/head_chunks.go`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/chunks/head_chunks.go) — 청크 쓰기/읽기/truncation/파일 관리
- [`tsdb/head.go`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/head.go) — Head 블록에서 mmap 청크 활용

## 관련 커널 파라미터

- `vm.max_map_count` — mmap 매핑 최대 개수, 시계열 많으면 상향 필요
- `vm.dirty_ratio` — mmap 읽기 전용에서는 dirty page 미발생이므로 직접 영향 없음

---

## 참고 문헌

- [Ganesh Vernekar - Prometheus TSDB (Part 3): Memory Mapping of Head Chunks from Disk](https://ganeshvernekar.com/blog/prometheus-tsdb-mmapping-head-chunks-from-disk/)
- [Prometheus GitHub (release-3.10)](https://github.com/prometheus/prometheus/tree/release-3.10)
  - [`tsdb/chunks/head_chunks.go`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/chunks/head_chunks.go)
  - [`tsdb/head.go`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/head.go)
- [Linux mmap(2) man page](https://man7.org/linux/man-pages/man2/mmap.2.html)
