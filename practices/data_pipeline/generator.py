
# 역할: 실제 적재 시스템처럼 바이너리 파일을 주기적으로 /incoming에 생성
import os, time, struct, random, logging


os.makedirs('/data/incoming', exist_ok=True)
os.makedirs('/data/processing', exist_ok=True)
os.makedirs('/data/done', exist_ok=True)
os.makedirs('/data/error', exist_ok=True)
os.makedirs('/data/logs', exist_ok=True)

logging.basicConfig(filename='/data/logs/app.log', level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] generator - %(message)s')

# 바이너리 파일 포맷 정의
# 헤더: magic(4) + version(2) + record_count(4) + checksum(4) = 14 bytes
# 데이터: record_count * 8 bytes (각 레코드는 int64 하나)
MAGIC = b'DAT1'          # 우리 파이프라인의 magic bytes
VERSION = 2              # 현재 스키마 버전

def make_file(filename, record_count, corrupt=False, wrong_version=False,
              wrong_checksum=False, size_mismatch=False, partial_body=False):
    """바이너리 파일 생성. 각 플래그로 다양한 오류 케이스를 유발"""
    filepath = os.path.join('/data/incoming', filename)

    records = [random.randint(0, 999999) for _ in range(record_count)]
    body = struct.pack(f'>{record_count}q', *records)
    checksum = sum(body) % (2**32)

    magic    = b'XXXX' if corrupt else MAGIC
    version  = 9 if wrong_version else VERSION

    if size_mismatch:
        # 헤더에는 record_count 기록, body는 1개 적게 작성
        body = struct.pack(f'>{record_count - 1}q', *records[:-1])

    elif partial_body:
        # body 뒤에 3바이트 추가 → body_size % 8 != 0 → layout_partial_fail
        body = body + b'\xff\xff\xff'

    if wrong_checksum:
        checksum = (checksum + 1) % (2**32)   # 정상값에서 1 오프셋 → checksum_fail

    header = struct.pack('>4sHIi', magic, version, record_count, checksum)

    with open(filepath, 'wb') as f:
        f.write(header)
        f.write(body)

    logging.info(f"[ALL]파일 생성: {filename} ({record_count}건)")
    return filepath

if __name__ == '__main__':
    file_num = 0
    logging.info("바이너리 파일 생성기 시작 (간격: 랜덤 3~20초)")

    while True:
        file_num += 1
        record_count = random.randint(800, 1200)
        filename = f'data_{file_num:05d}.bin'

        # --- 에러 시뮬레이션 로직 ---
        roll = random.random()
        if roll < 0.10:
            filepath = make_file(filename, record_count, corrupt=True)
            logging.warning(f"손상 파일 생성됨 (magic): {filename}")
        elif roll < 0.15:
            filepath = make_file(filename, record_count, wrong_version=True)
            logging.warning(f"손상 파일 생성됨 (version): {filename}")
        elif roll < 0.20:
            filepath = make_file(filename, record_count, wrong_checksum=True)
            logging.warning(f"손상 파일 생성됨 (checksum): {filename}")
        elif roll < 0.23:
            filepath = make_file(filename, record_count, size_mismatch=True)
            logging.warning(f"손상 파일 생성됨 (size_mismatch): {filename}")
        elif roll < 0.25:
            filepath = make_file(filename, record_count, partial_body=True)
            logging.warning(f"손상 파일 생성됨 (partial_body): {filename}")
        else:
            filepath = make_file(filename, record_count)
        # ----------------------------

        # 3초에서 20초 사이의 랜덤한 간격으로 대기
        sleep_time = random.uniform(3, 20)
        logging.info(f"{filename} 생성 완료. {sleep_time:.2f}초 후 다음 파일 생성...")

        time.sleep(sleep_time)