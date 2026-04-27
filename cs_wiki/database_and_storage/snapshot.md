# 1. DB에서의 Snapshot 개념

## 1-1. Snapshot이란?

Snapshot은 **특정 시점(point-in-time)의 데이터 상태를 읽기 전용으로 캡처한 것**입니다. 원본 데이터가 이후에 변경되더라도, 스냅샷은 캡처 시점의 상태를 그대로 유지합니다.

핵심 속성 세 가지:
- **시점 고정 (Point-in-time)**: "지금 이 순간"의 상태를 찍는다
- **읽기 전용 (Read-only)**: 스냅샷 자체는 수정할 수 없다
- **원본과 독립 (Isolation)**: 원본이 변해도 스냅샷에는 영향 없다

---

## 1-2. DB에서 스냅샷이 쓰이는 대표적인 맥락

### (1) MVCC (Multi-Version Concurrency Control)

PostgreSQL, MySQL InnoDB 등 대부분의 현대 RDBMS가 사용하는 동시성 제어 기법입니다.

**동작 원리:**
- 트랜잭션이 시작되면, 그 시점의 데이터베이스 상태를 "스냅샷"으로 봅니다.
- 다른 트랜잭션이 데이터를 변경해도, 현재 트랜잭션은 자신의 스냅샷 시점 기준으로만 데이터를 읽습니다.
- 읽기(read)가 쓰기(write)를 블록하지 않고, 쓰기가 읽기를 블록하지 않습니다.

**예시:**
```
시점 T1: 트랜잭션 A 시작 → 스냅샷 생성 (balance = 1000)
시점 T2: 트랜잭션 B가 balance를 500으로 변경 후 커밋
시점 T3: 트랜잭션 A가 balance를 읽음 → 여전히 1000 (자신의 스냅샷)
시점 T4: 트랜잭션 A 커밋 후 다시 읽으면 → 500
```

이것이 **Snapshot Isolation**이라 불리는 격리 수준의 핵심입니다.

### (2) Database Backup & Recovery

운영 중인 DB를 멈추지 않고 백업할 때 스냅샷을 활용합니다.

**전통적 방법의 문제:**
- 백업 중에 데이터가 변경되면 백업 파일 내에서 일관성이 깨짐
- 백업을 위해 DB를 멈추면 서비스 중단

**스냅샷 기반 백업:**
- 특정 시점의 consistent snapshot을 생성
- 이 스냅샷을 기반으로 백업을 수행
- 원본 DB는 계속 쓰기 가능

실제 구현은 **Copy-on-Write (CoW)** 기법을 많이 사용합니다. 스냅샷 생성 시 데이터를 실제로 복사하지 않고, 이후 원본이 변경될 때만 변경 전 데이터를 별도 공간에 보존합니다.

### (3) 스토리지 레벨 스냅샷

LVM, ZFS, AWS EBS 등 스토리지/파일시스템 레벨에서도 스냅샷을 제공합니다.

- **LVM Snapshot**: 논리 볼륨의 특정 시점 상태를 캡처. CoW 방식
- **ZFS Snapshot**: 파일시스템 전체의 특정 시점. CoW 기반이라 거의 즉시 생성
- **AWS EBS Snapshot**: 블록 스토리지의 증분(incremental) 스냅샷. S3에 저장

이 레벨의 스냅샷은 DB 스냅샷보다 더 낮은 추상화 수준에서 동작하며, DB가 아닌 전체 파일시스템 또는 블록 디바이스 단위로 캡처합니다.

### (4) In-Memory DB의 스냅샷 (Redis 등)

Redis의 RDB 스냅샷이 대표적입니다.

**동작:**
- 특정 시점에 메모리 전체 상태를 디스크에 직렬화(serialize)하여 저장
- `fork()` 시스템 콜로 자식 프로세스를 생성하면, OS의 CoW 덕분에 부모 프로세스는 계속 쓰기 가능
- 자식 프로세스가 fork 시점의 메모리 상태를 RDB 파일로 저장

