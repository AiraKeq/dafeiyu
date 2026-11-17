"""Pure, lossless dynamic chat-budget and continuation-page planning.

No AstrBot initialization, model call, network, filesystem write or send occurs.
The sibling segmentation module is loaded by file with importlib, so this module
can also be inspected/tested standalone without importing the plugin package.

plan_delivery first tries the ENTIRE reply at short/medium/long limits (20/40/60
by default), retaining segmentation's natural-unit and soft 60/30/10 scoring.
Only a plan within max_messages (1..3) is accepted.  If none fits, source slices
are paged at long_limit.  A nonfinal display is exactly content + '\n' + hint
(or just content when hint is empty).  Navigation shares the SAME message
budget: it may be merged into a body bubble or use a reserved bubble, never a
fourth message.  The last page has no added navigation.  Later small tails may
use the short or medium limit again.

Page index and source offsets are zero-based; source_end is exclusive.
content is always the exact original slice, without navigation.  Only content
from pages actually sent belongs in chat history; full_text and unsent pages
are a plan, not an assertion that the whole reply was already delivered.
score_plan scores the actual display_text, including navigation.

Lengths count Python str code points, including ALL spaces, CRLF, indentation,
punctuation and combining characters.  Fitting natural units are protected;
necessary oversized-token/budget cuts remain visible in scoring/boundary
metadata.  All emitted bubbles must contain a non-whitespace character.  Empty
input returns pages=[]; skipped nonempty whitespace also returns pages=[], but
with complete=False and explicit exception/fallback metadata.  A mixed input
that cannot be represented losslessly by nonblank QQ bubbles raises ValueError
before any partial plan can escape.  No whitespace is silently trimmed.

The pagination feasibility automaton uses suffix hit counts in O(n) time/space
for the fixed 1..3 budget.  Boundary analysis is linear; per-page bounded prefix
search and exact scoring operate only on at most 180 code points.  The legacy
plan_segments API retains its separate 1..20 ceiling.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

__all__ = ["plan_delivery"]


def _load_segmentation():
    path = Path(__file__).resolve().with_name("segmentation.py")
    spec = importlib.util.spec_from_file_location(__name__ + "_segmentation", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load standalone segmentation module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SEGMENTATION = _load_segmentation()


def _require_integer(value: object, name: str, lower: int, upper: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, not bool or float")
    if not lower <= value <= upper:
        raise ValueError(f"{name} must be in {lower}..{upper}")


def _has_suffix_hit(hits: list[int], lower: int, upper: int) -> bool:
    return lower <= upper and hits[lower] > hits[upper + 1]


def _next_visible(text: str) -> list[int]:
    n, following = len(text), len(text)
    result = [n] * (n + 1)
    for start in range(n - 1, -1, -1):
        if not text[start].isspace():
            following = start
        result[start] = following
    return result


def _hint_body_limits(suffix: str, limit: int, budget: int) -> dict[int, int]:
    """Map body-bubble count to its largest permitted LAST body-bubble size.

    A last body bubble may absorb c leading navigation characters; r remaining
    nonblank navigation bubbles must fit in budget-body_count.  c=0 also covers
    reserving navigation bubbles.  Longer hints are supported whenever the
    complete display still has room for at least one original character.
    """
    n = len(suffix)
    if n >= budget * limit:
        return {}
    following = _next_visible(suffix)
    feasible = [[False] * (n + 1) for _ in range(budget)]
    feasible[0][n] = True
    # Zero navigation bubbles cover only the empty suffix at offset n.
    previous_hits = [1] * (n + 1) + [0]
    for count in range(1, budget):
        hits = [0] * (n + 2)
        for start in range(n - 1, -1, -1):
            possible = _has_suffix_hit(
                previous_hits, following[start] + 1, min(n, start + limit)
            )
            feasible[count][start] = possible
            hits[start] = hits[start + 1] + possible
        previous_hits = hits
    result = {}
    for body_count in range(1, budget + 1):
        caps = [
            limit - prefix
            for remaining_count in range(budget - body_count + 1)
            for prefix in range(min(n, limit - 1) + 1)
            if feasible[remaining_count][prefix]
        ]
        if caps:
            result[body_count] = max(caps)
    return result


def _tail_viability(text: str, limit: int, budget: int,
                    body_limits: dict[int, int]) -> list[bool]:
    """Exact nonblank geometric feasibility for all remaining source offsets.

    Finals use up to budget full-size source bubbles without a hint.  Ordinary
    pages use k source bubbles, with the last size reduced by navigation.
    Every body edge contains original non-whitespace text: hint-only bubbles
    must not camouflage an otherwise unsendable all-whitespace body slice.
    Suffix counts answer each possible-edge interval without a quadratic scan.
    Natural-unit protection is applied during page selection, then relaxed
    only if the bounded, lossless nonblank geometry makes that necessary.
    """
    n, following = len(text), _next_visible(text)
    final_hits = [[0] * (n + 2) for _ in range(budget + 1)]
    final_hits[0] = [1] * (n + 1) + [0]
    page_hits = {
        count: [[0] * (n + 2) for _ in range(count + 1)]
        for count in body_limits
    }
    viable, tail_hits = [False] * (n + 1), [0] * (n + 2)
    viable[n], tail_hits[n] = True, 1
    for start in range(n - 1, -1, -1):
        lower, full_upper = following[start] + 1, min(n, start + limit)
        final_possible = False
        for count in range(1, budget + 1):
            possible = _has_suffix_hit(final_hits[count - 1], lower, full_upper)
            final_hits[count][start] = final_hits[count][start + 1] + possible
            final_possible = final_possible or possible
        ordinary_possible = False
        for count, last_limit in body_limits.items():
            states = page_hits[count]
            for remaining in range(1, count + 1):
                targets = tail_hits if remaining == 1 else states[remaining - 1]
                edge_limit = last_limit if remaining == 1 else limit
                possible = _has_suffix_hit(targets, lower, min(n, start + edge_limit))
                states[remaining][start] = states[remaining][start + 1] + possible
                if remaining == count:
                    ordinary_possible = ordinary_possible or possible
        viable[start] = final_possible or ordinary_possible
        tail_hits[start] = tail_hits[start + 1] + viable[start]
    return viable


def _forward_hits(paths: list[bool], data, source_start: int,
                  allow_protected: bool) -> list[int]:
    hits = [0] * (len(paths) + 1)
    for offset, possible in enumerate(paths):
        allowed = offset == 0 or allow_protected or not data.hard[source_start + offset]
        hits[offset + 1] = hits[offset] + (possible and allowed)
    return hits


def _has_forward_hit(hits: list[int], lower: int, upper: int) -> bool:
    return lower <= upper and hits[upper + 1] > hits[lower]


def _page_end(text: str, source_start: int, data, viable: list[bool],
              limit: int, budget: int, body_limits: dict[int, int]) -> int | None:
    capacity = max((count - 1) * limit + last for count, last in body_limits.items())
    width = min(capacity, len(text) - source_start - 1)
    if width < 1:
        return None
    latest, position = [-1] * (width + 1), -1
    for end in range(1, width + 1):
        if not text[source_start + end - 1].isspace():
            position = end - 1
        latest[end] = position
    for allow_protected in (False, True):
        paths = [False] * (width + 1)
        paths[0] = True
        layers = [_forward_hits(paths, data, source_start, allow_protected)]
        for _ in range(1, budget):
            paths = [False] * (width + 1)
            for end in range(1, width + 1):
                paths[end] = _has_forward_hit(
                    layers[-1], max(0, end - limit), latest[end]
                )
            layers.append(_forward_hits(paths, data, source_start, allow_protected))
        ends = []
        for end in range(1, width + 1):
            absolute = source_start + end
            if not viable[absolute] or (data.hard[absolute] and not allow_protected):
                continue
            if any(
                _has_forward_hit(layers[count - 1], max(0, end - last), latest[end])
                for count, last in body_limits.items()
            ):
                ends.append(absolute)
        if ends:
            farthest = ends[-1]
            # A nearby real pause can shorten the page, never delete its tail.
            nearby = [end for end in ends if end >= farthest - limit // 2]
            for quality in (0.94, 0.68):
                natural = [end for end in nearby if data.quality[end] >= quality]
                if natural:
                    return natural[-1]
            if allow_protected:
                # Relaxing an overlapping clause must not buy a gratuitous
                # CRLF/word/combining-pair cut when a gentler endpoint fits.
                intact = [end for end in nearby if not data.hard[end]]
                alternatives = intact or nearby
                best_quality = max(data.quality[end] for end in alternatives)
                return [end for end in alternatives
                        if data.quality[end] >= best_quality - 0.01][-1]
            return farthest
    return None


def _whole_plan(text: str, source_start: int, *, incoming: str, scene: str,
                weights: list[float], draw: float | None, budget: int,
                limits: tuple[int, int, int]) -> tuple[int, dict] | None:
    remaining_chars = len(text) - source_start
    if remaining_chars > budget * limits[-1]:
        return None
    remaining = text[source_start:]
    for limit in limits:
        if remaining_chars > budget * limit:
            continue
        plan = _SEGMENTATION.plan_segments_extended(
            remaining, incoming=incoming, scene=scene, max_chars=limit,
            weights=weights, draw=draw, max_messages=budget,
        )
        if plan["chosen_count"] <= budget and not plan["whitespace_segments"]:
            return limit, plan
    return None


def _make_page(text: str, start: int, end: int, *, index: int, limit: int,
               budget: int, suffix: str, score_plan: dict, data=None) -> dict:
    content, continuation = text[start:end], end < len(text)
    display = content + suffix if continuation else content
    segments = score_plan["segments"]
    if (not content or content.isspace() or "".join(segments) != display
            or not 1 <= len(segments) <= budget
            or any(not segment or segment.isspace() or len(segment) > limit
                   for segment in segments)):
        raise ValueError("QQ delivery cannot preserve this slice in nonblank budgeted bubbles")
    reasons = _SEGMENTATION._forced_reasons([end], data) if continuation and data else []
    return {
        "content": content, "display_text": display, "segments": segments,
        "char_limit": limit, "continuation": continuation, "index": index,
        "source_start": start, "source_end": end, "score_plan": score_plan,
        "page_boundary_forced": bool(reasons), "page_boundary_reasons": reasons,
        "exception": score_plan["exception"] or bool(reasons),
    }


def plan_delivery(text: str, *, incoming: str = '', weights=(60, 30, 10),
                  draw: float | None = None, max_messages: int = 3,
                  short_limit: int = 20, medium_limit: int = 40,
                  long_limit: int = 60,
                  continuation_hint: str = '说“继续”看下一页。') -> dict:
    """Return the whole lossless delivery plan; do not send or persist anything.

    Type errors raise TypeError; invalid ranges raise ValueError.  Integer
    budgets exclude bool/float and obey max_messages in 1..3 and
    20 <= short_limit < medium_limit < long_limit <= 60.  Weights and draw have
    exactly the legacy validation/soft-scoring semantics.  Empty hints suppress
    added navigation; other hints are copied verbatim after one display newline.
    An oversized/unsendable hint raises ValueError when pagination needs it.
    """
    _SEGMENTATION._require_string(text, "text")
    _SEGMENTATION._require_string(incoming, "incoming")
    _SEGMENTATION._require_string(continuation_hint, "continuation_hint")
    _require_integer(max_messages, "max_messages", 1, 3)
    for name, value in (("short_limit", short_limit), ("medium_limit", medium_limit),
                        ("long_limit", long_limit)):
        _require_integer(value, name, 20, 60)
    if not short_limit < medium_limit < long_limit:
        raise ValueError("limits must be strictly increasing: short_limit < medium_limit < long_limit")
    normalized = _SEGMENTATION._normalize_weights(weights)
    validated_draw = _SEGMENTATION._validate_draw(draw)
    scene = _SEGMENTATION.classify_scene(incoming, text)
    limits = (short_limit, medium_limit, long_limit)
    suffix = "\n" + continuation_hint if continuation_hint else ""
    result = {
        "full_text": text, "pages": [], "scene": scene, "weights": normalized,
        "source_chars": len(text), "max_messages": max_messages, "limits": list(limits),
        "continuation_hint": continuation_hint, "hint_suffix": suffix,
    }
    if not text or text.isspace():
        result.update(
            mode="empty", complete=not text, exception=bool(text),
            reason="whitespace_only_not_sent" if text else "empty",
            constraint_fallback="whitespace_only_not_sent" if text else "none",
        )
        return result

    pages, start, data, viable, body_limits = result["pages"], 0, None, None, None
    while start < len(text):
        whole = _whole_plan(
            text, start, incoming=incoming, scene=scene, weights=normalized,
            draw=validated_draw, budget=max_messages, limits=limits,
        )
        if whole is not None:
            limit, score_plan = whole
            pages.append(_make_page(
                text, start, len(text), index=len(pages), limit=limit,
                budget=max_messages, suffix=suffix, score_plan=score_plan,
            ))
            break
        if data is None:
            body_limits = _hint_body_limits(suffix, long_limit, max_messages)
            if not body_limits:
                raise ValueError("continuation_hint leaves no nonblank body room within the message budget")
            viable = _tail_viability(text, long_limit, max_messages, body_limits)
            if not viable[start]:
                raise ValueError("QQ cannot losslessly send this whitespace distribution in nonblank bubbles")
            data = _SEGMENTATION._analyse(text, long_limit)
        end = _page_end(text, start, data, viable, long_limit, max_messages, body_limits)
        if end is None:
            # A geometrically feasible final must not force an unsendable blank
            # page merely to retain protection.  Relax only this bounded final.
            score_plan = _SEGMENTATION.plan_segments_extended(
                text[start:], incoming=incoming, scene=scene, max_chars=long_limit,
                weights=normalized, draw=validated_draw, max_messages=max_messages,
                allow_long=False,
            )
            pages.append(_make_page(
                text, start, len(text), index=len(pages), limit=long_limit,
                budget=max_messages, suffix=suffix, score_plan=score_plan,
            ))
            break
        display = text[start:end] + suffix
        score_plan = _SEGMENTATION.plan_segments_extended(
            display, incoming=incoming, scene=scene, max_chars=long_limit,
            weights=normalized, draw=validated_draw, max_messages=max_messages,
            allow_long=False,
        )
        pages.append(_make_page(
            text, start, end, index=len(pages), limit=long_limit,
            budget=max_messages, suffix=suffix, score_plan=score_plan, data=data,
        ))
        start = end
    if "".join(page["content"] for page in pages) != text:
        raise RuntimeError("internal delivery integrity gate failed to preserve the complete source")
    result.update(
        mode="paged" if len(pages) > 1 else "single", complete=True,
        exception=any(page["exception"] for page in pages),
        reason="message_budget_pagination" if len(pages) > 1 else "whole_reply_fits",
        constraint_fallback="none",
    )
    return result
