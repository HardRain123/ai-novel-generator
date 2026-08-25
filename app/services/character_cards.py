"""Compact, forward-compatible character cards.

Planning uses one canonical representation.  Legacy database columns are only
a persistence projection and must never leak back into planning artifacts.
"""

from __future__ import annotations

from typing import Any


def text(value: Any) -> str:
    return str(value or "").strip()


def facets(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not text(key):
            continue
        if isinstance(item, dict):
            result[str(key).strip()] = item
        elif text(item):
            result[str(key).strip()] = {"content": text(item)}
    return result


def _same_text(left: Any, right: Any) -> bool:
    """Treat whitespace and punctuation-only differences as duplicated content."""
    normalized = lambda value: "".join(char for char in text(value) if char.isalnum() or "\u4e00" <= char <= "\u9fff")
    return bool(normalized(left)) and normalized(left) == normalized(right)


def compact_character(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize a generated, edited, or legacy character into one card.

    The legacy fields remain populated for persisted works and older API
    consumers.  New callers should prefer the independent ``appearance``,
    ``personality`` and ``voice`` fields.
    """
    item = dict(raw or {})
    supplied_core = item.get("dramatic_core")
    core = dict(supplied_core) if isinstance(supplied_core, dict) else {}
    for key in ("goal", "motivation", "flaw", "conflict"):
        core[key] = text(core.get(key) or item.get(key))

    biography = text(item.get("biography"))
    if not biography:
        biography = "；".join(
            part for part in (text(item.get("background")), text(item.get("personality"))) if part
        )
    legacy_portrayal = text(item.get("portrayal"))
    appearance = text(item.get("appearance")) or legacy_portrayal
    personality = text(item.get("personality"))
    voice = text(item.get("voice"))
    # Previous cards used the full portrayal as a fallback for both fields.
    # Keep the visual information, but do not surface it as three copies.
    if legacy_portrayal and _same_text(personality, legacy_portrayal):
        personality = ""
    if legacy_portrayal and _same_text(voice, legacy_portrayal):
        voice = ""
    arc = text(item.get("arc") or item.get("character_arc"))
    card_facets = facets(item.get("facets"))

    result = {
        **item,
        "name": text(item.get("name")),
        "role": text(item.get("role")),
        "story_function": text(item.get("story_function")),
        "biography": biography,
        "dramatic_core": core,
        "appearance": appearance,
        # Kept for older consumers; new planning and UI read ``appearance``.
        "portrayal": appearance,
        "personality": personality,
        "voice": voice,
        "arc": arc,
        "facets": card_facets,
        # Compatibility projection for the existing storage and APIs.
        "goal": core["goal"],
        "motivation": core["motivation"],
        "flaw": core["flaw"],
        "conflict": core["conflict"],
        "background": text(item.get("background")),
        "status": text(item.get("status")),
        "knowledge": text(item.get("knowledge")),
        "character_arc": arc,
        "secret": text(item.get("secret")),
        "relationships": text(item.get("relationships")),
    }
    return result


def planning_character(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return the small card that authors and planning models should see.

    Legacy fields are a persistence concern.  Keeping them out of planning
    artifacts prevents empty compatibility keys from looking like model output.
    """
    normalized = compact_character(raw)
    return {
        key: normalized.get(key)
        for key in (
            "name", "role", "story_function", "biography", "dramatic_core",
            "appearance", "personality", "voice", "arc", "secret", "relationships", "facets",
        )
    }


def character_context(card: dict[str, Any]) -> dict[str, Any]:
    """Return the compact stable card suitable for chapter generation."""
    normalized = compact_character(card)
    return {
        key: normalized.get(key)
        for key in (
            "id", "name", "role", "story_function", "biography", "dramatic_core",
            "appearance", "personality", "voice", "arc", "secret", "relationships", "facets",
        )
    }
