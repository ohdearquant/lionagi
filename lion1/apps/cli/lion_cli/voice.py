"""Speaking a message instead of typing it: Alt-V at the prompt records, Alt-V again transcribes, locally,
and the words land in the editor at the cursor for the person to read before Enter sends them."""

from __future__ import annotations

import asyncio
import importlib.util
import threading
from collections.abc import Callable
from typing import Any

__all__ = ("MODEL", "RATE", "NEEDS", "Voice")

MODEL = "mlx-community/whisper-large-v3-turbo"
RATE = 16_000  # Whisper's own sample rate: the microphone records at it, so nothing resamples
NEEDS = 'voice needs the voice extra: uv pip install "lion-cli[voice]"'


class Voice:
    """The microphone and a transcriber behind one key. `record()` starts recording and returns the
    function that stops it and hands back the audio (float32, mono, `RATE`); `transcribe(audio)` is
    the text said. Either left out is the real one, built on the first press: sounddevice and
    mlx_whisper, from the `voice` extra. `note` is what the toolbar shows about it, "" when nothing."""

    def __init__(
        self,
        record: Callable[[], Callable[[], Any]] | None = None,
        transcribe: Callable[[Any], str] | None = None,
    ):
        self.record = record
        self.transcribe = transcribe
        self.stop: Callable[[], Any] | None = None  # while recording: what ends it
        self.task: asyncio.Task | None = None  # the transcription on its way to the buffer
        self.note = ""

    def press(self, app: Any, buffer: Any) -> None:
        """Alt-V: start recording, or stop it and send the audio to be transcribed into `buffer`."""
        if self.task is not None and not self.task.done():
            return  # one transcription at a time: its text lands before the next recording starts
        if self.stop is None:
            try:
                if self.record is None or self.transcribe is None:
                    record, transcribe = _real()
                    self.record, self.transcribe = self.record or record, self.transcribe or transcribe
                self.stop = self.record()
            except ImportError:
                self.note = NEEDS
            except Exception as e:  # no microphone, no PortAudio: said, and the prompt goes on
                self.note = f"voice: {type(e).__name__}: {e}"
            else:
                self.note = "● recording … Alt-V to stop"
            return
        stop, self.stop = self.stop, None
        self.note = "transcribing …"
        self.task = app.create_background_task(self._land(stop, self.transcribe, buffer, app))

    def reset(self) -> None:
        """The prompt ended (Enter, Ctrl-C, Ctrl-D): a recording still running stops and is dropped."""
        stop, self.stop = self.stop, None
        if stop is not None:
            stop()
        self.note = ""

    async def _land(self, stop: Callable[[], Any], transcribe: Any, buffer: Any, app: Any) -> None:
        """The stopped recording, transcribed off the loop, inserted at the cursor from the loop."""
        try:
            text = (await _off_loop(lambda: transcribe(stop()))).strip()
        except ImportError:
            self.note = NEEDS
        except Exception as e:
            self.note = f"voice: {type(e).__name__}: {e}"
        else:
            self.note = "" if text else "voice: nothing heard"
            if text:
                if buffer.document.char_before_cursor.strip():  # after a word, not glued to it
                    text = " " + text
                buffer.insert_text(text)
        app.invalidate()


def _off_loop(work: Callable[[], str]) -> asyncio.Future[str]:
    """`work()` on a daemon thread, its outcome settled on the loop. Not the loop's thread pool: the
    loop's shutdown joins that, and a transcription (or the model's first download) still running when
    the chat ends would hold the exit (`stdin_ask` reads a pipe the same way for the same reason)."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()

    def settle(text: str | None, error: Exception | None) -> None:
        if fut.done():  # the prompt ended first and nothing waits for this
            return
        if error is not None:
            fut.set_exception(error)
        else:
            fut.set_result(text)

    def run() -> None:
        try:
            text, error = work(), None
        except Exception as e:
            text, error = None, e
        try:
            loop.call_soon_threadsafe(settle, text, error)
        except RuntimeError:  # the loop is closed: the chat ended while this ran
            pass

    threading.Thread(target=run, name="voice", daemon=True).start()
    return fut


def _real() -> tuple[Callable[[], Callable[[], Any]], Callable[[Any], str]]:
    """The microphone through sounddevice and Whisper through mlx_whisper, or ImportError without the
    extra. mlx_whisper is only looked for here: importing it takes seconds, so the import happens on the
    transcription's thread, and mlx_whisper keeps the model it loaded for the rest of the session."""
    import numpy as np
    import sounddevice as sd

    if importlib.util.find_spec("mlx_whisper") is None:
        raise ImportError("mlx_whisper")

    def record() -> Callable[[], Any]:
        chunks: list[Any] = []
        stream = sd.InputStream(
            samplerate=RATE,
            channels=1,
            dtype="float32",
            callback=lambda data, frames, time, status: chunks.append(data.copy()),  # PortAudio's thread
        )
        stream.start()

        def stop() -> Any:
            stream.stop()
            stream.close()
            return np.concatenate(chunks)[:, 0] if chunks else np.zeros(0, dtype=np.float32)

        return stop

    def transcribe(audio: Any) -> str:
        if len(audio) < RATE // 2:  # under half a second: a press and a release, nothing said
            return ""
        import mlx_whisper

        return mlx_whisper.transcribe(audio, path_or_hf_repo=MODEL)["text"]

    return record, transcribe
