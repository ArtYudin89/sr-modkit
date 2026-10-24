"""Оболочка файла .dat: контрольные суммы, поточный шифр, обёртка ZL.

Слои, снаружи внутрь:

1. необязательный внешний заголовок — 8 байт: длина остатка (поXOR-енная
   двумя ключами сумм) и внешняя контрольная сумма. Игра без него работает,
   только помечает у себя «контрольная сумма ресурса не сошлась»;
   BlockParEditor заголовок не пишет вовсе;
2. 8 байт: контрольная сумма расшифрованного и начальное число потока,
   поXOR-енное ключом;
3. поточный шифр — XOR с младшими байтами генератора 16807 по модулю 2^31-1;
4. обёртка ZL01 и внутри неё сжатое дерево.

Ключей два: для датников BlockPar (Main.dat, Lang.dat) и для данных
(CacheData.dat). Какой подошёл — видно по контрольной сумме, и это же
говорит, какое внутри дерево.
"""

import struct
import zlib
from collections import OrderedDict

from . import zl

BLOCK_SEED_KEY = 0xB1E8C689
RESOURCE_SEED_KEY = 0xEA8F3F37
CRC_KEY1 = 0x7DB6C99D
CRC_KEY2 = 0xC83FCBF3

KIND_BLOCK = "block"
KIND_DATA = "data"

SEED_KEYS = OrderedDict((
    (KIND_BLOCK, BLOCK_SEED_KEY),
    (KIND_DATA, RESOURCE_SEED_KEY),
))

# Начальное число по умолчанию при записи: любое годится, берём постоянное,
# чтобы одинаковое дерево давало одинаковый файл.
DEFAULT_SEED = 0x1F2E3D4C

MODULUS = 0x7FFFFFFF
MULTIPLIER = 16807
QUOTIENT = 127773
REMAINDER = 2836


class DatError(ValueError):
    """Файл не разбирается как .dat игры."""


def keystream(seed, size):
    """Байты гаммы. Генератор тот же, что в игре, с усечением деления к нулю.

    Шаг игры — метод Шраге: для состояния x из 1…M−1 он даёт ровно
    16807·x mod M (M = 2³¹−1), всегда снова из 1…M−1. Поэтому шаги по формуле
    игры нужны, только пока состояние вне этого промежутка (отрицательное
    семя, 0, M), дальше — умножение по модулю, и сразу кусками (`_fill_lanes`).
    Байты те же (`tests/test_datfile.py` сверяет с шагом игры).
    """
    out = bytearray(size)
    state = seed
    index = 0
    while index < size and not 0 < state < MODULUS:
        quotient = int(state / QUOTIENT)
        state = MULTIPLIER * (state - quotient * QUOTIENT) - REMAINDER * quotient
        if state <= 0:
            state += MODULUS
        out[index] = (state - 1) & 0xFF
        index += 1
    if index < size:
        if FAST_LANES and size - index >= _LANES:
            _fill_lanes(out, index, state)
        else:
            for position in range(index, size):
                state = state * MULTIPLIER % MODULUS
                out[position] = (state - 1) & 0xFF
    return bytes(out)


#: гамма кусками (`_fill_lanes`); False — по шагу, медленнее, байты те же
FAST_LANES = True

#: сколько состояний генератора в одном куске
_LANES = 4096
_LANE_BITS = 64
#: младший байт состояния минус один — таблица для `bytes.translate`
_MINUS_ONE = bytes((value - 1) & 0xFF for value in range(256))
_LANE_TABLES = []


def _lane_tables():
    """(степени 16807¹…16807ᴷ mod M по 64-битным дорожкам одного целого,
    маска младших 31 бита каждой дорожки, 16807ᴷ mod M) — один раз на процесс."""
    if not _LANE_TABLES:
        parts, value = [], 1
        for _ in range(_LANES):
            value = value * MULTIPLIER % MODULUS
            parts.append(value.to_bytes(8, "little"))
        powers = int.from_bytes(b"".join(parts), "little")
        mask = int.from_bytes(MODULUS.to_bytes(8, "little") * _LANES, "little")
        _LANE_TABLES.append((powers, mask, value))
    return _LANE_TABLES[0]


def _fold(value, mask):
    """Остаток от деления на M = 2³¹−1 в каждой дорожке сразу.

    В дорожке число меньше 2⁶² (произведение двух чисел меньше 2³¹), остаток
    по простому модулю от него не 0. y mod M = (y & M) + (y >> 31) по модулю M:
    после первого сложения меньше 2³², после второго — не больше M + 1, после
    третьего — ровно остаток из 1…M−1. Сдвиг целого на 31 переносит в дорожку
    биты соседней выше её 32-го бита — маска их отрезает.
    """
    value = (value & mask) + ((value >> 31) & mask)
    value = (value & mask) + ((value >> 31) & mask)
    return (value & mask) + ((value >> 31) & mask)


