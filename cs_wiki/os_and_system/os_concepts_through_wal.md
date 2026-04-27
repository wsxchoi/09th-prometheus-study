# 운영체제 핵심 개념 5가지를, Prometheus WAL 코드 한 줄 한 줄로 이해하기

> Sequential I/O, Random I/O, Write Amplification, Page, Atomic Operation — 교과서에서 따로 배우면 와닿지 않는 개념들이다. 이 글에서는 **Prometheus TSDB의 WAL(Write-Ahead Log) 코드**를 따라가면서, 이 5가지 개념이 실제 프로덕션 시스템에서 어떻게, 왜 쓰이는지를 살펴본다.
> 

---

## 전체 흐름: 샘플 하나가 디스크에 기록되기까지

Prometheus는 모니터링 시스템이다. 매 초 수만 개의 시계열 샘플이 쏟아져 들어온다. 이 데이터는 메모리(Head block)에 저장되는데, 프로세스가 죽으면 메모리 데이터는 전부 사라진다. 그래서 메모리에 쓰기 **전에** 먼저 디스크의 WAL에 기록해둔다. 크래시가 나면 WAL을 리플레이해서 메모리 상태를 복원하는 것이다. 

이 WAL의 코드를 따라가면, 운영체제의 핵심 개념들이 하나씩 자연스럽게 등장한다.

![image.png](attachment:81676f4b-e4fc-4390-8bcc-df6bf442297b:image.png)

분석 대상 코드:

