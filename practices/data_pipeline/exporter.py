# exporter.py
# 역할: generator/loader가 만들어내는 모든 결과물을 감시해서 메트릭 노출
import os, time, struct,  json
from datetime import date, timedelta
from prometheus_client import start_http_server, Gauge, Counter

# ── 메트릭 정의 ──────────────────────────────────
dir_file_count   = Gauge('dir_file_count',    '디렉터리 파일 수',   ['path'])
dir_bytes_total  = Gauge('dir_bytes_total',   '디렉터리 총 크기',   ['path'])
log_lines        = Counter('log_lines_total', '로그 라인 수',       ['level'])
partition_exists     = Gauge('partition_exists',       '파티션 존재 여부',   ['date'])
partition_file_count = Gauge('partition_file_count',   '파티션 파일 수',     ['date'])
partition_bytes      = Gauge('partition_bytes',        '파티션 총 크기',     ['date'])


ldr_magic_ok             = Gauge('loader_magic_ok_total',             '헤더 정상 파일 수 누계')
ldr_magic_fail           = Gauge('loader_magic_fail_total',           '헤더 손상 파일 수 누계')
ldr_layout_ok            = Gauge('loader_layout_ok_total',            '레이아웃 검증 통과 파일 수 누계')
ldr_layout_fail          = Gauge('loader_layout_fail_total',          '크기 불일치 파일 수 누계')
ldr_layout_partial_fail  = Gauge('loader_layout_partial_fail_total',  '잔여 바이트 파일 수 누계')
ldr_checksum_fail        = Gauge('loader_checksum_fail_total',        '체크섬 불일치 파일 수 누계')
ldr_ver_fail             = Gauge('loader_version_fail_total',         '버전 불일치 파일 수 누계')
ldr_records              = Gauge('loader_total_records',              '처리한 총 레코드 수 누계')

WATCH_DIRS = ['/data/incoming', '/data/processing', '/data/done', '/data/error']
LOG_FILE   = '/data/logs/app.log'
STATS_FILE = '/data/logs/loader_stats.json'

log_offset = 0   # 로그 tail용 오프셋

def collect_directory():
    for d in WATCH_DIRS:
        if not os.path.exists(d):
            continue
        files = [f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f))]
        total = sum(os.path.getsize(os.path.join(d, f)) for f in files)
        dir_file_count.labels(path=d).set(len(files))
        dir_bytes_total.labels(path=d).set(total)

# def get_partition_path():
#     today = datetime.date.today()
#     return (
#         f"/data/lake/year={today.year}/month={today.month:02d}/day={today.day:02d}",
#         today.isoformat(),
#     )

def collect_partition():
    today = date.today()

    # 오늘을 포함하여 최근 7일간의 날짜를 계산
    for i in range(7):
        target_date = today - timedelta(days=i)

        # 경로 구성: /data/lake/year=YYYY/month=MM/day=DD
        path = f"/data/lake/year={target_date.year}/month={target_date.month:02d}/day={target_date.day:02d}"
        date_label = target_date.isoformat() # "2026-03-16" 형식

        if os.path.exists(path):
            files = [f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))]
            total_size = sum(os.path.getsize(os.path.join(path, f)) for f in files)

            partition_exists.labels(date=date_label).set(1)
            partition_file_count.labels(date=date_label).set(len(files))
            partition_bytes.labels(date=date_label).set(total_size)
        else:
            # 파티션이 없는 경우 0으로 세팅하여 그래프에서 끊기지 않게 함
            partition_exists.labels(date=date_label).set(0)
            partition_file_count.labels(date=date_label).set(0)
            partition_bytes.labels(date=date_label).set(0)



def collect_log():
    global log_offset
    if not os.path.exists(LOG_FILE):
        return
    try:
        with open(LOG_FILE, 'r') as f:
            f.seek(log_offset)
            for line in f:
                if '[SUCCESS]' in line:
                    log_lines.labels(level='SUCCESS').inc()
                elif '[ERROR]'   in line: log_lines.labels(level='ERROR').inc()
                elif '[WARNING]'  in line: log_lines.labels(level='WARNING').inc()
                elif '[ALL]'  in line: log_lines.labels(level='ALL').inc()
            log_offset = f.tell()
    except Exception:
        pass

def collect_loader_stats():
    if not os.path.exists(STATS_FILE):
        return
    try:
        with open(STATS_FILE, 'r') as f:
            s = json.load(f)
        ldr_magic_ok.set(s.get('magic_ok', 0))
        ldr_magic_fail.set(s.get('magic_fail', 0))
        ldr_layout_ok.set(s.get('layout_ok', 0))
        ldr_layout_fail.set(s.get('layout_fail', 0))
        ldr_layout_partial_fail.set(s.get('layout_partial_fail', 0))
        ldr_checksum_fail.set(s.get('checksum_fail', 0))
        ldr_ver_fail.set(s.get('version_fail', 0))
        ldr_records.set(s.get('total_records', 0))
    except Exception:
        pass

if __name__ == '__main__':
    start_http_server(8000)
    print("exporter 시작 → http://localhost:8000/metrics")
    while True:
        collect_directory()
        collect_log()
        collect_loader_stats()
        collect_partition()
        time.sleep(5)
