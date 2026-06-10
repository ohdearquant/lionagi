import io

from lionagi.service.connections.endpoint import Endpoint

from .._config import GroqConfigs


@GroqConfigs.AUDIO_TRANSCRIPTION.register
class GroqAudioTranscriptionEndpoint(Endpoint):
    """Groq Whisper transcription endpoint.

    Groq supports the Whisper model for fast audio transcription.
    Uses multipart/form-data — pass ``file`` bytes and ``filename`` via kwargs.

    Usage::

        endpoint = GroqAudioTranscriptionEndpoint()
        with open("audio.mp3", "rb") as f:
            result = await endpoint.call(
                {"model": "whisper-large-v3"},
                file=f.read(),
                filename="audio.mp3",
            )
    """

    transport_arg_keys = ("file", "filename")

    def __init__(self, config=None, **kwargs):
        if config is None:
            from lionagi.config import settings

            kwargs.setdefault("api_key", settings.GROQ_API_KEY or "dummy-key-for-testing")
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config, **kwargs)

    async def _call(self, payload: dict, headers: dict, **kwargs):
        """Encode audio as multipart/form-data."""
        self._assert_ssrf_safe_url()

        import aiohttp

        file_data: bytes | None = kwargs.pop("file", None)
        filename: str = kwargs.pop("filename", "audio.mp3")

        form = aiohttp.FormData()
        for key, value in payload.items():
            if value is not None:
                form.add_field(key, str(value))

        if file_data is not None:
            form.add_field(
                "file",
                (io.BytesIO(file_data) if isinstance(file_data, (bytes, bytearray)) else file_data),
                filename=filename,
                content_type="application/octet-stream",
            )

        multipart_headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}

        async with self._create_http_session() as session:
            async with session.post(
                url=self.config.full_url,
                headers=multipart_headers,
                data=form,
            ) as response:
                if response.status != 200:
                    error_body = await response.text()
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                        message=f"Groq transcription failed ({response.status}): {error_body}",
                        headers=response.headers,
                    )
                return await response.json()
