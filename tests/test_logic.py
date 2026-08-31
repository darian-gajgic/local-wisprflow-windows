#!/usr/bin/env python3
"""Tests for the platform-independent logic of local-wisprflow.

These cover the parts that can be verified without Windows: the text pipeline (NoteMode
splitting, LLM output sanitising, the off-script and translation backstops), hotkey parsing,
and the double-tap state machine. The Win32 layers (SendInput, tray, hooks) need a real
Windows desktop and are exercised by wf_doctor.py there.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wf_hotkey  # noqa: E402
from wf_daemon import (Daemon, _OFF_SCRIPT_RE, format_notes,  # noqa: E402
                       translated_away)


class TestFormatNotes(unittest.TestCase):
    """NoteMode: one sentence per line, deterministically, from punctuated Whisper output."""

    def test_splits_on_sentence_boundaries(self):
        self.assertEqual(
            format_notes("First one. Second one! Third one?"),
            "First one.\nSecond one!\nThird one?")

    def test_collapses_incoming_whitespace(self):
        self.assertEqual(format_notes("  One  thing.\n\nAnother thing.  "),
                         "One thing.\nAnother thing.")

    def test_single_letter_before_the_dot_suppresses_the_split(self):
        # Known, inherited trade-off: the rule that keeps "J. R. R. Tolkien" on one line
        # cannot tell that apart from a sentence that genuinely ends in a single letter.
        # Documented here so the behaviour is a decision rather than a surprise.
        self.assertEqual(format_notes("The answer is B. That was the last one."),
                         "The answer is B. That was the last one.")

    def test_empty(self):
        self.assertEqual(format_notes(""), "")
        self.assertEqual(format_notes("   "), "")

    def test_no_trailing_punctuation_still_one_line(self):
        self.assertEqual(format_notes("just one thought"), "just one thought")

    def test_abbreviation_does_not_split(self):
        self.assertEqual(format_notes("Ask Dr. Meyer about it."),
                         "Ask Dr. Meyer about it.")
        self.assertEqual(format_notes("Use a cache, e.g. Redis, for this."),
                         "Use a cache, e.g. Redis, for this.")

    def test_german_abbreviation_does_not_split(self):
        self.assertEqual(format_notes("Nimm z.B. Redis dafür."), "Nimm z.B. Redis dafür.")

    def test_initials_do_not_split(self):
        self.assertEqual(format_notes("It was J. R. R. Tolkien."), "It was J. R. R. Tolkien.")

    def test_standalone_list_marker_does_not_split(self):
        self.assertEqual(format_notes("1. Buy milk. 2. Call Ana."),
                         "1. Buy milk.\n2. Call Ana.")

    def test_number_ending_a_clause_still_splits(self):
        # The list-marker rule must not swallow a real sentence that merely ends in a digit.
        self.assertEqual(format_notes("I scored 8. That was lucky."),
                         "I scored 8.\nThat was lucky.")

    def test_closing_quote_stays_with_its_sentence(self):
        self.assertEqual(format_notes('He said "go." Then he left.'),
                         'He said "go."\nThen he left.')

    def test_ellipsis_is_one_boundary(self):
        self.assertEqual(format_notes("Well… I suppose so."), "Well…\nI suppose so.")


class TestSanitize(unittest.TestCase):
    """The deterministic backstop against a small model's stray preamble/quotes/newlines."""

    def test_strips_known_preamble(self):
        self.assertEqual(
            Daemon._sanitize("Sure, here is the corrected text: Ship it on Friday."),
            "Ship it on Friday.")

    def test_strips_cleaned_up_preamble(self):
        self.assertEqual(Daemon._sanitize("Corrected text: Hello there."), "Hello there.")

    def test_keeps_a_real_spoken_colon(self):
        # The anchor list is why this survives: a colon alone must not trigger stripping.
        text = "My plan is this: buy milk."
        self.assertEqual(Daemon._sanitize(text), text)

    def test_strips_wrapping_quotes(self):
        self.assertEqual(Daemon._sanitize('"Ship it on Friday."'), "Ship it on Friday.")

    def test_keeps_inner_quotes(self):
        self.assertEqual(Daemon._sanitize('He said "go" and left.'),
                         'He said "go" and left.')

    def test_collapses_newlines(self):
        # Outside NoteMode the injected text must be a single line.
        self.assertEqual(Daemon._sanitize("one\ntwo\n\nthree"), "one two three")


