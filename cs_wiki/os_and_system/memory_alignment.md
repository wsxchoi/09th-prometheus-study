> **주제:** Memory Alignment — 컴퓨터 구조의 근본 원리가 Prometheus TSDB 성능 최적화에 녹아든 방식
> **흐름:** RISC Architecture → Load/Store → Word → Alignment Restriction → Binary 끝자리 트릭 → Compiler Padding → Prometheus Series ID 압축

---

# Part 1. 배경: RISC Architecture와 Load/Store 구조

## 1-1. CPU는 메모리를 직접 연산할 수 없다

MIPS, RISC-V 같은 **RISC(Reduced Instruction Set Computer)** Processor는 "명령어는 최대한 단순하게, 실행은 무조건 빠르게"를 모토로 합니다. 이 설계의 핵심 규칙이 **Load/Store Architecture**입니다.

CPU는 **절대 메모리에 있는 데이터를 직접 더하거나 뺄 수 없습니다.** 덧셈, 뺄셈 같은 ALU 연산은 오직 CPU 내부의 **Register**들끼리만 가능합니다. 따라서 메모리의 데이터를 가지고 연산하려면:

1. 메모리에서 Register로 가져온다 → **Load** (`lw`)
2. Register끼리 연산한다 → **ALU Operation** (`add`, `sub` 등)
3. 결과를 메모리로 돌려보낸다 → **Store** (`sw`)

이 3단계가 반드시 필요합니다. 이것이 "Load/Store Architecture"입니다.

```
Memory ──lw──→ Register ──연산──→ Register ──sw──→ Memory
```

> x86(CISC)은 `ADD [memory], value` 처럼 메모리를 직접 연산하는 명령어가 존재합니다. 하지만 내부적으로는 RISC처럼 Load → 연산 → Store를 Micro-op으로 분해하여 실행합니다. 근본 원리는 동일합니다.

## 1-2. Memory는 Byte-Addressable하다

메모리는 본질적으로 **1 Byte(8 bit)마다 주소가 하나씩 부여**되는 거대한 1차원 배열입니다.

```
주소:  0x00  0x01  0x02  0x03  0x04  0x05  0x06  0x07 ...
      ┌────┬────┬────┬────┬────┬────┬────┬────┐
데이터: │ AA │ BB │ CC │ DD │ EE │ FF │ 11 │ 22 │ ...
      └────┴────┴────┴────┴────┴────┴────┴────┘
       1B   1B   1B   1B   1B   1B   1B   1B
```

주소 `0x00`은 1 Byte인 `AA`를 가리킵니다. 주소 `0x01`은 `BB`를 가리킵니다. 이것이 **Byte-Addressable** 메모리입니다.

하지만 CPU Register의 크기는 32 bit(4 Byte) 또는 64 bit(8 Byte)입니다. CPU가 한 번에 처리하는 데이터의 기본 단위를 **Word**라고 부릅니다.

| Architecture | Word 크기 | Register 크기 |
| --- | --- | --- |
| 32-bit MIPS | **4 Bytes** (32 bits) | 32 bits |
| 64-bit x86_64 / ARM64 | **8 Bytes** (64 bits) | 64 bits |

---

# Part 2. `lw`와 `sw` — Word 단위로 메모리에 접근

## 2-1. lw (Load Word) 명령어

📎 **[슬라이드: Memory Operands — Main memory for composite data, Load/Store, Byte addressed, Words aligned, Big Endian]** 여기에 Memory Operands 슬라이드를 삽입하세요.

`lw` (Load Word): "메모리의 특정 주소부터 **4 Byte 뭉탱이**를 한 번에 가져와서 Register에 넣어라."
`sw` (Store Word): "Register의 **4 Byte 뭉탱이**를 메모리의 특정 주소에 써라."

## 2-2. lw 명령어의 실제 동작

📎 **[슬라이드: lw $t0, 24($s3) — Base Register + Offset 주소 계산, Binary 덧셈, Memory 다이어그램]** 여기에 lw 명령어 예시 슬라이드를 삽입하세요.

```
lw $t0, 24($s3)
```

이 명령어를 분해하면:

| 필드 | 값 | 의미 |
| --- | --- | --- |
| `lw` | Opcode 35 | Load Word 연산 |
| `$t0` | Register 8 | 데이터를 넣을 **목적지 Register** |
| `24` | Offset (Immediate) | Base 주소로부터의 **바이트 단위 오프셋** |
| `$s3` | Register 19 | **Base Address**를 담고 있는 Register |