def _fill_lanes(out, index, state):
    """Гамма с места `index`, состояние перед ним — `state` (из 1…M−1).

    Кусок из K состояний — одно большое целое, по 64 бита на состояние:
    x·16807ⁱ (i = 1…K) — умножение целого на x, следующий кусок — на 16807ᴷ,
    остатки — `_fold`. Младший байт каждой дорожки — срез байтов целого."""
    powers, mask, jump = _lane_tables()
    states = _fold(powers * state, mask)
    total = len(out)
    width = _LANE_BITS // 8
    for start in range(index, total, _LANES):
        take = min(_LANES, total - start)
        low = states.to_bytes(width * _LANES, "little")[0:width * take:width]
        out[start:start + take] = low.translate(_MINUS_ONE)
        states = _fold(states * jump, mask)


def apply_cipher(data, seed):
    """Наложить гамму. Действие обратно самому себе."""
    if not data:
        return b""
    gamma = keystream(seed, len(data))
    mixed = int.from_bytes(data, "little") ^ int.from_bytes(gamma, "little")
    return mixed.to_bytes(len(data), "little")


def _signed(value):
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value >= 1 << 31 else value


def outer_checksum(body):
    """Внешняя сумма: считается по телу файла в два приёма, как в игре."""
    first = (zlib.crc32(body) ^ CRC_KEY1) & 0xFFFFFFFF
    return (zlib.crc32(struct.pack("<I", first) + body) ^ CRC_KEY2) & 0xFFFFFFFF


def has_outer_header(raw):
    if len(raw) < 16:
        return False
    declared = struct.unpack_from("<I", raw, 0)[0] ^ (CRC_KEY1 ^ CRC_KEY2)
    return declared == len(raw) - 8


class DatFile(object):
    """Разобранная оболочка: что внутри и как файл был сложен."""

    def __init__(self, payload, kind, seed, outer, checksum_ok, signature):
        self.payload = payload          # байты дерева, уже распакованные
        self.kind = kind                # block или data
        self.seed = seed                # начальное число потока
        self.outer = outer              # был ли внешний заголовок
        self.checksum_ok = checksum_ok  # сошлась ли внешняя сумма
        self.signature = signature      # подпись обёртки ZL

    def describe(self):
        return OrderedDict((
            ("kind", self.kind),
            ("outer_header", self.outer),
            ("outer_checksum_ok", self.checksum_ok),
            ("seed", self.seed),
            ("zl_signature", self.signature.decode("ascii")),
            ("payload_size", len(self.payload)),
        ))


def _try_frame(raw, base, seed_key):
    if len(raw) < base + 12:
        return None
    stored_crc, seed_field = struct.unpack_from("<II", raw, base)
    seed = _signed(seed_field ^ seed_key)
    # Сначала расшифровываем четыре байта и смотрим на подпись ZL: это
    # отсеивает чужие файлы с расширением .dat, не расшифровывая их целиком.
    if apply_cipher(raw[base + 8:base + 12], seed) not in zl.SIGNATURES:
        return None
    plain = apply_cipher(raw[base + 8:], seed)
    if zlib.crc32(plain) & 0xFFFFFFFF != stored_crc:
        return None
    return plain, seed


def unpack(raw):
    """Снять оболочку. Перебирает обе рамки и оба ключа, опора — сумма."""
    if len(raw) < 16:
        raise DatError("файл слишком мал для .dat")
    outer = has_outer_header(raw)
    bases = (8, 0) if outer else (0, 8)
    for base in bases:
        for kind, seed_key in SEED_KEYS.items():
            found = _try_frame(raw, base, seed_key)
            if found is None:
                continue
            plain, seed = found
            body, signature = zl.unwrap(plain)
            checksum_ok = False
            if base == 8:
                checksum_ok = outer_checksum(raw[8:]) == struct.unpack_from("<I", raw, 4)[0]
            return DatFile(body, kind, seed, base == 8, checksum_ok, signature)
    raise DatError("ни одна из рамок не сошлась по контрольной сумме — это не датник игры")


def pack(payload, kind=KIND_BLOCK, seed=DEFAULT_SEED, outer=True, signature=zl.DEFAULT_SIGNATURE):
    """Сложить файл обратно. По умолчанию с внешним заголовком — как у игры."""
    if kind not in SEED_KEYS:
        raise DatError("незнакомый вид датника: %s" % kind)
    blob = zl.wrap(payload, signature)
    crc = zlib.crc32(blob) & 0xFFFFFFFF
    body = struct.pack("<II", crc, (seed ^ SEED_KEYS[kind]) & 0xFFFFFFFF) + apply_cipher(blob, seed)
    if not outer:
        return body
    head = struct.pack("<II", (len(body) ^ (CRC_KEY1 ^ CRC_KEY2)) & 0xFFFFFFFF, outer_checksum(body))
    return head + body
