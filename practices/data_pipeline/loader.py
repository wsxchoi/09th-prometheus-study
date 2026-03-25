
# 역할: /incoming 파일을 가져다 검증 후 /done 또는 /error로 이동
#       처리 결과를 app.log에 기록 → exporter가 이 로그를 tail함
import os, time, shutil, struct, logging, json, datetime

logging.basicConfig(filename='/data/logs/app.log', level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] loader - %(message)s')

INCOMING   = '/data/incoming'
PROCESSING = '/data/processing'
DONE       = '/data/done'
ERROR      = '/data/error'

def get_partition_path():
    today = datetime.date.today()
    path = f"/data/lake/year={today.year}/month={today.month:02d}/day={today.day:02d}"
    os.makedirs(path, exist_ok=True)
    return path


MAGIC      = b'DAT1'
VERSION    = 2
STATS_FILE = '/data/logs/loader_stats.json'

def load_stats():
    try:
        with open(STATS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {'magic_ok': 0, 'magic_fail': 0,
                'layout_ok': 0, 'layout_fail': 0,
                'layout_partial_fail': 0,
                'checksum_fail': 0,
                'version_fail': 0, 'total_records': 0}

stats = load_stats()

def save_stats():
    with open(STATS_FILE, 'w') as f:
        json.dump(stats, f)

def validate_header(filepath):
    """
    헤더 검증 → (ok, reason, record_count,checksum) 반환

    """
    try:
        with open(filepath, 'rb') as f:
            raw = f.read(14)   # 헤더 14 bytes만 읽기

        if len(raw) < 14:
            return False, 'header_too_short', 0, 0

        magic, version, record_count, checksum = struct.unpack('>4sHIi', raw)

        if magic != MAGIC:
            return False, 'invalid_magic', 0, 0
        if version != VERSION:
            return False, 'version_mismatch', 0, 0

        return True, 'ok', record_count, checksum

    except Exception as e:
        return False, str(e), 0, 0


def validate_layout(filepath, expected_count):
    """
    실제 데이터 레이아웃 검증 → (ok, reason, actual_count) 반환

    """
    try:
        file_size = os.path.getsize(filepath)
        body_size = file_size - 14

        if body_size % 8 != 0:
            return False, 'partial', body_size // 8

        if body_size != expected_count * 8:
            return False, 'size_mismatch', body_size // 8

        with open(filepath, 'rb') as f:
            f.seek(14)
            body = f.read(expected_count * 8)

        try:
            records = struct.unpack(f'>{expected_count}q', body)
        except struct.error:
            return False, 'unpack_fail', 0

        return True, 'ok', len(records)

    except Exception as e:
        return False, str(e), 0


def validate_checksum(filepath, expected_checksum, record_count):
    """
    body 바이트 합산 후 헤더의 checksum과 비교
    """
    with open(filepath, 'rb') as f:
        f.seek(14)
        body = f.read(record_count * 8)
    computed = sum(body) % (2**32)
    return computed == (expected_checksum % (2**32))


def process_file(filepath):
    """파일 하나를 처리하는 전체 흐름"""
    filename = os.path.basename(filepath)
    proc_path = os.path.join(PROCESSING, filename)

    # 1. incoming → processing 이동 (처리 시작)
    shutil.move(filepath, proc_path)
    logging.info(f"처리 시작: {filename}")

    start = time.time()

    # 2. 헤더 검증
    ok, reason, record_count, checksum = validate_header(proc_path)
    if not ok:
        shutil.move(proc_path, os.path.join(ERROR, filename))
        logging.error(f"헤더 검증 실패: {filename} reason={reason}")
        if reason == 'version_mismatch':
            stats['magic_ok'] += 1
            stats['version_fail'] += 1
        else:
            stats['magic_fail'] += 1
        save_stats()
        return

    stats['magic_ok'] += 1

    # 3. 레이아웃 검증 (잔여 바이트, 크기, 역직렬화)
    layout_ok, reason, actual_count = validate_layout(proc_path, record_count)
    if not layout_ok:
        shutil.move(proc_path, os.path.join(ERROR, filename))
        logging.error(f"레이아웃 검증 실패: {filename} reason={reason} "
                      f"expected={record_count} actual={actual_count}")
        if reason == 'partial':
            stats['layout_partial_fail'] += 1
        else:
            stats['layout_fail'] += 1
        save_stats()
        return

    # 4. 체크섬 검증
    if not validate_checksum(proc_path, checksum, record_count):
        shutil.move(proc_path, os.path.join(ERROR, filename))
        logging.error(f"체크섬 불일치: {filename}")
        stats['checksum_fail'] += 1
        save_stats()
        return

    # 5. 정상 → done으로 이동 + 레이크 저장
    elapsed = time.time() - start
    done_path = os.path.join(DONE, filename)
    shutil.move(proc_path, done_path)
    shutil.copy(done_path, os.path.join(get_partition_path(), filename))
    logging.info(f"[SUCCESS]처리 완료: {filename} records={actual_count} "
                 f"elapsed={elapsed:.3f}s")
    stats['layout_ok'] += 1
    stats['total_records'] += actual_count
    save_stats()


if __name__ == '__main__':
    logging.info("loader 시작")
    while True:
        files = sorted(os.listdir(INCOMING))   # 오래된 파일부터 처리
        for fname in files:
            fpath = os.path.join(INCOMING, fname)
            if os.path.isfile(fpath):
                process_file(fpath)
                time.sleep(0.5)   # 처리 간격

        time.sleep(2)   # 폴링 간격