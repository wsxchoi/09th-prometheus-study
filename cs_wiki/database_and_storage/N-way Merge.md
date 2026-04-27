## 1. 2-way Merge 기본 개념

**정의**: 이미 정렬된 두 개의 데이터 소스를 하나의 정렬된 결과로 합치는 알고리즘입니다.

**동작 원리**:

- 두 소스 A, B 각각에 포인터(커서)를 하나씩 둡니다.
- 매 스텝마다 두 포인터가 가리키는 값을 비교해서, 더 작은 쪽을 결과에 넣고 해당 포인터를 한 칸 전진시킵니다.
- 한쪽이 먼저 끝나면 나머지를 그대로 이어붙입니다.

**예시**:

```
A = [1, 3, 5, 7]    ← 포인터 pA
B = [2, 4, 6, 8]    ← 포인터 pB

Step 1: pA=1, pB=2 → 1 < 2 → 결과에 1, pA 전진
Step 2: pA=3, pB=2 → 2 < 3 → 결과에 2, pB 전진
Step 3: pA=3, pB=4 → 3 < 4 → 결과에 3, pA 전진
...
결과 = [1, 2, 3, 4, 5, 6, 7, 8]
```

**시간복잡도**: O(n + m), 여기서 n, m은 각 소스의 원소 수

**DB에서 쓰이는 곳**: Merge Sort의 마지막 단계, Sort-Merge Join에서 두 테이블 합칠 때, LSM-Tree에서 두 SSTable 합칠 때 사용됩니다.

---

## 2. N-way Merge 개념

**정의**: 2-way를 일반화한 것으로, N개의 정렬된 소스를 동시에 하나로 합칩니다.

**단순 구현의 문제**: 매 스텝마다 N개 포인터의 현재 값을 전부 비교해야 합니다. 전체 원소가 T개라면 O(T × N)이 됩니다. N이 크면 비효율적이에요.

**효율적 구현 — Min-Heap(우선순위 큐)**:

- N개 소스의 첫 번째 원소를 min-heap에 넣습니다.
- heap에서 최솟값을 pop하면 그게 결과의 다음 원소입니다.
- pop된 원소가 속한 소스에서 다음 원소를 heap에 push합니다.
- heap이 빌 때까지 반복합니다.

```
소스1 = [1, 5, 9]
소스2 = [2, 6, 10]
소스3 = [3, 7, 11]

초기 heap: {1, 2, 3}  (각 소스의 첫 원소)

Pop 1(소스1) → 결과=[1], Push 5 → heap={2, 3, 5}
Pop 2(소스2) → 결과=[1,2], Push 6 → heap={3, 5, 6}
Pop 3(소스3) → 결과=[1,2,3], Push 7 → heap={5, 6, 7}
...
최종 결과 = [1, 2, 3, 5, 6, 7, 9, 10, 11]
```

**시간복잡도**: O(T × log N) — heap 연산이 O(log N)이므로 단순 비교 방식 대비 훨씬 효율적입니다.

**2-way vs N-way 비교**:

| 항목 | 2-way Merge | N-way Merge |
| --- | --- | --- |
| 소스 수 | 2개 | N개 |
| 비교 방식 | 단순 if 비교 | min-heap 사용 |
| 스텝당 비교 | O(1) | O(log N) |
| 전체 복잡도 | O(n+m) | O(T × log N) |
| 패스 수 | 다단계면 log₂ 패스 필요 | 1패스로 끝남 |
| 메모리 | 매우 적음 | heap 크기 O(N) |

핵심 트레이드오프는 이렇습니다. 2-way를 반복하면 (예: 8개 소스를 2-way로만 처리하면 3패스 필요) 전체 데이터를 여러 번 읽고 써야 합니다. N-way는 1패스로 끝나지만 heap 관리 비용이 있어요. 디스크 I/O가 비싼 환경(DB, TSDB)에서는 패스 수를 줄이는 N-way가 거의 항상 유리합니다.

---

## 3. External Sort-Merge 알고리즘

위의 2-way / N-way merge가 가장 체계적으로 활용되는 곳이 External Sort-Merge입니다. 메모리보다 큰 데이터를 정렬해야 할 때 사용하는 알고리즘이에요.

### Phase 1: 정렬된 Run 생성 (Run Creation)

메모리 크기가 M page라고 할 때:

```
전체 Relation (메모리보다 훨씬 큼)
┌──────────────────────────────────────────────┐
│ ████████████████████████████████████████████ │
└──────────────────────────────────────────────┘
           ↓ M page씩 잘라서 읽기

읽기 1: [M page] → 메모리에서 정렬 → Run R₀ (디스크에 저장)
읽기 2: [M page] → 메모리에서 정렬 → Run R₁ (디스크에 저장)
읽기 3: [M page] → 메모리에서 정렬 → Run R₂ (디스크에 저장)
...
읽기 k: [M page] → 메모리에서 정렬 → Run Rₖ₋₁ (디스크에 저장)
```

