# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Speech engines: words from tokens, whisper.cpp output, Parakeet, and choices per call."""

from __future__ import annotations

import json
import stat
import sys
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from wirenote.capture.recorder import read_manifest, write_manifest
from wirenote.config.settings import Settings
from wirenote.process import engines
from wirenote.process.engines import (EngineError, check_overrides, choose, engine_label,
                                      make_engine, parse_whispercpp, tokens_to_words,
                                      with_overrides)
from wirenote.process.jobs import Jobs
from wirenote.process.worker import TRANSCRIPT, run_job

from test_process import FakeBackend, FakeTranscriber, make_recording, no_voices, settings_for


def test_tokens_join_into_words_with_punctuation_and_offset():
    words = tokens_to_words([" Ship", "ping", " on", " Fri", "day", "."],
                            [0.0, 0.2, 0.5, 0.8, 1.0, 1.2], [0.2, 0.4, 0.7, 1.0, 1.2, 1.3], 10.0)
    assert [w.text for w in words] == [" Shipping", " on", " Friday."]
    assert (words[0].start, words[0].end) == (10.0, 10.4)
    assert words[2].end == pytest.approx(11.3)
    # A first token without a leading space still starts a word.
    assert tokens_to_words(["Hallo"], [0.0], [0.3])[0].text == " Hallo"


def test_speech_pieces_are_padded_but_never_overlap(monkeypatch):
    """The VAD cuts speech longer than 30 s into pieces that touch; padding them must not
    make two pieces share audio, or the words there are heard twice."""
    import faster_whisper.vad

    found = [{"start": 16000, "end": 480000}, {"start": 480000, "end": 960000},
             {"start": 1000000, "end": 1100000}]
    monkeypatch.setattr(faster_whisper.vad, "get_speech_timestamps", lambda audio, opts: found)
    spans = engines.speech_spans(np.zeros(1102000, dtype=np.float32))
    assert spans == [(12800, 480000), (480000, 963200), (996800, 1102000)]


def test_whispercpp_json_skips_special_tokens():
    data = {"result": {"language": "de"}, "transcription": [
        {"tokens": [{"text": "[_BEG_]", "offsets": {"from": 0, "to": 0}},
                    {"text": " Wir", "offsets": {"from": 0, "to": 300}},
                    {"text": " liefern", "offsets": {"from": 300, "to": 700}},
                    {"text": ".", "offsets": {"from": 700, "to": 750}},
                    {"text": "[_TT_35]", "offsets": {"from": 750, "to": 750}}]},
        {"tokens": [{"text": " Gut", "offsets": {"from": 1000, "to": 1300}}]}]}
    words = parse_whispercpp(data)
    assert [w.text for w in words] == [" Wir", " liefern.", " Gut"]
    assert words[1].end == 0.75 and words[2].start == 1.0


