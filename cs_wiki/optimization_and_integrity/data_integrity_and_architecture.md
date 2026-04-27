# Prometheus TSDB: Immutable, Magic Number, Checksum(CRC32), Garbage Collection


## 0. Overview
Prometheus는 지속적으로 시계열 데이터를 수집하는 시스템이며,  
해당 과정에서 TSDB는 다음과 같은 사항이 요구된다:
- 데이터가 깨지지 않아야 함(데이터 무결성 보장)
- 메모리 사용량 최소화와 디스크 기반 데이터 접근 최적화
- 빠른 재시작 (WAL replay 최소화)
- 장애 이후 빠른 복구
- 오래된 데이터에 대한 안전하고 효율적인 삭제

Prometheus v2.19.0에서는 이를 위해  
**“full chunk를 디스크에 저장하고 memory-mapping으로 접근하는 구조”** 가 도입되었다.
이 구조로 다음과 같은 변화를 가져왔다:
- 메모리에는 chunk 대신 reference만 유지 → 메모리 사용량 감소(20~40%)
- 디스크의 chunk를 mmap으로 필요할 때만 로드
- 재시작 시 디스크의 chunk 정보를 기반으로 map 구성
- WAL replay 중 이미 존재하는 chunk 범위의 데이터는 skip(WAL replay 시간 20~30% 단축)

또한 이 구조는 Garbage Collection(truncation)과 결합되어:

- 메모리에서는 reference 제거
- 디스크에서는 오래된 파일을 통째로 삭제

하는 방식으로 효율적인 데이터 정리를 가능하게 한다.

---
## 1. Immutable (불변성)

### 1.1 Definition

Immutable은 한 번 생성된 데이터는 변경되지 않는다는 성질이다. 즉, 데이터를 수정하는 대신 새로운 데이터를 생성한다.

일반적인 CS에서 immutable은 객체 단위로 설명되는 경우가 많지만,  
Prometheus에서는 **객체가 아니라 데이터 단위(chunk)** 에 적용되는 개념이다.

불변성(Immutable)은 다음과 같은 장점을 제공한다:
- 동시성 처리 단순화 (락 감소)
- 데이터 신뢰성 증가
- 복구 및 디버깅 용이
- 파일 단위 관리 가능(GC가 용이해짐)


### 1.2. Role in Prometheus

Prometheus에서는 chunk가 일정량 채워지면 **full chunk**가 되고, 이후 수정되지 않는다.
- 현재 chunk → mutable
- full chunk → immutable
  
full chunk의 immutable 특성을 기반으로 다음 구조가 가능해진다:
#### 1. 디스크 저장 (Flush)
full chunk는 더 이상 변경되지 않기 때문에, 디스크 (`chunks_head`)로 안전하게 flush 가능하다.  
이후 mmap으로 read-only 접근.

#### 2. Memory Mapping (mmap)

immutable이기 때문에 디스크의 chunk가 변하지 않아
- mmap으로 안전하게 참조 가능

- 메모리에는 chunk 대신 reference만 저장하고, 실제 데이터는 디스크에 유지


#### 3. WAL Replay 최적화

재시작 시 디스크의 full chunk를 먼저 읽어 시간 범위를 구성한다.
 이후 WAL replay 중, 해당 범위에 포함된 sample은 skip된다.  
이는 full chunk가 **immutable**하여 디스크 데이터를 그대로 신뢰할 수 있기 때문이다.

#### 4. Garbage Collection 단순화

chunk 내부를 수정할 필요가 없어, 오래된 데이터는 그대로 유지하거나 통째로 삭제 가능

Prometheus에서는:

- 메모리 → reference 제거
- 디스크 → 파일 단위 삭제

 이 차이로 인해 GC가 매우 단순하고 효율적으로 동작한다.

### 1.3 Code
 
**head_chunks.go**  
`ChunkDiskMapperRef`는 디스크 상에서 chunk가 저장된 실제 위치(파일 번호 + 오프셋)으로 구성된 불변 참조값이다. 한번 기록된 chunk의 위치는 절대 변하지 않는다.
 
```go
// ChunkDiskMapperRef represents the location of a head chunk on disk.
// The upper 4 bytes hold the index of the head chunk file and
// the lower 4 bytes hold the byte offset in the head chunk file where the chunk starts.

type ChunkDiskMapperRef uint64
 
func newChunkDiskMapperRef(seq, offset uint64) ChunkDiskMapperRef {
    return ChunkDiskMapperRef((seq << 32) | offset)
}
```
한 번 기록된 chunk의 위치가 바뀌면 기존 ref는 더 이상 유효하지 않게 되므로, Prometheus는 항상 현재 파일의 끝에 새 chunk를 덧붙이는 append-only 방식을 사용.

