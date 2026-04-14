package main

import (
	"flag"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

var (
	promURL      = flag.String("prom", "http://192.168.1.50:9090", "Prometheus URL")
	exporterPort = flag.Int("port", 8000, "fake exporter 포트")
	concurrency  = flag.Int("c", 80, "동시 쿼리 수")
	cardinality  = flag.Int("card", 500, "시리즈 수")
	pid          = flag.Int("pid", 0, "Prometheus PID (컨테이너 내부 보통 1)")
)

func startFakeExporter() {
	http.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		ts := time.Now().UnixMilli()
		for i := 0; i < *cardinality; i++ {
			status := []string{"200", "404", "500"}[i%3]
			fmt.Fprintf(w,
				"stress_requests_total{pod=\"pod-%d\",ns=\"ns-%d\"" +
				",status=\"%s\",region=\"r%d\"} %d %d\n",
				i, i%20, status, i%5, rand.Intn(10000), ts,
			)
		}
	})
	addr := fmt.Sprintf(":%d", *exporterPort)
	fmt.Printf("[exporter] :%d 에서 %d개 시리즈 서빙\n", *exporterPort, *cardinality)
	go http.ListenAndServe(addr, nil)
}

func readFD(pid int) int {
	if pid <= 0 {
		return -1
	}
	entries, err := os.ReadDir(fmt.Sprintf("/proc/%d/fd", pid))
	if err != nil {
		return -1
	}
	return len(entries)
}

func readFDLimit(pid int) int {
	if pid <= 0 {
		return -1
	}
	data, err := os.ReadFile(fmt.Sprintf("/proc/%d/limits", pid))
	if err != nil {
		return -1
	}
	for _, line := range strings.Split(string(data), "\n") {
		if strings.Contains(line, "open files") {
			fields := strings.Fields(line)
			if len(fields) >= 4 {
				v, _ := strconv.Atoi(fields[3])
				return v
			}
		}
	}
	return -1
}

func fdBar(fd, limit int) string {
	if limit <= 0 || fd <= 0 {
		return strings.Repeat("░", 30)
	}
	filled := fd * 30 / limit
	if filled > 30 {
		filled = 30
	}
	return strings.Repeat("█", filled) + strings.Repeat("░", 30-filled)
}

var queries = []string{
	// 1. 모든 pod와 모든 region을 정규표현식으로 훑기 (가장 무거움)
	`sum by (pod, region) (rate(stress_requests_total{pod=~".+", region=~".+"}[24h]))`,

	// 2. 표준편차 계산 - CPU와 FD를 동시에 괴롭힘
	`stddev_over_time(stress_requests_total{status="500"}[24h])`,

	// 3. 99퍼센타일 계산 - 인덱스를 샅샅이 뒤져야 함
	`quantile_over_time(0.99, stress_requests_total[24h])`,

	// 4. 아주 넓은 범위를 합산 (N-way 병합 강제)
	`count_over_time(stress_requests_total{ns=~"ns-[0-9]+"}[24h])`,
}

func queryRange(q string, start, end time.Time) error {
	params := url.Values{}
	params.Set("query", q)
	params.Set("start", strconv.FormatInt(start.Unix(), 10))
	params.Set("end", strconv.FormatInt(end.Unix(), 10))
	params.Set("step", "60")

	resp, err := http.Get(*promURL + "/api/v1/query_range?" + params.Encode())
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if strings.Contains(string(body), "too many open files") {
		return fmt.Errorf("TOO_MANY_OPEN_FILES")
	}
	if resp.StatusCode != 200 {
		return fmt.Errorf("HTTP_%d", resp.StatusCode)
	}
	return nil
}

func main() {
	flag.Parse()


	fmt.Printf(`
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 prometheus.yml 에 아래를 추가하고 reload:

  - job_name: 'stress'
    static_configs:
      - targets: ['host.docker.internal:%d']
    scrape_interval: 10s

 FD 실시간 모니터링 (컨테이너 PID 1 기준):
   go run main.go -pid 1

 준비되면 Enter...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
`, *exporterPort)


	end := time.Now()
	start := end.Add(-24 * time.Hour)
	fmt.Printf("[stress] %s ~ %s | 동시쿼리: %d\n\n",
		start.Format("01/02 15:04"), end.Format("01/02 15:04"), *concurrency)

	var success, errTotal, fdErrors, fdPeak int64

	sem := make(chan struct{}, *concurrency)
	var wg sync.WaitGroup

	// FD 모니터
	go func() {
		limit := readFDLimit(*pid)
		for {
			fd := readFD(*pid)
			if fd > int(atomic.LoadInt64(&fdPeak)) {
				atomic.StoreInt64(&fdPeak, int64(fd))
			}
			if fd >= 0 {
				fmt.Printf("\r[fd] %4d/%-6d %s  ok:%-7d err:%-5d fd_err:%-4d",
					fd, limit, fdBar(fd, limit),
					atomic.LoadInt64(&success),
					atomic.LoadInt64(&errTotal),
					atomic.LoadInt64(&fdErrors),
				)
			} else {
				fmt.Printf("\r[query] ok:%-7d err:%-5d fd_err:%-4d peak_fd:%-5d",
					atomic.LoadInt64(&success),
					atomic.LoadInt64(&errTotal),
					atomic.LoadInt64(&fdErrors),
					atomic.LoadInt64(&fdPeak),
				)
			}
			time.Sleep(300 * time.Millisecond)
		}
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		for {
			sem <- struct{}{}
			wg.Add(1)
			q := queries[rand.Intn(len(queries))]
			go func(query string) {
				defer wg.Done()
				defer func() { <-sem }()
				err := queryRange(query, start, end)
				if err != nil {
					atomic.AddInt64(&errTotal, 1)
					if err.Error() == "TOO_MANY_OPEN_FILES" {
						atomic.AddInt64(&fdErrors, 1)
						fmt.Printf("\n🔴 TOO MANY OPEN FILES  FD=%d\n", readFD(*pid))
					}
				} else {
					atomic.AddInt64(&success, 1)
				}
			}(q)
		}
	}()

	<-sig
	wg.Wait()
	fmt.Printf("\n\n━━━ 결과 ━━━\n성공: %d\n에러: %d\nFD에러: %d\nFD최고: %d\n",
		atomic.LoadInt64(&success),
		atomic.LoadInt64(&errTotal),
		atomic.LoadInt64(&fdErrors),
		atomic.LoadInt64(&fdPeak),
	)
}