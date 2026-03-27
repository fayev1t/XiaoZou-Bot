from __future__ import annotations

import importlib
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

reply_plan_images = importlib.import_module("qqbot.services.reply_plan_images")


@dataclass
class FakeImageReference:
    file_hash: str
    timestamp: object | None = None


class ReplyPlanImageRefTests(unittest.TestCase):
    def test_resolve_reply_plan_image_refs_preserves_plan_order(self) -> None:
        first = FakeImageReference(file_hash="hash-a", timestamp=111)
        second = FakeImageReference(file_hash="hash-b", timestamp=222)
        third = FakeImageReference(file_hash="hash-c", timestamp=333)

        resolved = reply_plan_images.resolve_reply_plan_image_refs(
            [first, second, third],
            ["hash-c", "hash-a"],
        )

        self.assertEqual(resolved, [third, first])
        self.assertEqual([ref.timestamp for ref in resolved], [333, 111])

    def test_resolve_reply_plan_image_refs_ignores_missing_and_duplicate_hashes(self) -> None:
        first = FakeImageReference(file_hash="hash-a", timestamp=111)
        duplicate = FakeImageReference(file_hash="hash-a", timestamp=999)
        second = FakeImageReference(file_hash="hash-b", timestamp=222)

        resolved = reply_plan_images.resolve_reply_plan_image_refs(
            [first, duplicate, second],
            ["hash-b", "missing", "hash-b", "hash-a", ""],
        )

        self.assertEqual(resolved, [second, first])


if __name__ == "__main__":
    unittest.main()
