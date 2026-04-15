# Lazy Evaluation과 Lazy Loading — Prometheus TSDB 쿼리에서의 적용

> https://ganeshvernekar.com/blog/prometheus-tsdb-queries 기반으로 정리

## 1. Lazy Evaluation과 Lazy Loading

### 1.1 Lazy Evaluation (지연 평가)

Lazy Evaluation은 값이 실제로 필요해질 때까지 계산을 미루는 전략이다. 반대 개념인 Eager Evaluation에서는 표현식을 만나면 즉시 계산해서 결과를 확정짓지만, Lazy Evaluation은 “이 값을 나중에 계산할 수 있는 방법”만 기록해두고, 누군가가 그 값을 실제로 소비하려 할 때 비로소 계산을 수행한다.

### 1.2 Lazy Loading (지연 로딩)

Lazy Loading은 데이터(리소스)를 실제로 접근할 때까지 디스크나 네트워크에서 메모리로 올리지 않는 전략이다. Lazy Evaluation이 “계산의 지연”이라면, Lazy Loading은 “I/O(데이터 적재)의 지연”이다.

웹에서 흔히 볼 수 있는 이미지 Lazy Loading이 대표적인 예시다. 페이지를 열면 뷰포트에 보이지 않는 이미지는 로드하지 않다가, 사용자가 스크롤해서 해당 영역이 보이는 시점에 비로소 네트워크 요청을 보낸다.

### 1.3 Iterator 패턴으로 이해하기

Lazy Evaluation, Lazy Loading이 활용된 예로 Iterator 패턴이 있다. Iterator는 컬렉션의 원소를 하나씩 순회하는 객체로, 핵심 인터페이스는 `Next()`다. 호출할 때마다 다음 원소를 하나 반환하고, 더 이상 원소가 없으면 종료를 알린다.

Eager 방식과 Lazy(Iterator) 방식의 차이를 비교하면 다음과 같다.

```python
# ids: [1,2,3,4,...,1000000]
# Eager: 전부 계산해서 리스트로 반환
def get_all_results(ids):
    results = []
    for id in ids:
        results.append(load_from_disk(id))  # 전부 메모리에 올림
    return results

# Lazy: iterator로 반환, 소비할 때 계산 + 로드
def get_results_lazy(ids):
    for id in ids:
        yield load_from_disk(id)  # 호출될 때마다 하나씩
```

Eager 방식은 함수가 반환되는 시점에 모든 데이터가 이미 계산되어 메모리에 올라가 있다. 반면 Lazy 방식은 `yield`를 통해 iterator를 반환하며, 소비자가 `next()`를 호출할 때마다 한 건씩 계산(Lazy Evaluation)하고 디스크에서 로드(Lazy Loading)한다. 전체 데이터가 100만 건이어도 메모리에는 현재 소비 중인 1건만 존재하게 된다.

이를 소비하는 쪽의 코드는 다음과 같다.

```python
# === Eager 방식 ===
results = get_all_results(ids)  # 이 한 줄에서 전체 계산 + 전체 디스크 로드 완료
print(results[0])               # 이미 메모리에 있는 값을 꺼낼 뿐

# === Lazy 방식 ===
it = get_results_lazy(ids)      # 이 시점에는 아무 일도 일어나지 않는다
result1 = next(it)              # 이 시점에 ids[0]에 대한 계산 + 디스크 로드 발생
result2 = next(it)              # 이 시점에 ids[1]에 대한 계산 + 디스크 로드 발생
# ids[2] 이후는 next()를 호출하지 않는 한 계산도, 로드도 일어나지 않는다
```

`get_results_lazy(ids)`를 호출한 시점에는 아무 일도 일어나지 않는다. iterator 객체만 생성될 뿐, 실제 계산과 I/O는 `next()`를 호출하는 순간에 발생한다. 소비자가 결과 2건만 필요해서 `next()`를 2번만 호출하면, 나머지 데이터에 대한 계산과 디스크 접근은 수행되지 않는다.

-----

## 2. Prometheus TSDB 쿼리에서의 적용

### 2.1 왜 Lazy가 필요한가

Prometheus TSDB의 `Select([]matcher)` 쿼리는 matcher 조건에 맞는 시리즈의 샘플 데이터를 반환한다. 대규모 운영 환경에서는 단일 Prometheus 인스턴스가 수백만 개의 시리즈를 저장하며, 하나의 쿼리가 매칭하는 시리즈 수가 수십만 개에 달할 수 있다. 각 시리즈는 다시 여러 chunk로 구성되고, 각 chunk는 수백 바이트에서 수 KB의 압축된 샘플 데이터를 담고 있다.

