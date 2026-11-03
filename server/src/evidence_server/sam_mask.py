"""Decode a SAM 3.1 mask payload (`data=H,W,<payload>`) to a binary raster.

Port of `decode_mask_to_raster` from Meta's `meta-sam-parser` 0.1.0
(github.com/meta-models/meta-sam, `python/src/meta_sam_parser/_mask_codec.py`,
protocol in `protocol/sam3.md`). Copyright (c) Meta Platforms, Inc. and affiliates.
SAM Materials, used under the SAM License: see server/licenses/SAM-LICENSE.txt.
Only the decoder is ported (no encoder, no streaming parser); the arithmetic is
unchanged so the output is bit-identical to the reference implementation.

Format: the first character selects the encoding (`~` lossless, the API default;
`!` one-bit). The rest is base85 over the 85 printable characters `!`..`{` minus
`" \\ , ; < > |`: a 5-digit length prefix, then a range-coded body packed four
bytes to five digits. Lossless bodies decode to coverage bytes (spatial
predictor + order-0/1/2 context models), thresholded at 129; one-bit bodies
decode straight to 0/1 with a 12-pixel context. Pixels are row-major.

`decode` raises MaskError on anything malformed; callers treat that as
"mask not decoded" and fall back to the box, never to a guessed area.
"""

from array import array

_EXCLUDED = frozenset('"\\,;<>|')
_ALPHABET = "".join(c for c in map(chr, range(0x21, 0x7F)) if c not in _EXCLUDED)
_RADIX = 85
_PREFIX_LENGTH = 5
_VALUES = {c: i for i, c in enumerate(_ALPHABET)}

_TOP = 1 << 24
_BOTTOM = 1 << 16
_MASK_8 = 0xFF
_MASK_16 = 0xFFFF
_MASK_32 = 0xFFFFFFFF
_INCREMENT = 14
_COUNT_LIMIT = 4096
_CONTEXT_COUNT = 1 << 12
_PADDING = 2
_SYMBOLS = 256
_TOTAL_INDEX = _SYMBOLS
_ZERO_INCREMENT = 32
_ZERO_LIMIT = 16384
_ORDER_ONE_INCREMENT = 56
_ORDER_ONE_LIMIT = 8192
_ORDER_TWO_STEP = 14 * 44
_ORDER_TWO_LIMIT = 14 * 3584
_ORDER_ZERO_STEP = 2 * 16
_ORDER_ZERO_LIMIT = 2 * 2048
_ORDER_ZERO_INITIAL = 2
_ORDER_ONE_INITIAL = 1
MAX_PIXELS = 1 << 21  # SAM downscales masks to at most 1024 px a side; refuse anything far larger


class MaskError(ValueError):
    """The payload is not a valid SAM mask for the declared size."""


def decode(payload: str, width: int, height: int) -> bytes:
    """payload (with its marker), raster width and height -> width*height bytes of 0/1, row-major."""
    if not isinstance(payload, str) or not payload:
        raise MaskError("empty payload")
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise MaskError("bad dimensions")
    if width * height > MAX_PIXELS:
        raise MaskError("mask too large")
    try:
        if payload[0] == "~":
            return bytes(v >= 129 for v in _decode_lossless(payload, width, height))
        if payload[0] == "!":
            return _decode_one_bit(payload, width, height)
    except MaskError:
        raise
    except Exception as e:  # an index or arithmetic error means a malformed body
        raise MaskError(f"could not decode: {type(e).__name__}") from e
    raise MaskError("unknown encoding marker")


def _u32(value: int) -> int:
    return value & _MASK_32


def _digit(character: str) -> int:
    value = _VALUES.get(character)
    if value is None:
        raise MaskError("invalid character")
    return value


def _pack(data: bytes) -> str:
    """Bytes -> base85 text with the length prefix (used to check the canonical form)."""
    length = len(data)
    rest = length
    prefix = [""] * _PREFIX_LENGTH
    for i in range(_PREFIX_LENGTH - 1, -1, -1):
        prefix[i] = _ALPHABET[rest % _RADIX]
        rest //= _RADIX
    out = prefix
    offset = 0
    full = length - length % 4
    while offset < full:
        value = int.from_bytes(data[offset:offset + 4], "big")
        digits = [0] * 5
        for i in range(4, -1, -1):
            digits[i] = value % _RADIX
            value //= _RADIX
        out.extend(_ALPHABET[d] for d in digits)
        offset += 4
    remainder = length - offset
    if remainder:
        value = int.from_bytes(data[offset:] + b"\0" * (4 - remainder), "big")
        digits = [0] * 5
        for i in range(4, -1, -1):
            digits[i] = value % _RADIX
            value //= _RADIX
        out.extend(_ALPHABET[digits[i]] for i in range(remainder + 1))
    return "".join(out)


