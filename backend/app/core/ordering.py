"""Lexicographic fractional indexing for card order.

A card's `rank` is a string read as a base-62 fraction: "U" means 0.U, which is
roughly 0.48. To place a card between two others you take the midpoint of their
ranks. When two neighbours are adjacent characters the midpoint simply grows one
character longer ("U" and "V" -> "UU"), so precision never runs out and no other
row is ever rewritten.

Why this instead of an integer `position`:
  - Inserting at the top of a 200-card column writes 1 row, not 200.
  - Two clients inserting at the same spot concurrently produce two different
    ranks. Neither corrupts the other; they just end up adjacent.

Two invariants the rest of the system depends on:

1. The alphabet is in ASCII order (digits < uppercase < lowercase), so plain
   byte comparison matches numeric order. This is only true in the database if
   the `rank` column is `COLLATE "C"` - see models/card.py.
2. A rank never ends in the minimum digit '0'. "U" and "U0" are the same number
   but different strings; permitting both would break the equivalence between
   lexicographic and numeric order, and then a "midpoint" could equal one of its
   own bounds. Trailing zeros are stripped and the result is asserted to lie
   strictly between its bounds.
"""

from __future__ import annotations

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE = len(ALPHABET)  # 62
_INDEX = {ch: i for i, ch in enumerate(ALPHABET)}

