# mmap

## mmap 개념

`mmap`(Memory-Mapped File)은 디스크에 존재하는 파일이나 디바이스의 특정 영역을 커널을 거쳐 프로세스의 가상 주소 공간(VMA, Virtual Memory Area)에 직접 매핑하는 시스템 콜이다.

![image.png](https://github.com/user-attachments/assets/d925f318-991e-42f3-9b44-a83d52f7ec95)

내부적으로 리눅스 커널은 프로세스의 가상 메모리 레이아웃을 `vm_area_struct`라는 연결 리스트 자료구조로 관리한다. `mmap`을 호출하면 커널은 대상 파일의 메타데이터(Inode)와 연결된 새로운 `vm_area_struct` 영역을 생성하여 프로세스의 주소 공간에 편입시킨다.

![image.png](https://github.com/user-attachments/assets/31257949-958f-4cbb-8e1c-bd9e3881d1af)

이때 핵심이 되는 두 가지 특성은 **물리 메모리의 Lazy Loading**과 **Zero-Copy**다.

`mmap` 시스템 콜을 호출한 직후에는 프로세스에 가상 주소 공간만 할당될 뿐, 실제 디스크 I/O를 통해 물리 메모리로 데이터를 가져오지는 않는다. 이후 프로세스가 반환받은 포인터 주소에 처음 접근할 때 비로소 커널 영역의 페이지 캐시(Page Cache)와 유저 프로세스의 가상 주소가 직접 매핑된다.

전통적인 `read()` 시스템 콜이 디스크 컨트롤러 → 커널 버퍼(페이지 캐시) → 유저 공간 버퍼로 총 2번의 데이터 복사를 유발하여 CPU 사이클을 낭비하는 반면

![image.png](https://github.com/user-attachments/assets/0f1ab451-12b4-4e13-9c31-fbeb6c3a66cf)

`mmap`은 커널이 관리하는 페이지 캐시 메모리 영역을 유저 프로세스가 직접 참조하게 만들므로, 유저 공간으로의 메모리 복사 오버헤드와 잦은 Context Switching이 완전히 사라진다.

![image.png](https://github.com/user-attachments/assets/83ba8bec-03a5-41e9-97a4-ed0dbb8d0465)

### 프로메테우스에서의 mmap

프로메테우스는 실시간 데이터를 인메모리(Head)에 붙들고 있다가 flush 하면서 디스크에 내려보내고 데이터를 M-map 구간에서 참조만 하고 있다.

![image.png](https://github.com/user-attachments/assets/5b48e2da-9628-452b-955d-32afdd61848f)

- **동작 방식:** 프로메테우스(v2.19.0 이후)는 활성 청크(Active chunk)에 120개의 샘플이 가득 차면, 이를 더 이상 애플리케이션(Go 런타임)의 힙(Heap) 영역에 유지하지 않는다. 대신 청크를 디스크 파일로 즉각 플러시(Flush)한 뒤, `mmap()` 시스템 콜을 호출하여 해당 파일을 매핑한다.
- **의의:** 프로메테우스는 파일 단위의 디스크 I/O 로직(`read`/`write`)이나 자체적인 데이터베이스 버퍼 풀을 복잡하게 구현하지 않는다. `tsdb/head.go`가 메모리 맵핑을 '블랙박스'처럼 사용한다고 언급했듯, 파일과 메모리를 연결하는 무거운 작업을 전적으로 OS 커널에 위임한다.

---

## Virtual Memory 개념

가상 메모리(Virtual Memory)는 프로세스가 파편화된 물리적 RAM의 상태나 다른 프로세스의 간섭을 전혀 신경 쓰지 않고, 0번지부터 시작하는 거대하고 연속적인 독립된 주소 공간을 독점하고 있다는 '환상(Illusion)'을 제공하는 하드웨어와 OS의 합작 아키텍처다.

가상 메모리 시스템의 근간은 **주소 변환(Address Translation)**이다. CPU가 실행하는 기계어 명령어에 담긴 모든 메모리 포인터는 논리적인 가상 주소다. CPU가 이 가상 주소에 접근하려 할 때마다, CPU 내부에 장착된 하드웨어인 **MMU(Memory Management Unit)**가 가상 주소를 실제 RAM의 물리 주소로 실시간 변환한다.

이 변환 규칙은 물리 메모리에 저장된 **페이지 테이블(Page Table)**이라는 커널 자료구조에 명시되어 있다. 

현대의 64비트 아키텍처(x86_64)에서는 메모리 낭비를 막기 위해 페이지 테이블을 4단계(PGD → PUD → PMD → PTE)의 다층 계층 구조로 분할하여 관리한다. 

![image.png](https://github.com/user-attachments/assets/b82f4c59-1952-42e3-ae31-2a29e79360ce)

매번 메모리에 위치한 페이지 테이블을 여러 번 뒤져야 하는 오버헤드를 극복하기 위해, MMU 내부에는 **TLB(Translation Lookaside Buffer)**라는 초고속 SRAM 기반의 캐시가 존재하여 최근에 변환이 완료된 가상-물리 주소 쌍을 하드웨어 레벨에서 임시 저장한다.

### 프로메테우스에서의 Virtual memory

![image.png](https://github.com/user-attachments/assets/e25de0df-0570-4e9a-845d-e1a69f16deaa)

- **동작 방식:** 청크 파일이 디스크로 플러시되고 `mmap`이 완료되면, OS는 해당 파일을 프로메테우스 프로세스의 가상 주소 공간(Virtual Address Space)에 연결하고 그 시작 주소(Reference)를 반환한다.
- **의의:** Head 블록이 최대 3시간(`chunkRange*3/2`) 분량의 데이터를 보관한다고 설명한다. 이 방대한 데이터를 물리 메모리(RAM)에 모두 적재하면 메모리 낭비가 심하고 Go GC(Garbage Collector)의 부하가 극심해진다. 프로메테우스는 실제 데이터를 가상 메모리 영역으로 밀어내고 메모리 포인터(Reference)만 유지함으로써, 물리적 한계를 초과하는 대용량 시계열 데이터를 안정적으로 관리할 수 있는 논리적 기반을 확보한다.

---

## Demand Paging 개념

요구 페이징(Demand Paging)은 가상 메모리 시스템에서 한정된 RAM 공간의 낭비를 극도로 줄이기 위해, 프로세스가 **"실제로 해당 메모리 주소에 접근(Read/Write)하는 그 순간"**에만 물리적인 RAM 프레임을 할당하는 철저한 지연 로딩(Lazy-loading) 기법이다.

![image.png](https://github.com/user-attachments/assets/6f74effa-1900-463f-b49a-18bba41567fc)

이 메커니즘은 전적으로 하드웨어 인터럽트인 Page Fault를 기반으로 작동한다.

1. 프로세스가 특정 가상 주소에 접근하면 MMU가 페이지 테이블을 조회한다.
2. 조회한 페이지 테이블 엔트리(PTE)의 Present Bit(또는 Valid Bit)가 0으로 설정되어 있다면, 접근하려는 데이터가 물리 메모리에 없다는 뜻이므로 CPU는 실행을 중단하고 하드웨어 예외인 Page Fault를 발생시켜 커널 모드로 진입한다.
3. 제어권을 넘겨받은 커널의 Page Fault Handler가 호출된다. 만약 요구한 데이터가 디스크에 있어서 실제 디스크 I/O 작업이 수반되어야 한다면 이를 **Major Fault**라고 부르며, 디스크에서 데이터를 읽어와 물리 메모리의 빈 프레임에 적재하고 페이지 테이블을 갱신한다.
4. 반면, 데이터가 커널의 페이지 캐시에 이미 존재하지만 현재 프로세스의 페이지 테이블과 연결만 안 된 상태라면 디스크 I/O 없이 매핑만 업데이트하는 **Minor Fault**가 발생하여 매우 빠르게 처리된다.

이러한 메커니즘 덕분에 OS는 당장 실행에 필요한 워킹 세트(Working Set) 페이지만을 물리 메모리에 올려두며, 남는 메모리가 부족해지면 `kswapd` 같은 커널 백그라운드 스레드가 LRU(Least Recently Used) 계열의 알고리즘을 사용해 당장 안 쓰는 페이지를 디스크로 쫓아내는 축출(Eviction)을 백그라운드에서 수행한다.

### 프로메테우스에서의 Demand Paging

- **동작 방식:** 가상 주소(Reference)만 보관하고 있는 상태에서, 사용자가 과거 데이터를 조회(Query)하기 위해 프로메테우스가 해당 메모리 주소에 접근(Read)하는 상황이 발생한다. 이때 해당 주소의 데이터가 아직 물리 메모리에 올라와 있지 않다면 페이지 부재(Page Fault)가 발생한다.
- **의의:** 페이지 부재가 발생하면 OS 커널이 개입하여, 디스크의 청크 파일에서 정확히 쿼리에 필요한 4KB 페이지 블록들만 물리 메모리(Page Cache)로 동적 로드(Dynamically load)한다. Demand Paging 메커니즘 덕분에 프로메테우스는 애플리케이션 레벨의 캐시 적중/실패(Cache Hit/Miss) 로직을 짤 필요 없이, 단순히 메모리를 읽는 행위만으로 OS가 필요한 순간에만 최소한의 디스크 I/O를 수행하도록 유도한다.