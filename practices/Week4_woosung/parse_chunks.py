import struct
import datetime

with open("./chunks_head_000001", "rb") as f:
    # 헤더 파싱 (8바이트)
    magic = f.read(4)
    version = f.read(1)
    padding = f.read(3)
    print(f"Magic Number : 0x{magic.hex().upper()}")
    print(f"Version      : {version[0]}")
    print(f"Padding      : {padding.hex()}")
    print("---")

    # 첫 번째 청크 파싱
    series_ref = struct.unpack(">Q", f.read(8))[0]
    mint        = struct.unpack(">Q", f.read(8))[0]
    maxt        = struct.unpack(">Q", f.read(8))[0]

    mint_dt = datetime.datetime.fromtimestamp(mint / 1000)
    maxt_dt = datetime.datetime.fromtimestamp(maxt / 1000)

    print(f"Series Ref   : {series_ref}")
    print(f"Min Time     : {mint} ({mint_dt})")
    print(f"Max Time     : {maxt} ({maxt_dt})")
