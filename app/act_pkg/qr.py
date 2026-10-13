"""
QR-код для ссылки «Как посчитано» в акте (ТЗ 13.10.2026, день 9.1) — без внешних пакетов (qrcode / segno в окружении
нет, ставить без согласования нельзя).

Только то, что нужно акту: байтовый режим, уровень коррекции M (≈15 % повреждений), версии 1–10 (до 213 байт —
ссылка на сервер с номером акта занимает 60–90). Маска — по наименьшему штрафу ISO/IEC 18004, п. 7.8.3.

Чистые функции: matrix(text) → список строк из 0/1 (тёмный модуль = 1), без «тихой зоны» (её рисует вызывающий).
decode_self(m) — обратное чтение той же матрицы (формат, маска, коды Рида — Соломона, данные) для теста.
"""
from typing import List, Optional

# версия → (кодовых слов EC на блок, [(число блоков, слов данных в блоке), …]) для уровня M
_BLOCKS_M = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]), 5: (24, [(2, 43)]),
    6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
          9: [6, 26, 46], 10: [6, 28, 50]}
_REMAINDER = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0}
_EC_M = 0b00                                  # биты уровня коррекции в строке формата: L=01, M=00, Q=11, H=10
MAX_VERSION = 10


# --------------------------------------------------------------------------- #
#  Поле Галуа GF(256) и коды Рида — Соломона
# --------------------------------------------------------------------------- #

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]
del _x, _i


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _generator(n: int) -> list:
    g = [1]
    for i in range(n):
        nxt = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            nxt[j] ^= c
            nxt[j + 1] ^= _mul(c, _EXP[i])
        g = nxt
    return g


def rs_ecc(data: list, n: int) -> list:
    """n кодовых слов коррекции для блока данных (деление многочлена данных на порождающий)."""
    g = _generator(n)
    rem = list(data) + [0] * n
    for i in range(len(data)):
        c = rem[i]
        if c:
            for j in range(1, len(g)):
                rem[i + j] ^= _mul(g[j], c)
    return rem[len(data):]


# --------------------------------------------------------------------------- #
#  Данные: байтовый режим, блоки, чередование
# --------------------------------------------------------------------------- #

def _capacity(v: int) -> int:
    return sum(c * d for c, d in _BLOCKS_M[v][1])


def _version_for(n_bytes: int) -> Optional[int]:
    for v in range(1, MAX_VERSION + 1):
        count_bits = 8 if v < 10 else 16
        if 4 + count_bits + 8 * n_bytes <= 8 * _capacity(v):
            return v
    return None