class TestOffScript(unittest.TestCase):
    """Markers that mean the model replied to the dictation instead of cleaning it."""

    def test_catches_ai_self_reference(self):
        self.assertTrue(_OFF_SCRIPT_RE.search("I am a large language model and cannot do that"))

    def test_catches_capability_refusal(self):
        self.assertTrue(_OFF_SCRIPT_RE.search(
            "I do not have the ability to change that setting"))

    def test_ignores_ordinary_dictation(self):
        # Deliberately close to a refusal — this must NOT trip the guard.
        for phrase in ("I don't have time for this today",
                       "I can't make it on Friday",
                       "as an aside, the report is long"):
            self.assertIsNone(_OFF_SCRIPT_RE.search(phrase), phrase)


class TestTranslatedAway(unittest.TestCase):
    """Language backstop: cleanup must reuse the spoken words, not replace them."""

    def test_faithful_cleanup_is_not_drift(self):
        raw = "um so i think we should uh ship it on friday you know"
        out = "So I think we should ship it on Friday."
        self.assertFalse(translated_away(raw, out))

    def test_translation_is_drift(self):
        raw = "an unterschiedlichen standorten in business centern oder inhouse bei den kunden"
        out = "At different locations, in business centres or in-house at the customers."
        self.assertTrue(translated_away(raw, out))

    def test_short_outputs_are_exempt(self):
        # 'ok' -> 'Okay.' is a normalization, not a translation.
        self.assertFalse(translated_away("ok", "Okay."))

    def test_diacritics_do_not_count_as_drift(self):
        raw = "care era capitala frantei"
        out = "Care era capitala Franței?"
        self.assertFalse(translated_away(raw, out))


class TestHotkeyParsing(unittest.TestCase):
    def test_basic_combo(self):
        mods, vk = wf_hotkey.parse_hotkey("ctrl+alt+space")
        self.assertEqual(mods, wf_hotkey.MOD_CONTROL | wf_hotkey.MOD_ALT)
        self.assertEqual(vk, 0x20)

    def test_letter_key(self):
        mods, vk = wf_hotkey.parse_hotkey("ctrl+shift+d")
        self.assertEqual(mods, wf_hotkey.MOD_CONTROL | wf_hotkey.MOD_SHIFT)
        self.assertEqual(vk, ord("D"))

    def test_function_key_and_win(self):
        mods, vk = wf_hotkey.parse_hotkey("win+f9")
        self.assertEqual(mods, wf_hotkey.MOD_WIN)
        self.assertEqual(vk, 0x78)

    def test_case_and_spacing_are_ignored(self):
        self.assertEqual(wf_hotkey.parse_hotkey(" CTRL + Alt + Space "),
                         wf_hotkey.parse_hotkey("ctrl+alt+space"))

    def test_modifier_only_is_rejected(self):
        with self.assertRaises(wf_hotkey.HotkeyError):
            wf_hotkey.parse_hotkey("ctrl+alt")

    def test_bare_key_is_rejected(self):
        # RegisterHotKey would happily steal a bare key from every other app.
        with self.assertRaises(wf_hotkey.HotkeyError):
            wf_hotkey.parse_hotkey("space")

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(wf_hotkey.HotkeyError):
            wf_hotkey.parse_hotkey("ctrl+nope")

    def test_numpad_minus_alone(self):
        # A solo binding: no modifier, and VK_SUBTRACT (0x6D) — NOT the main-row
        # VK_OEM_MINUS (0xBD), so the "-" you type dashes with is left alone.
        for spec in ("num-", "numminus", "numsubtract", " NUM- "):
            mods, vk = wf_hotkey.parse_hotkey(spec)
            self.assertEqual(mods, 0, spec)
            self.assertEqual(vk, 0x6D, spec)

    def test_main_row_minus_is_a_different_key(self):
        self.assertEqual(wf_hotkey.parse_hotkey("ctrl+-")[1], 0xBD)

    def test_numpad_operators_and_digits(self):
        for spec, vk in (("num*", 0x6A), ("numplus", 0x6B), ("num.", 0x6E),
                         ("num/", 0x6F), ("num0", 0x60), ("num9", 0x69)):
            self.assertEqual(wf_hotkey.parse_hotkey(spec)[1], vk, spec)

    def test_solo_keys_accept_modifiers_too(self):
        mods, vk = wf_hotkey.parse_hotkey("ctrl+alt+num-")
        self.assertEqual(mods, wf_hotkey.MOD_CONTROL | wf_hotkey.MOD_ALT)
        self.assertEqual(vk, 0x6D)

    def test_bare_letter_and_main_row_keys_stay_rejected(self):
        # Binding these solo would make the character untypeable everywhere.
        for spec in ("a", "-", "f9", "enter", "space"):
            with self.assertRaises(wf_hotkey.HotkeyError, msg=spec):
                wf_hotkey.parse_hotkey(spec)

    def test_describe_numpad(self):
        self.assertEqual(wf_hotkey.describe("num-"), "Numpad -")
        self.assertEqual(wf_hotkey.describe("ctrl+alt+num-"), "Ctrl + Alt + Numpad -")

    def test_doubletap(self):
        self.assertEqual(wf_hotkey.parse_doubletap("doubletap:rctrl"), 0xA3)
        self.assertIsNone(wf_hotkey.parse_doubletap("ctrl+alt+space"))
        with self.assertRaises(wf_hotkey.HotkeyError):
            wf_hotkey.parse_doubletap("doubletap:banana")


