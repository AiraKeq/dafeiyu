"""Lossless, standard-library-only planning for short chat bubbles.

Public API
==========
``classify_scene(incoming, reply)`` returns one of ``casual``, ``playful``,
``comfort`` and ``explanation``.  It uses visible keyword/format rules, not an
emotion model.  An explicit ``scene`` in ``plan_segments`` overrides these rules.

``plan_segments(text, *, incoming='', scene=None, max_chars=20,
                weights=(0.6, 0.3, 0.1), draw=None)`` counts Python ``str`` code
points, including spaces, punctuation and newlines.  ``max_chars`` is an integer
in 1..20, not a bool.  Segments are slices of the original string, never a
rewrite.  Only an empty or whitespace-only input may return ``segments=[]``.
``boundaries`` contains strictly increasing INTERNAL offsets, excluding 0 and
``len(text)``.  Nonempty inputs expose all three ordinary candidate counts;
``feasible=False`` candidates have empty boundaries, completeness=0 and score=0.

``plan_segments_extended`` shares the same scoring and lossless core, but accepts
``max_chars`` in 1..60 and an explicit ``max_messages`` budget in 1..3.  It never
changes the legacy API's 20-character ceiling.  Its default ``allow_long=True``
prefers intact natural units even when more bubbles are necessary; delivery can
try wider limits before paging.  ``allow_long=False`` searches only budgeted
candidates, relaxing token protection when necessary.  Impossible bounded plans
raise ValueError.  Whitespace fallback metadata remains explicit; callers that
cannot send blank bubbles must reject ``whitespace_segments``.

Scoring choices (heuristics, not measured probabilities)
======================================================
The normalized 60/30/10 defaults are a SOFT prior for feasible counts 1/2/3.
The scene-fit table, in count order, is:

    casual       1.00 / 0.92 / 0.72
    playful      0.82 / 1.00 / 0.78
    comfort      1.00 / 0.96 / 0.76
    explanation  0.86 / 1.00 / 0.98

A playful reply with both a natural pause and an explicit reversal/setup cue
uses 0.44 / 1.00 / 0.70 instead, so a real setup/punchline CAN beat the one-bubble
prior.  Consolation does not automatically become three bubbles.

Completeness is a HARD gate: only feasible, exact character coverage receives
1.0; rejected proposals receive 0.0, never a fractional probability of keeping
content.  Naturalness combines the geometric mean of boundary quality, a
fragment penalty, and a small penalty for merging natural pauses.  Qualities
are 1.00 for strong stops/newlines, 0.84 for commas/colons, 0.68 for whitespace,
and 0.42 for an unmarked cut; protected-token cuts are at most 0.26.  Adjacent
1-2-character fragments and EXTRA forced cuts are rejected when a lower count
can avoid them.  Short clauses, ASCII words, numbers, balanced brackets/quotes,
CRLF and punctuation clusters are protected whenever they fit the character
limit.  Protection is relaxed only if no complete, nonblank plan exists.

    score = prior * scene_fit * completeness * naturalness

The default chooses the highest score deterministically.  No RNG is used.
An explicit ``draw`` (finite real scalar in [0, 1), not bool) may select by score
ONLY among positive scores within BOTH 5% and 0.015 of the best score.  It has
no effect on a clear winner or on long/empty plans.

Performance and exceptions
==========================
For inputs up to 60 code points an exact-k bounded dynamic program searches
all permitted boundaries for each ordinary candidate.  Longer inputs cannot
fit three bubbles under the 20-character ceiling.  A weighted DAG dynamic
program retains the ENTIRE reply in O(n * max_chars) time and O(n) space.
Its bubble overhead (0.90), squared boundary cost (3 * (1-quality)**2), and
short-fragment costs discourage gratuitous bubbles while allowing necessary
counts above three.  ``mode='long'`` means the chosen count exceeds three.

``exception`` marks long plans or a necessary protection/whitespace fallback.
``forced_breaks``/``forced_reasons`` mark cuts through a long URL/word/quote or
without a natural pause; the text still remains intact.  A long URL cannot be
kept indivisible while also enforcing a hard 20-character limit.

Pure whitespace bubbles are prohibited whenever any complete nonblank plan
exists, even if that requires more than three bubbles.  Extreme mixed inputs
(e.g. 'x' + 100 spaces + 'y') make this mathematically impossible.  Only then
are unavoidable whitespace slices retained and their indices exposed in
``whitespace_segments`` with ``exception=True``.  No whitespace is dropped,
no empty slices are emitted, and every traversal advances strictly.
"""

