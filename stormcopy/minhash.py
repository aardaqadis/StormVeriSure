"""Stable MinHash signatures and LSH bands for structural fingerprint sets.

The on-disk representation depends on the versioned hash domains below. If
they change, indexed signatures and bands must be rebuilt together.
"""

from hashlib import blake2b
from struct import pack
from typing import Iterable


NUM_HASHES = 64
NUM_BANDS = 16
ROWS_PER_BAND = NUM_HASHES // NUM_BANDS
_PRIME = (1 << 61) - 1
_TOKEN_DOMAIN = b"swcopy-token-v1"
_PERM_DOMAIN = b"swcopy-perm-v1"
_BAND_DOMAIN = b"swcopy-band-v1"
_EMPTY = (_PRIME,) * NUM_HASHES


def _permutations() -> tuple[tuple[int, int], ...]:
    coefficients = []
    for number in range(NUM_HASHES):
        digest = blake2b(pack(">I", number), digest_size=16,
                         person=_PERM_DOMAIN).digest()
        multiplier = int.from_bytes(digest[:8], "big") % (_PRIME - 1) + 1
        offset = int.from_bytes(digest[8:], "big") % _PRIME
        coefficients.append((multiplier, offset))
    return tuple(coefficients)


_PERMUTATIONS = _permutations()


def signature(hashes: Iterable[str]) -> tuple[int, ...]:
    """Return a 64-value MinHash sketch of unique structural hash strings.

    Input order and duplicate tokens do not affect the sketch. Empty input
    produces a sentinel signature, which is deliberately omitted from LSH.
    """
    minimums = list(_EMPTY)
    for token in set(hashes):
        if not isinstance(token, str):
            raise TypeError("MinHash tokens must be strings")
        value = int.from_bytes(blake2b(token.encode("utf-8"), digest_size=8,
                                       person=_TOKEN_DOMAIN).digest(), "big") % _PRIME
        for position, (multiplier, offset) in enumerate(_PERMUTATIONS):
            candidate = (multiplier * value + offset) % _PRIME
            if candidate < minimums[position]:
                minimums[position] = candidate
    return tuple(minimums)


def _check_signature(values: tuple[int, ...]) -> None:
    if len(values) != NUM_HASHES or any(
            not isinstance(value, int) or value < 0 or value > _PRIME
            for value in values):
        raise ValueError(f"Expected {NUM_HASHES} valid MinHash values")


def bands(sig: tuple[int, ...]) -> tuple[str, ...]:
    """Return 16 position-specific LSH bucket keys, four rows per band."""
    _check_signature(sig)
    if sig == _EMPTY:
        return ()
    output = []
    for number in range(NUM_BANDS):
        rows = sig[number * ROWS_PER_BAND:(number + 1) * ROWS_PER_BAND]
        digest = blake2b(pack(">B4Q", number, *rows), digest_size=16,
                         person=_BAND_DOMAIN).hexdigest()
        output.append(f"{number:02d}:{digest}")
    return tuple(output)


def estimate(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    """Estimate set Jaccard from the fraction of matching sketch rows."""
    _check_signature(a)
    _check_signature(b)
    if a == _EMPTY or b == _EMPTY:
        return 1.0 if a == b else 0.0
    return sum(left == right for left, right in zip(a, b)) / NUM_HASHES