**Recovery:**
- 재시작 시 RDB 파일을 읽어서 메모리 상태를 복원
- AOF(Append Only File, WAL과 유사) 대비 복원 속도가 훨씬 빠름

이 패턴이 Prometheus TSDB의 스냅샷과 가장 유사합니다.

---

## 1-3. 스냅샷의 구현 전략 비교

| 전략 | 원리 | 장점 | 단점 | 사용 예 |
|---|---|---|---|---|
| **Full Copy** | 전체 데이터를 복사 | 단순, 원본과 완전 독립 | 시간/공간 비용 큼 | `pg_dump` 등 |
| **Copy-on-Write** | 변경 시에만 원본 블록 보존 | 생성 즉시, 공간 절약 | 변경 많으면 성능 저하 | LVM, ZFS, MVCC |
| **Redirect-on-Write** | 변경 시 새 위치에 기록 | 쓰기 성능 유지 | 단편화 가능 | Btrfs |
| **직렬화(Serialization)** | 메모리 상태를 직렬화하여 저장 | 인메모리 상태 완벽 복원 | 생성에 시간 소요 | Redis RDB |

---

# 2. Prometheus TSDB의 Snapshot

## 2-1. 왜 필요한가? — WAL Replay의 한계

블로그 Part 2에서 다뤘듯이, TSDB는 WAL(Write-Ahead-Log)로 내구성(durability)을 보장합니다. 재시작 시 WAL을 처음부터 다시 재생(replay)하여 메모리 상태를 복원하는데, 시리즈 수가 많아지면 이 과정이 수 분 이상 걸립니다.

```
[기존 재시작 과정]
1. mmap 청크 순회                    ← 수 초 (빠름)
2. Checkpoint replay                ← 수 초 (빠름)
3. WAL replay (샘플 하나하나 재생)    ← 수 분 (병목!)
   - 각 레코드 디코딩
   - 압축 청크 재구축
   - 시리즈 인덱스 재생성
```

WAL replay가 느린 근본적 이유는 **개별 샘플 단위로 기록된 로그를 하나하나 읽어서 압축 청크를 재구축**해야 하기 때문입니다.

## 2-2. Prometheus Snapshot의 정체

Prometheus v2.30.0에서 도입된 `memory-snapshot-on-shutdown`은 **TSDB 종료 시점에 Head 블록의 인메모리 상태를 직렬화하여 디스크에 저장**하는 기능입니다.

블로그의 정의:

> Snapshot in TSDB is a read-only static view of in-memory data of TSDB at a given time.

스냅샷에 포함되는 것 (순서대로):
1. **모든 시리즈 + 인메모리 청크**: 각 시리즈의 라벨셋과 아직 디스크에 안 내려간 마지막 청크
2. **Tombstones**: Head 블록의 삭제 마커들
3. **Exemplars**: Head 블록의 예시 데이터

파일 형태는 `chunk_snapshot.X.Y`로, X는 마지막 WAL 세그먼트 번호, Y는 해당 세그먼트 내 바이트 오프셋입니다.

```
data/
├── chunk_snapshot.000005.12345    ← 스냅샷
│   ├── 000001
│   └── 000002
├── chunks_head/                   ← mmap 청크 (기존)
├── wal/                           ← WAL (기존)
│   ├── checkpoint.000003
│   ├── 000004
│   └── 000005
```

## 2-3. 스냅샷이 있을 때의 재시작 과정

```
[스냅샷 기반 재시작 과정]
1. mmap 청크 순회                                ← 수 초
2. chunk_snapshot.X.Y 읽기                       ← 수 초
   - 시리즈 레코드: 라벨 + 인메모리 청크 복원
   - mmap 청크 맵과 연결
   - Tombstone 복원
   - Exemplar 복원
3. WAL replay (X번 세그먼트의 Y 오프셋 이후만)   ← 거의 없음
```