def _unpack(text: str) -> bytes:
    """Base85 text after the marker -> the packed bytes; strict about length and canonical form."""
    if len(text) < _PREFIX_LENGTH:
        raise MaskError("missing length prefix")
    length = 0
    for c in text[:_PREFIX_LENGTH]:
        length = length * _RADIX + _digit(c)
    remainder = length % 4
    expected = _PREFIX_LENGTH + (length // 4) * 5 + (remainder + 1 if remainder else 0)
    if len(text) != expected:
        raise MaskError("length does not match its prefix")
    out = bytearray(length)
    src, dst, full = _PREFIX_LENGTH, 0, length - remainder
    while dst < full:
        value = 0
        for i in range(5):
            value = value * _RADIX + _digit(text[src + i])
        if value > _MASK_32:
            raise MaskError("out-of-range group")
        out[dst:dst + 4] = value.to_bytes(4, "big")
        src += 5
        dst += 4
    if remainder:
        value = 0
        for i in range(5):
            value = value * _RADIX + (_digit(text[src + i]) if i < remainder + 1 else _RADIX - 1)
        if value > _MASK_32:
            raise MaskError("out-of-range tail")
        for i in range(remainder):
            out[dst + i] = (value >> (24 - i * 8)) & _MASK_8
    if _pack(bytes(out)) != text:
        raise MaskError("not in canonical form")
    return bytes(out)


class _RangeDecoder:
    __slots__ = ("_code", "_input", "_low", "_position", "_range", "_scaled")

    def __init__(self, data: bytes):
        if len(data) < 4:
            raise MaskError("missing finalization")
        self._input = data
        self._position = 0
        self._low = 0
        self._range = _MASK_32
        self._code = 0
        self._scaled = 0
        for _ in range(4):
            self._code = _u32((self._code << 8) | self._read())

    def frequency(self, total: int) -> int:
        self._scaled = self._range // total
        if self._scaled == 0:
            raise MaskError("invalid coding state")
        value = _u32(self._code - self._low) // self._scaled
        return total - 1 if value >= total else value

    def decode(self, cumulative: int, frequency: int) -> None:
        self._low = _u32(self._low + self._scaled * cumulative)
        self._range = self._scaled * frequency
        while True:
            if _u32(self._low ^ _u32(self._low + self._range)) < _TOP:
                pass
            elif self._range < _BOTTOM:
                self._range = _u32(-self._low) & (_BOTTOM - 1)
            else:
                break
            self._code = _u32((self._code << 8) | self._read())
            self._low = _u32(self._low << 8)
            self._range = _u32(self._range << 8)

    def _read(self) -> int:
        if self._position >= len(self._input):
            raise MaskError("payload ended before decoding completed")
        value = self._input[self._position]
        self._position += 1
        return value


def _paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    dl, du, dul = abs(estimate - left), abs(estimate - up), abs(estimate - upper_left)
    if dl <= du and dl <= dul:
        return left
    return up if du <= dul else upper_left


def _predict(out: bytearray, offset: int, x: int, y: int, mode: int, width: int) -> int:
    if mode < 0 or mode > 4:
        raise MaskError("invalid predictor")
    left = out[offset - 1] if x > 0 else 0
    up = out[offset - width] if y > 0 else 0
    if mode == 1:
        return up
    if mode == 2:
        return left
    if mode == 3:
        return (left + up) >> 1
    upper_left = out[offset - width - 1] if x > 0 and y > 0 else 0
    if mode == 0:
        return _paeth(left, up, upper_left)
    if upper_left >= max(left, up):
        return min(left, up)
    if upper_left <= min(left, up):
        return max(left, up)
    return left + up - upper_left


def _zero_context(residuals: bytearray, offset: int, x: int, y: int, width: int) -> int:
    left = int(residuals[offset - 1] == 0) if x > 0 else 1
    up = int(residuals[offset - width] == 0) if y > 0 else 1
    upper_left = int(residuals[offset - width - 1] == 0) if x > 0 and y > 0 else 1
    upper_right = int(residuals[offset - width + 1] == 0) if y > 0 and x < width - 1 else 1
    return left | (up << 1) | (upper_left << 2) | (upper_right << 3)


def _halve(counts, start: int, floor_one: bool) -> int:
    total = 0
    for i in range(start, start + _SYMBOLS):
        counts[i] = (counts[i] >> 1) or 1 if floor_one else counts[i] >> 1
        total += counts[i]
    return total


def _decode_lossless(payload: str, width: int, height: int) -> bytes:
    packed = _unpack(payload[1:])
    if len(packed) < 5:
        raise MaskError("lossless payload truncated")
    selector = packed[0]
    decoder = _RangeDecoder(packed[1:])
    length = width * height
    out = bytearray(length)
    residuals = bytearray(length)
    zero_counts = array("I", [1]) * 32
    order_one = array("H", [_ORDER_ONE_INITIAL]) * (_SYMBOLS * _SYMBOLS)
    order_one_totals = array("i", [_SYMBOLS * _ORDER_ONE_INITIAL]) * _SYMBOLS
    order_zero = array("H", [_ORDER_ZERO_INITIAL]) * _SYMBOLS
    order_zero_total = _SYMBOLS * _ORDER_ZERO_INITIAL
    order_two: dict[int, array] = {}
    previous = 0
    for offset in range(length):
        x = offset % width
        y = offset // width
        z = _zero_context(residuals, offset, x, y, width) << 1
        zero, nonzero = zero_counts[z], zero_counts[z + 1]
        bit = 0 if decoder.frequency(zero + nonzero) < zero else 1
        decoder.decode(0 if bit == 0 else zero, zero if bit == 0 else nonzero)
        zero_counts[z + bit] = _u32(zero_counts[z + bit] + _ZERO_INCREMENT)
        if zero_counts[z] + zero_counts[z + 1] >= _ZERO_LIMIT:
            zero_counts[z] = (zero_counts[z] >> 1) or 1
            zero_counts[z + 1] = (zero_counts[z + 1] >> 1) or 1
        symbol = 0
        if bit == 1:
            above = residuals[offset - width] if offset >= width else 0
            key = (previous << 8) | above
            second = order_two.get(key)
            if second is None:
                second = order_two[key] = array("H", [0]) * (_SYMBOLS + 1)
            first = previous << 8
            excluded = second[0] + order_one[first] + order_zero[0]
            total = second[_TOTAL_INDEX] + order_one_totals[previous] + order_zero_total - excluded
            target = decoder.frequency(total)
            cumulative = 0
            symbol = 1
            frequency = second[1] + order_one[first + 1] + order_zero[1]
            while cumulative + frequency <= target:
                cumulative += frequency
                symbol += 1
                if symbol >= _SYMBOLS:
                    raise MaskError("lossless payload malformed")
                frequency = second[symbol] + order_one[first + symbol] + order_zero[symbol]
            decoder.decode(cumulative, frequency)
            second[symbol] = (second[symbol] + _ORDER_TWO_STEP) & _MASK_16
            second[_TOTAL_INDEX] = (second[_TOTAL_INDEX] + _ORDER_TWO_STEP) & _MASK_16
            if second[_TOTAL_INDEX] >= _ORDER_TWO_LIMIT:
                second[_TOTAL_INDEX] = _halve(second, 0, False) & _MASK_16
            order_one[first + symbol] = (order_one[first + symbol] + _ORDER_ONE_INCREMENT) & _MASK_16
            order_one_totals[previous] += _ORDER_ONE_INCREMENT
            if order_one_totals[previous] >= _ORDER_ONE_LIMIT:
                order_one_totals[previous] = _halve(order_one, first, True)
            order_zero[symbol] = (order_zero[symbol] + _ORDER_ZERO_STEP) & _MASK_16
            order_zero_total += _ORDER_ZERO_STEP
            if order_zero_total >= _ORDER_ZERO_LIMIT:
                order_zero_total = _halve(order_zero, 0, False)
        residuals[offset] = symbol
        delta = -((symbol + 1) >> 1) if symbol & 1 else symbol >> 1
        out[offset] = (_predict(out, offset, x, y, selector, width) + delta) & _MASK_8
        previous = symbol
    return bytes(out)


def _decode_one_bit(payload: str, width: int, height: int) -> bytes:
    decoder = _RangeDecoder(_unpack(payload[1:]))
    counts = array("H", [1]) * (_CONTEXT_COUNT * 2)
    padded_width = width + 2 * _PADDING
    padded = bytearray(padded_width * (height + _PADDING))
    out = bytearray(width * height)
    for y in range(height):
        row = (y + _PADDING) * padded_width + _PADDING
        for x in range(width):
            p = row + x
            prior = p - padded_width
            earlier = p - 2 * padded_width
            context = (
                padded[p - 1] | (padded[p - 2] << 1)
                | (padded[prior - 2] << 2) | (padded[prior - 1] << 3) | (padded[prior] << 4)
                | (padded[prior + 1] << 5) | (padded[prior + 2] << 6)
                | (padded[earlier - 2] << 7) | (padded[earlier - 1] << 8) | (padded[earlier] << 9)
                | (padded[earlier + 1] << 10) | (padded[earlier + 2] << 11)
            )
            c = context << 1
            zero, one = counts[c], counts[c + 1]
            bit = 0 if decoder.frequency(zero + one) < zero else 1
            if bit == 0:
                decoder.decode(0, zero)
            else:
                decoder.decode(zero, one)
            padded[p] = bit
            out[y * width + x] = bit
            counts[c + bit] = (counts[c + bit] + _INCREMENT) & _MASK_16
            if counts[c] + counts[c + 1] >= _COUNT_LIMIT:
                counts[c] = (counts[c] >> 1) or 1
                counts[c + 1] = (counts[c + 1] >> 1) or 1
    return bytes(out)
