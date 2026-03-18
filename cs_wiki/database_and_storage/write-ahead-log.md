# Write-Ahead Log (WAL)

## **1. WAL이란?**

![image.png](https://github.com/user-attachments/assets/3354b9f6-1480-4181-a978-28c4d3e42009)
[출처](https://www.intel.com/content/www/us/en/developer/articles/technical/optimizing-write-ahead-logging-with-intel-optane-persistent-memory.html)

- ‘로그 먼저쓰기’ 로 실제 데이터 구조에 반영하기 전에 먼저 로그를 저장하는 것이다.
- 서버의 crash를 대비하기위해 존재하며 recovery를 위해 저장된다.
- 장애시 로그를 replay하여 일관성 상태를 복구할 수 있다.
- 높은 일관성을 위해서는 필수적인 장치이다.
- 핵심 키워드: durability, crash recovery, sequential write(디스크에 순차 쓰기라 빠름).

### 1.2 append-only (sequential write 이점)

- append-only는 순차적으로 작성되는 것을 의미한다.
- disk의 특정 파티션의 데이터가 저장될때 기록되는 순서가 한방향인 것을 의미한다.
- disk에서 가장 느린 동작이 seek time이다. 순차적으로 쓰기를 하게되면 seek time이 없어지게 되어 빠른 쓰기가 가능해진다.

### **1.3 WAL과 성능의 상관관계 (Checkpointer)**

WAL을 사용하는 이유는 단순히 '안전성' 때문만은 아닙니다. 실제 데이터 파일(B-Tree 등)에 변경 사항을 바로 기록하려면 디스크의 여기저기를 찾아다니는 **Random Write**가 발생합니다.

- **효율성:** 모든 변경 사항을 일단 WAL에 **Sequential Write**로 빠르게 기록해두고, 실제 데이터 파일로의 반영(Checkpointing)은 나중에 백그라운드에서 한꺼번에 처리함으로써 사용자 응답 속도를 높입니다.

### **1.4 SSD에서의 Sequential Write 이점**

![image.png](https://github.com/user-attachments/assets/5a1344cb-4e68-4db5-ab41-904aecf273f3)

"SSD를 사용하면 큰 이점이 없을 수 있다" 라고 생각할 수 있는데, 이 부분은 조금 더 논의해 볼 가치가 있습니다.

- **여전한 이점:** SSD가 HDD보다 Random Access에 훨씬 강력한 것은 사실이지만, SSD 내부의 **FTL(Flash Translation Layer)** 구조상 여전히 **Sequential Write가 Random Write보다 빠르고 수명이 오래갑니다.**
- **지연 시간(Latency):** DB 입장에서 로그를 남기는 작업은 트랜잭션 완료(Commit)를 위해 반드시 기다려야 하는 작업입니다. 아무리 SSD라도 Random Write로 데이터 파일을 갱신하는 것보다, 로그 파일 끝에 붙이는 작업이 훨씬 지연 시간이 짧습니다.
- NAND Flash는 덮어쓰기가 불가능합니다. 데이터가 있는 페이지에 쓰기를 하기 위해선 해당 page(4KB ~ 16KB)가 포함된 block(2MB ~ 4MB)을 삭제하고 데이터를 저장하고, 삭제하지 않아야하는 데이터도 다시 쓰기를 해야합니다.
- 그러면 **읽기 - 지우기 - 쓰기** 가 발생됩니다. 보통 쓰기보다 지우는 속도가 느립니다.
- 또한 랜덤쓰기로 파편화된 데이터가 많아지면 GC의 부하가 됩니다.
- SSD의 수명과도 관련이 있는데 실제 쓰려고하는 데이터보다 많은 쓰기가 발생이되어 수명이 빠르게 줄어들 수 있다.

## **2. 다른 시스템에서의 WAL**

### MySQL (InnoDB 엔진 기준): **Redo Log**

MySQL의 InnoDB 스토리지 엔진은 WAL 메커니즘을 구현하기 위해 **Redo Log**라는 파일을 사용합니다.

- **동작 방식:** 데이터에 변경이 생기면 메모리(Buffer Pool)에 먼저 반영하고, 동시에 변경 내용을 Redo Log에 순차적으로 기록합니다. 실제 디스크의 데이터 파일(.ibd)에 반영하는 것은 나중에 비동기적으로 처리합니다.
- **목적:** **Crash Recovery**입니다. 서버가 갑자기 꺼졌을 때, 메모리에는 있었지만 데이터 파일에는 미처 기록되지 못한 변경 사항을 Redo Log를 읽어 재실행(Redo)함으로써 데이터 손실을 막습니다.
- **Binlog와의 차이:** MySQL에는 `Binlog`도 있지만, 이는 복제(Replication)나 시점 복구용이며, WAL의 역할을 하는 것은 엔진 레벨의 **Redo Log**입니다.

### Redis: **AOF (Append Only File)**

메모리 기반 DB인 Redis는 전원이 꺼지면 데이터가 날아가는 단점을 보완하기 위해 WAL 방식인 **AOF**를 사용합니다.

- **동작 방식:** Redis에서 실행되는 모든 쓰기 명령(Set, Del 등)을 실행 직후에 로그 파일(appendonly.aof) 끝에 그대로 기록합니다.
- **특징:** * **순차 쓰기:** 파일 끝에 명령어를 추가하기만 하므로 디스크 I/O 부하가 적습니다.
    - **AOF Rewrite:** 로그 파일이 너무 커지면, 현재 메모리 상태를 기준으로 파일을 다시 써서 크기를 줄이는 최적화 과정을 거칩니다.
- **설정 옵션:** 성능과 안전성 사이의 타협점에 따라 매 초마다 기록(everysec)할지, 명령마다 기록(always)할지 선택할 수 있습니다.

## **3. Prometheus TSDB에서의 WAL**

![image.png](https://github.com/user-attachments/assets/4e43c5e7-3b61-4e41-a643-e6d366f4fc78)

프로메테우스의 스토리지 엔진(TSDB)에서 WAL은 데이터의 유실을 막는 최전방 방어선 역할을 합니다.
****

### Head Block 구조와 WAL

프로메테우스는 최근에 들어온 데이터를 **Head Block**이라는 인메모리(In-Memory) 영역에 저장합니다.

- **메모리 Chunk:** 수집된 메트릭은 먼저 메모리 상의 'Chunk'에 기록되어 빠른 조회와 압축을 지원합니다.
- **동시 기록:** 하지만 메모리는 전원이 꺼지면 데이터가 사라지므로, 메모리에 쓰는 동시에 디스크의 **WAL 파일에 해당 데이터를 순차적으로 기록**합니다.
- **Memory-Mapped Chunks (v2.19.0+):** Chunk가 꽉 차면(120개 샘플 또는 2시간 경과) 디스크에 flush하고, OS의 mmap 기능으로 메모리에는 참조(reference)만 남깁니다. 실제 데이터가 필요할 때 OS가 동적으로 메모리에 로드합니다. 이를 통해 Head Block의 메모리 사용량을 크게 줄일 수 있습니다.
- **성능 최적화:** 프로메테우스의 WAL은 매우 효율적으로 설계되어 있어, 수만 개의 메트릭이 쏟아지는 환경에서도 디스크 I/O 병목을 최소화하며 데이터를 보존합니다.

### 복구 메커니즘 (Replay)

- **Crash Recovery:** 프로메테우스 프로세스가 비정상적으로 종료되었다가 재시작되면, 디스크의 **Memory-Mapped Chunks와 WAL 파일을 함께** 읽어들입니다.
- **Replay:** WAL에 기록된 내용을 순서대로 다시 실행(Replay)하여, 사고 직전 메모리에 있었지만 아직 영구적인 블록(Persistent Block)으로 저장되지 않았던 데이터들을 Head Block에 완벽하게 복구합니다.

### "블랙박스로 사용"

- 일반적인 운영 단계에서 WAL은 내부적으로 조용히 작동하는 **블랙박스**와 같습니다. 사용자가 직접 WAL 파일을 건드릴 일은 거의 없지만, 시스템 장애 발생 시에는 데이터의 정합성을 보장하는 핵심 장치가 됩니다.