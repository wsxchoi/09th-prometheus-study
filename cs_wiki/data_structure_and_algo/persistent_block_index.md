# 0. Overview

이 문서는 Prometheus가 inverted index를 사용하는 방식 중에서도,  
**Head가 compact된 뒤 생성되는 persistent block의 `index` 파일**을 대상으로 한다.

즉, 이 문서의 관심사는 다음 두 가지다.

- persistent block의 `index` 파일은 어떤 역할을 하는가
- 그 안에서 **Symbol Table**과 **postings(inverted index를 구성하는 구조)** 가 어떻게 함께 쓰이는가

Prometheus는 Head의 앞부분이 compaction되면 immutable한 persistent block을 만든다.
이 block은 `meta.json`, `chunks/`, `tombstones`, 과 함께 `index` 파일을 가지며,  
이 `index` 파일이 바로 디스크 기반 탐색 구조이다.

``` 
┌────────────────────────────┬─────────────────────┐
│ magic(0xBAAAD700) <4b>     │ version(1) <1 byte> │
├────────────────────────────┴─────────────────────┤
│ ┌──────────────────────────────────────────────┐ │
│ │                 Symbol Table                 │ │
│ ├──────────────────────────────────────────────┤ │
│ │                    Series                    │ │
│ ├──────────────────────────────────────────────┤ │
│ │                 Label Index 1                │ │
│ ├──────────────────────────────────────────────┤ │
│ │                      ...                     │ │
│ ├──────────────────────────────────────────────┤ │
│ │                 Label Index N                │ │
│ ├──────────────────────────────────────────────┤ │
│ │                   Postings 1                 │ │
│ ├──────────────────────────────────────────────┤ │
│ │                      ...                     │ │
│ ├──────────────────────────────────────────────┤ │
│ │                   Postings N                 │ │
│ ├──────────────────────────────────────────────┤ │
│ │              Label Offset Table              │ │
│ ├──────────────────────────────────────────────┤ │
│ │             Postings Offset Table            │ │
│ ├──────────────────────────────────────────────┤ │
│ │                      TOC                     │ │
│ └──────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────┘
```
이때 Prometheus는

- `Symbol Table`에 label name/value 문자열을 중복 없이 저장하고
- `Series` 영역에 각 series의 label 정보와 chunk metadata를 저장하며
- `Label Index`와 `Postings`를 통해 label 기반 탐색 경로를 만든다
- `Label Offset Table`, `Postings Offset Table`, `TOC`를 통해 각 섹션의 위치를 빠르게 찾아간다

즉, persistent block의 `index` 파일은 단순한 메타데이터 묶음이 아니라,
문자열 저장 최적화와 label 기반 검색 최적화를 함께 수행하는 디스크 탐색 구조다.

이 구조 안에서 특히 이 문서가 집중해서 보는 부분은 다음 두 가지다.

- 반복되는 문자열을 줄이기 위한 **symbol table**
- label 조건으로 series를 찾기 위한 **postings**

를 함께 저장해, persistent block을 작고 빠르게 유지한다.

---
# 1. Symbol Table

## 1.1 Definition

Symbol Table은 persistent block의 `index` 파일 안에서 사용되는 label 문자열들을  
중복 없이, 정렬된 상태로 저장하는 구조다.

일반적인 목적은 다음과 같다.

- 동일 문자열의 중복 저장 방지
- 저장 공간 절약
- 비교 및 탐색 비용 감소

Prometheus의 persistent block에서는 label name과 label value 문자열이 매우 자주 반복된다.

예를 들어 어떤 시계열이 아래와 같다면:

```text
{a="y", x="b"}
```

symbol table에는 다음 문자열들이 저장된다.

- `a`
- `b`
- `x`
- `y`

이처럼 문자열을 series마다 직접 반복 저장하지 않고,
block의 `index` 파일 안에 한 번만 저장해두면 index 크기를 크게 줄일 수 있다.

또한 index의 다른 section들은 문자열을 직접 저장하지 않고,
symbol table에 저장된 문자열을 참조한다.
이때 각 symbol이 파일 내에서 시작하는 바이트 offset이 그 symbol의 reference 역할을 한다.

## 1.2 Role in Prometheus

persistent block의 `index` 파일은 immutable하다.  
즉, 한 번 만들어진 뒤에는 block 내부의 series 메타데이터와 탐색 구조가 바뀌지 않는다.

이 특성 덕분에 Prometheus는 compaction 시점에 block 전체를 스캔해
해당 block에서 필요한 문자열들을 모아 symbol table로 만들 수 있다.

이 구조의 효과는 명확하다.

- 같은 label name/value를 여러 번 쓰지 않아도 됨
- index 파일 크기가 줄어듦
- 디스크에서 읽어야 할 메타데이터 양이 줄어듦

즉, symbol table은 persistent block의 index를 **작게 만들기 위한 압축성 구조**라고 볼 수 있다.

