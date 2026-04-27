# WAL(Write-Ahead Log)에 쓰이는 OS 개념

> https://ganeshvernekar.com/blog/prometheus-tsdb-wal-and-checkpoint/ 와 Prometheus TSDB의 `tsdb/wlog/wlog.go`를 기반으로 정리

> 참고: 아래의 설명에서 page라는 단어가 여러 의미로 쓰인다. WAL에서의 page, SSD에서의 page, OS에서의 page가 있는데 이름만 같은 별개의 개념이다.

---

## 1. Sequential I/O

### **개념**

디스크에 데이터를 쓸 때, 주소가 연속된 위치에 순서대로 쓰는 방식.
HDD인 경우 물리적 헤드 이동이 없어 빠르고, SSD를 쓰는 경우도 내부 플래시 특성상 Sequential I/O가 Random I/O보다 효율적이다.

### **WAL에서의 Sequential I/O**

WAL은 append-only 구조다. 새 레코드는 항상 현재 세그먼트 파일의 끝에 추가된다. 기존 데이터를 덮어쓰거나 중간에 끼워 넣는 일이 없다.

```
segment 00000000  [rec][rec][rec]...  → append
```

인메모리 head block을 수정할 때는 랜덤 쓰기가 발생하지만, WAL의 append-only 구조 덕분에 디스크에는 순차 쓰기만 일어난다.

---

## 2. Page

**Page 개념**

`tsdb/wlog/wlog.go`의 `page`는 32KiB의 고정 크기를 가지는 **논리적 쓰기 배치 단위**다. (가상 메모리의 Page와는 직접적인 연관이 없지만, 비슷한 개념이라 이름을 차용한듯하다.) WAL에서의 쓰기 작업은 Page 단위로 이루어진다.

**Page 구현**

```go
const pageSize = 32 * 1024  // 32KiB

type page struct {
    alloc   int
    flushed int
    buf     [pageSize]byte
}

```
레코드는 인코딩 된 후 buf에 저장된다.

---

## 3. Page Cache

WAL에도 Page Cache 개념이 쓰인다.

### **배경지식**

**Segment**

```
data
└── wal
    ├── checkpoint.000003
    |   ├── 000000
    |   └── 000001
    ├── 000004
    └── 000005
```
WAL의 폴더 구조에서 `000005` 같은 파일을 세그먼트라고 부른다.

```
Segment 파일 (128MB)
├── page 0  (32KiB)  ← page.buf가 가득 차서 flush됨
├── page 1  (32KiB)
├── page 2  (32KiB)
└── ...
```
하나의 segment는 앞서말한 page로 구성된다.

**Record**

한 번의 scrape으로 얻은 데이터는 여러 범주의 record로 가공된다. (series record, sample record 등등의 record가 하나씩 만들어진다) 이 가공된 record들은 바이트로 인코딩되어 WAL에 쓰인다.

### **page(논리적 쓰기 배치) -> Page Cache**

page의 buf가 32KiB로 다 찼을 때, 바로 디스크에 쓰이지 않는다. 먼저 Page Cache에 기록된다. 
32KiB가 다 찼을 때 `flushPage()`가 호출된다. `flushPage()`가 호출되면 내부의 `segment.Write()`를 통해 `page.buf`의 내용이 커널 메모리의 page cache에 기록된다. 디스크까지 내려가려면 추가로 `fsync()`가 필요하다.

```
page.buf  →(Write())→  OS page cache  →(fsync())→  Storage
 프로세스 메모리         커널 메모리              영속 저장소
```

**flushPage() 호출 조건**

`flushPage()`는 page가 꽉 찼을 때 호출되지만, 다른 경우에도 호출된다. 

* 조건 1: 루프 진입 전 — 이전에 page가 꽉 찬 채로 남아있을 때
* 조건 2: 루프 내부 — page에 데이터를 채운 직후 꽉 찼을 때
* 조건 3: 배치의 마지막 레코드일 때 — 덜 찬 page도 강제 flush

조건 3 때문에 마지막 레코드를 쓸 때, page가 덜 차있어도 무조건 `flushPage()`를 호출한다. `Commit()` 한 번이 완료되면 해당 배치의 모든 데이터는 page cache까지 내려간 것이 보장된다.

**디스크에 실제 쓰기가 이루어지는 두 경로**

1. **OS writeback (자동)**: 커널이 dirty page를 주기적으로 또는 메모리 압박 시 자동으로 디스크에 내려쓴다. 언제 내려갈지는 프로그램이 제어하지 않는다.

2. **fsync() (명시적)**: `segment.Sync()` → `syscall.Fsync(fd)` 커널 page cache에 있는 해당 파일의 데이터를 즉시 디스크에 쓰도록 강제한다. Prometheus에서는 세그먼트가 교체될 때 이전 세그먼트에 대해 호출된다(`wlog.go:552`).

**데이터 위치에 따른 안전 보장 범위**

| 상황 | 데이터 위치 | process crash | 전원 차단 |
|---|---|---|---|
| page.buf에만 있음 | 프로세스 메모리 | ❌ 소멸 | ❌ 소멸 |
| flushPage() -> segment.Write() 후 | page cache | ✅ 안전 | ❌ 소멸 |
| fsync() 후 | Storage | ✅ 안전 | ✅ 안전 |

`segment.Write()`는 `write()` syscall을 통해 데이터를 프로세스 메모리에서 커널의 page cache로 복사한다. 이 시점부터는 프로세스가 죽어도 커널이 살아있는 한 데이터는 유지된다.

Prometheus의 WAL 전략은 process crash 상황에서 데이터 유실을 거의 방지한다. 하지만 전원 차단은 보장하지 않는다. 전원 차단까지 보장하려면 매 write마다 fsync가 필요한데, 성능이 너무 떨어진다.

---

## 4. Atomic Operation (원자적 연산)

중간 상태 없이 완전히 완료되거나 완전히 실패하는 연산. 중간에 중단되어도 시스템이 일관된 상태를 유지해야 한다.

**WAL에서의 Atomic Operation**

WAL에 쓰이는 레코드가 page 크기인 32KiB를 넘을 경우 여러 page에 나뉘어 저장된다.

각 fragment 헤더에는 레코드 타입과 CRC32 체크섬이 포함된다.

```go
recFull   = 1  // 한 페이지에 완전히 들어가는 레코드
recFirst  = 2  // 분할된 레코드의 첫 번째 조각
recMiddle = 3  // 중간 조각
recLast   = 4  // 마지막 조각
```

```go
// 7바이트 헤더: type(1) + length(2) + crc32(4)
buf[0] = byte(typ)
binary.BigEndian.PutUint16(buf[1:], uint16(len(part)))
binary.BigEndian.PutUint32(buf[3:], crc)
```

replay 시 CRC가 맞지 않거나 `recLast` 없이 `recFirst`만 있으면 해당 레코드 전체를 버린다. 즉, **레코드 단위로 원자성을 보장**한다. 절반만 쓰인 레코드는 복구 시 없는 것으로 처리된다.

(예시)

* 쓰기가 완전히 완료된 경우 -> 2,3,3,...,4

* 중간에 쓰기가 중단된 경우 -> 2,3,3,3 (replay시 다 버린다)