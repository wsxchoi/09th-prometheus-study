# Page Cache

## File I/O 개념

File I/O는 유저 공간(User Space)의 프로세스가 디스크와 같은 영구 저장장치(Non-volatile storage)에 존재하는 데이터에 접근하고 조작하기 위해 커널에 요청을 보내는 일련의 로우레벨 과정이다.

![image.png](https://github.com/user-attachments/assets/a6b71a81-acec-4ce0-a214-17cbce6e66ac)

리눅스 커널은 다양한 파일 시스템(ext4, xfs 등)과 하드웨어 디바이스를 단일한 인터페이스로 추상화하기 위해 **VFS(Virtual File System)** 계층을 둔다. 프로세스가 `open()` 시스템 콜을 호출하면, 커널은 해당 파일의 메타데이터를 담은 **Inode** 객체를 찾고, 프로세스 내부의 파일 디스크립터(FD, File Descriptor) 테이블에 이를 등록하여 핸들을 반환한다.

![image.png](https://github.com/user-attachments/assets/fdc7c95f-d1fb-498c-9bff-4d676e1413c1)

가장 대표적인 `read()` 시스템 콜의 내부 동작을 분석해보면 다음과 같다.

1. 프로세스가 `read(fd, buffer, size)`를 호출하면 하드웨어 인터럽트가 발생하여 유저 모드에서 커널 모드로 **Context Switching**이 일어난다.
2. 제어권을 쥔 커널은 VFS 계층을 통해 해당 FD가 가리키는 파일의 Inode를 확인하고, 요청한 오프셋(Offset)의 데이터가 커널 메모리(Page Cache)에 존재하는지 먼저 검사한다.
3. 데이터가 캐시에 없다면(Cache Miss), 커널은 블록 I/O(Block I/O) 계층으로 요청을 내려보낸다. I/O 스케줄러가 요청을 정렬하고 병합한 뒤, 디바이스 드라이버를 통해 실제 디스크 컨트롤러에 물리적인 읽기 명령을 하달한다.
4. 디스크는 DMA(Direct Memory Access) 컨트롤러를 사용하여 CPU의 개입 없이 커널 영역의 버퍼(Page Cache)로 데이터를 직접 전송한다.
5. 전송이 완료되어 하드웨어 인터럽트가 발생하면, CPU는 깨어나 커널 영역에 올라온 데이터를 유저 프로세스가 지정한 `buffer` 주소 공간으로 **복사(Copy)**한다.

이러한 전통적인 File I/O 방식은 데이터를 Disk → Kernel Space → User Space로 이동시키는 과정에서 필수적으로 **메모리 Double Copy**와 잦은 **Context Switching 오버헤드**를 유발한다. 이것이 앞서 설명한 `mmap`이 대용량 데이터 처리에서 I/O 성능을 극적으로 끌어올릴 수 있는 근본적인 대비점이다.

### 프로메테우스에서의 File I/O

데이터를 잃어버리지 않기 위해 디스크의 WAL 파일에 '기록(record)'한다고 표현한 부분, 그리고 가득 찬 청크를 디스크로 '플러시(flush)'한다고 표현한 부분이 바로 전형적인 File I/O 작업이다. 메모리(Head)에만 데이터를 두면 머신 크래시 때 날아가므로, 시스템 콜(예: `write()`)을 호출해 디스크라는 물리적 장치에 안전하게 써내려가는 과정을 의미한다.

---

## Page Cache 개념

Page Cache(Buffer Cache)는 느린 디스크 I/O로 인한 시스템 전체의 병목 현상을 완화하기 위해, 리눅스 커널이 남는 여유 물리 메모리(RAM)를 활용하여 디스크의 파일 데이터를 투명하게(Transparently) 임시 저장해 두는 소프트웨어 캐시 영역이다.

![image.png](https://github.com/user-attachments/assets/c28436f5-3cd1-4c81-87c1-2d32f3b0b968)

커널은 파일과 메모리 간의 매핑을 고속으로 관리하기 위해 `address_space`라는 핵심 커널 자료구조를 사용한다. 각 파일의 Inode는 자신의 `address_space` 객체를 가지며, 이 객체 내부에는 파일의 특정 오프셋(Offset) 블록들이 물리 메모리의 어느 페이지 프레임에 캐싱되어 있는지를 O(1)에 가깝게 탐색할 수 있는 **Radix Tree(현대 커널에서는 XArray)** 구조가 연결되어 있다.

![image.png](https://github.com/user-attachments/assets/25fb58ae-47fd-411e-9f76-5640759c04d3)

이 페이지 캐시는 읽기와 쓰기 양쪽 모두에서 결정적인 최적화를 수행한다.

- **읽기 최적화 (Cache Hit):** 프로세스가 `read()`를 호출했을 때 요청한 파일 데이터가 페이지 캐시에 이미 존재한다면, 커널은 느린 블록 장치 접근을 완전히 생략하고 즉시 커널 메모리에서 유저 메모리로 데이터를 복사해 반환한다.
- **쓰기 최적화 (Delayed Write-back):** 프로세스가 `write()`를 호출하면, 커널은 데이터를 즉시 디스크에 쓰지 않는다. 대신 페이지 캐시의 해당 페이지에 데이터를 덮어쓴 뒤, 이 페이지를 디스크와 내용이 달라졌다는 의미로 **더티 페이지(Dirty Page)**로 마킹하고 유저 프로세스에게는 쓰기가 완료되었다고 신호를 보낸다.

더티 페이지들은 커널 백그라운드 스레드(ex. `kworker`, `pdflush/flusher` 스레드)에 의해 비동기적으로 모아져서 순차적(Sequential)인 I/O 패턴으로 디스크에 일괄 기록(Flush)된다.

![image.png](https://github.com/user-attachments/assets/23a139ca-a0f6-43bb-8540-61738642cc41)

시스템이 장기간 운영되어 페이지 캐시가 물리 메모리의 대부분을 점유하게 되면, 메모리 압박(Memory Pressure)이 발생한다. 이때 커널의 `kswapd` 데몬이 깨어나 **Active / Inactive LRU (Least Recently Used) 리스트**를 순회하며, 가장 오랫동안 참조되지 않은 클린(Clean) 페이지들을 즉각 메모리에서 해제(Eviction)하여 새로운 프로세스가 사용할 빈 공간을 확보한다.

### 프로메테우스에서의 Page Cache

[원문](https://ganeshvernekar.com/blog/prometheus-tsdb-the-head-block/)에서는 운영체제가 제공하는 기능(mmap)을 이용해 필요할 때 청크 데이터를 동적으로 '메모리'에 로드한다고 짧게 치고 넘어갔다. 여기서 말하는 디스크에서 읽혀 올라온 데이터가 자리 잡는 그 '메모리 영역'이 바로 커널의 **Page Cache**다.
프로메테우스가 애플리케이션 레벨에서 캐시를 직접 짜는 헛수고를 하지 않고 리눅스 커널의 Page Cache 메커니즘을 전적으로 사용한다고 볼 수 있다