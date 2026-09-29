"""Voice input at the prompt: Alt-V records, Alt-V again transcribes into the editor at the cursor, and
Enter sends what the person has read. The microphone and the transcriber are injected; the real ones
are exercised through stand-in modules, and one real transcription runs where the extra is installed."""

import asyncio
import importlib.machinery
import io
import sys
import threading
import types
from pathlib import Path

import pytest
from lion_cli.view import Editor
from lion_cli.voice import MODEL, NEEDS, RATE, Voice, _off_loop
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

ALT_V = "\x1bv"
LEFT = "\x1b[D"


class Terminal(Vt100_Output):
    # the toolbar is drawn once the rows below the cursor are known; a pipe never answers the request
    def get_rows_below_cursor_position(self) -> int:
        return 20


async def until(check, what: str) -> None:
    for _ in range(500):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never happened: {what}")


def microphone(events: list, audio="the audio"):
    def record():
        events.append("start")

        def stop():
            events.append("stop")
            return audio

        return stop

    return record


async def speak(voice: Voice, keys: list, *, status=None) -> tuple[str | None, list[str], Editor]:
    """Drive one prompt: each key string is sent once the previous one has had its effect on the
    screen or the buffer; returns what the prompt returned and the screen after each key."""
    with create_pipe_input() as pipe:
        screen = io.StringIO()
        out = Terminal(screen, lambda: Size(rows=24, columns=120), term="xterm")
        ed = Editor(None, status=status, voice=voice, input=pipe, output=out)
        asked = asyncio.create_task(ed.ask("you > "))
        shots = []
        for key, settled in keys:
            screen.seek(0)
            screen.truncate()
            pipe.send_text(key)
            await until(lambda settled=settled: settled(ed, screen.getvalue()), repr(key))
            shots.append(screen.getvalue())
        return await asyncio.wait_for(asked, 5), shots, ed


def test_alt_v_twice_puts_the_transcript_in_the_editor_and_enter_sends_it():
    events: list = []
    heard: list = []

    def transcribe(audio) -> str:
        heard.append(audio)
        return " said this \n"

    voice = Voice(microphone(events), transcribe)
    got, shots, ed = asyncio.run(
        speak(
            voice,
            [
                (ALT_V, lambda ed, s: "recording" in s),
                (ALT_V, lambda ed, s: ed.session.default_buffer.text == "said this"),
                ("\r", lambda ed, s: True),
            ],
            status=lambda: "inbox 0 · $0.0000",
        )
    )
    assert got == "said this"  # nothing was sent until Enter
    assert events == ["start", "stop"] and heard == ["the audio"]
    assert "● recording … Alt-V to stop · inbox 0 · $0.0000" in shots[0]  # the voice's line, then the status
    assert voice.note == "" and voice.stop is None


def test_the_transcript_goes_in_at_the_cursor_beside_what_was_typed():
    voice = Voice(microphone([]), lambda audio: "said this")
    got, _, _ = asyncio.run(
        speak(
            voice,
            [
                ("first second" + LEFT * 7, lambda ed, s: ed.session.default_buffer.cursor_position == 5),
                (ALT_V, lambda ed, s: voice.stop is not None),
                (ALT_V, lambda ed, s: "said" in ed.session.default_buffer.text),
                ("\r", lambda ed, s: True),
            ],
        )
    )
    assert got == "first said this second"  # a space after a word; what was typed is all still there


def test_without_the_extra_alt_v_says_how_to_install_it_and_inserts_nothing(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)  # `import sounddevice` raises ImportError
    voice = Voice()
    got, shots, _ = asyncio.run(
        speak(
            voice,
            [
                ("hi", lambda ed, s: ed.session.default_buffer.text == "hi"),
                (ALT_V, lambda ed, s: "uv pip install" in s),
                (ALT_V, lambda ed, s: True),  # the same notice again: nothing on screen changes
                ("\r", lambda ed, s: True),
            ],
        )
    )
    assert got == "hi"
    assert 'voice needs the voice extra: uv pip install "lion-cli[voice]"' in shots[1]
    assert voice.record is None and voice.stop is None and voice.note == ""  # the next prompt starts clean


