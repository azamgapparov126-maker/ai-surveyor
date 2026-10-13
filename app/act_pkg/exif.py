"""
Дата съёмки, координаты и поворот из EXIF фото для второй страницы акта (ТЗ 13.10.2026, день 9.1).

Читаем только три вещи: DateTimeOriginal (иначе DateTime), GPS-широту и долготу, Orientation. Остальное (модель
камеры, серийный номер, автор, комментарии) не читается и в акт не попадает — персональных данных из EXIF нет.
Без внешних пакетов; чистые функции, на испорченных данных — пустой результат, без исключений.
"""
import struct
from typing import Optional

TAG_EXIF_IFD, TAG_GPS_IFD = 0x8769, 0x8825
TAG_DATETIME, TAG_DATETIME_ORIG, TAG_ORIENTATION = 0x0132, 0x9003, 0x0112
_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def _app1(blob: bytes) -> Optional[bytes]:
    """Тело сегмента APP1 «Exif» JPEG (TIFF-заголовок и дальше) или None."""
    if blob[:2] != b"\xff\xd8":
        return None
    i, n = 2, len(blob)
    while i + 4 <= n and blob[i] == 0xFF:
        m = blob[i + 1]
        if m in (0xD9, 0xDA):
            return None
        seg = struct.unpack(">H", blob[i + 2:i + 4])[0]
        if m == 0xE1 and blob[i + 4:i + 10] == b"Exif\x00\x00":
            return blob[i + 10:i + 2 + seg]
        i += 2 + seg
    return None


def _ifd(t: bytes, off: int, e: str) -> dict:
    out = {}
    if off <= 0 or off + 2 > len(t):
        return out
    cnt = struct.unpack(e + "H", t[off:off + 2])[0]
    for k in range(min(cnt, 200)):
        p = off + 2 + 12 * k
        if p + 12 > len(t):
            break
        tag, typ, num = struct.unpack(e + "HHI", t[p:p + 8])
        size = _SIZES.get(typ, 1) * num
        data = t[p + 8:p + 12] if size <= 4 else None
        if data is None:
            vo = struct.unpack(e + "I", t[p + 8:p + 12])[0]
            if vo + size > len(t):
                continue
            data = t[vo:vo + size]
        out[tag] = (typ, num, data)
    return out


def _val(entry, e: str):
    typ, num, data = entry
    if typ == 2:
        return data.split(b"\x00")[0].decode("ascii", "replace").strip()
    if typ == 3:
        return struct.unpack(e + "H", data[:2])[0]
    if typ == 4:
        return struct.unpack(e + "I", data[:4])[0]
    if typ == 5:
        return [(lambda a, b: a / b if b else 0.0)(*struct.unpack(e + "II", data[8 * i:8 * i + 8]))
                for i in range(num)]
    return None


def _deg(v, ref) -> Optional[float]:
    if not isinstance(v, list) or len(v) < 3:
        return None
    d = v[0] + v[1] / 60 + v[2] / 3600
    if str(ref or "").upper() in ("S", "W"):
        d = -d
    return round(d, 5)


