from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeVar


class ImageReferenceLike(Protocol):
    file_hash: str


TImageReference = TypeVar("TImageReference", bound=ImageReferenceLike)


def resolve_reply_plan_image_refs(
    block_refs: Sequence[TImageReference],
    related_image_hashes: Sequence[str],
) -> list[TImageReference]:
    refs_by_hash: dict[str, TImageReference] = {}
    for ref in block_refs:
        file_hash = getattr(ref, "file_hash", "")
        if not isinstance(file_hash, str):
            continue

        normalized_hash = file_hash.strip()
        if not normalized_hash or normalized_hash in refs_by_hash:
            continue

        refs_by_hash[normalized_hash] = ref

    resolved_refs: list[TImageReference] = []
    seen_hashes: set[str] = set()
    for file_hash in related_image_hashes:
        normalized_hash = file_hash.strip()
        if not normalized_hash or normalized_hash in seen_hashes:
            continue

        ref = refs_by_hash.get(normalized_hash)
        if ref is None:
            continue

        resolved_refs.append(ref)
        seen_hashes.add(normalized_hash)

    return resolved_refs
