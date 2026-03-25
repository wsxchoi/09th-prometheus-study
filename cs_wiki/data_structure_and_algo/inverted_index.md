# Inverted Index
---

## forward index
<img src="./images/forward_index.png" width="60%">

데이터베이스나 파일 시스템 등에서 데이터 검색 속도를 향상시키기 위해 사용하는 자료 구조다. Full Scan 없이, Document Id를 통해 해당 문서의 내용에 빠르게 접근할 수 있다.

## inverted index
<img src="./images/inverted_index.png" width="70%">

forward index와 반대로 키워드(Term)를 통해 해당 키워드가 포함된 문서 목록에 접근하는 방식의 자료구조다. 보통 hash table 기반으로 구현되기 때문에 Documents List를 O(1)만에 얻을 수 있다. (만약 inverted index를 쓰지 않는다면 모든 문서의 내용을 탐색해야한다.) 따라서 Term이 포함된 문서를 자주 찾아야하는 경우에 유용하다.

* posting list

Term을 포함하는 문서의 Id 목록을 Posting List라고도 부른다.

* inverted index의 trade-off

Term 검색은 빨라지지만, 데이터를 추가/수정할 때 Term 추출 및 정규화, Postings 갱신등의 연산을 해야하므로 쓰기 성능이 일부 희생된다.

---
## Prometheus에서의 Inverted Index 도입 배경

### 배경지식
Prometheus에서 각 series는 다음과 같이 metric name에 label set을 결합해서 식별된다.
```{__name__="requests_total", path="/status", method="GET", instance=”10.0.0.1:80”}```
추가로 각 Series마다 할당받은 고유한 Series Id로도 식별된다.

### Series Churn 문제
<img src="./images/series_churn.png" width="70%">

Kubernetes같은 Cluster orchestration systems 환경에서는 auto-scaling, rolling updates 때문에 Series가 선형적으로 증가한다.

이 상황에서 ```app="foo"```라는 label을 가진 Series를 보여달라는 쿼리 요청을 받았을 때, inverted index를 쓰면 N(Series의 개수)이 아무리 커져도 O(1) 으로 Series id list를 얻을 수 있다. 반면 inverted index를 쓰지 않을 경우 모든 Series를 탐색해야하므로 O(N)의 시간이 걸리고, N이 커질수록 쿼리 부담이 커진다.

Prometheus는 약간의 쓰기 작업 지연을 감수하고, inverted index가 주는 빠른 검색의 이점을 취했다.

### + intersection 최적화

하지만 label pair를 여러개 가지는 쿼리가 오는 경우 병목이 발생한다.
```
__name__="requests_total"   ->   [ 9999, 1000, 1001, 2000000, 2000001, 2000002, 2000003 ]
        app="foo"           ->   [ 1, 3, 10, 11, 12, 100, 311, 320, 1000, 1001, 10002 ]

intersection   =>   [ 1000, 1001 ]
```
각 label pair에 대한 posting list(series id list)는 상수시간으로 얻을 수 있지만 교집합을 찾아야한다.

최악의 경우 series id list의 크기가 모두 N이고, brute force로 찾을 경우 복잡도가 O(N^2)이다. 만약 label pair가 k개였다면 복잡도는 O(N^k)이다.

v3에서는 위의 문제를 해결하기 위해 series id list를 정렬 상태로 유지하여, intersection 연산의 복잡도를 O(k*n)으로 개선했다.

### 메모리에서의 index

Head에서 관리하는 chunks(mmap chunks + active chunks)의 index는 메모리에서 관리한다. Head block은 시간이 지나 persistent block이 되는데, 이 때 index도 같이 디스크에 저장된다. 이후 Head에 더 이상 존재하지 않게되는 series에 대한 index는 메모리에서 삭제된다.

---
## 코드 분석

메모리에서 inverted index(postings)를 관리하는 부분은`prometheus/tsdb/index/postings.go`에 구현되어있다.

### inverted index를 정의하는 구조체 `MemPostings`
```go
type MemPostings struct {
    mtx sync.RWMutex

    m map[string]map[string][]storage.SeriesRef

    lvs map[string][]string

    ordered bool
}
```
* `m map[string]map[string][]storage.SeriesRef`

m은 postings의 전체적인 정보를 담는 맵이다. key는 label name, value는 또 다른 map이다. 여기서 value에 해당하는 map은 key가 label value, value가 SeriesRef을 담는 슬라이스(id 목록 배열)다.
```
# m 예시
{
  "__name__": {
    "http_requests_total": [1, 2, 3, 4, 5],
    "node_cpu_seconds_total": [6, 7]
  },
  "method": {
    "GET": [1, 2, 5],
    "POST": [3, 4]
  },
  "status": {
    "200": [1, 3],
    "404": [2, 4, 5]
  },
  "handler": {
    "/api/v1": [1, 2, 3, 4],
    "/metrics": [5]
  }
}
```
* `lvs map[string][]string`

lvs는 label name을 key, label value들을 담은 배열을 value로 가지는 map이다.

```
# lvs 예시
{
  "__name__": ["http_requests_total", "process_cpu_seconds"],
  "method":   ["GET", "POST", "PUT"],
  "status":   ["200", "404", "500"],
  "env":      ["dev", "prod"]
}
```
* `ordered bool`

ordered는 정렬 상태를 나타내는 boolean이다. (정렬 상태면 True)

### postings를 갱신 하는 함수 `Add`

```go
// head.go에서 쓰기 작업시 새로운 series가 등장하면 Add가 호출하여 postings를 갱신한다.
func (p *MemPostings) Add(id storage.SeriesRef, lset labels.Labels) {
    // id: series를 식별하는 id
    // lset: {__name__="requests_total", path="/status"} 같은 label set
    p.mtx.Lock()

    // Range는 lset(label pair 여러개)을 하나의 l(label pair)로 파싱함
    // 각 l을 순회하면서 p.addFor(id, l)를 호출함
    // addFor는 l(label pair)의 posting list에 id를 추가하는 함수
    lset.Range(func(l labels.Label) {
    	p.addFor(id, l)
    })
    p.addFor(id, allPostingsKey)
    p.mtx.Unlock()
}
```

### posting list에 id를 추가하는 함수 `Addfor`

```go
func (p *MemPostings) addFor(id storage.SeriesRef, l labels.Label) {
    // l.name은 label name (ex. method), l.value는 lavel value (ex. "GET")
    nm, ok := p.m[l.Name]
    if !ok {
        nm = map[string][]storage.SeriesRef{} // 없으면 새 맵 생성
        p.m[l.Name] = nm                      // 부모 맵에 등록
    }
    vm, ok := nm[l.Value]
    if !ok {
        // 이 Term이 처음 등장했다면, Term 목록(lvs)에 추가
        p.lvs[l.Name] = appendWithExponentialGrowth(p.lvs[l.Name], l.Value)
    }
    // 현재 데이터의 id를 추가
    list := appendWithExponentialGrowth(vm, id)
    nm[l.Value] = list

    // 이미 정렬되어있으면 return
    if !p.ordered {
        return  
    }
    // insertion sort로 정렬 상태를 유지
    for i := len(list) - 1; i >= 1; i-- {
    	if list[i] >= list[i-1] {
            break
    	}
    	list[i], list[i-1] = list[i-1], list[i]
    }
}
```
---

* 참고
https://ganeshvernekar.com/blog/prometheus-tsdb-the-head-block/
https://web.archive.org/web/20220205173824/https://fabxc.org/tsdb/