def read(blob: bytes) -> dict:
    """{taken: 'YYYY-MM-DD HH:MM' | None, lat, lon: float | None, orientation: 1..8 | None}."""
    out = {"taken": None, "lat": None, "lon": None, "orientation": None}
    try:
        t = _app1(blob or b"")
        if not t or t[:2] not in (b"II", b"MM"):
            return out
        e = "<" if t[:2] == b"II" else ">"
        ifd0 = _ifd(t, struct.unpack(e + "I", t[4:8])[0], e)
        if TAG_ORIENTATION in ifd0:
            o = _val(ifd0[TAG_ORIENTATION], e)
            out["orientation"] = o if o in range(1, 9) else None
        dt = None
        if TAG_EXIF_IFD in ifd0:
            sub = _ifd(t, _val(ifd0[TAG_EXIF_IFD], e) or 0, e)
            if TAG_DATETIME_ORIG in sub:
                dt = _val(sub[TAG_DATETIME_ORIG], e)
        if not dt and TAG_DATETIME in ifd0:
            dt = _val(ifd0[TAG_DATETIME], e)
        if dt and len(dt) >= 16 and dt[:4].isdigit():
            out["taken"] = f"{dt[0:4]}-{dt[5:7]}-{dt[8:10]} {dt[11:16]}"
        if TAG_GPS_IFD in ifd0:
            g = _ifd(t, _val(ifd0[TAG_GPS_IFD], e) or 0, e)
            if 2 in g and 4 in g:
                lat = _deg(_val(g[2], e), _val(g[1], e) if 1 in g else "N")
                lon = _deg(_val(g[4], e), _val(g[3], e) if 3 in g else "E")
                if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180 and (lat or lon):
                    out["lat"], out["lon"] = lat, lon
    except (struct.error, ValueError, TypeError, IndexError):
        return {"taken": None, "lat": None, "lon": None, "orientation": None}
    return out


def build_app1(taken: str = None, lat: float = None, lon: float = None, orientation: int = None) -> bytes:
    """Сегмент APP1 «Exif» с датой, координатами и поворотом — для учебных фото в тестах (обратная к read)."""
    e = "<"
    ifd0, exif, gps = [], [], []
    if orientation:
        ifd0.append((TAG_ORIENTATION, 3, 1, struct.pack(e + "HH", orientation, 0)))
    if taken:
        s = taken.replace("-", ":").encode("ascii") + b":00\x00"
        exif.append((TAG_DATETIME_ORIG, 2, len(s), s))

    def rat(x):
        d = int(abs(x))
        m = int((abs(x) - d) * 60)
        sec = round(((abs(x) - d) * 60 - m) * 60 * 100)
        return struct.pack(e + "IIIIII", d, 1, m, 1, sec, 100)
    if lat is not None and lon is not None:
        gps += [(1, 2, 2, (b"N" if lat >= 0 else b"S") + b"\x00\x00\x00"), (2, 5, 3, rat(lat)),
                (3, 2, 2, (b"E" if lon >= 0 else b"W") + b"\x00\x00\x00"), (4, 5, 3, rat(lon))]
    # раскладка: заголовок 8, IFD0, IFD Exif, IFD GPS, затем данные длиннее 4 байт
    n0 = len(ifd0) + (1 if exif else 0) + (1 if gps else 0)
    o0 = 8
    oe = o0 + 2 + 12 * n0 + 4
    og = oe + (2 + 12 * len(exif) + 4 if exif else 0)
    od = og + (2 + 12 * len(gps) + 4 if gps else 0)
    extra = bytearray()

    def ifd_bytes(entries):
        nonlocal extra
        b = struct.pack(e + "H", len(entries))
        for tag, typ, num, data in sorted(entries):
            if len(data) <= 4:
                b += struct.pack(e + "HHI", tag, typ, num) + data.ljust(4, b"\x00")
            else:
                b += struct.pack(e + "HHII", tag, typ, num, od + len(extra))
                extra += data
        return b + struct.pack(e + "I", 0)
    e0 = list(ifd0)
    if exif:
        e0.append((TAG_EXIF_IFD, 4, 1, struct.pack(e + "I", oe)))
    if gps:
        e0.append((TAG_GPS_IFD, 4, 1, struct.pack(e + "I", og)))
    body = b"II*\x00" + struct.pack(e + "I", o0) + ifd_bytes(e0)
    if exif:
        body += ifd_bytes(exif)
    if gps:
        body += ifd_bytes(gps)
    body += bytes(extra)
    payload = b"Exif\x00\x00" + body
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def with_exif(jpeg: bytes, **kw) -> bytes:
    """JPEG с вставленным после SOI сегментом EXIF (для тестов)."""
    return jpeg[:2] + build_app1(**kw) + jpeg[2:]