**주소 계산 과정:**

```
실효 주소(Effective Address) = $s3 + 24

$s3 = 0x12004094 (Base Address)
24  = 0x00000018 (Offset, 10진수 24 = 16진수 0x18)

  0x12004094    →  ...0001 1000  (24₁₀)
+ 0x00000018    →  ...1001 0100  ($s3)
─────────────      ─────────────
  0x120040AC    →  ...1010 1100

→ 메모리 주소 0x120040AC에서 4 Byte를 읽어 $t0에 저장
```

**왜 Offset이 Byte 단위인가:**

메모리가 Byte-Addressable이기 때문입니다. Offset 24는 "Base 주소로부터 24 Byte 떨어진 곳"을 의미합니다. 만약 `int` 배열 `A[]`의 Base 주소가 `$s3`에 있고, `A[6]`에 접근하고 싶다면:

```
A[6]의 Offset = 6 × 4 Bytes = 24 Bytes
→ lw $t0, 24($s3)
```

`int`가 4 Byte이므로 인덱스에 4를 곱합니다. 이것이 C 언어에서 `*(arr + 6)`이 내부적으로 `arr + 6*sizeof(int)`가 되는 이유입니다.

---

# Part 3. Alignment Restriction — 하드웨어는 이기적이다

## 3-1. Word는 반드시 정렬되어야 한다

CPU가 메모리에서 4 Byte(1 Word)를 Load할 때, **아무 주소에서나 시작할 수 있는 게 아닙니다.** 시작 주소가 반드시 **4의 배수**여야 합니다.

```
✅ 유효한 Word 주소:  0, 4, 8, 12, 16, 20, 24, ...  (4의 배수)
❌ 무효한 Word 주소:  1, 2, 3, 5, 6, 7, 9, 10, 11, ... (4의 배수 아님)
```

이것이 **Alignment Restriction(정렬 제약)**입니다.

## 3-2. 왜 이런 제약을 걸었는가 — 하드웨어 배선의 물리적 이유

CPU와 메모리를 연결하는 **Data Bus**는 32가닥(4 Byte) 또는 64가닥(8 Byte)의 전선 다발입니다. 메모리 칩은 이 Bus의 폭에 맞춰 **한 사이클에 딱 Bus 폭만큼의 데이터를 내보내도록** 설계되어 있습니다.

**Aligned Access (정렬된 접근) — 한 번에 끝:**

주소 `0x00`부터 4 Byte를 읽으라고 하면:

```
메모리:  [0x00][0x01][0x02][0x03] ← 이 4칸이 정확히 Bus 1회 전송에 매핑
Data Bus: ═══════════════════════ → CPU Register로 한 번에 도착
```

Bus 배선이 `0x00~0x03`, `0x04~0x07`, `0x08~0x0B`... 이렇게 4칸 단위로 그룹 지어져 있으므로, 정렬된 주소에서 읽으면 **메모리 칩이 한 번만 동작**하면 됩니다.

**Unaligned Access (비정렬 접근) — 대재앙:**

주소 `0x01`부터 4 Byte(0x01, 0x02, 0x03, 0x04)를 읽으라고 하면:

```
첫 번째 Bus 그룹:  [0x00][0x01][0x02][0x03] ← 이 중 0x01~0x03만 필요
두 번째 Bus 그룹:  [0x04][0x05][0x06][0x07] ← 이 중 0x04만 필요

→ 메모리를 2번 읽고, 비트를 자르고 이어 붙이는 Shift/Merge 회로가 필요!
→ 2배의 메모리 접근 + 추가 회로 = 느리고 비쌈
```

**RISC의 선택:** "하드웨어를 복잡하게 만들지 마!" → 비정렬 접근 자체를 **금지**. 4의 배수가 아닌 주소로 `lw`를 시도하면 **Alignment Fault Exception**을 발생시키고 프로그램을 죽여버립니다.

> **x86은 어떤가:** x86(CISC)은 Unaligned Access를 **허용**합니다. 하드웨어가 알아서 2번 읽고 이어붙입니다. 하지만 **성능 페널티**가 발생합니다. 허용은 하되 느린 것입니다. 현대 x86에서도 성능이 중요한 코드는 Aligned Access를 사용합니다.