class Frames:
    """What numpy's concatenate of (frames, 1) chunks is here: rows, of which `[:, 0]` is the channel."""

    def __init__(self, rows):
        self.rows = rows

    def __getitem__(self, key):
        return [row[key[1]] for row in self.rows]


def fake_modules(monkeypatch, *, whisper: bool) -> tuple[list, list]:
    streams: list = []
    calls: list = []

    class InputStream:
        def __init__(self, **kwargs):
            self.kwargs, self.events = kwargs, []
            streams.append(self)

        def start(self):
            self.events.append("start")

        def stop(self):
            self.events.append("stop")

        def close(self):
            self.events.append("close")

    numpy = types.SimpleNamespace(
        float32="float32",
        concatenate=lambda chunks: Frames([row for chunk in chunks for row in chunk]),
        zeros=lambda n, dtype: [0.0] * n,
    )
    monkeypatch.setitem(sys.modules, "numpy", numpy)
    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(InputStream=InputStream))
    if not whisper:
        monkeypatch.setitem(sys.modules, "mlx_whisper", None)
        return streams, calls
    mlx_whisper = types.ModuleType("mlx_whisper")
    mlx_whisper.__spec__ = importlib.machinery.ModuleSpec("mlx_whisper", None)

    def transcribe(audio, *, path_or_hf_repo):
        calls.append((len(audio), path_or_hf_repo))
        return {"text": " hello there "}

    mlx_whisper.transcribe = transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", mlx_whisper)
    return streams, calls


class App:
    """The two things `Voice` asks of the running application."""

    def __init__(self):
        self.drawn = 0

    def create_background_task(self, coro):
        return asyncio.get_running_loop().create_task(coro)

    def invalidate(self):
        self.drawn += 1


