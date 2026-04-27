---
title: "WAL and Checkpoint Code Analysis"
date: 2026-03-25
last_modified_at: 2026-03-25
author: Seungjin In
---
# Week3. WAL and Checkpoint Code Analysis

[`tsdb/wlog/wlog.go`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/wlog/wlog.go)는 WAL 핵심 구현, [`tsdb/wlog/checkpoint.go`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/wlog/checkpoint.go)는 Checkpointing 로직이 구현되어 있다.

> **패키지 경로 주의:** 블로그와 일부 문서에서 `tsdb/wal/`로 언급되지만, v3.10 기준에서는 `tsdb/wlog/`로 이동되었다. `Head` struct에서도 `wal, wbl *wlog.WL`로 import하고 있다.

---

## 1. WAL의 물리적 구조: 3계층 아키텍처

WAL은 하나의 거대한 파일이 아니라, 세그먼트(128MB 파일) → 페이지(32KB 버퍼) → 레코드(실제 데이터)의 3단 구조로 되어 있다. 세그먼트로 나누면 오래된 파일만 통째로 삭제할 수 있고, 페이지 버퍼로 모아쓰기를 하면 디스크 I/O 횟수를 줄일 수 있다.

#### 1.1. 상수 정의
[`tsdb/wlog/wlog.go:40-45`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/wlog/wlog.go#L40-L45):
```go
const (
    DefaultSegmentSize = 128 * 1024 * 1024 // 128 MB.
    pageSize           = 32 * 1024         // 32KB.
    recordHeaderSize   = 7                 // 각 레코드 조각 앞에 붙는 헤더 크기
    WblDirName         = "wbl"             // Out-of-Order용 WAL 디렉토리 이름
)
```

#### 1.2. page struct — 레코드를 모아두는 32KB 버퍼
[`tsdb/wlog/wlog.go:56-76`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/wlog/wlog.go#L56-L76):
```go
type page struct {
    alloc   int            // 버퍼에서 "여기까지 채웠어"라는 위치
    flushed int            // 버퍼에서 "여기까지 디스크에 내렸어"라는 위치
    buf     [pageSize]byte // 32KB 고정 크기 배열
}

func (p *page) full() bool { return pageSize-p.alloc < recordHeaderSize }
```

`alloc`과 `flushed`가 따로 있는 이유: 버퍼에 15KB를 채웠는데(`alloc=15KB`) 급하게 디스크에 내려야 하면 15KB만 내릴 수 있다(`flushed=15KB`). 이후 레코드가 오면 15KB 위치부터 이어서 채운다. 이것이 **부분 flush**이다.

#### 1.3. Segment struct — 세그먼트 파일 하나를 나타내는 구조체
[`tsdb/wlog/wlog.go:88-92`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/wlog/wlog.go#L88-L92):
```go
type Segment struct {
    SegmentFile  // Write, Read, Close 등이 가능한 파일 인터페이스
    dir string   // 이 세그먼트가 위치한 디렉토리
    i   int      // 이 세그먼트의 번호 (0, 1, 2, ...)
}
```

[`tsdb/wlog/wlog.go:508-510`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/wlog/wlog.go#L508-L510): 파일명은 8자리 zero-padding이다.
```go
func SegmentName(dir string, i int) string {
    return filepath.Join(dir, fmt.Sprintf("%08d", i))  // 00000000, 00000001, ...
}
```

#### 1.4. WL struct — WAL 전체를 관리하는 최상위 구조체
[`tsdb/wlog/wlog.go:182-199`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/wlog/wlog.go#L182-L199):
```go
type WL struct {
    dir         string
    logger      *slog.Logger
    segmentSize int
    mtx         sync.RWMutex
    segment     *Segment           // 지금 쓰고 있는 세그먼트 (항상 1개만 유지)
    donePages   int                // 현재 세그먼트에서 다 쓴 페이지 개수
    page        *page              // 지금 채우고 있는 32KB 버퍼 (항상 1개만 유지)
    stopc       chan chan struct{}         // 종료 시그널 채널
    actorc      chan func()               // 이전 세그먼트 fsync를 비동기로 처리하는 큐
    closed      bool
    compress    compression.Type          // Snappy, Zstd, 또는 None
    cEnc        compression.EncodeBuffer  // 압축 버퍼 재사용 (매번 할당하면 GC 부하)
    WriteNotified WriteNotified           // 새 데이터 기록 시 알림 인터페이스
    metrics     *wlMetrics
}
```

#### 1.5. Record Type
[`tsdb/wlog/wlog.go:620-628`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/wlog/wlog.go#L620-L628):

레코드가 32KB 페이지보다 크면 여러 조각으로 나뉘는데, 각 조각이 "전체 중 어디에 해당하는지"를 표시하는 것이 recType이다:
```go
type recType uint8
const (
    recPageTerm recType = 0 // "이 페이지 나머지는 비어있어요" 표시
    recFull     recType = 1 // 레코드가 한 페이지에 통째로 들어감 (가장 흔한 경우)
    recFirst    recType = 2 // 큰 레코드의 첫 번째 조각
    recMiddle   recType = 3 // 큰 레코드의 중간 조각
    recLast     recType = 4 // 큰 레코드의 마지막 조각
)
```

각 레코드 조각 앞에 붙는 **7바이트 헤더 구조:**
```
┌──────────┬───────────┬──────────┬──────────────────┐
│ type(1B) │ length(2B)│ CRC32(4B)│ data (가변)      │
└──────────┴───────────┴──────────┴──────────────────┘
```

---

## 2. WAL 기록 흐름: Log() → log() → flushPage()



#### 2.1. Log() — 여러 레코드를 한꺼번에 받는 입구
[`tsdb/wlog/wlog.go:657-669`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/wlog/wlog.go#L657-L669):
```go
func (w *WL) Log(recs ...[]byte) error {
    w.mtx.Lock()
    defer w.mtx.Unlock()
    for i, r := range recs {
        if err := w.log(r, i == len(recs)-1); err != nil {
            w.metrics.writesFailed.Inc()
            return err
        }
    }
    return nil
}
```
레코드 3개를 넘기면: `log(r1, false)` → `log(r2, false)` → `log(r3, true)`. 마지막 것만 `final=true`이고, `true`일 때만 디스크에 쓴다. 덕분에 3개가 한 번의 디스크 쓰기로 처리될 수 있다(배치 효과).

#### 2.2. log() — WAL 기능. 레코드 하나를 페이지 버퍼에 채우는 함수
[`tsdb/wlog/wlog.go:675-781`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/wlog/wlog.go#L675-L781):

5단계로 동작한다:

```go
func (w *WL) log(rec []byte, final bool) error {
    // 1. 이전에 flush가 실패해서 페이지가 꽉 찬 채로 남아있으면, 먼저 처리
    if w.page.full() {
        if err := w.flushPage(true); err != nil { return err }
    }

    // 2. 레코드를 압축한다 (Snappy 또는 Zstd)
    enc, err := compression.Encode(w.compress, rec, w.cEnc)

    // 3. 현재 세그먼트에 이 레코드가 들어갈 공간이 있는지 확인한다.
    //    부족하면 레코드를 쓰기 전에 새 세그먼트를 먼저 만든다.
    left := w.page.remaining() - recordHeaderSize
    left += (pageSize - recordHeaderSize) * (w.pagesPerSegment() - w.donePages - 1)
    if len(enc) > left {
        if _, err := w.nextSegment(true); err != nil { return err }
    }

    // 4. 레코드를 페이지 크기에 맞게 잘라서 채운다.
    //    페이지에 다 들어가면 recFull, 안 들어가면 recFirst/recMiddle/recLast로 분할
    for i := 0; i == 0 || len(enc) > 0; i++ {
        p := w.page
        l := min(len(enc), (pageSize-p.alloc)-recordHeaderSize)
        part := enc[:l]
        buf := p.buf[p.alloc:]

        // 이 조각이 전체 레코드 중 어디에 해당하는지 결정
        switch {
        case i == 0 && len(part) == len(enc): typ = recFull   // 한 번에 다 들어감
        case len(part) == len(enc):           typ = recLast   // 마지막 조각
        case i == 0:                          typ = recFirst  // 첫 조각
        default:                              typ = recMiddle // 중간 조각
        }

        // 7바이트 헤더 작성 + 데이터 복사
        buf[0] = byte(typ)
        binary.BigEndian.PutUint16(buf[1:], uint16(len(part)))
        binary.BigEndian.PutUint32(buf[3:], crc32.Checksum(part, castagnoliTable))
        copy(buf[recordHeaderSize:], part)
        p.alloc += len(part) + recordHeaderSize

        // 페이지가 가득 차면 디스크에 내리고 새 페이지 시작
        if w.page.full() {
            if err := w.flushPage(true); err != nil { return err }
        }
        enc = enc[l:]
    }

    // 5. 이 레코드가 배치의 마지막이면, 페이지가 다 안 찼어도 쌓인 만큼만 디스크에 내린다
    if final && w.page.alloc > 0 {
        if err := w.flushPage(false); err != nil { return err }
    }
    return nil
}
```

#### 2.3. flushPage() — 페이지 버퍼를 디스크에 내리는 함수
[`tsdb/wlog/wlog.go:583-609`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/wlog/wlog.go#L583-L609):
```go
func (w *WL) flushPage(forceClear bool) error {
    p := w.page
    shouldClear := forceClear || p.full() //가득 차있거나, forceClear가 True

    if shouldClear {
        p.alloc = pageSize  // 나머지를 0으로 채워서 32KB를 꽉 채움
    }

    n, err := w.segment.Write(p.buf[p.flushed:p.alloc])
    if err != nil {
        p.flushed += n
        return err
    }
    p.flushed += n

    if shouldClear {
        p.reset()       // 버퍼를 0으로 초기화하고 새 페이지 시작
        w.donePages++
    }
    return nil
}
```

**`flushPage(false)` — 부분 flush:** 채운 만큼만 디스크에 내리고, 이 페이지를 계속 이어서 쓴다. 배치의 마지막 레코드일 때 호출된다.

**`flushPage(true)` — 완전 flush:** 나머지를 0으로 채워 32KB를 꽉 채운 뒤 디스크에 내리고, 이 페이지를 끝낸다. 페이지가 가득 찼을 때, 또는 세그먼트를 교체할 때 호출된다.

#### 2.4. nextSegment() — 새 세그먼트 만들기
[`tsdb/wlog/wlog.go:530-565`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/wlog/wlog.go#L530-L565):
```go
func (w *WL) nextSegment(async bool) (int, error) {
    // 현재 페이지가 반쯤 차있어도 강제로 닫는다 (세그먼트를 깔끔하게 마무리)
    if w.page.alloc > 0 {
        if err := w.flushPage(true); err != nil { return 0, err }
    }
    next, err := CreateSegment(w.Dir(), w.segment.Index()+1)
    prev := w.segment
    w.setSegment(next)

    // 이전 세그먼트의 마무리(fsync + close)를 처리
    f := func() {
        w.fsync(prev)
        prev.Close()
    }
    if async {
        w.actorc <- f  // 별도 goroutine이 나중에 처리 (다음 쓰기를 안 막기 위해)
    } else {
        f()
    }
    return next.Index(), nil
}
```

---

## 3. Checkpoint: WAL Truncation 전 데이터 보존

WAL이 계속 커지면 디스크가 부족해진다. Compaction 후 해당 시간의 WAL 데이터가 필요 없으니 삭제해야 한다. 그런데 오래된 세그먼트를 그냥 지우면, **거기에만 있는 Series 레코드**(시계열 메타데이터)가 사라진다. 크래시 복구 시 "이 ID가 어떤 시계열인지" 모르게 된다. 그래서 삭제 전에 **아직 필요한 데이터만 골라서 별도 파일(checkpoint)에 보관**한다.

#### 3.1. Checkpoint() 함수
[`tsdb/wlog/checkpoint.go:95`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/wlog/checkpoint.go#L95):

```go
func Checkpoint(logger *slog.Logger, w *WL, from, to int,
    keep func(id chunks.HeadSeriesRef) bool, mint int64) (*CheckpointStats, error) {
```

매개변수 설명:
- `from, to`: 대상 세그먼트 범위 (예: 세그먼트 100~106)
- `keep`: "이 시계열이 아직 메모리(Head)에 살아있는가?" 판단하는 함수
- `mint`: 이 시간 이전의 Samples/Tombstones는 이미 디스크 블록에 있으니 버린다

함수 내부 흐름:

```go
    // (1) 이전 checkpoint가 있으면 거기까지는 이미 처리됐으므로,
    //     WAL 세그먼트는 idx+1부터만 읽는다 (이전 checkpoint 내용은 별도로 포함)
    dir, idx, err := LastCheckpoint(w.Dir())
    if err == nil {
        from = idx + 1
    }

    // (2) 임시 이름으로 새 checkpoint 파일 생성
    cpdir := checkpointDir(w.Dir(), to)  // "checkpoint.00000106"
    cpdirtmp := cpdir + ".tmp"
    cp, _ := New(nil, nil, cpdirtmp, w.CompressionType())

    // 실패하면 .tmp 폴더를 자동으로 지운다
    defer func() { cp.Close(); os.RemoveAll(cpdirtmp) }()

    // (3) 세그먼트의 모든 레코드를 읽으면서, 필요한 것만 골라서 새 checkpoint에 기록
    for r.Next() {
        switch dec.Type(rec) {
        case record.Series:
            // keep(시계열ID)가 true인 것만 보존
        case record.Samples:
            // timestamp >= mint인 것만 보존
        case record.Tombstones:
            // 삭제 범위의 끝 >= mint인 것만 보존
        case record.Metadata:
            // 같은 시계열의 metadata는 가장 마지막 것만 남긴다
        default:
            continue  // 모르는 레코드 타입은 건너뜀
        }
    }

    // (4) 남은 레코드 기록
    cp.Log(recs...)

    // (5) .tmp → 최종 이름으로 변경 (중간에 크래시 나도 반쯤 만든 파일이 정상으로 인식되지 않는다)
    fileutil.Replace(cpdirtmp, cpdir)
```

복구 시 읽는 순서: `checkpoint.00000106 먼저 읽기 → 세그먼트 107부터 이어서 읽기`

#### 3.2. Checkpoint 네이밍
[`tsdb/wlog/checkpoint.go:397-399`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/wlog/checkpoint.go#L397-L399):
```go
func checkpointDir(dir string, i int) string {
    return filepath.Join(dir, fmt.Sprintf(CheckpointPrefix+"%08d", i))
    // 예: "data/wal/checkpoint.00000106"
}
```
이름의 숫자(106)는 "세그먼트 106까지의 데이터가 이 checkpoint에 담겨있다"는 뜻이다.

#### 3.3. Truncate() — checkpoint 생성 후 오래된 세그먼트 삭제
[`tsdb/wlog/wlog.go:800-820`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/wlog/wlog.go#L800-L820):
```go
func (w *WL) Truncate(i int) (err error) {
    refs, err := listSegments(w.Dir())
    for _, r := range refs {
        if r.index >= i { break }
        os.Remove(filepath.Join(w.Dir(), r.name))  // i보다 작은 세그먼트를 모두 삭제
    }
    return nil
}
```

---

## 참고 문헌

- [Ganesh Vernekar - Prometheus TSDB (Part 2): WAL and Checkpoint](https://ganeshvernekar.com/blog/prometheus-tsdb-wal-and-checkpoint/)
- [Prometheus GitHub (release-3.10)](https://github.com/prometheus/prometheus/tree/release-3.10)
  - [`tsdb/wlog/wlog.go`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/wlog/wlog.go) — WAL 핵심 구현
  - [`tsdb/wlog/checkpoint.go`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/wlog/checkpoint.go) — Checkpointing 로직
- [LevelDB Log Format](https://github.com/google/leveldb/blob/main/doc/log_format.md) — record format의 원류

---
author: [Seungjin In](https://github.com/m1cks)