## 3-3. 64-bit Architecture에서의 확장

| Architecture | Word 크기 | Alignment 요구 |
| --- | --- | --- |
| 32-bit MIPS | 4 Bytes | 주소가 **4의 배수** |
| 64-bit ARM64 (aarch64) | 8 Bytes | `ld` (Load Doubleword) 시 주소가 **8의 배수** |
| 64-bit x86_64 | 8 Bytes | Alignment 강제 안 함 (하지만 정렬 시 성능 향상) |

Rocky Linux가 실행되는 **aarch64**(ARM64)는 RISC 계열이므로 Alignment Restriction이 있습니다. Unaligned Access 시 Kernel이 Software로 처리할 수 있지만 극심한 성능 저하가 발생합니다.

---

# Part 4. 이진수 끝자리 `00`의 비밀 — 하드웨어의 치트키

## 4-1. 4의 배수를 검사하는 가장 빠른 방법

주소가 4의 배수인지 어떻게 가장 빨리 알 수 있을까요? 이진수로 변환하면 패턴이 보입니다.

```
 0 = 0000 00    ← 끝 2자리 00
 4 = 0001 00    ← 끝 2자리 00
 8 = 0010 00    ← 끝 2자리 00
12 = 0011 00    ← 끝 2자리 00
16 = 0100 00    ← 끝 2자리 00

 1 = 0000 01    ← 끝자리 01 → 4의 배수 아님
 2 = 0000 10    ← 끝자리 10 → 4의 배수 아님
 3 = 0000 11    ← 끝자리 11 → 4의 배수 아님
 5 = 0001 01    ← 끝자리 01 → 4의 배수 아님
```

**규칙:** 4의 배수는 이진수로 **맨 끝 두 자리가 반드시 `00`**입니다.

수학적으로 당연합니다: 끝자리(LSB)가 1이면 +1, 둘째자리가 1이면 +2가 추가되므로 4의 배수가 될 수 없습니다.

## 4-2. 하드웨어 설계자의 관점 — 비트 검사로 끝

CPU 회로 설계자 입장에서는 주소가 4의 배수인지 확인하기 위해 **나눗셈 회로(Modulo 연산)**를 만들 필요가 없습니다. 그냥 Address Bus의 **맨 끝 두 가닥(Least Significant 2 Bits)**에 전기 신호가 있는지만 확인하면 됩니다.

```
Address Bus: [ ... | bit3 | bit2 | bit1 | bit0 ]
                                    ↓      ↓
                              이 두 bit이 모두 0인가?
                                    ↓
                              bit1 OR bit0 == 0?  → Aligned!
                              bit1 OR bit0 != 0?  → Alignment Fault!
```

**OR 게이트 하나**로 끝입니다. 나노초도 안 걸리는 극한의 효율.

## 4-3. N의 배수 검사의 일반화

이 원리는 2의 거듭제곱 배수에 일반적으로 적용됩니다:

| 정렬 단위 | 이진수 조건 | 마스크 연산 |
| --- | --- | --- |
| 2-byte Aligned | 끝 **1 bit**가 `0` | `address & 0x1 == 0` |
| 4-byte Aligned | 끝 **2 bits**가 `00` | `address & 0x3 == 0` |
| 8-byte Aligned | 끝 **3 bits**가 `000` | `address & 0x7 == 0` |
| 16-byte Aligned | 끝 **4 bits**가 `0000` | `address & 0xF == 0` |
| 64-byte Aligned (Cache Line) | 끝 **6 bits**가 `000000` | `address & 0x3F == 0` |

이 마스크 연산은 CPU에서 **Bitwise AND 한 번**이면 끝나므로, Alignment 검사는 사실상 공짜입니다.

---

# Part 5. Compiler Padding — 소프트웨어가 하드웨어를 배려하는 방법

## 5-1. 왜 Struct에 빈 공간이 생기는가

C 언어에서 Struct를 선언하면 Compiler가 멤버 사이에 **Padding(빈 공간)**을 끼워넣습니다. 메모리를 낭비하면서까지!

```c
struct Example {
    char  a;    // 1 byte
    int   b;    // 4 bytes
    char  c;    // 1 byte
};
// 예상 크기: 1 + 4 + 1 = 6 bytes
// 실제 크기: 12 bytes (!)
```

**실제 메모리 배치:**