각 Run 내부는 이미 정렬된 상태입니다. 메모리 내 정렬은 quicksort 등 아무 알고리즘이나 쓸 수 있어요. 전체 relation 크기가 b block이면, Run 개수 N = ⌈b/M⌉ 개가 생성됩니다.

### Phase 2: Run 병합 (Merge)

여기서 N-way merge가 등장합니다.

**Case 1: N < M (run 개수가 메모리 블록 수보다 적은 경우)**

![image.png](attachment:94b5c898-b7eb-4f3f-b73b-c197bd8ceaa0:image.png)

- 각 Run에서 첫 번째 block을 메모리의 input buffer에 읽어옵니다.
- 모든 buffer에서 정렬 순서상 가장 작은 record를 선택해서 output buffer에 씁니다 (여기서 N-way merge).
- output buffer가 가득 차면 디스크에 flush합니다.
- input buffer가 비면 해당 Run에서 다음 block을 읽어옵니다.
- 모든 Run이 소진될 때까지 반복합니다.

**Case 2: N ≥ M (run 개수가 메모리 블록 수 이상인 경우)**

한 번에 모든 Run을 동시에 열 수 없으므로, 여러 pass에 걸쳐 점진적으로 합칩니다.

![image.png](attachment:fc599b35-b2e8-4277-baf5-570ca64f48ed:image.png)

일반적으로 필요한 총 pass 수 = ⌈log_{M-1}(N)⌉ 입니다.

**비용 분석**: 전체 relation 크기가 b block이고, 메모리가 M page일 때:

| 항목 | 계산 |
| --- | --- |
| Run 개수 | N = ⌈b/M⌉ |
| Merge pass 수 | ⌈log_{M-1}(N)⌉ |
| 총 pass 수 | 1(run 생성) + ⌈log_{M-1}(N)⌉(merge) |
| 총 디스크 I/O | 2b × (총 pass 수) — 각 pass마다 전체 데이터를 한번 읽고 한번 쓰므로 |

---

## 4. External Sort-Merge → Prometheus TSDB 연결

이제 이 개념들이 Prometheus TSDB compaction에 어떻게 대응되는지 정리합니다.

### 4-1. 구조적 대응 관계

| External Sort-Merge | Prometheus TSDB Compaction |
| --- | --- |
| 정렬된 Run | 각 Block (시리즈가 라벨 기준 정렬) |
| Run 내부의 Record | 개별 시리즈 (라벨셋 + 청크들) |
| 정렬 키 (sort key) | 시리즈의 라벨셋 (사전순) |
| Merge Phase | N-way merge of series from source blocks |
| Output Run (병합 결과) | 새로 생성되는 compacted Block |
| Input Buffer (Run별 1 page) | 각 Block의 인덱스 iterator |
| Output Buffer | 새 Block의 writer |

Prometheus의 각 Block은 이미 내부 인덱스에 시리즈가 라벨 기준으로 정렬되어 있습니다. 이건 External Sort-Merge의 "이미 정렬된 Run"과 완전히 같은 상태예요.

### 4-2. Compaction = Merge Phase에 해당

Prometheus에서는 Run Creation Phase가 따로 없습니다. Head block에서 2시간마다 자연스럽게 정렬된 block이 생성되기 때문이에요. 이미 만들어진 block들이 "정렬된 Run"이고, compaction은 오직 Merge Phase만 수행합니다.

```
External Sort-Merge와의 비교:

[External Sort-Merge]
  Phase 1: 정렬된 Run 생성 ← Prometheus에서는 Head compaction이 담당
  Phase 2: N-way Merge     ← Prometheus compaction이 바로 이 단계

[Prometheus]
  Head block → 2h block 생성 (= Run 생성, 자동으로 정렬됨)
  Compaction: 여러 block을 N-way merge → 더 큰 block
```

### 4-3. N-way Merge의 실제 적용

블로그의 핵심 문장:

> compaction does an N way merge of the series from the source block while iterating through series one by one in a sorted fashion
> 

compaction 대상이 N개 블록이면, 각 블록의 인덱스에서 시리즈를 하나씩 꺼내면서 라벨 순서가 가장 빠른 시리즈를 선택합니다. External Sort-Merge에서 각 Run의 buffer에서 가장 작은 record를 고르는 것과 동일한 로직이에요.

```
블록1의 시리즈 (정렬됨): [cpu{host=a}, cpu{host=b}, mem{host=a}]
블록2의 시리즈 (정렬됨): [cpu{host=a}, cpu{host=c}, mem{host=b}]
블록3의 시리즈 (정렬됨): [cpu{host=b}, cpu{host=c}, mem{host=a}]

N-way merge 과정:
Step 1: 3개 블록 헤드 비교 → cpu{host=a} 가장 작음 (블록1, 블록2 모두 보유)
        → 새 블록에 cpu{host=a} 기록, 청크는 블록1+블록2에서 가져옴
Step 2: cpu{host=b} 가장 작음 (블록1, 블록3)
        → 새 블록에 cpu{host=b} 기록
Step 3: cpu{host=c} 가장 작음 (블록2, 블록3)
        → 새 블록에 cpu{host=c} 기록
...
```