- [`wal/wal.go`](https://github.com/prometheus-junkyard/tsdb/blob/master/wal/wal.go) — WAL 핵심 구현
- [`chunks/chunks.go`](https://github.com/prometheus-junkyard/tsdb/blob/master/chunks/chunks.go) — 청크 랜덤 읽기
- [`checkpoint.go`](https://github.com/prometheus-junkyard/tsdb/blob/master/checkpoint.go) — 체크포인트 생성

---

## 1. Sequential I/O vs Random I/O — WAL이 존재하는 이유

### 개념

**Sequential I/O(순차 I/O)** 는 디스크의 연속된 위치에 데이터를 순서대로 읽거나 쓰는 것이다. **Random I/O(랜덤 I/O)** 는 디스크의 여기저기 떨어진 위치를 왔다 갔다 하면서 접근하는 것이다.

HDD에서 이 차이는 극적이다. HDD의 읽기/쓰기 헤드는 물리적으로 움직여야 하므로, 떨어진 위치를 접근할 때마다 **seek time**(탐색 시간)이 발생한다. 순차 접근은 헤드가 한 방향으로만 이동하므로 seek이 거의 없다. HDD에서 순차 I/O와 랜덤 I/O의 처리량 차이는 **수십~수백 배**에 달한다.

SSD는 기계적 동작이 없어서 차이가 줄어들지만, 여전히 순차 I/O가 유리하다. SSD의 내부 구조(FTL, 가비지 컬렉션, 프리패칭)가 순차 패턴에 최적화되어 있고, OS의 I/O 스케줄러나 페이지 캐시도 순차 접근 시 read-ahead를 적극적으로 활용하기 때문이다.

|  | Sequential I/O | Random I/O |
| --- | --- | --- |
| 접근 패턴 | 연속된 주소를 순서대로 | 떨어진 주소를 왔다 갔다 |
| HDD 성능 | 100~200 MB/s | 0.5~2 MB/s |
| SSD 성능 (NVMe PCIe 3.0) | ~3,500 MB/s | 100~200 MB/s (4K QD1) |

---

## 2. Page (페이지) — 왜 32KB 단위로 쓰는가

### 개념

**페이지(Page)** 는 시스템이 데이터를 관리하는 고정 크기 단위다. 이 개념은 여러 계층에 걸쳐 존재한다:

- **가상 메모리 페이지**: OS가 메모리를 관리하는 기본 단위. 대부분의 시스템에서 **4KB**. 프로세스가 메모리에 접근하면 CPU 안에 내장된 하드웨어 유닛인 **MMU(Memory Management Unit)** 가 가상 주소를 물리 주소로 변환하는데, 이 변환 단위가 페이지다.
- 예를 들어 크롬과 카카오톡이 동시에 "나는 주소 0x1000을 쓸게"라고 해도, MMU가 크롬의 0x1000은 물리 메모리 0x9F000으로, 카카오의 0x1000은 0x3A000으로 각각 다르게 변환해서 충돌을 막는다.
    
    ![image.png](attachment:1f3d68e7-e379-4933-8ed5-015db17b6c7d:image.png)
    
- **파일시스템 블록**: ext4, XFS 등이 디스크를 관리하는 단위. 보통 **4KB**.
- **디스크 섹터**: 물리적 디스크의 최소 I/O 단위. 최신 디스크는 **4KB** (Advanced Format).

Prometheus WAL은 여기에 **애플리케이션 레벨 페이지**를 하나 더 추가한다: **32KB**.

![image.png](attachment:69245a9e-3c96-4e32-8aab-1bf366caabc5:image.png)

### 32KB는 어디서 왔는가

이 숫자는 Prometheus가 독자적으로 설계한 것이 아니다. Prometheus 공식 포맷 문서(`tsdb/docs/format/wal.md`)에 명시되어 있듯이, [**"페이지 인코딩 방식은 LevelDB/RocksDB의 WAL에서 가져왔다"**](https://github.com/prometheus/prometheus/blob/main/tsdb/docs/format/wal.md#segment-encoding). 즉 32KB라는 수치는 Google이 LevelDB를 설계할 때 결정한 것이고, Prometheus는 이를 그대로 채택했다.

https://github.com/facebook/rocksdb/wiki/Write-Ahead-Log-File-Format

LevelDB가 블록 기반 포맷을 택한 이유는 LevelDB 공식 문서에 명시되어 있다:

1. **Resync가 단순해짐**: 크래시 후 WAL을 리플레이할 때, 중간에 깨진 레코드를 만나면 "다음 32KB 블록 경계로 점프"하면 그게 곧 유효한 시작점이다. 블록이 없으면 깨진 지점 이후 어디서부터 다시 읽어야 할지 바이트 단위로 스캔하며 추측해야 한다.
2. **Approximate boundary splitting이 쉬움**: MapReduce처럼 파일을 대략적인 경계로 나눌 때, 블록 경계를 찾으면 된다.
3. **큰 레코드에 별도 버퍼 불필요**: 레코드를 조각내서 쓰기 때문에 큰 데이터를 메모리에 통째로 올릴 필요가 없다.

핵심은 write amplification 최소화가 아니라, **크래시 복구 경계를 수학적으로 명확하게 만드는 것**이다. Write amplification은 이 설계의 부작용으로 발생한다.

### 코드에서 확인

```go
const (
    DefaultSegmentSize = 128 * 1024 * 1024 // 128MB
    pageSize           = 32 * 1024         // 32KB
    recordHeaderSize   = 7                 // 각 레코드 조각의 헤더 크기
)
```

레코드 헤더 7바이트의 구조는 다음과 같다:

![image.png](attachment:a1af0c51-4f4b-46ea-9c36-a8a80d9e9639:image.png)

여기서 **CRC32** 는 데이터 무결성 검증용이다. 레코드를 쓸 때 data를 CRC32로 해싱해서 헤더에 저장하고, 읽을 때 다시 해싱해서 비교한다. 값이 다르면 크래시로 인해 절반만 쓰인 것이다 — 이렇게 "깨진 레코드"를 감지한다.

레코드 타입:

```go
const (
    recPageTerm recType = 0 // 페이지의 나머지가 비어있음 (패딩)
    recFull     recType = 1 // 완전한 레코드
    recFirst    recType = 2 // 레코드의 첫 번째 조각
    recMiddle   recType = 3 // 레코드의 중간 조각
    recLast     recType = 4 // 레코드의 마지막 조각
)
```

---

## 3. Write Amplification (쓰기 증폭) — 왜 데이터가 부풀어나는가

### 개념

**Write Amplification(쓰기 증폭)** 이란, 쓰고자 하는 논리적 데이터양보다 실제로 디스크에 쓰이는 물리적 데이터양이 더 많아지는 현상이다.

```
Write Amplification Factor (WAF) = 실제 디스크에 쓴 양 / 쓰려고 했던 논리적 양
```

### 코드에서 확인 — 페이지 패딩

WAL에서 write amplification이 발생하는 대표적인 지점은 **페이지 패딩**이다. 예를 들어 5KB 레코드를 쓴 직후 새 세그먼트로 넘어가야 한다면 남은 27KB가 0으로 채워진다:

```go
func (w *WAL) flushPage(clear bool) error {
    if clear {
        p.alloc = pageSize // 남은 공간을 0 패딩으로 채움
    }
    n, err := w.segment.Write(p.buf[p.flushed:p.alloc])
    // ...
}
```

![image.png](attachment:2f16ece1-6665-4c92-937f-ebdfaea9ca34:image.png)

### Snappy 압축으로 WAF 완화

Prometheus는 **Snappy 압축**을 기본 활성화한다. Snappy는 Google이 만든 압축 알고리즘으로, gzip처럼 압축률을 극대화하는 것이 목표가 아니라 **"빠른 속도 + 적당한 압축률"** 이 목표다. Prometheus처럼 매 초 수만 개 샘플이 쏟아지는 환경에서 느린 압축은 ingest 속도를 떨어뜨리기 때문이다.

Snappy가 WAF를 완화하는 원리를 수치로 보면:

![image.png](attachment:c9940389-a9d7-40ef-bfa1-bf61cc6acaa5:image.png)

레코드 크기가 줄면 같은 페이지에 더 많은 레코드가 들어가고, 자투리 패딩이 줄어 WAF가 낮아진다. 압축이 write amplification을 직접 해결하는 것이 아니라 간접적으로 완화하는 것이다.

```go
if w.compress && len(rec) > 0 {
    w.snappyBuf = snappy.Encode(w.snappyBuf, rec)
    // 압축 결과가 원본보다 작을 때만 압축본을 사용
    if len(w.snappyBuf) < len(rec) {
        rec = w.snappyBuf
        compressed = true
    }
}
```

### Write Amplification의 3개 레이어

WAL의 패딩은 애플리케이션 레벨의 write amplification일 뿐이다. 실제로는 하위 레이어에서도 각각 증폭이 발생하고, 이들은 **곱연산**으로 누적된다:

![image.png](attachment:5f3f57eb-a562-41b2-afd3-d0e6b216cd8e:image.png)

---

## 4. Atomic Operation (원자적 연산) — Checkpoint의 rename 패턴

### 개념

**Atomic Operation(원자적 연산)** 이란, 완전히 수행되거나 전혀 수행되지 않는 연산이다. 중간 상태가 외부에 노출되지 않는다.

파일시스템에서는 `rename()` 시스템 콜이 대표적인 원자적 연산이다. POSIX 표준에 의해, 같은 파일시스템 내에서 `rename()`은 원자적으로 보장된다.

### 코드에서 확인 — tmp → rename 패턴

```go
func Checkpoint(w *wal.WAL, from, to int, ...) (*CheckpointStats, error) {
    cpdir    := filepath.Join(w.Dir(), fmt.Sprintf(checkpointPrefix+"%06d", to))
    cpdirtmp := cpdir + ".tmp"

    // [1] 임시 디렉토리에 먼저 기록
    os.MkdirAll(cpdirtmp, 0777)
    cp, _ := wal.New(nil, nil, cpdirtmp, ...)

    // [2] 실패 시 .tmp 잔해 정리
    defer os.RemoveAll(cpdirtmp)

    // [3] WAL 세그먼트 순회하며 필요한 데이터만 필터링해서 기록
    // ...

    // [4] ★ 원자적 rename: checkpoint.000003.tmp → checkpoint.000003
    fileutil.Replace(cpdirtmp, cpdir)
}
```

크래시 시나리오별 동작:

```
[시나리오 A] 기록 중 크래시:
  .tmp만 남음 → 재시작 시 .tmp는 무시/삭제 → 이전 checkpoint부터 정상 리플레이

[시나리오 B] rename 직후 크래시:
  checkpoint.000003 정상 존재 → 데이터 유실 없음
```

### 왜 이름에 세그먼트 번호가 필요한가

rename 자체는 원자적이지만, "checkpoint 완성 → 오래된 WAL 세그먼트 삭제" 두 작업을 묶어서 원자적으로 만드는 것은 파일시스템 수준에서 불가능하다. 그래서 디렉토리 이름 자체에 번호를 박아서 "이 checkpoint가 어디까지 커버하는가"를 명시한다:

```
data/wal/
├── checkpoint.000003/   ← "세그먼트 0~3의 내용이 여기 있다"
├── 000004               ← 리플레이는 여기서부터
└── 000005
```

복합 작업을 원자적으로 만드는 대신 **멱등적(idempotent) 복구**로 문제를 해결한 설계 선택이다.

---

## 마무리: 이 패턴은 Prometheus만의 것이 아니다

여기서 살펴본 5가지 개념의 조합은 Prometheus WAL에만 있는 특수한 설계가 아니다. **데이터를 안전하고 빠르게 저장해야 하는 시스템이라면 거의 필연적으로 같은 선택에 도달한다:**

| 시스템 | Sequential I/O | Page 단위 | Write Amplification 완화 | Atomic 교체 |
| --- | --- | --- | --- | --- |
| **Prometheus WAL** | O_APPEND | 32KB (from LevelDB) | Snappy 압축 | tmp → rename |
| **Kafka** | segment 파일에 append | OS page cache 배치 | 압축 코덱(lz4, zstd) | atomic offset commit |
| **RocksDB WAL** | append-only log | 32KB (LevelDB 계승) | compression + compaction | MANIFEST rename |
| **PostgreSQL WAL** | pg_wal에 순차 기록 | 8KB page | full_page_writes 옵션 | checkpoint + fsync |

Sequential I/O를 위해 append-only를 선택하면, 효율적인 쓰기를 위해 Page 단위 배칭이 필요하다. Page 단위 배칭은 필연적으로 Write Amplification을 유발하고, 이 모든 것이 크래시로부터 안전하려면 Atomic Operation이 필요하다. 각 개념이 서로의 존재 이유가 되는 구조다.

운영체제 개념은 교과서에서 개별적으로 배우면 추상적이지만, 하나의 프로덕션 코드를 따라가면 "왜 이 개념이 필요한가"가 명확해진다. Prometheus WAL은 이 5가지를 약 850줄의 Go 코드 안에 전부 녹여낸, 교과서적이면서도 실용적인 구현이다.

---

**참고 코드 및 문서**

- [wal/wal.go (prometheus-junkyard/tsdb)](https://github.com/prometheus-junkyard/tsdb/blob/master/wal/wal.go)
- [chunks/chunks.go (prometheus-junkyard/tsdb)](https://github.com/prometheus-junkyard/tsdb/blob/master/chunks/chunks.go)
- [checkpoint.go (prometheus-junkyard/tsdb)](https://github.com/prometheus-junkyard/tsdb/blob/master/checkpoint.go)
- [Prometheus TSDB Blog Series by Ganesh Vernekar](https://ganeshvernekar.com/blog/prometheus-tsdb-wal-and-checkpoint/)
- [tsdb/docs/format/wal.md (prometheus/prometheus)](https://github.com/prometheus/prometheus/blob/main/tsdb/docs/format/wal.md)
- [doc/log_format.md (google/leveldb)](https://github.com/google/leveldb/blob/main/doc/log_format.md)