---
# 2. Inverted Index

## 2.1 Definition

Inverted Index는 검색 조건에서 대상 목록으로 바로 갈 수 있게 만드는 역방향 인덱스 구조다.

즉, 일반적으로 데이터를 저장할 때의 관점이

- `series -> label set`

이라면, inverted index는 이를 뒤집어

- `label pair -> series들의 목록`

으로 저장한다.

Prometheus의 시계열을 예로 들면, 실제 series 정보는 다음처럼 label set을 가진다.

```text
series A -> {job="api", method="GET", status="200"}
series B -> {job="api", method="POST", status="200"}
series C -> {job="web", method="GET", status="500"}
```

이걸 inverted index 관점으로 뒤집으면 다음처럼 된다.

```text
job="api"    -> [A, B]
method="GET" -> [A, C]
status="200" -> [A, B]
```

즉, inverted index의 핵심은
**label pair를 입력으로 받아 그 조건을 만족하는 series 후보 목록을 바로 얻는 것**이다.

Prometheus의 persistent block에서는 이 구조가 on-disk 형태로 직렬화되어 `index` 파일 안에 저장된다.

## 2.2 Role in Prometheus

Prometheus가 persistent block에서 inverted index를 사용한다는 것은,
label pair를 통해 해당 block 안의 series 후보를 빠르게 찾을 수 있다는 뜻이다.

여기서 먼저 짚고 갈 점은, Prometheus TSDB에서 posting은 결국 특정 series ID를 가리키는 값이라는 점이다.

persistent block의 `Series` 영역에는 해당 block에 존재하는 모든 시계열 정보가 저장되며,
각 series는 label set 기준으로 사전순 정렬되어 있다.

또한 series entry는 16바이트 단위로 정렬되므로 series ID는 offset과 다음 관계를 가진다.

```text
series ID = offset / 16
offset = ID * 16
```

즉, posting list 안에 저장되는 값은 결국 `Series` 영역의 특정 entry를 가리키는 series ID라고 볼 수 있다.  
다시 말해, persistent block에서 inverted index는

- key: `job="api"` 같은 label pair
- value: 그 조건에 맞는 series ID들의 정렬된 목록

으로 구성된 on-disk 검색 구조다.

예를 들면 개념적으로 다음과 같다.

```text
job="api"       -> [series #7, series #18, series #24]
method="GET"    -> [series #7, series #24]
status="200"    -> [series #7, series #11, series #24]
```

Prometheus는 persistent block의 `index` 파일에 이런 posting list들을 저장해 둠으로써,
쿼리 시 block 안의 모든 series를 순회하지 않고도 조건에 맞는 series 후보를 빠르게 찾을 수 있게 한다.

## 2.3 Postings와 Postings Offset Table

postings는 각 label pair에 대한 posting list를 저장하는 영역이다.

예를 들어 block 안에 다음 두 시계열이 있다고 하자.

```text
{a="b", x="y1"} -> series ID 120
{a="b", x="y2"} -> series ID 145
```

그러면 postings는 개념적으로 다음과 같이 저장될 수 있다.

```text
a="b"  -> [120, 145]
x="y1" -> [120]
x="y2" -> [145]
```

이때 `Postings Offset Table`은 각 label pair가 어떤 posting list를 가리키는지 offset으로 연결해주는 테이블이다.

예를 들면:

```text
a="b"  -> offset 100
x="y1" -> offset 200
x="y2" -> offset 240
```

즉, 쿼리 시 Prometheus는 먼저 `Postings Offset Table`에서
원하는 label pair의 posting list 위치를 찾고,
그 다음 해당 offset으로 이동해 실제 series ID 목록을 읽는다.

이 구조 덕분에 label pair마다 posting list를 직접 선형 탐색하지 않고도,
필요한 postings에 빠르게 접근할 수 있다.

---
# 3. Why This Structure Is Needed After Compaction

Head의 앞부분이 persistent block으로 내려가는 순간,
해당 시간 구간의 데이터는 더 이상 수정되지 않는 immutable 데이터가 된다.

이때 Prometheus가 필요한 것은 단순 저장이 아니라
**나중에 이 block을 다시 빠르게 읽기 위한 검색 구조**다.

만약 persistent block에 `chunks`만 저장하고 `index`가 없다면:

- 어떤 series가 block에 들어있는지 찾기 어렵고
- label matcher 조건을 block마다 매번 전체 순회해야 하며
- 디스크 기반 쿼리 비용이 크게 증가한다.

반대로 `index` 파일 안에 symbol table과 postings를 함께 두면:

- 문자열은 중복 없이 compact하게 저장되고
- label 조건으로 series를 빠르게 좁힐 수 있으며
- immutable block을 효율적으로 조회할 수 있다.

즉, persistent block의 `index`는
compaction 이후의 읽기 성능을 책임지는 핵심 구조다.