`writeChunk()`는 항상 파일 끝에 append하며, 기존 데이터를 덮어쓰지 않는다 (append-only).  
 
파일이 닫힐 때 (`cut()`) maxt가 확정되고, 이후 그 파일은 수정되지 않는다:
 
```go
// cut() 내부 — 새 파일로 전환 시 현재 파일을 닫고 maxt를 확정
cdm.mmappedChunkFiles[cdm.curFileSequence].maxt = cdm.curFileMaxt
```
Prometheus에서는 이미 기록된 chunk의 위치와 내용이 바뀌지 않기 때문에, reader는 과거의 닫힌 파일을 read-only 데이터처럼 다룰 수 있다. 그래서 readPathMtx를 sync.RWMutex로 두고, 일반 조회 시에는 RLock()만으로 여러 reader가 동시에 접근할 수 있다.


---
## 2. Magic Number

### 2.1 Definition

Magic Number는 파일의 시작 부분에 위치한 고정된 값으로, 해당 파일의 형식을 식별하는 역할을 한다.

일반적인 프로그래밍에서 Magic Number는 “특별한 의미를 가지는 상수 값”을 의미하지만,  
파일 시스템이나 바이너리 포맷에서는 특히 **파일의 타입을 구분하기 위한 식별자(signature)** 로 사용된다.

즉, Magic Number는 파일의 첫 몇 바이트를 확인하여 올바른 포맷인지 검증한다.


### 2.2 Role in Prometheus

Prometheus의 `chunks_head` 파일 구조에는 header가 존재하며,  
이 header에 magic number가 포함된다.  
chunks_head의 구조는 다음과 같다:
```
┌──────────────────────────────┐
│  magic(0x0130BC91) <4 byte>  │
├──────────────────────────────┤
│    version(1) <1 byte>       │
├──────────────────────────────┤
│    padding(0) <3 byte>       │
├──────────────────────────────┤
│ ┌──────────────────────────┐ │
│ │         Chunk 1          │ │
│ ├──────────────────────────┤ │
│ │          ...             │ │
│ ├──────────────────────────┤ │
│ │         Chunk N          │ │
│ └──────────────────────────┘ │
└──────────────────────────────┘
```

파일을 읽을 때 Prometheus는:

1. 파일의 첫 4 byte를 읽어 magic number 확인
2. 해당 값이 `memory-mapped head chunks` 파일인지 검증
3. 검증이 성공하면 version과 구조에 맞게 chunk decoding 수행

Prometheus는 재시작 시 `chunks_head` 파일들을 직접 읽어  
chunk 정보를 기반으로 map을 구성하고 WAL replay를 최적화하므로

- 잘못된 파일을 읽으면 전체 복구 과정이 깨질 수 있음
- mmap 기반 접근이므로 잘못된 데이터 해석 시 위험 

따라서 Magic Number는 디스크 기반 chunk를 신뢰하고 사용할 수 있는 최소한의 검증 단계이다.

### 2.3 Code
**`head_chunks.go`**:
 
```go
const (
    // MagicHeadChunks is 4 bytes at the beginning of a head chunk file.
    MagicHeadChunks    = 0x0130BC91
    headChunksFormatV1 = 1
)
```
 
파일을 열 때 (`openMMapFiles()`) magic number와 version을 모두 검증한다:
 
```go
// Verify magic number.
if m := binary.BigEndian.Uint32(b.byteSlice.Range(0, MagicChunksSize)); m != MagicHeadChunks {
    return fmt.Errorf("%s: invalid magic number %x", files[i], m)
}
// Verify chunk format version.
if v := int(b.byteSlice.Range(MagicChunksSize, MagicChunksSize+ChunksFormatVersionSize)[0]); v != chunksFormatV1 {
    return fmt.Errorf("%s: invalid chunk format version %d", files[i], v)
}
```
검증 실패 시 에러를 반환하고, 이후 Head 레벨에서 `repairLastChunkFile()`을 통해  
손상된 마지막 파일을 자동으로 제거하는 복구 로직도 존재한다:
 