```
Offset:  0    1    2    3    4    5    6    7    8    9   10   11
       ┌────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┐
       │ a  │pad │pad │pad │ b  │ b  │ b  │ b  │ c  │pad │pad │pad │
       └────┴────┴────┴────┴────┴────┴────┴────┴────┴────┴────┴────┘
        char  ← 3B padding → int (4B, aligned to 4)  char ← 3B padding →
```

- `a` (char, 1B)는 Offset 0에 배치.
- `b` (int, 4B)는 **4의 배수 주소**에서 시작해야 하므로, Offset 1~3에 3 Byte Padding 삽입 → Offset 4에 배치.
- `c` (char, 1B)는 Offset 8에 배치.
- Struct 전체 크기가 **가장 큰 멤버(int, 4B)의 배수**가 되도록 끝에 3 Byte Padding 추가 → 총 12 Bytes.

## 5-2. Padding의 근본 이유

CPU가 `b`를 `lw`로 읽을 때, `b`의 시작 주소가 4의 배수가 아니면 **Alignment Fault**(RISC) 또는 **성능 저하**(x86)가 발생합니다. Compiler는 이를 방지하기 위해 메모리를 희생하고 성능을 취합니다.

이것이 "**하드웨어 친화적으로 데이터를 정렬하면 소프트웨어 성능이 극대화된다**"는 CS의 근본 진리입니다.

## 5-3. 멤버 순서를 바꾸면 Padding을 줄일 수 있다

```c
// 비효율적 순서 → 12 bytes
struct Bad {
    char  a;   // 1 + 3 padding
    int   b;   // 4
    char  c;   // 1 + 3 padding
};

// 효율적 순서 → 8 bytes
struct Good {
    int   b;   // 4
    char  a;   // 1
    char  c;   // 1 + 2 padding
};
```

**큰 멤버부터 작은 멤버 순서**로 선언하면 Padding을 최소화할 수 있습니다. 대량의 Struct를 메모리에 올리는 시스템(DB, TSDB, Game Engine 등)에서는 이 순서가 메모리 사용량과 Cache 효율에 직접적 영향을 줍니다.

---

# Part 6. CPU Cache와 Alignment — 성능의 실체

## 6-1. Cache Line

현대 CPU는 메모리를 직접 읽지 않습니다. **Cache(L1/L2/L3)**를 거칩니다. Cache는 메모리를 **Cache Line**(보통 64 Bytes) 단위로 가져옵니다.

```
메모리에서 1 Byte만 읽고 싶어도 → 해당 Byte가 속한 64 Byte 블록 전체를 Cache에 로드
```

**Alignment과 Cache Line의 관계:**

데이터가 Aligned되어 있으면 하나의 Cache Line 안에 깔끔하게 들어갈 확률이 높습니다. Unaligned 데이터는 **두 개의 Cache Line에 걸칠 수 있어** 2번의 Cache Line Fetch가 필요합니다.

```
Cache Line 0: [0x00 ───────────────── 0x3F]  (64 bytes)
Cache Line 1: [0x40 ───────────────── 0x7F]  (64 bytes)

Aligned 8B:   [0x38 ~ 0x3F]  → Cache Line 0 하나에서 해결 ✅
Unaligned 8B: [0x3C ~ 0x43]  → Cache Line 0 + Cache Line 1 걸침 ❌
              → Cache Miss 2번 → 성능 저하
```

## 6-2. mmap과 Alignment

파일을 `mmap()`으로 메모리에 매핑하면, 디스크의 파일 내용이 **Virtual Memory에 직접 매핑**됩니다. 이때 파일 내부의 데이터가 Aligned되어 있으면:

- CPU가 mmap된 메모리에서 데이터를 Load할 때 **Alignment Fault 없이** 최고 성능으로 접근
- Page Fault로 디스크에서 읽어올 때도 **Cache Line 경계에 맞아** 효율적 전송

이것이 Prometheus TSDB의 Index 파일 설계와 직결됩니다.

---

# Part 7. Prometheus TSDB에서의 Memory Alignment 응용

## 7-1. Persistent Block의 구조 (복습)

Prometheus TSDB의 디스크 Block은 4가지 컴포넌트로 구성됩니다:

```
data/
└── 01EM6Q6A1YPX4G9TEB20J22B2R/   (Block ULID)
    ├── chunks/      ← Raw Chunk 데이터 (시계열 Sample들)
    ├── index        ← 이 Block의 Index (Inverted Index)
    ├── meta.json    ← Block Metadata
    └── tombstones   ← 삭제 마커
```