def _codewords(payload: bytes, v: int) -> list:
    bits = []

    def put(val, n):
        bits.extend((val >> (n - 1 - i)) & 1 for i in range(n))
    put(0b0100, 4)                                         # байтовый режим
    put(len(payload), 8 if v < 10 else 16)
    for b in payload:
        put(b, 8)
    cap = 8 * _capacity(v)
    put(0, min(4, cap - len(bits)))                        # терминатор
    while len(bits) % 8:
        bits.append(0)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    i = 0
    while len(data) < _capacity(v):
        data.append(pad[i % 2])
        i += 1
    ec_n, groups = _BLOCKS_M[v]
    blocks, k = [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(data[k:k + size])
            k += size
    ecs = [rs_ecc(b, ec_n) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        out += [b[i] for b in blocks if i < len(b)]
    for i in range(ec_n):
        out += [e[i] for e in ecs]
    return out


# --------------------------------------------------------------------------- #
#  Матрица
# --------------------------------------------------------------------------- #

def _bch_format(mask: int) -> int:
    d = (_EC_M << 3) | mask
    r = d << 10
    for i in range(14, 9, -1):
        if (r >> i) & 1:
            r ^= 0x537 << (i - 10)
    return ((d << 10) | r) ^ 0x5412


def _bch_version(v: int) -> int:
    r = v << 12
    for i in range(17, 11, -1):
        if (r >> i) & 1:
            r ^= 0x1F25 << (i - 12)
    return (v << 12) | r


_MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)


def _base(v: int) -> tuple:
    """Функциональные узоры: поисковые, разделители, синхронизация, выравнивание, тёмный модуль, резерв формата
    и версии. Возвращает (матрица, занято-функциями)."""
    n = 17 + 4 * v
    m = [[0] * n for _ in range(n)]
    f = [[False] * n for _ in range(n)]

    def setf(r, c, val):
        m[r][c] = val
        f[r][c] = True
    for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = r0 + r, c0 + c
                if 0 <= rr < n and 0 <= cc < n:
                    dark = (0 <= r <= 6 and 0 <= c <= 6 and (r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4)))
                    setf(rr, cc, 1 if dark else 0)
    for i in range(8, n - 8):
        setf(6, i, 1 if i % 2 == 0 else 0)
        setf(i, 6, 1 if i % 2 == 0 else 0)
    pos = _ALIGN[v]
    for r in pos:
        for c in pos:
            if (r, c) in ((6, 6), (6, pos[-1]), (pos[-1], 6)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    setf(r + dr, c + dc, 1 if max(abs(dr), abs(dc)) != 1 else 0)
    setf(n - 8, 8, 1)                                     # тёмный модуль
    for i in range(9):                                    # резерв формата
        if not f[8][i]:
            setf(8, i, 0)
        if not f[i][8]:
            setf(i, 8, 0)
    for i in range(8):
        setf(8, n - 1 - i, 0)
        setf(n - 1 - i, 8, 0)
    if v >= 7:
        bits = _bch_version(v)
        for i in range(18):
            b = (bits >> i) & 1
            setf(n - 11 + i % 3, i // 3, b)
            setf(i // 3, n - 11 + i % 3, b)
    return m, f


def _zigzag(n: int):
    """Порядок модулей данных: пары столбцов справа налево, вверх-вниз змейкой, столбец 6 пропускается."""
    c = n - 1
    up = True
    while c > 0:
        if c == 6:
            c -= 1
        rows = range(n - 1, -1, -1) if up else range(n)
        for r in rows:
            for cc in (c, c - 1):
                yield r, cc
        up = not up
        c -= 2


def _place(m, f, words: list, rem: int) -> None:
    bits = [(w >> (7 - i)) & 1 for w in words for i in range(8)] + [0] * rem
    k = 0
    for r, c in _zigzag(len(m)):
        if not f[r][c]:
            m[r][c] = bits[k] if k < len(bits) else 0
            k += 1


def _put_format(m, mask: int) -> None:
    n = len(m)
    bits = _bch_format(mask)
    b = [(bits >> i) & 1 for i in range(15)]
    # копия 1 — вокруг левого верхнего поискового узора
    for i in range(6):
        m[8][i] = b[14 - i]
    m[8][7] = b[8]
    m[8][8] = b[7]
    m[7][8] = b[6]
    for i in range(6):
        m[5 - i][8] = b[5 - i]
    # копия 2 — у правого верхнего и левого нижнего
    for i in range(8):
        m[8][n - 1 - i] = b[i]
    for i in range(7):
        m[n - 7 + i][8] = b[8 + i]


def _penalty(m) -> int:
    n = len(m)
    p = 0
    for line in list(m) + [list(col) for col in zip(*m)]:
        run, prev = 0, None
        for x in line:
            if x == prev:
                run += 1
            else:
                if run >= 5:
                    p += run - 2
                run, prev = 1, x
        if run >= 5:
            p += run - 2
        s = "".join(map(str, line))
        p += 40 * (s.count("10111010000") + s.count("00001011101"))
    for r in range(n - 1):
        for c in range(n - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                p += 3
    dark = sum(map(sum, m))
    p += 10 * (abs(dark * 20 - n * n * 10) // (n * n))
    return p


def matrix(text: str) -> List[List[int]]:
    """QR-код строки (UTF-8, байтовый режим, уровень M). Длиннее 213 байт — ValueError."""
    payload = str(text).encode("utf-8")
    v = _version_for(len(payload))
    if v is None:
        raise ValueError("строка длиннее %d байт — QR версии 1–10 не вмещает" % (_capacity(MAX_VERSION) - 2))
    words = _codewords(payload, v)
    best, best_p = None, None
    for mask in range(8):
        m, f = _base(v)
        _place(m, f, words, _REMAINDER[v])
        fn = _MASKS[mask]
        for r in range(len(m)):
            for c in range(len(m)):
                if not f[r][c] and fn(r, c):
                    m[r][c] ^= 1
        _put_format(m, mask)
        p = _penalty(m)
        if best_p is None or p < best_p:
            best, best_p = m, p
    return best


# --------------------------------------------------------------------------- #
#  Обратное чтение (проверка в тестах)
# --------------------------------------------------------------------------- #

def decode_self(m) -> dict:
    """Читает матрицу, построенную matrix(): формат (обе копии), маска, кодовые слова, остаток RS = 0, текст."""
    n = len(m)
    v = (n - 17) // 4
    b1 = [m[8][i] for i in range(6)] + [m[8][7], m[8][8], m[7][8]] + [m[5 - i][8] for i in range(6)]
    f1 = int("".join(map(str, b1)), 2)
    b2 = [m[n - 1 - i][8] for i in range(7)] + [m[8][n - 8 + i] for i in range(8)]
    f2 = int("".join(map(str, b2)), 2)
    mask = None
    for k in range(8):
        if _bch_format(k) == f1:
            mask = k
    _base_m, f = _base(v)
    bits = []
    for r, c in _zigzag(n):
        if not f[r][c]:
            x = m[r][c] ^ (1 if _MASKS[mask](r, c) else 0)
            bits.append(x)
    total = len(_codewords(b"", v))
    words = [int("".join(map(str, bits[i * 8:i * 8 + 8])), 2) for i in range(total)]
    ec_n, groups = _BLOCKS_M[v]
    sizes = [s for cnt, s in groups for _ in range(cnt)]
    blocks = [[] for _ in sizes]
    k = 0
    for i in range(max(sizes)):
        for j, s in enumerate(sizes):
            if i < s:
                blocks[j].append(words[k])
                k += 1
    ecs = [[] for _ in sizes]
    for i in range(ec_n):
        for j in range(len(sizes)):
            ecs[j].append(words[k])
            k += 1
    rs_ok = all(rs_ecc(b, ec_n) == e for b, e in zip(blocks, ecs))
    data = [x for b in blocks for x in b]
    dbits = "".join(format(x, "08b") for x in data)
    mode = int(dbits[:4], 2)
    cb = 8 if v < 10 else 16
    ln = int(dbits[4:4 + cb], 2)
    raw = bytes(int(dbits[4 + cb + 8 * i:4 + cb + 8 * i + 8], 2) for i in range(ln))
    return {"version": v, "mask": mask, "format_copies_equal": f1 == f2, "rs_ok": rs_ok, "mode": mode,
            "text": raw.decode("utf-8", "replace")}