핵심은 3번입니다. 스냅샷이 종료 직전에 만들어졌으므로, **그 이후의 WAL은 거의 없습니다**. 정상 종료(graceful shutdown)의 경우 쓰기를 멈춘 뒤 스냅샷을 찍기 때문에 재생할 WAL이 0에 가깝습니다.

비정상 종료(crash)의 경우에는 새 스냅샷이 생성되지 않으므로, 마지막 정상 종료 시점의 스냅샷 + 그 이후의 WAL을 재생합니다.

---

# 3. DB 스냅샷 vs Prometheus 스냅샷 비교

## 3-1. Redis RDB와의 유사성 (가장 닮음)

Prometheus의 스냅샷은 Redis의 RDB 스냅샷과 구조적으로 매우 유사합니다.

| 항목 | Redis RDB | Prometheus chunk_snapshot |
|---|---|---|
| **본질** | 인메모리 상태를 디스크에 직렬화 | Head 블록 인메모리 상태를 직렬화 |
| **생성 시점** | 주기적 / shutdown 시 | **shutdown 시에만** |
| **내용물** | 전체 key-value 데이터 | 시리즈 + 인메모리 청크 + tombstone + exemplar |
| **WAL과의 관계** | AOF와 병행 사용 | WAL과 병행 사용 |
| **복원 방식** | RDB 로드 → AOF 재생 | 스냅샷 로드 → 잔여 WAL 재생 |
| **crash 시** | 마지막 RDB + AOF로 복구 | 마지막 스냅샷 + WAL로 복구 |
| **성능 이점** | AOF 전체 재생 대비 빠름 | WAL 전체 재생 대비 50-80% 빠름 |

**핵심 차이점**: Redis는 `fork()` + CoW로 서비스 중에도 스냅샷을 생성할 수 있지만, Prometheus는 **오직 종료 시에만** 스냅샷을 찍습니다.

## 3-2. 왜 종료 시에만 찍는가?

블로그에서 직접 답하고 있습니다:

> If we take snapshots at intervals while Prometheus is running, this can increase the number of times a sample is written to disk by a big %, hence causing unnecessary write amplification.

각 샘플이 디스크에 기록되는 횟수를 생각해보면:
```
[현재 경로]
샘플 → WAL에 1회 기록 → 청크로 압축 → mmap 파일에 1회 → compaction에서 N회

[주기적 스냅샷을 추가하면]
샘플 → WAL에 1회 → 스냅샷에 M회(주기마다) → mmap에 1회 → compaction에서 N회
                     ↑ 불필요한 write amplification
```

Redis는 인메모리 DB라서 디스크 쓰기 자체가 적지만, Prometheus는 이미 WAL + mmap + compaction으로 여러 번 쓰고 있어서 추가 write amplification을 피하려는 것입니다.

## 3-3. MVCC 스냅샷과의 비교

| 항목 | MVCC Snapshot | Prometheus Snapshot |
|---|---|---|
| **목적** | 트랜잭션 격리 (읽기 일관성) | 빠른 재시작 (상태 복원) |
| **수명** | 트랜잭션 동안만 유효 | 다음 종료 시까지 디스크에 유지 |
| **생성 빈도** | 트랜잭션 시작마다 (매우 빈번) | 종료 시 1회만 |
| **구현** | 버전 체인 + visibility check | WAL 포맷으로 직렬화 |
| **공간 비용** | old version 유지 비용 | 별도 파일로 추가 디스크 사용 |
| **데이터 범위** | 논리적 뷰 (실제 복사 안 함) | 물리적 직렬화 (실제 기록) |

MVCC 스냅샷은 "이 시점 이후의 변경을 안 보겠다"라는 논리적 개념인 반면, Prometheus 스냅샷은 "이 시점의 메모리를 디스크에 물리적으로 저장하겠다"라는 물리적 행위입니다.