def test_the_real_pair_records_mono_at_16khz_and_asks_whisper_large_v3_turbo(monkeypatch):
    streams, _ = fake_modules(monkeypatch, whisper=False)
    voice, app, buffer = Voice(), App(), Buffer()
    voice.press(app, buffer)  # sounddevice is there, mlx_whisper is not: nothing starts
    assert voice.note == NEEDS and streams == [] and voice.record is None
    streams, calls = fake_modules(monkeypatch, whisper=True)

    async def main():
        voice.press(app, buffer)
        (stream,) = streams
        assert {k: stream.kwargs[k] for k in ("samplerate", "channels", "dtype")} == {
            "samplerate": RATE,
            "channels": 1,
            "dtype": "float32",
        }
        for _ in range(2):  # a second of sound, as PortAudio's callback hands it over
            stream.kwargs["callback"]([[0.5]] * (RATE // 2), RATE // 2, None, None)
        voice.press(app, buffer)
        await voice.task
        assert stream.events == ["start", "stop", "close"]
        return buffer.text

    assert asyncio.run(main()) == "hello there"
    assert calls == [(RATE, MODEL)] and voice.note == ""


def test_a_press_and_release_with_nothing_said_is_said_so_and_whisper_is_not_asked(monkeypatch):
    streams, calls = fake_modules(monkeypatch, whisper=True)
    voice, app, buffer = Voice(), App(), Buffer()

    async def main():
        voice.press(app, buffer)
        streams[-1].kwargs["callback"]([[0.5]] * 100, 100, None, None)  # a few milliseconds
        voice.press(app, buffer)
        await voice.task
        voice.press(app, buffer)
        voice.press(app, buffer)  # no sound reached the callback at all
        await voice.task

    asyncio.run(main())
    assert calls == [] and buffer.text == "" and voice.note == "voice: nothing heard"


def test_a_microphone_or_transcriber_that_fails_is_said_in_the_toolbar_and_the_prompt_goes_on():
    def no_microphone():
        raise OSError("PortAudio library not found")

    def broken(audio):
        raise RuntimeError("the model did not load")

    def missing(audio):
        raise ImportError("mlx_whisper")

    app, buffer = App(), Buffer()

    async def main() -> list[str]:
        notes = []
        voice = Voice(no_microphone, broken)
        voice.press(app, buffer)
        notes.append(voice.note)
        for transcribe in (broken, missing):
            voice = Voice(microphone([]), transcribe)
            voice.press(app, buffer)
            voice.press(app, buffer)
            await voice.task
            notes.append(voice.note)
        return notes

    assert asyncio.run(main()) == [
        "voice: OSError: PortAudio library not found",
        "voice: RuntimeError: the model did not load",
        NEEDS,
    ]
    assert buffer.text == "" and app.drawn == 2  # each failed transcription redrew the toolbar


def test_a_press_while_a_transcription_runs_does_nothing_until_it_lands():
    events: list = []
    gate = threading.Event()

    def slow(audio) -> str:
        gate.wait(5)
        return "first"

    voice, app, buffer = Voice(microphone(events), slow), App(), Buffer()

    async def main():
        voice.press(app, buffer)
        voice.press(app, buffer)
        task = voice.task
        voice.press(app, buffer)  # still transcribing: no second recording
        assert events.count("start") == 1 and voice.task is task and not task.done()
        assert voice.note == "transcribing …"
        gate.set()
        await voice.task
        assert events == ["start", "stop"]
        voice.press(app, buffer)
        assert events == ["start", "stop", "start"]
        voice.reset()  # the prompt ended mid-recording: the microphone stops, the audio is dropped

    asyncio.run(main())
    assert buffer.text == "first" and events[-1] == "stop" and voice.stop is None and voice.note == ""


def test_enter_during_a_transcription_sends_what_was_typed_and_the_late_transcript_goes_nowhere():
    gate = threading.Event()

    def slow(audio) -> str:
        gate.wait(5)
        return "late"

    voice = Voice(microphone([]), slow)

    async def main():
        before = set(threading.enumerate())
        first, _, ed = await speak(
            voice,
            [
                ("typed", lambda ed, s: ed.session.default_buffer.text == "typed"),
                (ALT_V, lambda ed, s: voice.stop is not None),
                (ALT_V, lambda ed, s: voice.task is not None),
                ("\r", lambda ed, s: True),
            ],
        )
        assert voice.task.cancelled() and voice.note == ""
        (thread,) = [t for t in threading.enumerate() if t.name == "voice" and t not in before]
        gate.set()
        await until(lambda: not thread.is_alive(), "the transcription thread ends")
        await asyncio.sleep(0.05)  # its outcome reaches the loop, and finds nothing waiting
        return first

    assert asyncio.run(main()) == "typed"


def test_a_transcription_that_outlives_the_loop_ends_quietly():
    gate = threading.Event()
    threads: list = []

    async def main():
        before = set(threading.enumerate())
        _off_loop(lambda: gate.wait(5) and "late")
        threads.extend(t for t in threading.enumerate() if t.name == "voice" and t not in before)

    asyncio.run(main())
    gate.set()
    (thread,) = threads
    thread.join(5)
    assert not thread.is_alive()  # an unhandled error on the thread would fail the run as a warning


def test_the_real_whisper_transcribes_a_second_of_silence_to_a_string():
    np = pytest.importorskip("numpy", reason="the voice extra is not installed")
    pytest.importorskip("mlx_whisper", reason="the voice extra (mlx-whisper) is not installed")
    from huggingface_hub.constants import HF_HUB_CACHE

    if not (Path(HF_HUB_CACHE) / f"models--{MODEL.replace('/', '--')}").is_dir():
        pytest.skip(f"{MODEL} is not in the local model cache; a test does not download it")
    from lion_cli.voice import _real

    _, transcribe = _real()
    assert isinstance(transcribe(np.zeros(RATE, dtype=np.float32)), str)
