# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Sentences from Whisper words, and one speaker per sentence."""

from __future__ import annotations

from wirenote.process.speakers import (SpeakerBook, Turn, Voices, Word, _unit, assign_sentence,
                                       sentences)


def words(spec: str, start: float = 0.0, step: float = 0.4) -> list[Word]:
    out, t = [], start
    for w in spec.split():
        out.append(Word(t, t + step - 0.05, " " + w))
        t += step
    return out


def text(ws):
    return "".join(w.text for w in ws).strip()


def test_sentences_end_at_punctuation_or_a_pause():
    ws = words("Neither did I. Okay, I heard a beep.") + words("no punctuation here", start=10)
    assert [text(s) for s in sentences(ws)] == ["Neither did I.", "Okay, I heard a beep.",
                                                 "no punctuation here"]


def test_sentence_goes_to_its_main_speaker_even_across_a_turn_edge():
    ws = words("Okay, I thought, you know, I heard a beep.", start=10.0)   # 10.0 .. 13.6
    turns = [Turn(9.0, 10.5, 1), Turn(10.5, 14.0, 0)]                     # edge clips 'Okay,'
    assert assign_sentence(ws, turns) == [(0, ws)]


def test_long_sentence_spanning_two_people_is_split_without_stray_words():
    ws = words("I will send the draft today and you review it by Friday please.", start=0.0)
    turns = [Turn(0.0, 2.4, 0), Turn(2.4, 2.8, 1), Turn(2.8, 6.0, 1)]
    runs = assign_sentence(ws, turns)
    assert [l for l, _ in runs] == [0, 1]
    assert text(runs[0][1]) == "I will send the draft today"


def test_speaker_book_and_voices_match_the_same_voice(tmp_path):
    book = SpeakerBook(0.55)
    assert book.label_for(_unit([1, 0, 0])) == "S1"
    assert book.label_for(_unit([0.95, 0.1, 0])) == "S1"     # same voice in a later segment
    assert book.label_for(_unit([0, 1, 0])) == "S2"
    v = Voices(tmp_path / "voices.json")
    v.remember("Diane", [1, 0, 0])
    assert v.match([0.9, 0.2, 0], 0.55)[0] == "Diane"
    assert v.match([0, 1, 0], 0.55)[0] is None
    assert (tmp_path / "voices.json").stat().st_mode & 0o777 == 0o600