## 3-4. 스토리지 레벨 스냅샷과의 비교

| 항목 | LVM/ZFS/EBS Snapshot | Prometheus Snapshot |
|---|---|---|
| **추상화 수준** | 블록 디바이스 / 파일시스템 | 애플리케이션 (TSDB 내부) |
| **인식 대상** | 파일/블록 (내용 모름) | 시리즈, 청크, tombstone 등 의미 단위 |
| **일관성** | crash-consistent (DB 모를 수 있음) | application-consistent (TSDB가 직접 관리) |
| **복원 시** | 파일 복원 → DB가 WAL replay | 스냅샷 복원 → WAL replay 최소화 |
| **구현** | CoW (OS/하드웨어 레벨) | 직렬화 (애플리케이션 레벨) |

스토리지 스냅샷은 DB의 내부 구조를 모르기 때문에, 복원 후에도 DB가 자체적으로 recovery를 수행해야 합니다. Prometheus 스냅샷은 TSDB 자체가 의미를 이해하고 만들기 때문에 복원이 훨씬 효율적입니다.

---

# 4. 전체 흐름 정리

## 4-1. Prometheus 재시작 시나리오 비교

```
[스냅샷 없이 정상 재시작]
종료: 즉시 종료
시작: mmap 순회(초) → Checkpoint replay(초) → WAL 전체 replay(분~) 
총 다운타임: 수 분

[스냅샷 있는 정상 재시작]
종료: 쓰기 중단 → 스냅샷 생성(초~1분) → 종료
시작: mmap 순회(초) → 스냅샷 복원(초) → WAL replay 거의 없음
총 다운타임: 50-80% 감소

[비정상 종료 (crash)]
종료: 즉시 (스냅샷 없음)
시작: mmap 순회(초) → 마지막 스냅샷 복원(초) → 스냅샷 이후 WAL replay(가변)
총 다운타임: 마지막 스냅샷 이후 WAL 양에 비례
```

## 4-2. DB 스냅샷 계보에서 Prometheus의 위치

```
[스냅샷 개념의 계보]

논리적 스냅샷 (데이터 뷰)
├── MVCC Snapshot Isolation     → 트랜잭션 격리용
└── Consistent Read View        → 백업용

물리적 스냅샷 (디스크 저장)
├── 스토리지 레벨
│   ├── LVM Snapshot (CoW)
│   ├── ZFS Snapshot (CoW)
│   └── EBS Snapshot (증분)
│
└── 애플리케이션 레벨
    ├── Redis RDB (fork + CoW, 주기적)
    └── Prometheus chunk_snapshot (직렬화, 종료 시만)  ← 여기
```

Prometheus의 스냅샷은 **애플리케이션 레벨의 물리적 스냅샷**이며, Redis RDB와 가장 유사하되 **write amplification을 피하기 위해 종료 시에만 생성**한다는 점이 가장 큰 설계 차별점입니다.

## 4-3. WAL과 스냅샷의 관계 — 왜 WAL은 여전히 필요한가?

블로그에서도 이 질문에 답합니다:

> If Prometheus happens to crash due to various reasons, we need the WAL for durability since a snapshot cannot be taken. Additionally, remote-write depends on the WAL.

이건 Redis에서 RDB만으로는 부족하고 AOF도 필요한 것과 같은 이유입니다.

| 상황 | 스냅샷만 | WAL만 | 스냅샷 + WAL |
|---|---|---|---|
| 정상 종료 후 재시작 | 빠름, 데이터 손실 없음 | 느림, 데이터 손실 없음 | 빠름, 손실 없음 |
| crash 후 재시작 | 마지막 종료 이후 데이터 손실 | 느림, 손실 없음 | 적절한 속도, 손실 없음 |
| remote-write | 지원 불가 | 지원 | 지원 |

스냅샷은 **성능 최적화**이지 WAL의 **대체재가 아닙니다**. 둘은 상호 보완 관계입니다.