이 상황에서 Eager 방식, 즉 매칭되는 모든 시리즈의 posting list를 전부 메모리에 올리고, 모든 chunk를 한꺼번에 로드한 뒤 결과를 반환하는 방식은 두 가지 문제를 일으킨다. 첫째, 메모리 사용량이 쿼리 결과 크기에 비례해서 폭발한다. 둘째, 첫 번째 결과를 반환하기까지의 지연 시간이 전체 데이터 로드 시간만큼 길어진다. PromQL 엔진이 실제로 필요한 것은 시리즈를 하나씩 순차적으로 소비하는 것이므로, 전체를 미리 준비할 필요가 없다.

이 때문에 Prometheus TSDB는 쿼리의 전 구간에 걸쳐 Lazy Evaluation과 Lazy Loading을 적용한다.

(요약)

* series가 너무 많다
* 메모리에 다 못올린다

### 2.2 mmap과 Demand Paging

persistent block의 파일은 mmap으로 매핑되기 때문에 Lazy 전략을 자연스럽게 사용할 수 있다.

Go 코드에서 index 파일을 읽을 때는 일반 byte slice를 인덱싱하는 것과 동일하게 작성한다.

```go
// posting 하나를 읽는 코드 — 일반 메모리 접근과 구분이 없다
seriesID := binary.BigEndian.Uint32(indexBytes[offset:])
```

이 코드가 실행되면 CPU의 MMU(Memory Management Unit)가 해당 가상 주소를 물리 주소로 변환한다. 만약 해당 페이지가 이미 물리 메모리에 올라가 있다면(page table에 valid bit이 세팅되어 있다면) 일반 메모리 접근과 동일한 속도(수 ns)로 데이터를 읽는다. 반면 해당 페이지가 물리 메모리에 없다면 page fault가 발생하고, 커널이 디스크에서 해당 파일 오프셋의 페이지를 page cache로 읽어온 뒤 물리 프레임에 매핑한다. 이것이 demand paging이다.

이 처리는 전부 하드웨어(MMU)와 커널 레벨에서 투명하게 일어난다.

### 2.3 Posting List의 Lazy Iteration

이 mmap 구조 위에서 각 matcher의 posting list는 전체를 메모리에 펼쳐놓는 것이 아니라 iterator로 표현된다. 예를 들어 `job=~"app.*"` matcher를 처리할 때, `job="app1"`의 posting list iterator와 `job="app2"`의 posting list iterator를 Union Iterator로 감싸 sorted merge를 수행한다. 이때 전체 posting을 미리 합쳐놓는 것이 아니라, `Next()`가 호출될 때마다 양쪽 iterator에서 하나씩 꺼내 비교하는 방식이다.

여러 matcher가 있는 경우도 마찬가지다. 각 matcher의 결과 iterator를 Intersect Iterator로 감싸면, `Next()` 호출 시 내부의 iterator들이 연쇄적으로 `Next()`를 호출하면서 교집합에 해당하는 series ID만 반환한다. 최종적으로 아무리 복잡한 matcher 조합이라도 하나의 iterator로 축약되며, 전체 posting list가 한꺼번에 메모리에 올라가는 시점은 존재하지 않는다.

```
[mmap된 인덱스 파일]
       ↓ demand paging으로 필요한 페이지만 물리 메모리에 적재
[matcher1 posting iterator]   [matcher2 posting iterator]
       ↓                              ↓
     ┌──────────────────────────────────┐
     │   Intersect Iterator (lazy)      │
     └───────────────┬──────────────────┘
                     ↓ Next() 호출 시 연쇄적으로 내부 iterator 구동
              [최종 series ID 스트림]
```

### 2.4 Sample Iterator의 Lazy Loading

`Select([]matcher)`는 모든 시리즈의 샘플을 한꺼번에 반환하지 않는다. 시리즈를 하나씩 순회하는 series iterator를 반환하고, 각 시리즈의 sample iterator는 chunk 데이터를 실제로 순회하려는 시점에 비로소 chunk 파일에서 로드한다.(Lazy Loading)

결과적으로 전체 파이프라인은 다음과 같다.

```
PromQL 엔진이 Next() 호출
       ↓
  Series Iterator — 다음 시리즈의 sample iterator를 반환 (Lazy Evaluation)
       ↓
  Sample Iterator — 해당 시리즈의 chunk를 디스크에서 로드 (Lazy Loading)
       ↓
  개별 샘플 (timestamp, value) 반환
```

PromQL 엔진이 소비하는 속도에 맞춰 각 단계가 필요한 만큼만 계산하고 로드한다. 

* 결론: 매칭되는 시리즈가 수십만 개라 해도 실제로 메모리에 올라가 있는 것은 현재 소비 중인 시리즈의 chunk 데이터뿐이다.