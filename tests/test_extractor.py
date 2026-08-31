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

    def test_no_additional_preference_is_scoped_to_the_named_attribute(self) -> None:
        # "no ADDITIONAL preference for <typed attribute>" ends questioning
        # about THAT attribute, not the conversation.
        #
        # This test previously asserted global exhaustion here. That was only
        # ever right because the policy asked `other` every turn, so the named
        # attribute was always the universal question. Once the clarification
        # formula started asking typed questions, latching global exhaustion on
        # the first unanswerable attribute ended sessions after one question --
        # measured at 0.852 -> 0.222 technical on public200/official.
        extracted = HeuristicTurnExtractor().extract(
            "I don't have an additional preference for material."
        )
        self.assertFalse(extracted.information_exhausted)
        self.assertIn(("material", "no_preference", None), operations_of(extracted))

    def test_no_additional_preference_for_other_still_exhausts(self) -> None:
        # `other` is answered without being classified, so having nothing more
        # to say about it means having nothing more to say at all.
        for message in (
            "I don't have an additional preference for other.",
            "I don't have an additional preference.",
        ):
            with self.subTest(message=message):
                extracted = HeuristicTurnExtractor().extract(message)
                self.assertTrue(extracted.information_exhausted)
                self.assertEqual(operations_of(extracted), [])

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

    def test_normalizes_noisy_aliases_without_losing_raw_evidence(self) -> None:
        extracted = HeuristicTurnExtractor().extract("I need navy jogging shoes")
        actions = {item.attribute: item for item in extracted.operations if item.action == "set"}
        self.assertEqual(actions["color"].value, "blue")
        self.assertEqual(actions["color"].raw_text, "navy")
        self.assertEqual(actions["use_case"].value, "running")

    def test_material_label_is_not_misclassified_as_feature(self) -> None:
        extracted = HeuristicTurnExtractor().extract("What matters is: Material:alloy")
        self.assertIn(("material", "set", "Material:alloy"), operations_of(extracted))


if __name__ == "__main__":
    unittest.main()
