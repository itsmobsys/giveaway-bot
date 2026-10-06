"""Settings parsing, for the values that reach Discord unchanged."""

from __future__ import annotations

import os
import unittest

from giveaway_bot.config import get_settings

VAR = "EMBED_COLOR"


class ColorTests(unittest.TestCase):
    """EMBED_COLOR in every spelling, always 24 bits wide."""

    def setUp(self) -> None:
        self._saved = os.environ.get(VAR)

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop(VAR, None)
        else:
            os.environ[VAR] = self._saved

    def color(self, raw: str) -> int:
        os.environ[VAR] = raw
        return get_settings().embed_color

    def test_every_documented_spelling_works(self) -> None:
        for raw in ("0x7C5CFF", "#7C5CFF", "7c5cff", "8150271"):
            self.assertEqual(self.color(raw), 0x7C5CFF, raw)

    def test_junk_keeps_the_default(self) -> None:
        for raw in ("", "   ", "#", "purple"):
            self.assertEqual(self.color(raw), 0x7C5CFF, raw)

    def test_an_over_wide_value_is_masked_to_24_bits(self) -> None:
        # Discord's colour field is 24 bits: an 8-digit ARGB value pasted in
        # must not be sent as 4294900991 for the API to reject.
        self.assertEqual(self.color("0xFF7C5CFF"), 0x7C5CFF)


if __name__ == "__main__":
    unittest.main()