# Rank handed to the first card in an empty column: mid-alphabet, so there is
# room to insert on either side of it without immediately lengthening.
FIRST_RANK = ALPHABET[BASE // 2]  # "V"

MAX_RANK_LENGTH = 128


class RankError(ValueError):
    """A rank string was malformed, or the requested bounds were not ordered."""


class RankExhaustedError(RankError):
    """The gap between two neighbours needs a longer rank than we allow.

    Fractional indexing trades unbounded precision for growing strings. Normal
    use barely grows them (5000 random inserts peak around 6 characters, and
    5000 consecutive appends around 83), but inserting repeatedly into one
    fixed gap halves it every time and costs a character every ~6 inserts.

    This is not a corruption - order is still correct - so it is raised as a
    distinct error and the service layer answers it by rebalancing the column
    with `evenly_spaced_ranks` and retrying.
    """


def _digits(rank: str) -> list[int]:
    try:
        return [_INDEX[ch] for ch in rank]
    except KeyError as exc:  # pragma: no cover - defensive
        raise RankError(f"rank contains a character outside the alphabet: {rank!r}") from exc


def _to_string(digits: list[int]) -> str:
    return "".join(ALPHABET[d] for d in digits)


def _average(lower: list[int], upper: list[int]) -> list[int]:
    """Midpoint of two base-62 fractions, as a digit list.

    Both arguments are the digits after an implicit "0.". `lower` must be
    numerically smaller than `upper`.
    """
    width = max(len(lower), len(upper)) + 1
    a = lower + [0] * (width - len(lower))
    b = upper + [0] * (width - len(upper))

    # Add the two fractions, least significant digit first.
    summed = [0] * width
    carry = 0
    for i in range(width - 1, -1, -1):
        total = a[i] + b[i] + carry
        summed[i] = total % BASE
        carry = total // BASE

    # Halve the sum, most significant digit first. `carry` is the integer part
    # (0 or 1) that the addition produced.
    result: list[int] = []
    remainder = carry
    for digit in summed:
        current = remainder * BASE + digit
        result.append(current // 2)
        remainder = current % 2
    if remainder:
        # One bit of precision left over: it is worth exactly half a digit.
        result.append(BASE // 2)

    # Canonical form - trailing zeros do not change the value but do change the
    # string, and only one of those two may be true.
    while result and result[-1] == 0:
        result.pop()
    return result


def _strip_trailing_zeros(digits: list[int]) -> list[int]:
    out = list(digits)
    while out and out[-1] == 0:
        out.pop()
    return out


def _decrement(digits: list[int]) -> list[int]:
    """Shortest canonical digit list strictly less than `digits`.

    Used for the open lower end. Halving towards zero would also work, but it
    burns a character every ~6 inserts; stepping down one digit gives ~62.
    """
    for k in range(1, len(digits) + 1):
        if digits[k - 1] == 0:
            continue
        candidate = _strip_trailing_zeros([*digits[: k - 1], digits[k - 1] - 1])
        if candidate:
            return candidate
    # The decrement collapsed to zero, so `digits` is 0...01. Go one digit
    # deeper. The new digit is the *maximum*, not the midpoint: this is the
    # ceiling of a fresh character level, so starting at the top leaves all 61
    # steps of that level available before another character is needed.
    return [*digits[:-1], 0, BASE - 1]


def _increment(digits: list[int]) -> list[int]:
    """Shortest canonical digit list strictly greater than `digits`.

    Used for the open upper end, for the same reason as `_decrement`.
    """
    for k in range(1, len(digits) + 1):
        if digits[k - 1] == BASE - 1:
            continue
        # Incrementing at position k-1 raises the value there, so truncating
        # everything after it is still strictly greater.
        return [*digits[: k - 1], digits[k - 1] + 1]
    # All digits are the maximum: extend instead of carrying. The new digit is
    # the *minimum* non-zero one for the mirror-image reason - this is the floor
    # of a fresh character level, so all 61 steps up remain available.
    return [*digits, 1]


def rank_between(lower: str | None, upper: str | None) -> str:
    """Return a rank strictly between `lower` and `upper`.

    `None` means "open end": `rank_between(None, x)` goes before everything,
    `rank_between(x, None)` goes after everything, and `rank_between(None, None)`
    is the first card in an empty column.
    """
    if lower is None and upper is None:
        return FIRST_RANK

    if lower is not None and upper is not None and lower >= upper:
        raise RankError(f"bounds are not ordered: {lower!r} >= {upper!r}")

    # Open ends are a different problem from a closed gap. There is no second
    # bound to be fair to, so the goal is only "shortest string on that side",
    # which is a single-digit step, not a midpoint. Averaging against a 0.0 or
    # 0.zzz sentinel also works but consumes a character every ~6 inserts
    # instead of every ~62, so a column that is always appended to grows
    # unusable ranks surprisingly fast.
    if upper is None:
        result = _to_string(_increment(_digits(lower)))  # type: ignore[arg-type]
    elif lower is None:
        result = _to_string(_decrement(_digits(upper)))
    else:
        # A real gap: the midpoint is right, because it leaves the most room on
        # both sides for whoever inserts next.
        exact = _average(_digits(lower), _digits(upper))

        # The exact average is valid but not always the *shortest* valid rank.
        # Truncating a fraction only ever decreases it, so every prefix of the
        # average is still below `upper`; take the first one also above `lower`.
        for length in range(1, len(exact) + 1):
            candidate_digits = _strip_trailing_zeros(exact[:length])
            if not candidate_digits:
                continue  # an all-zero prefix is not a valid rank
            candidate = _to_string(candidate_digits)
            if candidate > lower:
                result = candidate
                break
        else:  # pragma: no cover - the full average always satisfies both bounds
            raise RankError(f"no rank fits between {lower!r} and {upper!r}")

    # The algorithm is total, but this is the invariant everything else trusts,
    # so it is checked rather than assumed.
    if (lower is not None and result <= lower) or (upper is not None and result >= upper):
        raise RankError(f"computed rank {result!r} is not between {lower!r} and {upper!r}")
    if len(result) > MAX_RANK_LENGTH:
        raise RankExhaustedError(
            f"rank between {lower!r} and {upper!r} would exceed {MAX_RANK_LENGTH} characters"
        )
    return result


def _int_to_digits(value: int, width: int) -> list[int]:
    digits = [0] * width
    for i in range(width - 1, -1, -1):
        digits[i] = value % BASE
        value //= BASE
    return digits


def evenly_spaced_ranks(count: int) -> list[str]:
    """`count` ascending ranks spread evenly across the whole rank space.

    Used to rebalance a column whose ranks have grown long. Rewriting N rows is
    exactly the O(N) write that fractional indexing exists to avoid, which is
    why it is a rare repair path and not the normal insert path.
    """
    if count <= 0:
        return []

    # Enough width that consecutive slots are at least one integer step apart.
    width = 1
    while BASE**width < count + 2:
        width += 1

    step = BASE**width // (count + 1)
    ranks: list[str] = []
    for i in range(1, count + 1):
        digits = _strip_trailing_zeros(_int_to_digits(i * step, width))
        ranks.append(_to_string(digits))

    if ranks != sorted(ranks) or len(set(ranks)) != len(ranks):  # pragma: no cover
        raise RankError("rebalance produced a non-monotonic sequence")
    return ranks