```go
// repairLastChunkFile deletes the last file if it's empty.
// Because we don't fsync when creating these files, we could end
// up with an empty file at the end during an abrupt shutdown.
func repairLastChunkFile(files map[int]string) (_ map[int]string, returnErr error) {
    // ...
    // We either don't have enough bytes for the magic number or the magic number is 0.
    if size < MagicChunksSize || binary.BigEndian.Uint32(buf) == 0 {
        if err := os.RemoveAll(files[lastFile]); err != nil { ... }
        delete(files, lastFile)
    }
    return files, nil
}
```
Prometheus는 마지막 chunk 파일이 너무 짧거나 magic number가 0인 경우, 이를 비정상 종료 중에 생성만 되고 제대로 기록되지 못한 파일로 보고 자동 삭제한다.


---
## 3. Checksum (CRC32)

### 3.1 Definition

Checksum은 데이터의 무결성을 검증하기 위한 값이다. (즉, 데이터 전송 또는 저장 과정에서 발생할 수 있는 오류를 검출하기 위해 사용되는 값이다.)
데이터 내용을 기반으로 계산되며, 저장 시 함께 기록된다.

Prometheus에서는 CRC32를 사용한다.


### 3.2 Role in Prometheus

Prometheus chunk 구조:
```
┌─────────────────────┬───────────────────────┬───────────────────────┬───────────────────┬───────────────┬──────────────┬────────────────┐
| series ref <8 byte> | mint <8 byte, uint64> | maxt <8 byte, uint64> | encoding <1 byte> | len <uvarint> | data <bytes> │ CRC32 <4 byte> │
└─────────────────────┴───────────────────────┴───────────────────────┴───────────────────┴───────────────┴──────────────┴────────────────┘
```

- series ref: series 식별자
- mint / maxt: 청크의 시간 범위
- encoding: 압축 방식
- data: 실제 샘플 데이터
- CRC32: 위 모든 필드를 포함한 계산된 checksum 값

chunk를 읽을 때:
1. chunk의 데이터를 읽고
2. 동일한 방식으로 CRC32를 다시 계산한 뒤
3. 저장된 CRC32 값과 비교한다
4. 불일치 시 → 데이터 손상 판단(CorruptionErr 반환)

### 3.3 Code
 
**쓸 때 — `writeChunk()`**:
 
```go
cdm.crc32.Reset()
 
// 헤더 필드들(seriesRef, mint, maxt, encoding, length)을 쓰면서 동시에 CRC에도 반영
if err := cdm.writeAndAppendToCRC32(cdm.byteBuf[:bytesWritten]); err != nil {
    return err
}
// 실제 chunk 데이터를 쓰면서 CRC에 반영
if err := cdm.writeAndAppendToCRC32(chk.Bytes()); err != nil {
    return err
}
// 최종적으로 계산된 CRC32 값을 파일에 기록
if err := cdm.writeCRC32(); err != nil {
    return err
}
```
 
**읽을 때 — `Chunk()`**:
 
```go
// 저장된 CRC32 값을 읽어서
sum := mmapFile.byteSlice.Range(chkDataEnd, chkDataEnd+CRCSize)
// 실제 데이터로 재계산한 값과 비교
if err := checkCRC32(mmapFile.byteSlice.Range(chkStart-(...), chkDataEnd), sum); err != nil {
    return nil, &CorruptionErr{
        Dir:       cdm.dir.Name(),
        FileIndex: sgmIndex,
        Err:       err,
    }
}
```
 
**재시작 시 — `IterateAllChunks()`**:
 
```go
// Check CRC.
sum := mmapFile.byteSlice.Range(idx, idx+CRCSize)
if err := checkCRC32(mmapFile.byteSlice.Range(startIdx, idx), sum); err != nil {
    return &CorruptionErr{
        Dir:       cdm.dir.Name(),
        FileIndex: segID,
        Err:       err,
    }
}
```
 
CRC32 검증은 **청크를 읽는 모든 경로**에서 수행된다.  
- 일반 쿼리 시 (`Chunk()`)
- 재시작 후 전체 scan 시 (`IterateAllChunks()`)
 


---
## 4. Garbage Collection (GC)

### 4.1 Definition

Garbage Collection(GC)은 더 이상 사용되지 않는 데이터를 자동으로 제거하여  
메모리 또는 저장 공간을 확보하는 기법이다.

시간이 지남에 따라:

- 오래된 데이터가 계속 쌓이고
- 메모리 및 디스크 사용량이 증가한다

