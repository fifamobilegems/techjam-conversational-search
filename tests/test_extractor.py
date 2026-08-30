from __future__ import annotations

import unittest

from starter.extractor import HeuristicTurnExtractor


def operations_of(extracted) -> list[tuple[str, str, str | None]]:
    return [(item.attribute, item.action, item.value) for item in extracted.operations]


class ExtractorTest(unittest.TestCase):
    def test_extracts_category_and_key_requirement(self) -> None:
        extracted = HeuristicTurnExtractor().extract(
            "I'm looking for Boots. A key requirement is: black leather."
        )
        self.assertEqual(extracted.intent, "buying")
        self.assertIn(("category", "set", "Boots"), operations_of(extracted))
        self.assertIn(("material", "set", "black leather"), operations_of(extracted))

    def test_extracts_direct_clear(self) -> None:
        extracted = HeuristicTurnExtractor().extract("Please clear material.")
        self.assertIn(("material", "clear", None), operations_of(extracted))

    def test_boundary_reply_marks_no_preference(self) -> None:
        extracted = HeuristicTurnExtractor().extract(
            "I don't have a preference for color; please use your judgment."
        )
        self.assertIn(("color", "no_preference", None), operations_of(extracted))
        self.assertFalse(extracted.information_exhausted)

    def test_no_additional_preference_signals_exhaustion_not_no_preference(self) -> None:
        # "no ADDITIONAL preference" ends the questioning. It does not mean
        # the field is irrelevant, so it must not blacklist the attribute.
        extracted = HeuristicTurnExtractor().extract(
            "I don't have an additional preference for material."
        )
        self.assertTrue(extracted.information_exhausted)
        self.assertNotIn(("material", "no_preference", None), operations_of(extracted))

    def test_override_demotes_instead_of_clearing(self) -> None:
        class FakeState:
            slots = {"material": "leather"}
            history: list = []
            scenario = "unknown"

        extracted = HeuristicTurnExtractor().extract(
            "Actually ignore leather; I need wool", FakeState()
        )
        actions = operations_of(extracted)
        self.assertIn(("material", "demote", "leather"), actions)
        self.assertIn(("material", "set", "wool"), actions)
        self.assertNotIn(("material", "clear", None), actions)

    def test_scenario_is_read_from_opening_wording(self) -> None:
        extractor = HeuristicTurnExtractor()
        cases = {
            "I'm looking for Boots. A key requirement is: leather.": "buying",
            "I'm looking for Boots, but I'm still exploring.": "browsing_or_boundary",
            "I'm looking for Belts. Buckle closure": "intent_override",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(extractor.extract(message).scenario, expected)

    def test_scenario_is_only_set_once(self) -> None:
        class FakeState:
            slots: dict = {}
            history: list = []
            scenario = "buying"

        extracted = HeuristicTurnExtractor().extract(
            "I'm looking for Boots, but I'm still exploring.", FakeState()
        )
        self.assertIsNone(extracted.scenario)


if __name__ == "__main__":
    unittest.main()
