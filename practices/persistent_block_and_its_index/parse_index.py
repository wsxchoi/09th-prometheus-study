#!/usr/bin/env python3
"""
Prometheus TSDB Index File Parser
=================================
persistent block의 index 파일을 바이너리 레벨에서 파싱하여
inverted index 구조를 직접 확인하는 실습 스크립트.

참고: https://ganeshvernekar.com/blog/prometheus-tsdb-persistent-block-and-its-index
소스: https://github.com/prometheus/prometheus/blob/master/tsdb/docs/format/index.md

Usage:
    python parse_index.py <path-to-index-file>
    python parse_index.py /path/to/data/01XXXX/index
"""

import struct  # 바이너리 데이터를 파이썬 정수/문자열로 변환하는 표준 라이브러리
import sys
import os

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
INDEX_MAGIC = 0xBAAA_D700  # index 파일의 첫 4바이트가 이 값이면 Prometheus TSDB index 파일임을 의미
TOC_SIZE = 52  # TOC는 파일 맨 끝 52바이트. 6개 오프셋(각 8바이트=48) + CRC32(4바이트) = 52

# ANSI colors for terminal output
class C:
    H  = "\033[1;36m"   # header (cyan bold)
    K  = "\033[1;33m"   # key (yellow bold)
    V  = "\033[0;37m"   # value (white)
    OK = "\033[1;32m"   # success (green bold)
    W  = "\033[1;31m"   # warning (red bold)
    D  = "\033[0;90m"   # dim
    R  = "\033[0m"      # reset

# ─────────────────────────────────────────────
# Binary helpers
# ─────────────────────────────────────────────
class IndexReader:
    """index 파일의 바이너리를 읽기 위한 래퍼"""

    def __init__(self, filepath: str):
        with open(filepath, "rb") as f:
            self.data = f.read()  # 파일 전체를 메모리에 올림 (index 파일은 보통 수 MB 이하)
        self.size = len(self.data)
        self.pos = 0  # 현재 읽기 커서 위치 (파일 내 byte offset)

    def seek(self, offset: int):
        self.pos = offset  # 커서를 특정 위치로 점프

    def read_bytes(self, n: int) -> bytes:
        b = self.data[self.pos:self.pos + n]  # 현재 위치에서 n바이트 슬라이싱
        self.pos += n  # 커서를 읽은 만큼 앞으로 이동
        return b

    def read_uint32(self) -> int:
        # ">I" = Big Endian 4바이트 unsigned int. Prometheus는 네트워크 바이트 순서(Big Endian) 사용
        return struct.unpack(">I", self.read_bytes(4))[0]

    def read_uint64(self) -> int:
        # ">Q" = Big Endian 8바이트 unsigned int
        return struct.unpack(">Q", self.read_bytes(8))[0]

    def read_uvarint(self) -> int:
        """unsigned variable-length integer (protobuf style)

        가변 길이 정수 인코딩: 작은 숫자는 1바이트, 큰 숫자는 여러 바이트 사용.
        각 바이트의 최상위 비트(MSB)가 1이면 "다음 바이트도 있다"는 뜻,
        0이면 "이 바이트가 마지막"이라는 뜻.
        하위 7비트만 실제 데이터이며, Little Endian 순서로 이어붙임.

        예) 300 = 0b100101100
            → 첫 바이트: 0xAC = 1_0101100 (MSB=1이므로 계속)
            → 둘째 바이트: 0x02 = 0_0000010 (MSB=0이므로 끝)
            → 값 = 0101100 | (0000010 << 7) = 300
        """
        result = 0
        shift = 0
        while True:
            b = self.data[self.pos]
            self.pos += 1
            result |= (b & 0x7F) << shift  # 하위 7비트만 추출하여 결과에 합침
            if (b & 0x80) == 0:  # MSB가 0이면 마지막 바이트
                break
            shift += 7  # 다음 7비트는 더 상위 자리에 배치
        return result

    def read_varint(self) -> int:
        """signed variable-length integer (zigzag encoding)

        음수를 효율적으로 인코딩하기 위한 방식.
        일반 2's complement에서는 -1이 매우 큰 값(0xFFFF...)이 되지만,
        zigzag에서는: 0→0, -1→1, 1→2, -2→3, 2→4, ... 로 매핑하여
        절댓값이 작은 음수도 적은 바이트로 표현 가능.
        """
        uv = self.read_uvarint()
        # zigzag decode: 홀수면 음수, 짝수면 양수로 복원
        return (uv >> 1) ^ -(uv & 1)

    def read_string(self) -> str:
        """uvarint-prefixed UTF-8 string

        먼저 uvarint로 문자열 길이(바이트 수)를 읽고,
        그 길이만큼 바이트를 읽어 UTF-8 디코딩.
        """
        length = self.read_uvarint()
        raw = self.read_bytes(length)
        return raw.decode("utf-8", errors="replace")