class TestDoubleTap(unittest.TestCase):
    """The state machine behind 'doubletap:rctrl', driven by a fake clock."""

    VK = 0xA3
    OTHER = 0x41

    def setUp(self):
        self.now = 100.0
        self.fired = 0
        self._real = wf_hotkey.time.monotonic
        wf_hotkey.time.monotonic = lambda: self.now
        self.tap = wf_hotkey._DoubleTap(self.VK, 0.4, self._fire)

    def tearDown(self):
        wf_hotkey.time.monotonic = self._real

    def _fire(self):
        self.fired += 1

    def _press(self, vk=None, dt=0.0):
        self.now += dt
        vk = self.VK if vk is None else vk
        self.tap.on_event(vk, True)
        self.tap.on_event(vk, False)

    def test_two_quick_taps_fire_once(self):
        self._press()
        self._press(dt=0.15)
        self.assertEqual(self.fired, 1)

    def test_slow_taps_do_not_fire(self):
        self._press()
        self._press(dt=0.9)
        self.assertEqual(self.fired, 0)

    def test_third_tap_starts_a_fresh_pair(self):
        self._press()
        self._press(dt=0.15)          # fires
        self._press(dt=0.15)          # consumed pair -> this is tap 1 of the next pair
        self.assertEqual(self.fired, 1)
        self._press(dt=0.15)          # tap 2 -> fires again
        self.assertEqual(self.fired, 2)

    def test_other_key_in_between_cancels(self):
        # Ctrl+C then Ctrl+V must not read as a double-tapped Ctrl.
        self._press()
        self.tap.on_event(self.OTHER, True)
        self.tap.on_event(self.OTHER, False)
        self._press(dt=0.1)
        self.assertEqual(self.fired, 0)

    def test_autorepeat_does_not_fire(self):
        # Holding the key produces a stream of key-downs with no key-up between them.
        self.tap.on_event(self.VK, True)
        for _ in range(20):
            self.tap.on_event(self.VK, True)
        self.tap.on_event(self.VK, False)
        self.assertEqual(self.fired, 0)


class TestConfig(unittest.TestCase):
    def test_defaults_are_self_consistent(self):
        import wf_paths
        cfg = dict(wf_paths.DEFAULTS)
        self.assertIn(cfg["inject_method"], ("type", "paste", "clipboard"))
        self.assertIn(cfg["asr_device"], ("auto", "cpu", "cuda"))
        self.assertIn(cfg["paste_chord"], ("ctrl+v", "ctrl+shift+v", "shift+insert"))
        # the default hotkey must actually parse
        wf_hotkey.parse_hotkey(cfg["hotkey"])
        wf_hotkey.parse_hotkey(cfg["hotkey_cancel"])

    def test_paste_chords_match_between_modules(self):
        import wf_input
        import wf_paths
        self.assertIn(wf_paths.DEFAULTS["paste_chord"], wf_input.PASTE_CHORDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
