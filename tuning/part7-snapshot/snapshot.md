# Prometheus TSDB (Part 7): Snapshot on Shutdown
> https://ganeshvernekar.com/blog/prometheus-tsdb-snapshot-on-shutdown 를 기반으로 작성
---

## 실습 배경

Prometheus 프로세스를 종료할 때(Graceful Shutdown), 빠른 재시작을 위해 snapshot을 남길 수 있다.

snapshot 쓰기는 디스크에 buffered I/O를 쏟아붓는 작업이고, 디스크에 snapshot 데이터가 완전히 Flush 될 때까지 종료가 지연된다.

이 때 snapshot 쓰기 과정은 **write() → page cache → dirty pages → background flush** 경로를 탄다.

따라서 커널 파라미터 `vm.dirty_background_ratio` 의 값이 graceful shutdown 의 지연시간에 영향을 줄 것이다.

---

## 실습 목표

본 실습은 `vm.dirty_background_ratio` 값에 따른 graceful shutdown의 지연시간 추세를 예상해보고 직접 확인하는 것이 목표이다.

---

## 예상 결과

- `vm.dirty_background_ratio` 가 큰 경우
1. sigterm 받았을 때 dirty page가 꽤 차있는 상태이다.
2. 지연시간: WAL flush 시간 + snapshot flush 시간
3. dirty_background_ratio 가 높기 때문에 디스크에 flush 되지 않은 WAL이 많다.
4. WAL flush 시간이 커지고 graceful stop의 지연시간이 커진다.
>`vm.dirty_background_ratio`가 커지면 graceful stop의 latency도 커질 것이다.

- `vm.dirty_background_ratio` 가 작은 경우
1. **background flush**를 자주 하기 때문에 sigterm을 받았을 때, dirty page가 별로 없는 상태이다.
2. 지연시간: WAL flush 시간 + snapshot flush 시간
3. WAL이 대부분 flush 되어있다.
4. WAL flush 시간이 작아지고 graceful stop의 지연시간이 작아진다.
>`vm.dirty_background_ratio`가 작아지면 graceful stop의 latency도 작아질 것이다.

---

## 실습 방식

- `vm.dirty_background_ratio` 의 값을 [1, 5, 10, 20] 로 바꿔가며 sigterm latency를 3번씩 반복 측정.
- `vm.dirty_ratio` 는 기본값 고정
- 메트릭은 avalanche로 생성 (20만개의 series 생성)

- 한 번의 측정 사이클
1. `vm.dirty_background_ratio` 값 할당
2. 이전 실험에서의 data 삭제
3. page cache 초기화
4. Prometheus 실행
5. 15분간 데이터 쌓기
6. sigterm 주고 종료 될때까지의 시간 측정
7. 측정 데이터 기록

---

## 실습 환경

- macos → utm → ubuntu vm (4코어 8GB)
- vm에 ssh 연결해서 실습 진행

## go 설치

```bash
# 1. 기존 설치본 제거 및 깨끗한 환경 조성
sudo rm -rf /usr/local/go

# 2. 최신 Go 1.26.2 다운로드 (현재 환경이 arm64인지 확인 필수)
cd /tmp
wget https://go.dev/dl/go1.26.2.linux-arm64.tar.gz

# 3. 압축 해제
sudo tar -C /usr/local -xzf go1.26.2.linux-arm64.tar.gz

# 4. PATH 등록 (중복 등록 방지를 위해 체크 후 추가)
if ! grep -q "/usr/local/go/bin" ~/.bashrc; then
  echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.bashrc
  echo 'export PATH=$PATH:$HOME/go/bin' >> ~/.bashrc
fi

# 5. 설정 반영 및 확인
source ~/.bashrc
go version
```

## Prometheus 설치

```bash
cd ~
mkdir -p workspace && cd workspace

# 최신 버전 확인은 https://github.com/prometheus/prometheus/releases
# 3.5.3 기준 (블로그의 memory-snapshot 기능은 2.30+ 면 다 가능)
wget https://github.com/prometheus/prometheus/releases/download/v3.5.3/prometheus-3.5.3.linux-arm64.tar.gz
tar xzf prometheus-3.5.3.linux-arm64.tar.gz
cd prometheus-3.5.3.linux-arm64

# 확인
./prometheus --version
```

## avalanche 설치

```bash
go install github.com/prometheus-community/avalanche/cmd/avalanche@latest

# 확인
avalanche --help
```

## prometheus.yml

```bash
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: 'prometheus'
    static_configs:
      - targets: ['localhost:9090']

  - job_name: 'avalanche'
    static_configs:
      - targets: ['localhost:9001']
```