# ─────────────────────────────────────────────
# 1. Header: magic + version
# ─────────────────────────────────────────────
def parse_header(r: IndexReader):
    print(f"\n{C.H}{'='*60}")
    print(f" 1. HEADER (magic + version)")
    print(f"{'='*60}{C.R}\n")

    r.seek(0)  # 파일 맨 앞으로 이동
    magic = r.read_uint32()  # 첫 4바이트: 매직 넘버 (파일 형식 식별자)
    version = r.data[4]  # 5번째 바이트: 포맷 버전 (보통 2)

    magic_ok = magic == INDEX_MAGIC  # 0xBAAAD700이 아니면 index 파일이 아님
    status = f"{C.OK}✓ OK{C.R}" if magic_ok else f"{C.W}✗ MISMATCH{C.R}"
    print(f"  {C.K}magic number:{C.R}  0x{magic:08X}  (expected 0xBAAA_D700) {status}")
    print(f"  {C.K}version:{C.R}       {version}")

    if not magic_ok:
        print(f"\n  {C.W}ERROR: 이 파일은 Prometheus TSDB index가 아닙니다.{C.R}")
        sys.exit(1)

    return version


# ─────────────────────────────────────────────
# 2. TOC (Table Of Contents) — 파일 끝 52바이트
# ─────────────────────────────────────────────
def parse_toc(r: IndexReader) -> dict:
    print(f"\n{C.H}{'='*60}")
    print(f" 2. TOC (Table Of Contents) — last {TOC_SIZE} bytes")
    print(f"{'='*60}{C.R}\n")

    # TOC는 파일의 맨 마지막 52바이트에 위치. 각 섹션이 파일 내 어디에 있는지 오프셋을 기록.
    # → 파서가 원하는 섹션으로 바로 점프할 수 있게 해주는 "목차" 역할.
    toc_offset = r.size - TOC_SIZE  # 파일 끝에서 52바이트 앞 = TOC 시작 위치
    r.seek(toc_offset)

    # TOC에 순서대로 기록된 6개 섹션의 이름
    fields = [
        "symbols",               # 심볼 테이블 (모든 label name/value 문자열 저장소)
        "series",                # 시리즈 데이터 (각 시리즈의 label + chunk 정보)
        "label_indices_start",   # label index (deprecated, 더 이상 안 씀)
        "label_offset_table",    # label offset table (deprecated)
        "postings_start",        # posting list 데이터 (label pair → series ID 목록)
        "postings_offset_table", # posting offset table (어떤 label pair가 어떤 posting에 있는지)
    ]
    toc = {}
    for name in fields:
        toc[name] = r.read_uint64()  # 각 섹션의 시작 byte offset (8바이트씩)

    crc = r.read_uint32()  # TOC 자체의 무결성 검증용 CRC32 체크섬

    print(f"  {C.D}TOC starts at byte offset: {toc_offset} (0x{toc_offset:X}){C.R}\n")
    for name, offset in toc.items():
        flag = f" {C.D}(not present){C.R}" if offset == 0 else ""
        print(f"  {C.K}{name:30s}{C.R} → offset {offset:>12d}  (0x{offset:X}){flag}")
    print(f"\n  {C.D}CRC32: 0x{crc:08X}{C.R}")

    return toc


