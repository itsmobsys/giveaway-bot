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

    def test_hash_and_bare_six_digit_hex_are_hex_not_decimal(self) -> None:
        # '#112233' / '112233' used to be read as decimal 112233 (0x01B669).
        for raw in ("#112233", "112233", "0x112233", "#123456"):
            self.assertEqual(self.color(raw), int(raw.lstrip("#").removeprefix("0x"), 16), raw)

    def test_other_bare_digits_stay_decimal(self) -> None:
        self.assertEqual(self.color("11223"), 11223)
        self.assertEqual(self.color("8150271"), 0x7C5CFF)

    def test_junk_keeps_the_default(self) -> None:
        for raw in ("", "   ", "#", "purple"):
            self.assertEqual(self.color(raw), 0x7C5CFF, raw)

    def test_an_over_wide_value_is_masked_to_24_bits(self) -> None:
        # Discord's colour field is 24 bits: an 8-digit ARGB value pasted in
        # must not be sent as 4294900991 for the API to reject.
        self.assertEqual(self.color("0xFF7C5CFF"), 0x7C5CFF)


class DashboardUrlTests(unittest.TestCase):
    """DASHBOARD_URL: default only when absent; explicit empty disables the button."""

    def setUp(self) -> None:
        self._saved = os.environ.get("DASHBOARD_URL")

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("DASHBOARD_URL", None)
        else:
            os.environ["DASHBOARD_URL"] = self._saved

    def test_unset_uses_the_default(self) -> None:
        os.environ.pop("DASHBOARD_URL", None)
        self.assertTrue(get_settings().dashboard_url.startswith("https://"))

    def test_explicit_empty_disables_the_button(self) -> None:
        for raw in ("", "   "):
            os.environ["DASHBOARD_URL"] = raw
            self.assertEqual(get_settings().dashboard_url, "", repr(raw))

    def test_custom_value_is_used(self) -> None:
        os.environ["DASHBOARD_URL"] = " https://example.com/ "
        self.assertEqual(get_settings().dashboard_url, "https://example.com/")


if __name__ == "__main__":
    unittest.main()