따라서 사용되지 않는 데이터를 주기적으로 정리하는 과정이 필요하다.

### 4.2 Role in Prometheus

Prometheus에서 GC는 **Head truncation 과정**에서 수행된다.  
GC가 일어나기 전, 약 2시간 단위로 Head의 데이터는 디스크의 **Persistent Block(영구 블록)** 으로 복사(Flush)된다.

#### 1. 메모리에서의 GC
Head는 chunk reference (ref, mint, maxt)를 메모리에 유지하는데,  
특정 시점 T 이전 데이터가 더 이상 필요 없어지면 해당 **chunk reference**를 메모리에서 제거한다.  
-> 더 이상 접근할 수 없는 unreachable 상태

#### 2. 디스크에서의 GC
실제 chunk 데이터는 디스크의 `chunks_head`에 존재한다.  
chunks_head의 각 파일에 대해 해당 파일에 포함된 chunk들의 최대 시간을 관리한다.  
파일의 max time < 특정 시점 T 이면, 해당 파일 전체를 삭제한다. 

단, 
- 현재 쓰고 있는 live file은 제외
- 파일 순서(sequence)는 유지하면서 삭제

해당 방식으로 복잡한 개별 데이터 삭제 없이, 파일 단위로 빠르게 정리가 가능하다. 

#### Sequence 보존 이유
 
파일 `5, 6, 7, 8` 중 `5`와 `7`이 삭제 대상이어도 `7`은 삭제하지 않는다.  
`6`이 아직 살아있기 때문에, 중간에 구멍이 생기면 m-map 파일 순서 참조가 깨진다.
 
```
삭제 전: [5, 6, 7, 8]
         ↑     ↑
      T 이전  T 이전이지만 6이 살아있으므로 보존
 
삭제 후: [6, 7, 8]
```
### File Rotation
truncation 이후, 기존 live file을 닫고 새로운 파일을 생성하는데, 이를 **file rotation**이라고 한다. 

rotate가 필요한 이유:
- 데이터가 적으면 파일이 오래 유지되어 오래된 데이터가 파일 안에 계속 남게 된다. 
- 데이터를 여러 파일로 나누어, 이후 truncation에서 파일 단위 삭제가 가능하도록 한다. 


### 4.3 Code
 
**`head_chunks.go`의 `Truncate()`**:
 
```go
func (cdm *ChunkDiskMapper) Truncate(fileNo uint32) error {
    cdm.readPathMtx.RLock()
 
    chkFileIndices := make([]int, 0, len(cdm.mmappedChunkFiles))
    for seq := range cdm.mmappedChunkFiles {
        chkFileIndices = append(chkFileIndices, seq)
    }
    slices.Sort(chkFileIndices)
 
    var removedFiles []int
    for _, seq := range chkFileIndices {
       //현재 쓰고 있는 live file, fileNo 이상의 파일은 삭제 대상 아님
        if seq == cdm.curFileSequence || uint32(seq) >= fileNo {
            break  // sequence 보존의 핵심(앞에서부터 연속된 파일만 삭제)
        }
        removedFiles = append(removedFiles, seq)
    }
    cdm.readPathMtx.RUnlock()
 
    // GC 전에 현재 live file을 닫고 새 파일 시작 (rotation)
    if cdm.curFileSize() > HeadChunkFileHeaderSize {
        cdm.CutNewFile()
    }
 
    cdm.deleteFiles(removedFiles)
    // ...
}
```
Truncate()에서 파일 번호를 정렬한 뒤, 현재 쓰고 있는 live file이 아니면서 truncation 경계 이전에 있는 파일만 앞에서부터 연속적으로 삭제 대상으로 고른다.  
여기서 break를 사용하는 이유는 중간 파일은 남겨둔 채 뒤 파일만 삭제하는 상황을 막고, 파일 sequence를 보존하기 위해서다.  
또한 삭제 전에 CutNewFile()을 호출해 현재 live file을 닫고 새 파일로 회전시킴으로써, 이후 GC가 닫힌 파일만 대상으로 안정적으로 동작하도록 한다.


---
## 5. Reference
https://github.com/prometheus/prometheus/blob/main/tsdb/chunks/head_chunks.go
https://github.com/prometheus/prometheus/blob/main/tsdb/head.go
https://ganeshvernekar.com/blog/prometheus-tsdb-mmapping-head-chunks-from-disk/