# ─────────────────────────────────────────────
# 3. Symbol Table
# ─────────────────────────────────────────────
def parse_symbol_table(r: IndexReader, offset: int, max_display: int = 30) -> dict:
    print(f"\n{C.H}{'='*60}")
    print(f" 3. SYMBOL TABLE")
    print(f"{'='*60}{C.R}\n")

    r.seek(offset)
    section_len = r.read_uint32()   # 심볼 테이블 전체 바이트 크기
    num_symbols = r.read_uint32()   # 심볼 개수

    print(f"  {C.K}section length:{C.R}  {section_len} bytes")
    print(f"  {C.K}# symbols:{C.R}      {num_symbols}")
    print()

    # 심볼 테이블은 모든 label name과 label value 문자열을 한 곳에 모아둔 "사전".
    # 다른 섹션(series, postings 등)에서는 문자열을 직접 저장하지 않고,
    # 이 테이블의 인덱스 번호(순번)로 참조하여 공간을 절약함.
    symbols_by_index = {}  # index(순번) → string  (예: 0→"__name__", 1→"job", ...)
    symbols_by_offset = {}  # file byte offset → string

    # symbols 데이터 시작 위치: offset + 4(len) + 4(#symbols)
    data_start = offset + 4 + 4

    for i in range(num_symbols):
        sym_offset = r.pos  # 이 symbol의 파일 내 byte offset
        sym = r.read_string()  # uvarint(길이) + UTF-8 바이트 → 문자열 한 개 읽기
        symbols_by_index[i] = sym  # 순번으로 조회용
        symbols_by_offset[sym_offset] = sym  # byte offset으로 조회용

    # 처음 N개만 출력
    display_n = min(max_display, num_symbols)
    print(f"  {C.D}── first {display_n} symbols (of {num_symbols}) ──{C.R}")
    for i in range(display_n):
        s = symbols_by_index[i]
        display = s if len(s) <= 60 else s[:57] + "..."
        print(f"    [{i:>5d}] {C.V}\"{display}\"{C.R}")

    if num_symbols > max_display:
        print(f"    {C.D}... ({num_symbols - max_display} more symbols){C.R}")

    return symbols_by_index


