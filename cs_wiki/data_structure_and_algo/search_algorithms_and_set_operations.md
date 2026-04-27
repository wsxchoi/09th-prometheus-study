# 0. Overview

이 문서는 Prometheus가 label 기반 탐색을 수행할 때 자주 등장하는  
**검색 알고리즘 및 집합 연산(Search Algorithms & Set Operations)** 을 정리한다.

특히 관심사는 다음 네 가지다.

- **Intersection**: 여러 조건을 모두 만족하는 대상을 찾는 교집합 연산
- **Union**: 여러 후보를 하나로 합치는 합집합 연산
- **Set Subtraction**: 특정 조건에 해당하는 대상을 제외하는 차집합 연산
- **N-way Merge**: 여러 정렬된 결과를 하나의 정렬된 흐름으로 병합하는 연산

이 연산들은 일반적인 검색 시스템에서도 중요하지만,  
Prometheus에서는 특히 **inverted index의 postings list** 를 다룰 때 핵심이 된다.

Postings를 이미 얻었다고 가정했을 때, Prometheus가 그 postings들을 어떤 방식으로 조합해  
실제 쿼리 결과 후보 series를 빠르게 찾는지 살펴보자.

---
# 1. Intersection

## 1.1 Definition

Intersection(교집합)은 두 개 이상의 집합에서  
**모두 공통으로 포함된 원소만 남기는 연산**이다.

집합이 정렬되어 있다면 교집합은 매우 효율적으로 계산할 수 있다.
대표적인 방식은 포인터 두 개(또는 여러 개)를 동시에 이동시키는 방식이다.

예를 들어 두 정렬 리스트:

```text
[1, 5, 8, 21]
[3, 5, 8, 34]
```

를 비교하면, 작은 값을 가진 쪽 포인터를 전진시키면서
같은 값이 만날 때만 결과에 넣으면 된다.

이 방식의 장점은 전체를 브루트포스로 비교하지 않고,
각 리스트를 거의 한 번씩만 훑는다는 점이다.

## 1.2 Role in Prometheus

Prometheus에서 교집합은 중요한 postings 연산이다.

`Select([]matcher)` 는 주어진 matcher 조건을 만족하는 series를 찾고,
그 series들로부터 raw TSDB sample 데이터를 읽기 위한 저수준 쿼리다.

즉, Prometheus는 sample을 읽기 전에 먼저
"어떤 series가 조건에 맞는가"를 결정해야 하고,
이 과정에서 matcher별 결과를 교집합으로 결합한다.

Prometheus는 series를 찾을 때 matcher를 사용한다.

matcher는
"어떤 label name과 value 조합이 series에서 만족되어야 하는가"
를 표현하는 조건이다.

예를 들어:

```text
a="b"
```

라는 matcher는 `a="b"` label pair를 가진 모든 series를 고르라는 뜻이다.

Prometheus의 matcher는 보통 네 가지로 나눌 수 있다.

- `Equal` : `labelName="value"`
- `Not Equal` : `labelName!="value"`
- `Regex Equal` : `labelName=~"regex"`
- `Regex Not Equal` : `labelName!~"regex"`

여기서 label name에는 regex를 사용할 수 없고,
regex matcher는 부분 매칭이 아니라 label value 전체를 기준으로 매칭된다.

예를 들어 series가 아래와 같다고 하자.

```text
s1 = {job="app1", status="404"}
s2 = {job="app2", status="501"}
s3 = {job="bar1", status="402"}
s4 = {job="bar2", status="501"}
```

그러면 matcher 예시는 다음과 같이 해석된다.

```text
status="501"  -> (s2, s4)
status!="501" -> (s1, s3)
job=~"app.*"  -> (s1, s2)
job!~"app.*"  -> (s3, s4)
```

그리고 matcher가 두 개 이상이면 Prometheus는 이를 **AND**, 즉 교집합으로 처리한다.

예를 들면:

```text
job=~"app.*", status="501"
-> (s1, s2) ∩ (s2, s4)
-> (s2)
```

```text
job=~"bar.*", status!~"5.."
-> (s3, s4) ∩ (s1, s3)
-> (s3)
```

Prometheus에서 교집합은
여러 matcher 결과를 결합해 최종 series 후보를 좁히는 연산이다. 즉, 각 matcher에 대해 해당하는 모든 series를 먼저 구한 뒤,
마지막으로 그것들의 **교집합(intersection)** 을 계산한다.

다시 말해 `Select([]matcher)`의 초반부는
"sample을 바로 읽는 과정"이라기보다
"sample을 읽어도 되는 series 후보를 집합 연산으로 좁히는 과정"
이라고 볼 수 있다.