## 터미널 1 : avalanche 실행 커맨드

```bash
# metric이 1000개, 그 metric name에 해당하는 series가 200개. -> 총 200000개
avalanche \
  --gauge-metric-count=1000 \
  --counter-metric-count=0 \
  --histogram-metric-count=0 \
  --native-histogram-metric-count=0 \
  --series-count=200 \
  --label-count=10 \
  --port=9001
```

## **터미널 2:  Prometheus 실행 커맨드**

```bash
cd ~/workspace/prometheus-3.5.3.linux-arm64

./prometheus \
  --config.file=prometheus.yml \
  --storage.tsdb.path=./data \
  --enable-feature=memory-snapshot-on-shutdown
```

## 동작 확인

```bash
# scrape target들이 UP 상태인지
curl -s http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | {job: .labels.job, health}'

# 현재 head block의 시계열 개수
curl -s 'http://localhost:9090/api/v1/query?query=prometheus_tsdb_head_series' | jq '.data.result[0].value[1]'
```

## 실험 자동화 bash script 작성

반복 실험을 자동화하는 bash script

```bash
cat > run_sweep.sh << 'EOF'
#!/bin/bash
# Sweep a sysctl parameter across multiple values, 3 trials each.
# Output: results.csv + summary table

set -e

PROM_DIR=~/workspace/prometheus-3.5.3.linux-arm64
DATA_DIR=$PROM_DIR/data
PROM_LOG=$PROM_DIR/prom.log

# === Configuration ===
PARAM="vm.dirty_background_ratio"
VALUES=(1 5 10 20)
TRIALS=3
LOAD_WAIT=900   # seconds to wait for series to load
RESULTS=results.csv

# Save original value to restore at end
ORIGINAL=$(sysctl -n $PARAM)
echo "Original $PARAM = $ORIGINAL"

# Initialize CSV
echo "param,value,trial,active_series,head_chunks,shutdown_seconds,snapshot_size_mb" > $RESULTS

# === Helper: run one trial ===
run_trial() {
    local value=$1
    local trial=$2

    echo ""
    echo "=== $PARAM=$value, trial $trial/$TRIALS ==="

    # 1. Set sysctl
    sudo sysctl -w $PARAM=$value > /dev/null

    # 2. Clean state
    rm -rf $DATA_DIR

    # 3. Drop page cache to ensure cold start (optional but consistent)
    echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null

    # 4. Start Prometheus
    cd $PROM_DIR
    ./prometheus \
        --config.file=prometheus.yml \
        --storage.tsdb.path=./data \
        --enable-feature=memory-snapshot-on-shutdown \
        > $PROM_LOG 2>&1 &
    local prom_pid=$!

    # 5. Wait for load
    echo "  Loading for ${LOAD_WAIT}s..."
    sleep $LOAD_WAIT

    # 6. Capture pre-shutdown metrics
    local series=$(curl -s 'http://localhost:9090/api/v1/query?query=prometheus_tsdb_head_series' | jq -r '.data.result[0].value[1]')
    local chunks=$(curl -s 'http://localhost:9090/api/v1/query?query=prometheus_tsdb_head_chunks' | jq -r '.data.result[0].value[1]')

    # 7. Measure shutdown time
    local start=$(date +%s.%N)
    kill -SIGTERM $prom_pid
    wait $prom_pid 2>/dev/null
    local end=$(date +%s.%N)
    local shutdown_time=$(echo "$end - $start" | bc)

    # 8. Snapshot size in MB
    local snapshot_mb=$(du -sm $DATA_DIR/chunk_snapshot.* 2>/dev/null | awk '{print $1}' | head -1)
    snapshot_mb=${snapshot_mb:-0}

    echo "  series=$series chunks=$chunks shutdown=${shutdown_time}s snapshot=${snapshot_mb}MB"

    # 9. Append to CSV
    echo "$PARAM,$value,$trial,$series,$chunks,$shutdown_time,$snapshot_mb" >> $RESULTS
}

# === Main loop ===
for value in "${VALUES[@]}"; do
    for trial in $(seq 1 $TRIALS); do
        run_trial $value $trial
    done
done

# Restore original sysctl
sudo sysctl -w $PARAM=$ORIGINAL > /dev/null
echo ""
echo "Restored $PARAM=$ORIGINAL"

# === Summary table ===
echo ""
echo "================================================"
echo "Summary (averaged over $TRIALS trials)"
echo "================================================"
printf "%-10s %-15s %-15s %-15s %-15s\n" "value" "series_avg" "chunks_avg" "shutdown_avg" "snapshot_avg"
echo "------------------------------------------------"

for value in "${VALUES[@]}"; do
    awk -F',' -v v=$value '
        $2==v {
            series+=$4; chunks+=$5; shutdown+=$6; snapshot+=$7; n++
        }
        END {
            if (n>0) printf "%-10s %-15.0f %-15.0f %-15.3f %-15.1f\n", v, series/n, chunks/n, shutdown/n, snapshot/n
        }' $RESULTS
done

echo ""
echo "Full results saved to $RESULTS"
EOF

chmod +x run_sweep.sh
```