# ─────────────────────────────────────────────
# 4. Series section — 몇 개만 샘플로 파싱
# ─────────────────────────────────────────────
def parse_series_sample(r: IndexReader, offset: int, symbols: dict, count: int = 5):
    print(f"\n{C.H}{'='*60}")
    print(f" 4. SERIES (first {count} entries)")
    print(f"{'='*60}{C.R}\n")

    r.seek(offset)
    parsed = 0

    for _ in range(count * 3):  # 16-byte alignment padding이 있을 수 있으므로 넉넉히
        if parsed >= count:
            break

        # 16-byte alignment: 현재 pos가 16의 배수가 아니면 패딩 건너뛰기
        entry_start = r.pos
        if entry_start % 16 != 0:
            # 다음 16배수로 이동
            next_aligned = ((entry_start + 15) // 16) * 16
            r.seek(next_aligned)
            entry_start = r.pos

        series_id = entry_start // 16

        try:
            entry_len = r.read_uvarint()
            if entry_len == 0:
                continue

            save_pos = r.pos

            # labels
            label_count = r.read_uvarint()
            labels = []
            for _ in range(label_count):
                name_ref = r.read_uvarint()
                value_ref = r.read_uvarint()
                name = symbols.get(name_ref, f"<sym#{name_ref}>")
                value = symbols.get(value_ref, f"<sym#{value_ref}>")
                labels.append((name, value))

            # chunks
            chunk_count = r.read_uvarint()

            label_str = ", ".join(f'{n}="{v}"' for n, v in labels)
            print(f"  {C.K}Series ID {series_id}{C.R}  {C.D}(offset {entry_start}){C.R}")
            print(f"    labels ({label_count}): {{{label_str}}}")
            print(f"    chunks: {chunk_count}")

            # 첫 번째 chunk의 mint/maxt 보여주기
            if chunk_count > 0:
                c0_mint = r.read_varint()
                c0_duration = r.read_uvarint()
                c0_maxt = c0_mint + c0_duration
                c0_ref = r.read_uvarint()
                print(f"    chunk[0]: mint={c0_mint}, maxt={c0_maxt}, ref=0x{c0_ref:X}")

            print()
            parsed += 1

            # 다음 series entry로 이동: save_pos + entry_len + CRC(4)
            r.seek(save_pos + entry_len + 4)

        except Exception as e:
            # 파싱 실패 시 다음 16-byte 경계로 이동
            r.seek(entry_start + 16)
            continue


# ─────────────────────────────────────────────
# 5. Postings Offset Table — inverted index의 핵심
# ─────────────────────────────────────────────
def parse_postings_offset_table(r: IndexReader, offset: int, max_display: int = 20) -> list:
    print(f"\n{C.H}{'='*60}")
    print(f" 5. POSTINGS OFFSET TABLE (inverted index directory)")
    print(f"{'='*60}{C.R}\n")

    r.seek(offset)
    section_len = r.read_uint32()
    num_entries = r.read_uint32()

    print(f"  {C.K}section length:{C.R}  {section_len} bytes")
    print(f"  {C.K}# entries:{C.R}      {num_entries}")
    print()

    entries = []
    display_n = min(max_display, num_entries)

    print(f"  {C.D}── first {display_n} entries (of {num_entries}) ──{C.R}")
    print(f"  {'label name':>30s}  {'label value':<30s}  {'postings offset':>15s}")
    print(f"  {'-'*30}  {'-'*30}  {'-'*15}")

    for i in range(num_entries):
        n = r.read_uvarint()  # always 2 (name + value)
        name = r.read_string()
        value = r.read_string()
        postings_offset = r.read_uvarint()

        entry = (name, value, postings_offset)
        entries.append(entry)

        if i < display_n:
            v_display = value if len(value) <= 28 else value[:25] + "..."
            n_display = name if len(name) <= 28 else name[:25] + "..."
            print(f"  {n_display:>30s}  {v_display:<30s}  {postings_offset:>15d}")

    if num_entries > max_display:
        print(f"  {C.D}... ({num_entries - max_display} more entries){C.R}")

    return entries


# ─────────────────────────────────────────────
# 6. Postings list 읽기 — 특정 label pair의 series 목록
# ─────────────────────────────────────────────
def parse_postings_list(r: IndexReader, offset: int, label_name: str, label_value: str):
    print(f"\n  {C.K}▶ Postings for {label_name}=\"{label_value}\"{C.R}"
          f"  {C.D}(offset {offset}){C.R}")

    r.seek(offset)
    section_len = r.read_uint32()
    num_entries = r.read_uint32()

    print(f"    # series in this postings list: {num_entries}")

    series_ids = []
    for _ in range(num_entries):
        sid = r.read_uint32()
        series_ids.append(sid)

    display_n = min(20, num_entries)
    ids_str = ", ".join(str(s) for s in series_ids[:display_n])
    if num_entries > display_n:
        ids_str += f" ... (+{num_entries - display_n} more)"

    print(f"    series IDs: [{ids_str}]")
    return series_ids


# ─────────────────────────────────────────────
# 7. Deep dive: 특정 posting을 따라가서 series 확인
# ─────────────────────────────────────────────
def lookup_series_by_id(r: IndexReader, series_id: int, symbols: dict):
    """series ID(= offset/16)로 series entry를 직접 찾아가기"""
    offset = series_id * 16
    r.seek(offset)

    try:
        entry_len = r.read_uvarint()
        label_count = r.read_uvarint()
        labels = []
        for _ in range(label_count):
            name_ref = r.read_uvarint()
            value_ref = r.read_uvarint()
            name = symbols.get(name_ref, f"<sym#{name_ref}>")
            value = symbols.get(value_ref, f"<sym#{value_ref}>")
            labels.append((name, value))

        label_str = ", ".join(f'{n}="{v}"' for n, v in labels)
        return f"{{{label_str}}}"
    except:
        return "<parse error>"


# ─────────────────────────────────────────────
# 8. Chunks directory의 chunk 파일도 간단히 확인
# ─────────────────────────────────────────────
CHUNK_MAGIC = 0x85BD40DD

def parse_chunk_file_header(filepath: str):
    print(f"\n{C.H}{'='*60}")
    print(f" BONUS: CHUNK FILE HEADER")
    print(f"{'='*60}{C.R}\n")

    with open(filepath, "rb") as f:
        header = f.read(8)

    magic = struct.unpack(">I", header[0:4])[0]
    version = header[4]
    padding = header[5:8]

    magic_ok = magic == CHUNK_MAGIC
    status = f"{C.OK}✓ OK{C.R}" if magic_ok else f"{C.W}✗ MISMATCH{C.R}"

    print(f"  {C.K}file:{C.R}          {filepath}")
    print(f"  {C.K}magic number:{C.R}  0x{magic:08X}  (expected 0x85BD40DD) {status}")
    print(f"  {C.K}version:{C.R}       {version}")
    print(f"  {C.K}padding:{C.R}       {padding.hex()}")

    file_size = os.path.getsize(filepath)
    print(f"  {C.K}file size:{C.R}     {file_size:,} bytes ({file_size/1024/1024:.2f} MiB)")


# ─────────────────────────────────────────────
# 9. meta.json 파싱
# ─────────────────────────────────────────────
def parse_meta_json(block_dir: str):
    import json

    meta_path = os.path.join(block_dir, "meta.json")
    if not os.path.exists(meta_path):
        return

    print(f"\n{C.H}{'='*60}")
    print(f" 0. META.JSON")
    print(f"{'='*60}{C.R}\n")

    with open(meta_path) as f:
        meta = json.load(f)

    print(f"  {C.K}ULID:{C.R}         {meta.get('ulid', '?')}")
    print(f"  {C.K}minTime:{C.R}      {meta.get('minTime', '?')}")
    print(f"  {C.K}maxTime:{C.R}      {meta.get('maxTime', '?')}")

    stats = meta.get("stats", {})
    print(f"  {C.K}numSeries:{C.R}    {stats.get('numSeries', '?'):,}")
    print(f"  {C.K}numChunks:{C.R}    {stats.get('numChunks', '?'):,}")
    print(f"  {C.K}numSamples:{C.R}   {stats.get('numSamples', '?'):,}")

    comp = meta.get("compaction", {})
    print(f"  {C.K}compaction:{C.R}   level={comp.get('level', '?')}, sources={comp.get('sources', [])}")

    # time range을 사람이 읽을 수 있게
    from datetime import datetime, timezone
    try:
        mint = datetime.fromtimestamp(meta["minTime"] / 1000, tz=timezone.utc)
        maxt = datetime.fromtimestamp(meta["maxTime"] / 1000, tz=timezone.utc)
        duration = (meta["maxTime"] - meta["minTime"]) / 1000 / 3600
        print(f"\n  {C.D}time range: {mint.isoformat()} ~ {maxt.isoformat()}")
        print(f"  duration:   {duration:.1f} hours{C.R}")
    except:
        pass


# ─────────────────────────────────────────────
# 10. Tombstones
# ─────────────────────────────────────────────
TOMBSTONE_MAGIC = 0x0130BA30

def parse_tombstones(block_dir: str):
    path = os.path.join(block_dir, "tombstones")
    if not os.path.exists(path):
        return

    file_size = os.path.getsize(path)

    print(f"\n{C.H}{'='*60}")
    print(f" BONUS: TOMBSTONES")
    print(f"{'='*60}{C.R}\n")

    with open(path, "rb") as f:
        data = f.read()

    if len(data) < 5:
        print(f"  {C.D}(empty or too small: {file_size} bytes){C.R}")
        return

    magic = struct.unpack(">I", data[0:4])[0]
    version = data[4]

    magic_ok = magic == TOMBSTONE_MAGIC
    status = f"{C.OK}✓ OK{C.R}" if magic_ok else f"{C.W}✗ MISMATCH{C.R}"
    print(f"  {C.K}magic:{C.R}    0x{magic:08X}  (expected 0x0130BA30) {status}")
    print(f"  {C.K}version:{C.R}  {version}")
    print(f"  {C.K}size:{C.R}     {file_size} bytes")

    if file_size <= 9:  # header(5) + CRC(4) = no tombstones
        print(f"  {C.D}(no tombstone entries){C.R}")


# ─────────────────────────────────────────────
# MAIN: 전체 파싱 흐름
# ─────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <block-dir-or-index-file>")
        print(f"Example: python {sys.argv[0]} ./data/01EM6Q6A1YPX4G9TEB20J22B2R")
        print(f"         python {sys.argv[0]} ./data/01EM6Q6A1YPX4G9TEB20J22B2R/index")
        sys.exit(1)

    path = sys.argv[1]

    # block 디렉토리가 주어졌으면 index 파일 경로 자동 결정
    if os.path.isdir(path):
        block_dir = path
        index_path = os.path.join(path, "index")
    else:
        index_path = path
        block_dir = os.path.dirname(path)

    if not os.path.exists(index_path):
        print(f"ERROR: index file not found: {index_path}")
        sys.exit(1)

    file_size = os.path.getsize(index_path)
    print(f"\n{C.H}╔══════════════════════════════════════════════════════════╗")
    print(f"║  Prometheus TSDB Index Parser                           ║")
    print(f"╚══════════════════════════════════════════════════════════╝{C.R}")
    print(f"\n  {C.K}file:{C.R}  {index_path}")
    print(f"  {C.K}size:{C.R}  {file_size:,} bytes ({file_size/1024:.1f} KiB)")

    # ── 0. meta.json ──
    parse_meta_json(block_dir)

    # ── 1. Header ──
    r = IndexReader(index_path)
    version = parse_header(r)

    # ── 2. TOC ──
    toc = parse_toc(r)

    # ── 3. Symbol Table ──
    symbols = parse_symbol_table(r, toc["symbols"])

    # ── 4. Series (sample) ──
    parse_series_sample(r, toc["series"], symbols)

    # ── 5. Postings Offset Table ──
    entries = []
    if toc["postings_offset_table"] > 0:
        entries = parse_postings_offset_table(r, toc["postings_offset_table"])

    # ── 6. Postings deep dive ──
    #    몇 가지 흥미로운 label pair를 골라서 postings list까지 따라가기
    if entries:
        print(f"\n{C.H}{'='*60}")
        print(f" 6. POSTINGS DEEP DIVE — label pair → series IDs → series")
        print(f"{'='*60}{C.R}")

        # 흥미로운 label pair 고르기: __name__ 이 있으면 그걸 먼저
        interesting = []
        seen_names = set()
        for name, value, off in entries:
            if name == "__name__" and len(interesting) < 3:
                interesting.append((name, value, off))
                seen_names.add(value)
        # job, instance 등 일반적인 것도 추가
        for name, value, off in entries:
            if name in ("job", "instance") and len(interesting) < 5:
                interesting.append((name, value, off))
        # 그래도 부족하면 아무거나
        for name, value, off in entries:
            if len(interesting) >= 5:
                break
            if (name, value) not in [(n, v) for n, v, _ in interesting]:
                interesting.append((name, value, off))

        for name, value, off in interesting:
            series_ids = parse_postings_list(r, off, name, value)

            # 첫 2개 series의 label set을 실제로 따라가서 확인
            for sid in series_ids[:2]:
                label_set = lookup_series_by_id(r, sid, symbols)
                print(f"      → series {sid}: {label_set}")
            print()

    # ── Bonus: chunks file header ──
    chunks_dir = os.path.join(block_dir, "chunks")
    if os.path.isdir(chunks_dir):
        chunk_files = sorted(os.listdir(chunks_dir))
        if chunk_files:
            parse_chunk_file_header(os.path.join(chunks_dir, chunk_files[0]))

    # ── Bonus: tombstones ──
    parse_tombstones(block_dir)

    # ── Summary ──
    print(f"\n{C.H}{'='*60}")
    print(f" SUMMARY")
    print(f"{'='*60}{C.R}\n")
    print(f"  index 파일 구조 (byte offset 순서):")
    print(f"  ┌─ 0x00000000  Header (magic=0xBAAAD700 + version)")
    if toc["symbols"]:
        print(f"  ├─ 0x{toc['symbols']:08X}  Symbol Table ({len(symbols)} symbols)")
    if toc["series"]:
        print(f"  ├─ 0x{toc['series']:08X}  Series")
    if toc["label_indices_start"]:
        print(f"  ├─ 0x{toc['label_indices_start']:08X}  Label Indices {C.D}(deprecated, not read){C.R}")
    if toc["postings_start"]:
        print(f"  ├─ 0x{toc['postings_start']:08X}  Postings")
    if toc["label_offset_table"]:
        print(f"  ├─ 0x{toc['label_offset_table']:08X}  Label Offset Table {C.D}(deprecated){C.R}")
    if toc["postings_offset_table"]:
        print(f"  ├─ 0x{toc['postings_offset_table']:08X}  Postings Offset Table")
    print(f"  └─ 0x{r.size - TOC_SIZE:08X}  TOC (last 52 bytes)")
    print()


if __name__ == "__main__":
    main()