### 4-4. 같은 시리즈가 여러 블록에 있을 때: 청크 처리

여기서 External Sort-Merge와 차이가 발생합니다. 일반 External Sort-Merge는 같은 키의 레코드가 여러 Run에 없지만, Prometheus에서는 같은 시리즈가 여러 블록에 존재합니다. 이때 시간 범위에 따라 처리가 달라져요.

**Non-overlapping (시간 범위 안 겹침) → Concatenation**

```
블록1: cpu{host=a} 청크 [00:00~02:00]
블록2: cpu{host=a} 청크 [02:00~04:00]

→ 압축 해제 없이 청크를 그냥 이어붙임 (비용 ≈ 0)
```

이건 External Sort-Merge에서 서로 다른 Run의 레코드가 키 범위가 안 겹칠 때 그냥 순서대로 쓰는 것과 같습니다.

**Overlapping (시간 범위 겹침) → 샘플 레벨 Dedup Merge**

```
블록1: cpu{host=a} 청크 [00:00~03:00] 샘플: t1=10, t2=20, t3=30
블록2: cpu{host=a} 청크 [02:00~05:00] 샘플: t2=20, t3=30, t4=40

→ 겹치는 청크를 압축 해제
→ 샘플 단위 2-way merge + dedup (같은 timestamp → 1개만 유지)
→ 결과: t1=10, t2=20, t3=30, t4=40
→ 다시 압축하여 새 청크 생성 (최대 120샘플/청크)
```

이건 External Sort-Merge에서는 없는 추가 단계입니다. 시계열 DB 특화 로직이에요.

### 4-5. Merge 계층 전체 구조

```
[최상위] N-way Merge of Series (라벨 기준 정렬)
  │       ← External Sort-Merge의 Merge Phase와 동일
  │
  ├─ 같은 시리즈, 비겹침 → 청크 Concatenation
  │       ← 그냥 순서대로 이어붙이기 (무비용)
  │
  └─ 같은 시리즈, 겹침 → 청크 Decompress
                           │
                           └─ 샘플 레벨 Merge + Dedup
                               │    ← 2-way/N-way merge at sample level
                               └─ 재압축하여 새 청크 생성
```

### 4-6. Preset Time Range와 Multi-pass Merge의 유사성

External Sort-Merge에서 N ≥ M이면 여러 pass에 걸쳐 점진적으로 합치듯, Prometheus도 한번에 모든 블록을 합치지 않고 preset time range [2h, 6h, 18h, 54h, ...]를 따라 단계적으로 합칩니다.

```
[External Sort-Merge]
Pass 1: 90개 run → 10개씩 → 9개 run
Pass 2: 9개 run → 1개 run

[Prometheus Compaction]
Level 1: 2h 블록 3개 → 6h 블록 1개
Level 2: 6h 블록 3개 → 18h 블록 1개
Level 3: 18h 블록 3개 → 54h 블록 1개
...
```

차이점은 External Sort-Merge는 "메모리 제약 때문에" multi-pass를 하지만, Prometheus는 "쿼리 효율성과 compaction 비용 균형 때문에" 단계적으로 합친다는 점입니다. 메모리 제약이 아니라 설계 선택이에요.

### 4-7. 전체 비교 정리

| 항목 | External Sort-Merge | LSM-Tree (RocksDB 등) | Prometheus TSDB |
| --- | --- | --- | --- |
| 정렬 단위 | Run (메모리 크기만큼) | SSTable (key 정렬) | Block (라벨 정렬) |
| Run 생성 | Phase 1에서 명시적 | memtable flush | Head compaction (2h마다) |
| Merge 방식 | N-way (min-heap) | N-way on keys | N-way on series labels |
| Multi-pass 이유 | 메모리 부족 | Level 구조 설계 | Preset time range 설계 |
| 비겹침 처리 | 순서대로 출력 | 순서대로 출력 | 청크 concatenation |
| 겹침 처리 | 해당 없음 (키 유일) | 최신 버전만 유지 | 샘플 dedup (같은 ts → 1개) |
| 삭제 처리 | 해당 없음 | tombstone 필터링 | tombstone 범위 샘플 제외 |
| I/O 최적화 | buffer page 관리 | block cache | mmap (OS 위임) |
| 결과물 | 정렬된 최종 파일 | 새 SSTable | 새 Block |

### 4-8. 코드 위치

| 역할 | 파일 |
| --- | --- |
| Plan 생성 + Compaction 실행 | `tsdb/compact.go` |
| 청크 concatenation / 샘플 merge | `storage/merge.go` |
| Compaction 주기 트리거 + Retention | `tsdb/db.go` |