from __future__ import annotations

import math
import re
import unicodedata
from fractions import Fraction
from itertools import islice
from numbers import Real
from collections.abc import Mapping

__all__ = ["classify_scene", "plan_segments", "plan_segments_extended"]

_SCENE_FITS = {
    "casual": (1.00, 0.92, 0.72),
    "playful": (0.82, 1.00, 0.78),
    "comfort": (1.00, 0.96, 0.76),
    "explanation": (0.86, 1.00, 0.98),
}
_NEAR_TIE_RELATIVE = 0.05
_NEAR_TIE_ABSOLUTE = 0.015
_BUBBLE_COST = 0.90

_COMFORT_IN = (
    "难过", "难受", "伤心", "委屈", "焦虑", "孤独", "心累", "失眠",
    "想哭", "崩溃", "分手", "撑不住", "压力很大", "抑郁", "失落",
    "不开心", "害怕", "好累", "很累", "sad", "lonely", "anxious",
)
_COMFORT_OUT = (
    "抱抱", "别怕", "别难过", "辛苦了", "不要自责", "别自责",
    "陪着你", "陪你", "我在呢", "我一直在", "有我在", "慢慢来",
)
_PLAYFUL = (
    "笑话", "段子", "开玩笑", "逗我", "逗你", "猜猜", "反转", "皮一下",
    "哈哈", "嘿嘿", "嘻嘻", "233", "lol", "joke", "punchline",
)
_EXPLANATION = (
    "解释", "说明", "步骤", "详细", "原理", "教程", "代码", "算法",
    "报错", "配置", "计算", "如何", "为什么", "怎么做", "怎么用",
    "怎么设置", "怎么解决", "怎么实现", "explain", "tutorial", "error",
)
_STRUCTURED_RE = re.compile(
    r"(?m)^[ \t]*(?:\d{1,3}[.)、]|[-*][ \t]|步骤[一二三四五六七八九十\d])"
)
_REVERSAL_RE = re.compile(
    r"但是|不过|然而|结果|其实|只是|才怪|骗你的|没想到|反转|当然是|答案是|可惜|却"
)
_URL_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:https?://|ftp://|www\.)[^\s<>\"'，。！？；、（）【】《》]+"
)
_WORD_RE = re.compile(r"[A-Za-z0-9_]+(?:[.'’\-][A-Za-z0-9_]+)*")
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_])[+\-￥¥$]?(?:\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:[.:/]\d+)*(?:[eE][+\-]?\d+)?[%％]?"
)
_PUNCT_RUN_RE = re.compile(r"[。！？!?；;…]{2,}|\.{2,}|—{2,}|-{2,}|\r\n")

# Separate masks preserve transparent forced-break diagnostics.  Interval
# difference events keep nested brackets/quotes linear, not quadratic.
_WORD, _NUMBER, _URL, _BRACKET, _QUOTE, _CLAUSE, _PAIR = (1 << i for i in range(7))
_LEXICAL = _WORD | _NUMBER | _URL
_BRACKETS = {"(": ")", "[": "]", "{": "}", "（": "）", "【": "】", "《": "》", "〈": "〉"}
_QUOTES = {"“": "”", "‘": "’", "「": "」", "『": "』"}
_OPENERS = frozenset(_BRACKETS) | frozenset(_QUOTES)
_CLOSERS = frozenset(_BRACKETS.values()) | frozenset(_QUOTES.values())
_STRONG = frozenset("。.!?！？;；")
_MEDIUM = frozenset(",，、:：")
_LINES = frozenset("\r\n\v\f\x85\u2028\u2029")
_STOP_CONTINUATIONS = _STRONG | _MEDIUM | frozenset("…—-")