- shell script에서 sudo가 안먹히는 상황 해결

```bash
# timeout으로 종료됨
[sudo: authenticate] Password:
sudo: timed out

sudo visudo -f /etc/sudoers.d/prometheus-experiment

# nano 화면으로 이동하고 저장
woo ALL=(ALL) NOPASSWD: /usr/sbin/sysctl, /usr/bin/tee
```

* 실행 커맨드

```bash
# 실행
nohup ./run_sweep.sh > my_log.txt 2>&1 &

# 로그 확인 시
tail -f my_log.txt
```

---

## 실습 결과

```bash
woo@ubuntu-node1:~/workspace$ tail -f my_log.txt
Original vm.dirty_background_ratio = 10

=== vm.dirty_background_ratio=1, trial 1/3 ===
  Loading for 900s...
  series=3200577 chunks=3200577 shutdown=22.226347118s snapshot=912MB

=== vm.dirty_background_ratio=1, trial 2/3 ===
  Loading for 900s...
  series=3000577 chunks=3000577 shutdown=19.321304352s snapshot=857MB

=== vm.dirty_background_ratio=1, trial 3/3 ===
  Loading for 900s...
  series=3000577 chunks=3000577 shutdown=17.308794750s snapshot=865MB

=== vm.dirty_background_ratio=5, trial 1/3 ===
  Loading for 900s...
  series=3200577 chunks=3200577 shutdown=24.539570985s snapshot=919MB

=== vm.dirty_background_ratio=5, trial 2/3 ===
  Loading for 900s...
  series=3000577 chunks=3000577 shutdown=21.056195887s snapshot=863MB

=== vm.dirty_background_ratio=5, trial 3/3 ===
  Loading for 900s...
  series=3000577 chunks=3000577 shutdown=25.923831213s snapshot=917MB

=== vm.dirty_background_ratio=10, trial 1/3 ===
  Loading for 900s...
  series=3200577 chunks=3200577 shutdown=17.931416418s snapshot=920MB

=== vm.dirty_background_ratio=10, trial 2/3 ===
  Loading for 900s...
  series=3000577 chunks=3201154 shutdown=19.427862579s snapshot=862MB

=== vm.dirty_background_ratio=10, trial 3/3 ===
  Loading for 900s...
  series=3200577 chunks=3200577 shutdown=17.889737539s snapshot=918MB

=== vm.dirty_background_ratio=20, trial 1/3 ===
  Loading for 900s...
  series=3200577 chunks=3200577 shutdown=22.654054647s snapshot=919MB

=== vm.dirty_background_ratio=20, trial 2/3 ===
  Loading for 900s...
  series=3000577 chunks=3000577 shutdown=19.542880914s snapshot=865MB

=== vm.dirty_background_ratio=20, trial 3/3 ===
  Loading for 900s...
  series=3200577 chunks=3200577 shutdown=20.249528312s snapshot=919MB

Restored vm.dirty_background_ratio=10

================================================
Summary (averaged over 3 trials)
================================================
value      series_avg      chunks_avg      shutdown_avg    snapshot_avg
------------------------------------------------
1          3067244         3067244         19.619          878.0
5          3067244         3067244         23.840          899.7
10         3133910         3200769         18.416          900.0
20         3133910         3133910         20.815          901.0

Full results saved to results.csv
```

`vm.dirty_background_ratio` 가 증가하면서 `shutdown_avg` 도 같이 증가할 것이라고 예상했으나, 본 실습에서는 증가 추세가 보이지 않았다.

---

## 결과 분석

본 실험의 가설은 `shutdown latency = WAL flush + snapshot flush`에서 `WAL flush` 항이 `dirty_background_ratio`에 의존한다는 것이었다. 그러나 결과상 추세가 관측되지 않았다.

* 원인 예상
(1) ratio별 잔여 dirty page 차이가 미미했다.
(2) `shutdown latency = WAL flush + snapshot flush`에서 `WAL flush`에 비해 `snapshot flush`가 훨씬 커서 `WAL flush`의 변화가 묻혔다.