**Index 파일**이 핵심입니다. 이 파일에 모든 Series 정보, Symbol Table, Postings List가 들어 있고, 쿼리 시 이 파일을 `mmap()`으로 메모리에 매핑하여 접근합니다.

## 7-2. Index 내부의 Series Section

Index의 Series Section에는 이 Block에 존재하는 **모든 Series**의 정보가 들어 있습니다. 각 Series 항목에는 Label Set과 Chunk Reference가 포함됩니다.

```
┌──────────────────────────────────────────────────────┐
│ len <uvarint>                                        │
├──────────────────────────────────────────────────────┤
│   labels count <uvarint64>                           │
│   ref(l_1.name) <uvarint32>  ← Symbol Table 참조     │
│   ref(l_1.value) <uvarint32>                         │
│   ...                                                │
│   chunks count <uvarint64>                           │
│   c_0.mint <varint64>                                │
│   c_0.maxt - c_0.mint <uvarint64>                    │
│   ref(c_0.data) <uvarint64>  ← Chunk 파일 참조       │
│   ...                                                │
├──────────────────────────────────────────────────────┤
│ CRC32 <4b>                                           │
└──────────────────────────────────────────────────────┘
```

## 7-3. 16-Byte Alignment — 핵심 최적화

여기서 Memory Alignment 지식이 기가 막히게 응용됩니다.

**각 Series 항목은 16 Byte Aligned입니다.** 즉, Series 항목이 시작되는 Byte Offset은 반드시 16으로 나누어떨어집니다.

```
Series 0: Offset 0    = 0x0000  (16 × 0)
Series 1: Offset 16   = 0x0010  (16 × 1)
Series 2: Offset 32   = 0x0020  (16 × 2)
Series 3: Offset 48   = 0x0030  (16 × 3)
...
Series N: Offset N×16  = 16N
```

실제 Series 데이터가 16 Byte보다 작으면 **Padding으로 채웁니다.** Compiler가 Struct에 Padding을 넣는 것과 완전히 같은 원리입니다. 메모리(디스크)를 약간 낭비하지만, 그 대가로 두 가지 엄청난 최적화를 얻습니다.

## 7-4. 최적화 1: Series ID 압축 — 같은 비트로 16배 더 큰 Offset을 가리키기

### 문제

Index 파일이 수십 GB로 커지면, 특정 Series의 위치(Offset)를 저장하는 데 큰 자료형이 필요합니다. Offset이 4GB를 넘으면 32 bit(4 Byte)로는 표현 불가능합니다.

### 해결 — Alignment으로 ID 압축

모든 Series가 16의 배수 Offset에 존재하므로:

```
Series ID = Offset / 16
```

| 실제 Offset | 이진수 (하위 4bit 항상 0000) | Series ID (Offset ÷ 16) |
| --- | --- | --- |
| 0 | 0000 **0000** | 0 |
| 16 | 0001 **0000** | 1 |
| 32 | 0010 **0000** | 2 |
| 48 | 0011 **0000** | 3 |
| 1,048,576 | 0001 0000 0000 0000 0000 **0000** | 65,536 |

16의 배수이므로 하위 4 bit는 **항상 `0000`**입니다 (Part 4에서 배운 원리!). 이 4 bit는 정보를 담고 있지 않으므로 **저장할 필요가 없습니다.**

4 Byte(32 bit) ID로 표현 가능한 최대값:
- Alignment 없이: 2^32 = 4 GiB까지의 Offset
- **16-byte Alignment 적용: 2^32 × 16 = 64 GiB까지의 Offset**

**같은 4 Byte로 16배 더 큰 파일을 가리킬 수 있습니다.** 디스크 공간 절약이자, Index 크기 축소입니다.

### 원래 Offset 복원

Series ID로 실제 파일 위치를 찾을 때는:

```
Offset = Series ID × 16
       = Series ID << 4    (Left Shift 4 bits)
```

**Bit Shift 한 번**이면 끝. CPU에서 가장 빠른 연산 중 하나입니다. 곱셈 회로를 사용할 필요도 없습니다.

```
Series ID:  0000 0000 0000 0000 0000 0001 0000 0000  (= 256)
                                                 ↓ << 4
Offset:     0000 0000 0000 0000 0001 0000 0000 0000  (= 4096)
```

