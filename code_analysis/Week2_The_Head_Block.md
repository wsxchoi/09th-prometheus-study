---
title: "The Head Block Code Analysis"
date: 2026-03-15
last_modified_at: 2026-03-15
author: Byeonggyu Park
---
# Week2. The Head Block Code Analysis

[Blog](https://ganeshvernekar.com/blog/prometheus-tsdb-the-head-block/) 내용 기반으로 교차검증하면서 작성했으며, [`tsdb/db.go`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/db.go)는 TSDB의 전체적인 동작들, [`tsdb/head.go`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/head.go) 및 [`tsdb/head_append.go`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/head_append.go)는 sample들의 In-Memory Chunk, WAL, Memory Mapping 관련 로직이 구현되어 있습니다.

---

## 1. Head block은 In-Memory, Grey blocks는 Immutable Disk Blocks

### Blog
> Head block is the in-memory part of the database and the grey blocks are persistent blocks on disk which are immutable.

### Code
#### 1.1. DB struct
[`tsdb/db.go:272-291`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/db.go#L272-L291): `DB struct`는 `head *Head`(In-Memory)와 `blocks []*Block`(Disk) 등을 갖고 있습니다.
```go
type DB struct {
    blocks []*Block    // Disk Immutable Blocks
    head   *Head       // In-Memory Head Block
    // ...
}
```

#### 1.2. Head struct
[`tsdb/head.go:68-145`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/head.go#L68-L145): `Head struct`는 In-Memory Series(`*stripeSeries`), Inverted-Index(`*index.MemPostings`) 등을 갖고 있습니다.
```go
type Head struct {
    series   *stripeSeries          // In-Memory Series
    postings *index.MemPostings     // In-Memory Inverted-Index
    wal, wbl *wlog.WL               // Write-Ahead-Log
    // ...
}
```

#### 1.3. Disk Block Directory structure
[`tsdb/block.go:339`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/block.go#L339): Disk Block을 읽기 위해 directory에 접근함을 알 수 있습니다. 단일 Block이 가지는 파일들은 아래와 같습니다.
```
data/
└── 01BKGV7JBM69T2G1BGBGM6KB12/ (ULID)
    ├── meta.json
    ├── index
    ├── chunks/
    │   └── 000001
    └── tombstones
```

---

## 2. WAL을 통한 Durability 보장

### Blog
> We have a Write-Ahead-Log (WAL) for durable writes. An incoming sample first goes into the Head block... while committing the sample into the chunk, we also record it in the WAL on disk for durability.

### Code
#### 2.1. WAL 기록 시점
[`tsdb/head_append.go:1705-1728`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/head_append.go#L1705-L1728): **WAL 기록이 In-Memory commit보다 먼저** 수행됩니다. 즉, Disk WAL 기록을 실패하면 In-Memory 연산들도 rollback됩니다.
```go
// Commit writes to the WAL and adds the data to the Head.
func (a *headAppenderBase) Commit() (err error) {
    // (1) 먼저 WAL에 기록
    if err := a.log(); err != nil {
        _ = a.Rollback() // 실패 시 rollback
        return fmt.Errorf("write to WAL: %w", err)
    }
    // (2) 그 다음 In-Memory에 commit
    a.commitFloats(b, acc)
    a.commitHistograms(b, acc)
    // ...
}
```
만약 commit이 먼저 수행된다면, WAL 기록 실패 시 복구가 어려워질 것입니다.  

#### 2.2. WBL과 OOO(Out of Order)
[`tsdb/head.go:84`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/head.go#L84): 위의 `Head struct`를 보셨다 싶이, wal과 같은 포인터가 하나 더(`wal, wbl *wlog.WL`)있습니다. `wbl`은 OOO(Out of Order) Sample들만을 기록합니다.  

Out of Order란, timestamp 순서를 벗어난 Sample이 들어오는 경우를 의미합니다. 서로 다른 서버에서 `t=10`과 `t=9` sample을 보냈는데, `t=9`를 보낸 서버가 네트워크 지연으로 인해 더 늦게 들어올 수 있습니다.
Sample이 OOO인지 판단하는 로직은 [`tsdb/head_append.go:643`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/head_append.go#L643)에 있습니다.

In Order Sample과 Out of Order Sample은 compaction 로직 및 replay 로직이 다르기 때문에, wal과 wbl를 분리합니다. 또한 위에서 봤던 `Commit()`함수에서 WBL 기록은 In-Memory commit 후에 실행됩니다. 
```go
// Commit writes to the WAL and adds the data to the Head.
func (a *headAppenderBase) Commit() (err error) {
    h := a.head
    // (1) 먼저 WAL에 기록
    if err := a.log(); err != nil {
        _ = a.Rollback() // 실패 시 rollback
        return fmt.Errorf("write to WAL: %w", err)
    }
    // (2) 그 다음 In-Memory에 commit
    a.commitFloats(b, acc)
    a.commitHistograms(b, acc)
    // (3) OOO record 수집 후 WBL에 기록
    acc.collectOOORecords(a)
    if h.wbl != nil {
        if err := h.wbl.Log(acc.oooRecords...); err != nil {
            // TODO(codesome): Currently WBL logging of ooo samples is best effort here since we cannot try logging
            // until we have found what samples become OOO. We can try having a metric for this failure.
            // Returning the error here is not correct because we have already put the samples into the memory,
            // hence the append/insert was a success.
            h.logger.Error("Failed to log out of order samples into the WAL", "err", err)
        }
    }
}
```
이는 명백한 Atomicity 위배(WBL 기록이 실패해도 OOO Sample들은 In-Memory에 commit)입니다. Code Owner도 이를 알고 있어 `//TODO` 주석으로 남겼습니다.  
* `//TODO` 의역: OOO 판별 자체는 `Append()` 단계의 `appendable()`에서 이미 수행되지만, WBL에 기록할 OOO record의 수집은 `commitFloats()` 내부에서 OOO sample을 실제 insert하면서(`wblSamples`에 추가) 이루어집니다. 따라서 WBL 기록은 구조적으로 In-Memory commit 이후에만 가능합니다. 그렇기에 best-effort로 log만 남기고 있고, metric을 추가하는게 어떻냐는 제안을 하고 있습니다.

---

## 3. Chunk Size: 120 Sample or 2h chunkRange

### Blog
> Once the chunk fills till 120 samples (or) spans up to chunk/block range (chunkRange, 2h by default), a new chunk is cut.

### Code
#### 3.1. Chunk Cut 로직
[`tsdb/head.go:208-209`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/head.go#L208-L209): 기본값으로 최대 120 Sample/Chunk가 정의되어 있습니다.
```go
// DefaultSamplesPerChunk provides a default target number of samples per chunk.
DefaultSamplesPerChunk = 120
```

[`tsdb/head_append.go:2030-2033`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/head_append.go#L2030-L2033): Chunk cutting 조건 로직은 아래와 같습니다.
```go
if t >= s.nextAt || numSamples >= o.samplesPerChunk*2 {
    c = s.cutNewHeadChunk(t, e, o.chunkRange)
    chunkCreated = true
}
```
* `t >= s.nextAt`: **Primary 조건**. 시간 기반으로 예측된 Chunk 종료 시점을 넘었을 때 cut합니다. `s.nextAt`은 초기에는 `chunkRange` 경계로 설정되고, 25% 지점에서 `computeChunkEndTime()`으로 재조정됩니다.
* `numSamples >= o.samplesPerChunk*2`: **Safety net**. 120이 아닌 240인 이유는, 시간 기반 예측(`s.nextAt`)이 주된 cut 메커니즘이기 때문입니다. Sample rate가 급격히 변해 예측이 빗나갔을 때만 작동하는 안전장치이며, 120으로 하면 예측 로직이 `s.nextAt`을 재조정하기도 전에 sample 수만으로 잘려버립니다.

#### 3.2. 추가적인 예측 기반 Chunk Cut 로직
[`tsdb/head_append.go:2022-2023`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/head_append.go#L2022-L2023): 블로그에서는 "120 Sample" 또는 "chunkRange 도달" 두 조건만 언급했지만, 실제 코드에는 **동적 예측 로직**도 있습니다.  
최대 Sample의 25%가 채워졌을 때 `computeChunkEndTime()`으로 끝 시간을 재계산하여 Chunk들이 시간 기준으로 조금 더 균등하게 분배되도록 합니다.
```go
// start=c.minTime, cur=c.maxTime, maxT=s.nextAt, ratioToFull=4 (25%만 채워진 상태)
// 현재 속도로 채워지면 s.nextAt까지 chunk가 몇 개 필요한가? = n
// n <= 1이면 현재 chunk 하나로 충분하므로 그대로, n >= 2이면 nextAt을 앞당겨 균등 분배
func computeChunkEndTime(start, cur, maxT int64, ratioToFull float64) int64 {
	n := float64(maxT-start) / (float64(cur-start+1) * ratioToFull)
	if n <= 1 {
		return maxT
	}
	return int64(float64(start) + float64(maxT-start)/math.Floor(n))
}
// 25%가 채워지면, s.nextAt을 재계산해 시간 기반으로 균등하게 분포하는 효과
if numSamples == o.samplesPerChunk/4 {
    s.nextAt = computeChunkEndTime(c.minTime, c.maxTime, s.nextAt, 4)
}
```
예를 들어, 120 Sample이 들어오는 시간이 30min이고 그 속도가 들쑥날쑥하다면, 항상 (120 Sample) * (4 Chunk)로 cutting하지 않고 최대한 시간 기반(0m-60m, 60m-120m, 120m-180m, 180m-240m에 근접)으로 cutting할 수 있도록 재조정합니다.

---

## 4. Chunk Memory-Mapping

### Blog
> Since Prometheus v2.19.0, we are not storing all the chunks in the memory. As soon as a new chunk is cut, the full chunk is flushed to the disk and memory-mapped from the disk while only storing a reference in the memory.

### Code

[`tsdb/head_append.go:2207`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/head_append.go#L2207): `mmapChunks()` 함수는 현재 Head Chunk을 제외한 모든 이전 Chunk를 Disk에 쓰고 reference를 유지합니다.
```go
func (s *memSeries) mmapChunks(chunkDiskMapper *chunks.ChunkDiskMapper) (count int) {
    if s.headChunks == nil || s.headChunks.prev == nil {
        return count  // Head Chunk 하나뿐이면 m-map 할 것 없음
    }
    // 가장 오래된 것부터 현재 Head Chunk 직전까지 Disk에 기록
    for i := s.headChunks.len() - 1; i > 0; i-- {
        chk := s.headChunks.atOffset(i)
        chunkRef := chunkDiskMapper.WriteChunk(s.ref, chk.minTime, chk.maxTime, chk.chunk, false, handleChunkWriteError)
        s.mmappedChunks = append(s.mmappedChunks, &mmappedChunk{
            ref: chunkRef,  // Disk refer만 In-Memory 저장
            // ...
        })
    }
    s.headChunks.prev = nil  // 이전 Chunk link 해제 (GC 대상)
}
```

[`tsdb/head.go:1891-1903`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/head.go#L1891-L1903): `Head.mmapHeadChunks()`가 모든 Series를 순회하며 m-map을 수행합니다.
```go
func (h *Head) mmapHeadChunks() {
    var count int
    for i := 0; i < h.series.size; i++ {
        h.series.locks[i].RLock()  // stripe 단위 RLock (series 추가/삭제 방지)
        for _, series := range h.series.series[i] {
            series.Lock()
            count += series.mmapChunks(h.chunkDiskMapper)
            series.Unlock()
        }
        h.series.locks[i].RUnlock()
    }
}
```

[`tsdb/db.go:1189`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/db.go#L1189): m-mapping이 **주기적으로(Default: 1min)** `db.run()` 루프에서 호출됩니다.
```go
for { // infinite loop
    // ...
    select {
    case <-time.After(db.opts.BlockReloadInterval): // Default 1min
        // ...
        db.head.mmapHeadChunks()
        // ...
    }
    // ...
}
```
즉, 블로그 글에서의 new chunk cut 이벤트와는 관계 없이, 1분마다 주기적으로 Head Chunk가 아닌 Chunk들을 확인해 m-mapping합니다.

---

## 5. Compaction trigger: chunkRange * 3/2

### Blog
> When the data in the Head spans chunkRange*3/2, the first chunkRange of data (2h here) is compacted into a persistent block.

### Code

[`tsdb/head.go:1792-1801`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/head.go#L1792-L1801): `compactable()`에서 기본 `chunkRange=2h`이므로 `3h`의 데이터가 쌓이면 compaction이 trigger됩니다.
```go
// The head has a compactable range when the head time range is 1.5 times the chunk range.
// The 0.5 acts as a buffer of the appendable window.
func (h *Head) compactable() bool {
    if !h.initialized() {
        return false
    }
    return h.MaxTime()-h.MinTime() > h.chunkRange.Load()/2*3
}
```

---

## 6. Inverted-Index in Head

### Blog
> It (the index) is in the memory and stored as an inverted index.

### Code
[`tsdb/head.go:117`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/head.go#L117): `Head struct`에서 `postings *index.MemPostings`이 Inverted-Index입니다.

[`tsdb/index/postings.go:60-79`](https://github.com/prometheus/prometheus/blob/release-3.10/tsdb/index/postings.go#L60-L79): `MemPostings struct`가 정의되어 있습니다.
```go
// MemPostings holds postings list for series ID per label pair.
// They may be written to out of order.
type MemPostings struct {
    mtx     sync.RWMutex
    m       map[string]map[string][]storage.SeriesRef // Inverted-Index 자료구조
    //          ^name      ^value   ^Series ID list
    // e.g. m["method"]["GET"] = [1, 3, 5]
    //      m["status"]["200"] = [1, 2]
    ordered bool
}
```
`m`은 `map[labelName]map[labelValue][]SeriesRef` 타입의 **2-depth nested map**입니다. 첫 번째 key가 Label Name, 두 번째 key가 Label Value이며, value가 해당 Label Pair를 가진 Series ID 목록입니다.  

`Add()`는 Label Set의 각 Label Pair에 대해 Series ID를 Posting List에 추가합니다.
```go
func (p *MemPostings) Add(id storage.SeriesRef, lset labels.Labels) {
    p.mtx.Lock()
    lset.Range(func(l labels.Label) {
        p.addFor(id, l)
        // e.g. p.m["method"]["GET"] = append(..., id)
        //      p.m["status"]["200"] = append(..., id)
    })
    p.addFor(id, allPostingsKey)
    p.mtx.Unlock()
}
```
`index.MemPostings` 자료구조를 구축/업데이트함으로서, 이후 어떤 Label 조합으로 쿼리하더라도 만족하는 Series를 빠르게 찾을 수 있습니다.

번외: 현재 `MemPostings.addFor()`에서 Data Race(Lock 관련 문제)가 발생하는 Bug가 있습니다.
```go
// BUG: There's currently a data race in addFor, which might modify the tail of the postings list:
// https://github.com/prometheus/prometheus/issues/15317
m map[string]map[string][]storage.SeriesRef
```

---
author: [Byeonggyu Park](https://github.com/ggyuchive)