특히 postings가 series ID 기준으로 정렬되어 있으면:

- 쿼리 시 빠른 AND 연산이 가능하고
- 후보 집합을 초기에 작게 줄일 수 있으며
- 이후 series metadata, chunk, sample을 읽는 비용도 감소한다

결과적으로 Prometheus에서 교집합은  
**multi-label matcher를 빠르게 처리하기 위한 기본 검색 연산**이라고 볼 수 있다.

---
# 2. Union

## 2.1 Definition

Union(합집합)은 두 개 이상의 집합에 대해 **어느 하나라도 포함된 원소를 모두 모으는 연산**이다.

정렬된 집합의 경우 합집합 역시 merge 방식으로 효율적으로 계산할 수 있다.
두 리스트의 현재 값을 비교해 더 작은 값을 결과에 넣고,
같은 값이면 한 번만 넣고 양쪽 포인터를 함께 전진시키면 된다.

## 2.2 Role in Prometheus

Prometheus에서 합집합은 여러 postings를 하나의 후보 집합으로 묶어야 할 때 사용된다.

대표적인 경우는 다음과 같다.

- 하나의 matcher가 여러 label value를 허용하는 경우
- 정규식 matcher가 여러 postings로 확장되는 경우
- 여러 block 또는 여러 source에서 얻은 결과를 하나로 합쳐야 하는 경우

예를 들어:

```text
status=~"200|404"
```

라는 조건은 개념적으로:

```text
status="200" -> [1, 5, 8, 13]
status="404" -> [3, 8, 21]
```

를 얻은 뒤,

```text
[1, 5, 8, 13] ∪ [3, 8, 21]
= [1, 3, 5, 8, 13, 21]
```

처럼 후보를 만든다고 볼 수 있다.

이후 다른 positive matcher가 있다면 그 결과와 다시 교집합을 수행한다.

즉, `Regex Equal(a=~"<rgx>")` 같은 matcher는
label `a`의 모든 value를 순회하면서 regex 조건을 만족하는 값들을 찾고,
해당 value들이 가리키는 postings list를 모두 가져와 **합집합(union)** 한다고 이해할 수 있다.

예를 들어 `a=~"b.*"` 라면,
`a="b1"`, `a="b2"`, `a="blue"` 처럼 regex에 매칭되는 모든 value의 postings를 모아
하나의 후보 집합으로 합친 뒤 다음 단계로 넘긴다.

즉, Prometheus에서 Union은 **여러 허용 조건을 하나의 허용 집합으로 정리하는 연산**이다.

Intersection이 후보를 줄이는 연산이라면,
Union은 먼저 여러 후보를 **논리적으로 묶는 연산**이라고 이해할 수 있다.

---
# 3. Set Subtraction

## 3.1 Definition

Set Subtraction(차집합)은 한 집합에서 **다른 집합에 포함된 원소를 제거하는 연산**이다.

정렬된 리스트라면 차집합도 포인터 기반으로 효율적으로 계산할 수 있다.

- 값이 다르면 작은 쪽을 전진
- 같은 값이면 제거 대상으로 보고 양쪽을 함께 전진

이 방식 역시 전체 비교보다 훨씬 효율적이다.

## 3.2 Role in Prometheus

Prometheus에서 차집합은 **negative matcher** 를 처리할 때 특히 중요하다.

특히 `Not Equal` 과 `Regex Not Equal` matcher는 내부적으로 조금 다르게 처리한다.

개념적으로는 다음처럼 대응시킬 수 있다.

```text
a!="b"      -> a="b"
a!~"<rgx>"  -> a=~"<rgx>"
```

이유는 "조건을 만족하지 않는 모든 series"를 직접 구하는 방식이
너무 크고 비효율적일 수 있기 때문이다.

그래서 Prometheus는 먼저 `Equal` 또는 `Regex Equal` 방식으로
매칭되는 postings를 구한 뒤,
이를 기준 집합에서 **차집합(set subtraction)** 하는 식으로 처리한다고 볼 수 있다.

예를 들어 series가 아래와 같다고 하자.

```text
s1 = {job="app1", status="404"}
s2 = {job="app2", status="501"}
s3 = {job="bar1", status="402"}
s4 = {job="bar2", status="501"}
```

이때:

```text
job=~"bar.*", status!~"5.*"
```

를 집합 연산 관점에서 풀어 쓰면 다음과 같다.

```text
(job=~"bar.*") ∩ (status!~"5.*")
-> (job=~"bar.*") - (status=~"5.*")
-> ((job="bar1") ∪ (job="bar2")) - (status="501")
-> ((s3) ∪ (s4)) - (s2, s4)
-> (s3, s4) - (s2, s4)
-> (s3)
```

