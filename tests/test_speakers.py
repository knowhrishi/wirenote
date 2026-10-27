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


def test_a_speaker_with_a_few_seconds_joins_the_one_heard_nearest():
    from wirenote.process.merge import Line
    from wirenote.process.speakers import SpeakerBook, fold_short_speakers

    book = SpeakerBook(0.55)
    for label, seconds in (("S1", 2.0), ("S2", 300.0), ("S3", 120.0), ("S4", 1.0)):
        book.prints[label], book.seconds[label] = label, seconds
    lines = [Line("S1", 0.0, 2.0, "Hm."),                      # the laugh before S2 starts
             Line("S2", 3.0, 20.0, "Let's start with the release."),
             Line("Me", 21.0, 24.0, "Sure."),
             Line("S3", 30.0, 40.0, "The build is green."),
             Line("S4", 41.0, 42.0, "Yes."),                    # right after S3
             Line("S2", 60.0, 70.0, "Then we ship on Friday.")]
    out = fold_short_speakers(lines, book)
    # S2 speaks first now, so it is S1; S3 becomes S2. "Me" is never touched.
    assert [l.speaker for l in out] == ["S1", "S1", "Me", "S2", "S2", "S1"]
    assert book.prints == {"S1": "S2", "S2": "S3"} and book.seconds == {"S1": 300.0, "S2": 120.0}


def test_a_short_call_keeps_its_one_voice_and_real_speakers_stay():
    from wirenote.process.merge import Line
    from wirenote.process.speakers import SpeakerBook, fold_short_speakers

    book = SpeakerBook(0.55)
    book.prints.update(S1="a", S2="b")
    book.seconds.update(S1=5.0, S2=3.0)                       # a 10-second call
    lines = [Line("S1", 0.0, 5.0, "Hello?"), Line("S2", 6.0, 9.0, "Hi.")]
    assert [l.speaker for l in fold_short_speakers(lines, book)] == ["S1", "S1"]
    book.prints.update(S1="a", S2="b")
    book.seconds.update(S1=60.0, S2=9.0)                      # both long enough: unchanged
    assert fold_short_speakers(lines, book) == lines and set(book.prints) == {"S1", "S2"}