def _require_string(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")


def _normalize_weights(weights: object) -> list[float]:
    if isinstance(weights, (str, bytes, bytearray, Mapping)):
        raise TypeError("weights must contain exactly three real numbers")
    try:
        values = list(islice(iter(weights), 4))
    except TypeError as exc:
        raise TypeError("weights must contain exactly three real numbers") from exc
    if len(values) != 3:
        raise ValueError("weights must contain exactly three values")
    for value in values:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise TypeError("each weight must be a real number, not bool")
        if value < 0:
            raise ValueError("weights must be nonnegative")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            # Arbitrarily large integers/Fractions are still finite.  Ratios
            # are converted below, rather than overflowing the original value.
            finite = isinstance(value, (int, Fraction))
        if not finite:
            raise ValueError("weights must be finite")
    if not any(value > 0 for value in values):
        raise ValueError("weights must not all be zero")
    try:
        floats = [float(value) for value in values]
        total = math.fsum(floats)
        if total > 0 and math.isfinite(total):
            return [value / total for value in floats]
        peak = max(floats)
        if peak > 0:
            scaled = [value / peak for value in floats]
        else:
            # Positive exact weights can all underflow when converted to float.
            exact = [Fraction(value) for value in values]
            peak = max(exact)
            scaled = [float(value / peak) for value in exact]
    except OverflowError:
        exact = [Fraction(value) for value in values]
        peak = max(exact)
        scaled = [float(value / peak) for value in exact]
    total = math.fsum(scaled)
    return [value / total for value in scaled]


def _validate_draw(draw: object) -> float | None:
    if draw is None:
        return None
    if isinstance(draw, bool) or not isinstance(draw, Real):
        raise TypeError("draw must be a real scalar, not bool")
    if not 0 <= draw < 1:
        raise ValueError("draw must be finite and in [0, 1)")
    return float(draw)


def classify_scene(incoming: str, reply: str) -> str:
    """Classify by ordered, inspectable cues; this is not emotion inference.

    Incoming distress or direct consolation wins, then explicit explanation
    requests/structured replies, then playful cues.  Ordinary greetings and
    ambiguous messages remain casual.  The caller may override this result.
    """
    _require_string(incoming, "incoming")
    _require_string(reply, "reply")
    prompt, answer = incoming.casefold(), reply.casefold()
    if any(cue in prompt for cue in _COMFORT_IN) or any(cue in answer for cue in _COMFORT_OUT):
        return "comfort"
    if any(cue in prompt for cue in _EXPLANATION):
        return "explanation"
    if "```" in answer or _STRUCTURED_RE.search(answer):
        return "explanation"
    if any(cue in prompt or cue in answer for cue in _PLAYFUL):
        return "playful"
    if any(cue in answer for cue in _EXPLANATION) or (
        len(reply) >= 32 and "因为" in answer and "所以" in answer
    ):
        return "explanation"
    return "casual"


class _BoundaryData:
    __slots__ = ("visible", "payload", "quality", "hard", "soft", "pauses", "cut_cost")

    def __init__(self, visible, payload, quality, hard, soft, pauses):
        self.visible = visible
        self.payload = payload
        self.quality = quality
        self.hard = hard
        self.soft = soft
        self.pauses = pauses
        self.cut_cost = [3.0 * (1.0 - quality_i) ** 2 for quality_i in quality]
        self.cut_cost[0] = 0.0


def _analyse(text: str, limit: int) -> _BoundaryData:
    n = len(text)
    visible, payload = [0] * (n + 1), [0] * (n + 1)
    for i, char in enumerate(text, 1):
        visible[i] = visible[i - 1] + (not char.isspace())
        payload[i] = payload[i - 1] + (
            not char.isspace() and not unicodedata.category(char).startswith("P")
        )

    events: dict[int, list[tuple[int, int, int]]] = {}

    def protect(start: int, end: int, kind: int) -> None:
        if end - start < 2:
            return
        tier = 0 if end - start <= limit else 1
        bit_index = kind.bit_length() - 1
        events.setdefault(start + 1, []).append((tier, bit_index, 1))
        events.setdefault(end, []).append((tier, bit_index, -1))

    for match in _URL_RE.finditer(text):
        end = match.end()
        while end > match.start() and text[end - 1] in ".,;!?":
            end -= 1
        protect(match.start(), end, _URL)
    for regex, kind in ((_WORD_RE, _WORD), (_NUMBER_RE, _NUMBER), (_PUNCT_RUN_RE, _PAIR)):
        for match in regex.finditer(text):
            protect(match.start(), match.end(), kind)

    opening, closing = [False] * n, [False] * n
    stack: list[tuple[int, str, int]] = []
    i, escaped = 0, False
    while i < n:
        char = text[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if char == "\\":
            escaped = True
            i += 1
            continue
        apostrophe = char in "'’" and 0 < i < n - 1 and text[i - 1].isalnum() and text[i + 1].isalnum()
        if stack and stack[-1][2] == _QUOTE:
            start, expected, kind = stack[-1]
            if expected.startswith("`") and char == "`":
                end = i + 1
                while end < n and text[end] == "`":
                    end += 1
                if end - i == len(expected):
                    stack.pop()
                    closing[end - 1] = True
                    protect(start, end, kind)
                i = end
                continue
            if char == expected and not apostrophe:
                stack.pop()
                closing[i] = True
                protect(start, i + 1, kind)
            i += 1
            continue
        if char == "`":
            end = i + 1
            while end < n and text[end] == "`":
                end += 1
            opening[i] = True
            stack.append((i, text[i:end], _QUOTE))
            i = end
            continue
        if char in ('"', "'") and not apostrophe:
            opening[i] = True
            stack.append((i, char, _QUOTE))
        elif char in _QUOTES:
            opening[i] = True
            stack.append((i, _QUOTES[char], _QUOTE))
        elif char in _BRACKETS:
            opening[i] = True
            stack.append((i, _BRACKETS[char], _BRACKET))
        elif stack and char == stack[-1][1]:
            start, _, kind = stack.pop()
            closing[i] = True
            protect(start, i + 1, kind)
        i += 1

    for offset in range(1, n):
        # Preserve an opener with its contents, a closer with its preceding
        # contents, and common combining/ZWJ pairs whenever physically possible.
        if (
            opening[offset - 1] or text[offset] in _CLOSERS or closing[offset]
            or unicodedata.category(text[offset]).startswith("M")
            or text[offset] == "\u200d" or text[offset - 1] == "\u200d"
        ):
            protect(offset - 1, offset + 1, _PAIR)

    hard, soft = [0] * (n + 1), [0] * (n + 1)
    active = [[0] * 7, [0] * 7]
    masks = [0, 0]
    for offset in range(n + 1):
        for tier, bit_index, delta in events.get(offset, ()):
            before = active[tier][bit_index]
            after = before + delta
            active[tier][bit_index] = after
            if before == 0 and after > 0:
                masks[tier] |= 1 << bit_index
            elif before > 0 and after == 0:
                masks[tier] &= ~(1 << bit_index)
        hard[offset], soft[offset] = masks

    quality = [0.42] * (n + 1)
    quality[0] = quality[n] = 1.0
    pauses: list[tuple[int, int, float]] = []
    carry, pause_start = 0.0, 0
    for offset in range(1, n + 1):
        char = text[offset - 1]
        if char in _STRONG or char in _LINES:
            q, carry, pause_start = 1.0, 1.0, offset
        elif char in _MEDIUM:
            q, carry, pause_start = 0.84, 0.84, offset
        elif char in "…—" or (char == "-" and (
            (offset > 1 and text[offset - 2] == "-") or (offset < n and text[offset] == "-")
        )):
            q, carry, pause_start = 0.90, 0.90, offset
        elif char in _CLOSERS or closing[offset - 1]:
            q = carry or 0.58
            if carry:
                pause_start = offset
        elif char.isspace():
            q = carry or 0.68
        else:
            q, carry = 0.42, 0.0
        if offset < n:
            next_char = text[offset]
            if next_char in _LINES:
                q = max(q, 0.94)
            elif next_char.isspace():
                q = max(q, 0.68)
            if next_char in _CLOSERS or closing[offset]:
                q = min(q, 0.12)
            if (
                carry >= 0.83 and not next_char.isspace()
                and next_char not in _STOP_CONTINUATIONS and next_char not in _CLOSERS
                and not closing[offset]
                and not ((hard[offset] | soft[offset]) & _LEXICAL)
                and not (hard[offset] & (_BRACKET | _QUOTE | _PAIR))
            ):
                pauses.append((pause_start, offset, carry))
        if opening[offset - 1]:
            q = min(q, 0.12)
        quality[offset] = q

    # A short clause without an actual pause is not cut merely to satisfy a
    # count prior.  More than three intact clauses may therefore require long.
    clause_diff = [0] * (n + 1)
    start = 0
    for end in [pause[1] for pause in pauses] + [n]:
        if 1 < end - start <= limit:
            clause_diff[start + 1] += 1
            clause_diff[end] -= 1
        start = end
    depth = 0
    for offset in range(n + 1):
        depth += clause_diff[offset]
        if depth:
            hard[offset] |= _CLAUSE
        protected = hard[offset] | soft[offset]
        if protected & _URL:
            quality[offset] = min(quality[offset], 0.06)
        elif protected & _NUMBER:
            quality[offset] = min(quality[offset], 0.14)
        elif protected & _WORD:
            quality[offset] = min(quality[offset], 0.10)
        elif protected & (_QUOTE | _BRACKET):
            quality[offset] = min(quality[offset], 0.26)
        elif protected & _PAIR:
            quality[offset] = min(quality[offset], 0.08)
        elif protected & _CLAUSE:
            quality[offset] = min(quality[offset], 0.20)
    return _BoundaryData(visible, payload, quality, hard, soft, pauses)


def _fragment_cost(visible_chars: int) -> float:
    return (3.0, 1.50, 0.65, 0.20)[visible_chars] if visible_chars < 4 else 0.0


def _exact_path(n: int, data: _BoundaryData, count: int, limit: int,
                allow_protected: bool, allow_whitespace: bool) -> list[int] | None:
    if n > count * limit or n < count or (not allow_whitespace and data.visible[n] < count):
        return None
    previous = [math.inf] * (n + 1)
    previous[0] = 0.0
    backs = []
    visible, hard, cut_cost = data.visible, data.hard, data.cut_cost
    for layer in range(1, count + 1):
        current, parents = [math.inf] * (n + 1), [-1] * (n + 1)
        for end in range(layer, min(n, layer * limit) + 1):
            best, best_start = math.inf, -1
            for start in range(max(layer - 1, end - limit), min(end - 1, (layer - 1) * limit) + 1):
                if math.isinf(previous[start]) or (hard[start] and not allow_protected):
                    continue
                size = visible[end] - visible[start]
                if size == 0 and not allow_whitespace:
                    continue
                cost = previous[start] + cut_cost[start] + _fragment_cost(size)
                if cost < best or (cost == best and start > best_start):
                    best, best_start = cost, start
            current[end], parents[end] = best, best_start
        backs.append(parents)
        previous = current
    if math.isinf(previous[n]):
        return None
    result, end = [], n
    for parents in reversed(backs):
        start = parents[end]
        if start > 0:
            result.append(start)
        end = start
    result.reverse()
    return result


def _long_path(n: int, data: _BoundaryData, limit: int,
               allow_protected: bool, allow_whitespace: bool) -> list[int] | None:
    if not allow_whitespace and n > data.visible[n] * limit:
        return None
    costs, counts, previous = [math.inf] * (n + 1), [n + 1] * (n + 1), [-1] * (n + 1)
    costs[0], counts[0] = 0.0, 0
    visible, hard, cut_cost = data.visible, data.hard, data.cut_cost
    for end in range(1, n + 1):
        best, best_count, best_start = math.inf, n + 1, -1
        for start in range(max(0, end - limit), end):
            if math.isinf(costs[start]) or (hard[start] and not allow_protected):
                continue
            size = visible[end] - visible[start]
            if size == 0 and not allow_whitespace:
                continue
            cost = costs[start] + _BUBBLE_COST + cut_cost[start] + _fragment_cost(size)
            count = counts[start] + 1
            if cost < best or (cost == best and (count < best_count or (
                count == best_count and start > best_start
            ))):
                best, best_count, best_start = cost, count, start
        costs[end], counts[end], previous[end] = best, best_count, best_start
    if math.isinf(costs[n]):
        return None
    result, end = [], n
    while end:
        start = previous[end]
        if not 0 <= start < end:
            raise RuntimeError("internal segmentation path did not advance")
        if start:
            result.append(start)
        end = start
    result.reverse()
    return result


def _slices(text: str, boundaries: list[int]) -> list[str]:
    offsets = [0, *boundaries, len(text)]
    return [text[start:end] for start, end in zip(offsets, offsets[1:])]


def _forced_reasons(boundaries: list[int], data: _BoundaryData) -> list[dict]:
    result = []
    tags = ((_URL, "url"), (_NUMBER, "number"), (_WORD, "ascii_word"),
            (_QUOTE, "quote"), (_BRACKET, "bracket"), (_PAIR, "paired_characters"),
            (_CLAUSE, "short_clause"))
    for offset in boundaries:
        mask = data.hard[offset] | data.soft[offset]
        reason = next((label for bit, label in tags if mask & bit), None)
        if reason is None and data.quality[offset] < 0.55:
            reason = "no_natural_pause"
        if reason is not None:
            result.append({"offset": offset, "reason": reason})
    return result


def _naturalness(boundaries: list[int], data: _BoundaryData, n: int) -> float:
    offsets = [0, *boundaries, n]
    sizes = [data.visible[end] - data.visible[start] for start, end in zip(offsets, offsets[1:])]
    if boundaries:
        quality = math.prod(data.quality[offset] for offset in boundaries) ** (1.0 / len(boundaries))
        fragments = sum(_fragment_cost(size) for size in sizes) / len(sizes)
        fragments += 0.60 * sum(a <= 2 and b <= 2 for a, b in zip(sizes, sizes[1:]))
        if data.payload[n] and any(data.payload[end] == data.payload[start]
                                   for start, end in zip(offsets, offsets[1:])):
            fragments += 1.0
        quality /= 1.0 + fragments
    else:
        quality = 1.0
    missed_strong, missed_medium = 0, 0
    for left, right, strength in data.pauses:
        if not any(left <= offset <= right for offset in boundaries):
            if strength >= 0.95:
                missed_strong += 1
            else:
                missed_medium += 1
    quality /= 1.0 + 0.06 * missed_strong + 0.025 * missed_medium
    return min(1.0, max(0.0, quality))


def _infeasible(count: int, prior: float, scene_fit: float, reason: str) -> dict:
    return {"count": count, "prior": prior, "scene_fit": scene_fit,
            "completeness": 0.0, "naturalness": 0.0, "score": 0.0,
            "boundaries": [], "feasible": False, "reason": reason,
            "forced_breaks": []}


def _candidates(text: str, data: _BoundaryData, limit: int, weights: list[float],
                scene: str, allow_protected: bool, allow_whitespace: bool,
                max_messages: int = 3) -> list[dict]:
    fits = _SCENE_FITS[scene]
    if scene == "playful" and data.pauses and _REVERSAL_RE.search(text):
        fits = (0.44, 1.00, 0.70)
    n, result = len(text), []
    for count in (1, 2, 3):
        if count > max_messages:
            result.append(_infeasible(count, weights[count - 1], fits[count - 1], "message_budget"))
            continue
        boundaries = _exact_path(n, data, count, limit, allow_protected, allow_whitespace)
        if boundaries is None:
            reason = "char_limit" if n > count * limit else "protected_units_or_whitespace"
            result.append(_infeasible(count, weights[count - 1], fits[count - 1], reason))
            continue
        segments = _slices(text, boundaries)
        # Completeness is verified BEFORE any score can give a proposal weight.
        if (len(segments) != count or "".join(segments) != text
                or any(not segment or len(segment) > limit for segment in segments)
                or (not allow_whitespace and any(segment.isspace() for segment in segments))):
            result.append(_infeasible(count, weights[count - 1], fits[count - 1], "integrity_gate"))
            continue
        naturalness = _naturalness(boundaries, data, n)
        prior, fit = weights[count - 1], fits[count - 1]
        result.append({"count": count, "prior": prior, "scene_fit": fit,
                       "completeness": 1.0, "naturalness": naturalness,
                       "score": prior * fit * 1.0 * naturalness, "boundaries": boundaries,
                       "feasible": True, "forced_breaks": [item["offset"] for item in _forced_reasons(boundaries, data)]})
    # A soft count prior cannot buy extra forced token cuts or a row of tiny
    # fragments.  The lowest feasible count itself is never rejected here.
    for index, candidate in enumerate(result):
        lower = [item for item in result[:index] if item["feasible"]]
        if not candidate["feasible"] or not lower:
            continue
        offsets = [0, *candidate["boundaries"], n]
        sizes = [data.visible[end] - data.visible[start] for start, end in zip(offsets, offsets[1:])]
        reason = None
        if len(candidate["forced_breaks"]) > min(len(item["forced_breaks"]) for item in lower):
            reason = "gratuitous_forced_breaks"
        elif not allow_whitespace and any(a <= 2 and b <= 2 for a, b in zip(sizes, sizes[1:])):
            reason = "adjacent_tiny_fragments"
        elif allow_whitespace:
            blank_count = sum(size == 0 for size in sizes)
            lower_blanks = []
            for item in lower:
                bounds = [0, *item["boundaries"], n]
                lower_blanks.append(sum(data.visible[a] == data.visible[b] for a, b in zip(bounds, bounds[1:])))
            if blank_count > min(lower_blanks):
                reason = "gratuitous_whitespace_segments"
        if reason is not None:
            result[index] = _infeasible(candidate["count"], candidate["prior"], candidate["scene_fit"], reason)
    return result


def _choose(candidates: list[dict], draw: float | None) -> tuple[dict, list[int]]:
    ranked = sorted((candidate for candidate in candidates if candidate["feasible"]),
                    key=lambda item: (-item["score"], -item["naturalness"], item["count"]))
    best = ranked[0]
    if draw is None or best["score"] <= 0:
        return best, []
    close = [item for item in ranked if item["score"] > 0
             and best["score"] - item["score"] <= _NEAR_TIE_ABSOLUTE
             and best["score"] - item["score"] <= _NEAR_TIE_RELATIVE * best["score"]]
    if len(close) < 2:
        return best, []
    ticket, cumulative = draw * math.fsum(item["score"] for item in close), 0.0
    for item in close:
        cumulative += item["score"]
        if ticket < cumulative:
            return item, [candidate["count"] for candidate in close]
    return close[-1], [candidate["count"] for candidate in close]


def _plan_segments_core(text: str, *, incoming: str, scene: str | None,
                        max_chars: int, weights, draw: float | None,
                        char_ceiling: int, max_messages: int,
                        allow_long: bool) -> dict:
    """Return lossless segments plus auditable candidate/exception metadata.

    Wrong types raise TypeError; out-of-range values raise ValueError.  No
    arguments are silently coerced from strings, booleans or float lengths.
    ``weights`` accepts exactly three nonnegative finite real numbers, including
    zero-valued individual priors, but excludes all-zero weights and booleans.
    ``feasible`` is authoritative even when a valid candidate has prior/score 0.
    A necessary whitespace fallback may have mode='scored' with exception=True;
    mode still honestly reflects its actual count rather than inventing bubbles.
    """
    _require_string(text, "text")
    _require_string(incoming, "incoming")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        raise TypeError("max_chars must be an integer, not bool or float")
    if not 1 <= max_chars <= char_ceiling:
        raise ValueError(f"max_chars must be in 1..{char_ceiling}")
    if isinstance(max_messages, bool) or not isinstance(max_messages, int):
        raise TypeError("max_messages must be an integer, not bool or float")
    if not 1 <= max_messages <= 3:
        raise ValueError("max_messages must be in 1..3")
    if not isinstance(allow_long, bool):
        raise TypeError("allow_long must be bool")
    if scene is not None:
        if not isinstance(scene, str):
            raise TypeError("scene must be str or None")
        if scene not in _SCENE_FITS:
            raise ValueError("scene must be casual, playful, comfort or explanation")
    normalized, validated_draw = _normalize_weights(weights), _validate_draw(draw)
    chosen_scene = classify_scene(incoming, text) if scene is None else scene
    n = len(text)
    if not text or text.isspace():
        return {"segments": [], "scene": chosen_scene, "chosen_count": 0,
                "source_chars": n, "mode": "empty", "weights": normalized,
                "candidates": [], "exception": False, "forced": False,
                "forced_breaks": [], "forced_reasons": [], "whitespace_segments": [],
                "reason": "whitespace_only" if text else "empty", "near_tie_counts": []}

    data = _analyse(text, max_chars)
    fallback, near_tie_counts, candidates = "none", [], []
    boundaries = None
    # Prefer ANY nonblank, protected plan over a whitespace bubble.  The final
    # tier always has a path because every edge advances by 1..max_chars.
    tiers = ((False, False, "none"), (True, False, "unavoidable_protected_break"),
             (False, True, "unavoidable_whitespace_segments"),
             (True, True, "unavoidable_protected_and_whitespace_breaks"))
    for allow_protected, allow_whitespace, fallback in tiers:
        candidates = _candidates(text, data, max_chars, normalized, chosen_scene,
                                 allow_protected, allow_whitespace, max_messages)
        if any(candidate["feasible"] for candidate in candidates):
            chosen, near_tie_counts = _choose(candidates, validated_draw)
            boundaries = chosen["boundaries"]
            break
        if allow_long:
            boundaries = _long_path(n, data, max_chars, allow_protected, allow_whitespace)
            if boundaries is not None:
                break
    if boundaries is None:
        if not allow_long:
            raise ValueError("no complete segmentation fits max_messages and max_chars")
        raise RuntimeError("internal segmentation planner failed to preserve text")
    segments = _slices(text, boundaries)
    if "".join(segments) != text or any(not segment or len(segment) > max_chars for segment in segments):
        raise RuntimeError("internal segmentation integrity gate failed")
    count = len(segments)
    mode = "long" if count > max_messages else "scored"
    forced_reasons = _forced_reasons(boundaries, data)
    whitespace_segments = [index for index, segment in enumerate(segments) if segment.isspace()]
    if fallback != "none":
        reason = fallback
    elif mode == "long":
        budget_name = "three" if max_messages == 3 else "budget"
        reason = (f"length_requires_more_than_{budget_name}" if n > max_messages * max_chars
                  else f"natural_units_require_more_than_{budget_name}")
    elif near_tie_counts:
        reason = "near_tie_draw"
    elif not any(candidate["feasible"] and candidate["score"] > 0 for candidate in candidates):
        reason = "feasibility_over_prior"
    else:
        reason = "highest_score"
    return {"segments": segments, "scene": chosen_scene, "chosen_count": count,
            "source_chars": n, "mode": mode, "weights": normalized,
            "candidates": candidates, "exception": mode == "long" or fallback != "none",
            "forced": bool(forced_reasons or whitespace_segments),
            "forced_breaks": [item["offset"] for item in forced_reasons],
            "forced_reasons": forced_reasons, "whitespace_segments": whitespace_segments,
            "boundaries": boundaries, "reason": reason, "constraint_fallback": fallback,
            "near_tie_counts": near_tie_counts}


def plan_segments(text: str, *, incoming: str = '', scene: str | None = None,
                  max_chars: int = 20, weights=(0.6, 0.3, 0.1),
                  draw: float | None = None) -> dict:
    """Legacy lossless API: max_chars remains an integer strictly in 1..20.

    The signature, scoring, long-content fallback and result shape are unchanged.
    Wider chat budgets belong to plan_segments_extended, never to this API.
    """
    return _plan_segments_core(
        text, incoming=incoming, scene=scene, max_chars=max_chars, weights=weights,
        draw=draw, char_ceiling=20, max_messages=3, allow_long=True,
    )


def plan_segments_extended(text: str, *, incoming: str = '',
                           scene: str | None = None, max_chars: int = 20,
                           weights=(0.6, 0.3, 0.1), draw: float | None = None,
                           max_messages: int = 3, allow_long: bool = True) -> dict:
    """Score 1/2/3 bubbles with a 1..60 character ceiling and 1..3 budget.

    allow_long=True preserves the legacy naturalness-first policy, allowing a
    larger count when no intact, nonblank ordinary plan fits the budget.  With
    allow_long=False, the budget is hard and necessary protected cuts are tried
    before an explicit whitespace fallback.  Callers unable to send whitespace
    bubbles must check whitespace_segments; impossible bounded coverage raises
    ValueError.  With default options and max_chars<=20, results equal the legacy
    function's results exactly.
    """
    return _plan_segments_core(
        text, incoming=incoming, scene=scene, max_chars=max_chars, weights=weights,
        draw=draw, char_ceiling=60, max_messages=max_messages, allow_long=allow_long,
    )