이것이 Part 4에서 배운 "이진수 끝자리 트릭"의 실전 응용입니다.

> **이 ID가 Prometheus TSDB에서 "Posting"이라고 불립니다.** Inverted Index의 세계에서 Document ID를 Posting이라고 부르는 전통에서 온 이름입니다. Postings List는 이 Series ID(= Posting)의 정렬된 목록입니다.

## 7-5. 최적화 2: CPU Cache Line과 mmap Load 최적화

Prometheus는 Index 파일을 `mmap()`으로 Virtual Memory에 매핑합니다. 쿼리 시 CPU가 이 매핑된 메모리에서 Series 데이터를 Load합니다.

16-Byte Alignment이 보장되므로:

1. **Unaligned Access Penalty 제로:** CPU가 `ld` (Load Doubleword, 8 Byte)나 SIMD 명령어로 데이터를 가져올 때, 시작 주소가 항상 16의 배수이므로 **Alignment Fault가 절대 발생하지 않습니다.** 특히 aarch64(ARM64) 같은 RISC 계열에서는 이 보장이 필수적입니다.

2. **Cache Line 경계 걸침 방지:** 64-Byte Cache Line 안에 16-Byte 단위 데이터가 **정확히 4개** 들어갑니다 (64 / 16 = 4). 하나의 Series 항목이 두 Cache Line에 걸치는 일이 없으므로 Cache Miss가 최소화됩니다.

3. **Prefetch 효율 극대화:** CPU의 Hardware Prefetcher는 메모리 접근 패턴이 일정할 때 가장 잘 동작합니다. 16-Byte 간격으로 일정하게 배치된 데이터는 Prefetcher가 다음 접근 위치를 정확히 예측하여 미리 Cache에 올려놓을 수 있습니다.

## 7-6. Chunk Reference에서도 동일한 원리

Chunk 파일 내에서 특정 Chunk에 접근하기 위한 **Chunk Reference**도 비트 트릭을 사용합니다:

```
Reference = (파일번호 << 32) | 파일내_Offset
```

8 Byte Reference에서:
- 상위 4 Byte: Chunk 파일 번호 (1-based → 0-based 변환)
- 하위 4 Byte: 파일 내 Byte Offset

파일 번호가 `00093`이고 Chunk의 시작 Offset이 `1234`라면:

```
Reference = (92 << 32) | 1234
```

이것도 **Bit Shift와 Bitwise OR**로 하나의 정수에 두 가지 정보를 압축하는 기법입니다. Alignment과 직접적으로 결합되지는 않지만, 같은 "비트 레벨 사고방식"의 산물입니다.

---

# Part 8. 정리 — CS 근본에서 시스템 설계까지

```
[컴퓨터 구조]
  CPU는 Register끼리만 연산 가능 (Load/Store Architecture)
  메모리는 Byte-Addressable (1 주소 = 1 Byte)
  Word 접근 시 주소가 Word 크기의 배수여야 (Alignment Restriction)
     ↓
  이유: Data Bus 배선이 Word 단위로 그룹화
  비정렬 접근 = 2번 읽기 + Shift/Merge = 느리고 비쌈
     ↓
[이진수 트릭]
  N의 배수 = 하위 log₂(N) bits가 전부 0
  4의 배수 = 끝 2bit가 00 → AND 마스크 한 번으로 검사
  16의 배수 = 끝 4bit가 0000
     ↓
[Compiler]
  Struct 멤버 사이에 Padding 삽입 → Aligned Access 보장
  메모리 약간 낭비 + 성능 대폭 향상
     ↓
[Prometheus TSDB 응용]
  Series 항목을 16-Byte Aligned 배치
     ↓
  최적화 1: Series ID = Offset/16 → 같은 bit로 16배 큰 파일 가리킴
  최적화 2: mmap으로 메모리 매핑 시 Unaligned Penalty 제로 + Cache 효율 극대화
```

**"하드웨어 친화적으로 데이터를 정렬하면 소프트웨어 성능이 극대화된다"** — 이 CS의 근본 진리가 컴퓨터 구조 수업의 `lw` 명령어에서 시작하여, C Compiler의 Struct Padding을 거쳐, Prometheus라는 실전 오픈소스 시스템의 TSDB Index 설계에까지 녹아들어 있습니다.