즉, negation matcher는
"매칭되지 않는 전체 집합"을 직접 구하는 대신,
먼저 positive matcher로 대응되는 집합을 구한 뒤
기준 집합에서 **빼는 방식**으로 이해할 수 있다.

더 일반적으로, matchers가 다음과 같다면:

```text
a="b", c!="d", e=~"f.*", g!~"h.*"
```

집합 연산 관점에서는:

```text
((a="b") ∩ (e=~"f.*")) - (c="d") - (g=~"h.*")
```

처럼 해석할 수 있다.

Prometheus 조회 경로에서는 이 연산 덕분에:

- `!=`, `!~` 같은 matcher를 효율적으로 처리할 수 있고
- 전체 스캔 없이 제외 대상만 제거할 수 있으며
- positive matcher로 먼저 좁힌 후보 집합을 유지하면서 후처리할 수 있다

따라서 차집합은 Prometheus의 label matcher semantics를 정확하게 구현하기 위한 필수 연산이다.

---
# 4. N-way Merge

## 4.1 Definition

N-way Merge(N-way 병합)은  
**N개의 정렬된 입력을 하나의 정렬된 출력으로 합치는 연산**이다.

두 개를 합치는 merge를 여러 입력으로 일반화한 개념이라고 볼 수 있다.

예를 들면:

```text
L1 = [1, 5, 9]
L2 = [2, 5, 8]
L3 = [3, 4, 10]
```

이를 병합하면:

```text
[1, 2, 3, 4, 5, 5, 8, 9, 10]
```

와 같은 정렬된 결과를 얻는다.

필요에 따라:

- 중복을 유지하는 merge
- 중복을 제거하는 merge

두 방식 모두 가능하다.

입력이 많을수록 단순하게 매번 전체 비교를 하는 것보다,
힙(min-heap)이나 여러 포인터를 사용한 방식이 더 효율적이다.

## 4.2 Role in Prometheus

Prometheus에서 N-way merge는  
"이미 정렬된 여러 결과를 하나의 정렬 흐름으로 읽어야 할 때" 등장한다.

대표적으로는 다음 같은 상황과 연결해 이해할 수 있다.

- 여러 postings 결과를 순서 있게 합칠 때
- 여러 block에서 얻은 series/chunk 탐색 결과를 조합할 때
- 시간 순으로 정렬된 여러 입력 스트림을 하나로 읽을 때

Prometheus TSDB는 Head와 여러 persistent block을 함께 조회할 수 있다.  
이때 각 block은 자체적으로 정렬된 탐색 결과를 만들 수 있으므로,
상위 레벨에서는 이 정렬된 결과들을 병합해 전체 조회 흐름을 구성해야 한다.

즉, block 단위로는 이미 탐색이 끝났더라도  
전체 query engine 입장에서는 여러 입력을 한 번 더 정렬된 형태로 묶어야 한다.

또한 postings 자체가 정렬되어 있기 때문에,
Prometheus는 이 정렬 특성을 활용해 merge 기반 연산을 일관되게 적용할 수 있다.

결과적으로 N-way merge는 Prometheus에서

- 분산된 여러 검색 결과를 하나의 읽기 흐름으로 통합하고
- 정렬 상태를 유지하며
- 후속 처리 비용을 낮추는

기반 연산으로 이해할 수 있다.

### Querying Multiple Blocks

querier의 시간 범위 `mint ~ maxt` 와 겹치는 block이 여러 개인 경우,
querier는 실제로 **merge querier**처럼 동작한다.

즉, Prometheus는 각 block을 따로 조회한 뒤
정렬된 결과들을 다시 하나로 합쳐
상위 계층에는 마치 하나의 연속된 결과처럼 보이게 만든다.

`LabelNames()`

- 모든 block에서 label name들을 가져온 뒤, **N-way merge** 를 수행한다

`LabelValues(name)`

- 모든 block에서 label value들을 가져온 뒤, **N-way merge** 를 수행한다

`Select([]matcher)`

- 각 block에서 `Select`를 수행하여 series iterator를 가져온다
- 이를 다시 **lazy한 N-way merge** 로 결합한다
- 즉, 모든 결과를 한 번에 메모리에 모으는 것이 아니라 iterator 방식으로 병합한다

따라서 N-way merge는 단순한 알고리즘 개념이 아니라,
Prometheus가 여러 block의 조회 결과를 하나의 query 결과로 통합하는 실제 메커니즘이기도 하다.