FAKE_CLI = """#!{python}
import json, os, sys
a = sys.argv[1:]
out = a[a.index("-of") + 1]
if os.environ.get("FAKE_WHISPER_FAIL"):
    sys.stderr.write("ggml_vulkan: no device\\n"); sys.exit(3)
json.dump({{"result": {{"language": "en"}}, "transcription": [{{"tokens": [
    {{"text": " Hello", "offsets": {{"from": 100, "to": 400}}}},
    {{"text": " team", "offsets": {{"from": 1100, "to": 1400}}}}]}}]}}, open(out + ".json", "w"))
"""


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "whisper-cli"
    exe.parent.mkdir()
    exe.write_text(FAKE_CLI.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    model = tmp_path / "ggml-test.bin"
    model.write_bytes(b"x")
    # Two speech pieces: 1-2 s and 5-6 s; whisper-cli hears them back to back.
    monkeypatch.setattr(engines, "speech_spans", lambda audio: [(16000, 32000), (80000, 96000)])
    return replace(Settings(), asr_engine="whisper.cpp", whispercpp_bin=str(exe),
                   whispercpp_model=str(model), whisper_language="en")


def test_whispercpp_hears_only_speech_and_maps_times_back(fake_cli, monkeypatch):
    seen = {}
    real_run = engines.subprocess.run

    def run(argv, **kw):
        seen["argv"] = argv
        return real_run(argv, **kw)
    monkeypatch.setattr(engines.subprocess, "run", run)
    engine = make_engine(fake_cli, ["Kubernetes", "Wirenote."])
    heard = engine.hear(np.zeros(16000 * 8, dtype=np.float32))
    assert [w.text for w in heard.words] == [" Hello", " team"]
    assert heard.words[0].start == pytest.approx(1.1)      # first piece starts at 1 s
    assert heard.words[1].start == pytest.approx(5.1)      # 1.1 s into the joined file
    assert heard.language == "en" and heard.speech_s == 2.0
    argv = seen["argv"]
    assert argv[argv.index("-l") + 1] == "en" and "-ojf" in argv
    assert argv[argv.index("--prompt") + 1] == "Kubernetes. Wirenote."
    assert engine_label(fake_cli) == "whisper.cpp ggml-test.bin"


def test_whispercpp_failure_names_the_last_error_line(fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_WHISPER_FAIL", "1")
    with pytest.raises(EngineError, match=r"failed \(3\): ggml_vulkan: no device"):
        make_engine(fake_cli, []).hear(np.zeros(16000 * 8, dtype=np.float32))


def test_whispercpp_missing_is_a_clear_error(tmp_path):
    s = replace(Settings(), asr_engine="whisper.cpp", whispercpp_bin=str(tmp_path / "nope"))
    with pytest.raises(EngineError, match="not found"):
        make_engine(s, [])
    with pytest.raises(EngineError, match="asr_engine must be one of"):
        make_engine(replace(Settings(), asr_engine="vosk"), [])


def test_parakeet_decodes_speech_pieces_with_their_offsets(monkeypatch):
    import sherpa_onnx

    from wirenote.process import models

    made = {}

    class Stream:
        def accept_waveform(self, sr, samples):
            self.n = len(samples)

    class Recognizer:
        def create_stream(self):
            return Stream()

        def decode_stream(self, st):
            made.setdefault("decoded", []).append(st.n)
            st.result = SimpleNamespace(tokens=[" Ja", "."], timestamps=[0.1, 0.4],
                                        durations=[0.2, 0.1])

    def from_transducer(**kw):
        made["kw"] = kw
        return Recognizer()
    monkeypatch.setattr(sherpa_onnx.OfflineRecognizer, "from_transducer", from_transducer)
    monkeypatch.setattr(models, "hub_file", lambda repo, name, **kw: f"/models/{name}")
    spans = [(i * 32000, i * 32000 + 16000) for i in range(10)]
    monkeypatch.setattr(engines, "speech_spans", lambda audio: spans)

    heard = make_engine(replace(Settings(), asr_engine="parakeet"), []).hear(
        np.zeros(16000 * 20, dtype=np.float32))
    assert made["kw"]["model_type"] == "nemo_transducer"
    assert made["kw"]["encoder"] == "/models/encoder.int8.onnx"
    assert made["decoded"] == [16000] * 10       # one piece at a time: batches cost GBs
    assert len(heard.words) == 10 and heard.words[0].text == " Ja."
    assert heard.words[1].start == pytest.approx(2.1) and heard.words[1].end == pytest.approx(2.5)
    assert heard.speech_s == 10.0 and heard.language is None


@pytest.mark.parametrize("lang_env, settings, vocabulary, chosen", [
    ("de_DE.UTF-8", {}, [], "parakeet"),
    ("en_US.UTF-8", {}, [], "parakeet"),
    ("ja_JP.UTF-8", {}, [], "whisper"),                 # Parakeet does not know Japanese
    ("C.UTF-8", {}, [], "whisper"),                     # no language to go by
    ("ja_JP.UTF-8", {"whisper_language": "de"}, [], "parakeet"),
    ("de_DE.UTF-8", {"whisper_language": "ja"}, [], "whisper"),
    ("de_DE.UTF-8", {}, ["Kubernetes"], "whisper"),     # only Whisper uses the vocabulary
    ("de_DE.UTF-8", {"whisper_model": "large-v3"}, [], "whisper"),
    ("ja_JP.UTF-8", {"asr_engine": "parakeet"}, [], "parakeet"),   # a choice is a choice
    ("de_DE.UTF-8", {"asr_engine": "whisper"}, [], "whisper"),
])
def test_auto_picks_parakeet_only_where_it_fits(monkeypatch, lang_env, settings, vocabulary, chosen):
    for var in ("LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", lang_env)
    assert Settings().asr_engine == "auto"
    assert choose(replace(Settings(), **settings), vocabulary) == chosen


def test_auto_falls_back_to_whisper_when_parakeet_cannot_load(monkeypatch, capsys):
    from wirenote.process import models

    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)

    def offline(repo, name, **kw):
        raise OSError("offline")
    monkeypatch.setattr(models, "hub_file", offline)
    monkeypatch.setattr(engines, "WhisperEngine", lambda s, v: SimpleNamespace(label=s.whisper_model,
                                                                               engine=s.asr_engine))
    engine = make_engine(Settings(), [])
    assert (engine.label, engine.engine) == ("large-v3-turbo", "whisper")
    assert "parakeet unavailable (OSError: offline); using Whisper" in capsys.readouterr().err
    # A Parakeet you chose yourself fails loudly instead.
    with pytest.raises(OSError):
        make_engine(replace(Settings(), asr_engine="parakeet"), [])


def test_choices_are_checked_and_beat_the_settings():
    assert check_overrides({"engine": "parakeet", "model": " ", "language": "de"}) == \
        {"engine": "parakeet", "language": "de"}
    for bad in ({"engine": "vosk"}, {"beam": "5"}, {"model": "a\nb"}, {"model": "x" * 101}):
        with pytest.raises(ValueError):
            check_overrides(bad)
    s = with_overrides(Settings(), {"transcribe": {"model": "medium", "language": "de"}})
    assert (s.asr_engine, s.whisper_model, s.whisper_language) == ("auto", "medium", "de")
    # A hand-edited, broken manifest falls back to the settings rather than failing the job.
    assert with_overrides(Settings(), {"transcribe": {"engine": "vosk"}}) == Settings()


def test_worker_names_the_engine_that_ran(tmp_path):
    rec = make_recording(tmp_path, transcribe={"engine": "parakeet"})
    jobs = Jobs(tmp_path / "jobs.db")
    run_job(jobs.add(rec), settings_for(tmp_path), jobs, FakeTranscriber(),
            lambda _s: FakeBackend(), voices=no_voices(tmp_path))
    assert json.loads((rec / TRANSCRIPT).read_text())["model"] == "parakeet-tdt-0.6b-v3"
    assert "parakeet-tdt-0.6b-v3" in open(read_manifest(rec)["note"]).read()


def test_worker_lets_the_speech_models_go_before_the_summary(tmp_path, monkeypatch):
    import gc
    import weakref

    from wirenote.process import transcribe

    made = []

    class Transcriber(FakeTranscriber):
        def __init__(self, settings, vocabulary):
            super().__init__()
            made.append(weakref.ref(self))

    class Backend(FakeBackend):
        def complete_json(self, system, user, schema):
            gc.collect()
            assert made and made[0]() is None, "the speech models are still loaded"
            return super().complete_json(system, user, schema)

    monkeypatch.setattr(transcribe, "Transcriber", Transcriber)
    rec = make_recording(tmp_path)
    jobs = Jobs(tmp_path / "jobs.db")
    job = run_job(jobs.add(rec), settings_for(tmp_path), jobs, None, lambda _s: Backend(),
                  voices=no_voices(tmp_path))
    assert job.state == "done"


def test_requeue_with_choices_keeps_only_what_differs(tmp_path):
    from wirenote.ui.data import Invalid, Store

    rec = make_recording(tmp_path)
    s = replace(settings_for(tmp_path), recordings_dir=str(tmp_path / "recordings"))
    st = Store(s, tmp_path / "state", voices=no_voices(tmp_path))
    st.requeue(rec.name, summary_only=False,
               choices={"engine": "auto", "model": "medium", "language": "de"})
    assert read_manifest(rec)["transcribe"] == {"model": "medium", "language": "de"}
    assert st.call(rec.name)["transcribe"]["model"] == "medium"
    st.requeue(rec.name, summary_only=False, choices={})
    assert read_manifest(rec)["transcribe"] == {}
    with pytest.raises(Invalid, match="engine"):
        st.requeue(rec.name, summary_only=False, choices={"engine": "vosk"})
    with pytest.raises(Invalid):
        st.update_settings({"asr_engine": "vosk"}, confirm_cloud=False)


def test_cli_reprocess_writes_the_choices(capsys):
    from wirenote import cli
    from wirenote.config.settings import data_dir

    rec = data_dir() / "recordings" / "20261027-100000-chrome"
    rec.mkdir(parents=True)
    write_manifest(rec, {"version": 1, "id": rec.name, "started": "2026-10-27T10:00:00+01:00",
                         "transcribe": {"model": "medium"}})
    (rec / "mic-000.wav").write_bytes(b"")
    assert cli.main(["reprocess", rec.name, "--engine", "parakeet", "--language", "de"]) == 0
    assert read_manifest(rec)["transcribe"] == {"model": "medium", "engine": "parakeet",
                                                "language": "de"}
    assert cli.main(["reprocess", rec.name, "--summary", "--model", "small"]) == 2
    assert "need a new transcript" in capsys.readouterr().out


def test_doctor_checks_the_chosen_engine(tmp_path, monkeypatch):
    from wirenote.process import models
    from wirenote.tools import setup

    def not_cached(repo, name, local_only=False):
        raise FileNotFoundError(name)
    monkeypatch.setattr(models, "hub_file", not_cached)
    monkeypatch.setattr("shutil.which", lambda name: None)
    s = replace(Settings(), asr_engine="whisper.cpp", diarize=False, whispercpp_bin="whisper-cli",
                whispercpp_model=str(tmp_path / "missing.bin"))
    checks = setup._model_checks(s)
    assert checks[0].status == "bad" and "not installed" in checks[0].message
    checks = setup._model_checks(replace(s, asr_engine="vosk"))
    assert checks[0].status == "bad" and "vosk" in checks[0].message
    checks = setup._model_checks(replace(s, asr_engine="parakeet"))
    assert checks[0].status == "warn" and "670 MB" in checks[0].message
