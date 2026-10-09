import io
import shutil
import subprocess
import sys
import wave
from types import SimpleNamespace

import httpx
import pytest

from app.config import PROGRAM_BASE_URL
from app.providers import ProviderError
from app.speech import CloudRuSpeechProvider, normalized_wav


def test_normalization_uses_bounded_copy_and_gateway_minimal_fields(monkeypatch):
    pcm = b'\x00\x00' * 16000
    def convert(command, **kwargs):
        assert kwargs['input'] == b'synthetic-original-webm'
        assert kwargs['timeout'] == 25 and kwargs['stderr'] == subprocess.DEVNULL
        assert '-fs' in command and 'app.audio_process' in command
        return SimpleNamespace(returncode=0, stdout=pcm)
    monkeypatch.setattr('app.speech.subprocess.run', convert)
    calls = []
    def handler(request):
        calls.append(request)
        assert b'recording.wav' in request.content and b'RIFF' in request.content
        assert b'synthetic-original-webm' not in request.content
        assert b'name="language"' not in request.content and b'name="response_format"' not in request.content
        return httpx.Response(200, json={'text':'Синтетическая речь'})
    provider = CloudRuSpeechProvider('synthetic-secret','whisper-large-v3',base_url=PROGRAM_BASE_URL,
                                    normalize_audio=True,transport=httpx.MockTransport(handler))
    source = io.BytesIO(b'synthetic-original-webm')
    assert provider.transcribe(source,media_type='audio/webm') == 'Синтетическая речь'
    assert source.getvalue() == b'synthetic-original-webm' and len(calls) == 1
    with wave.open(io.BytesIO(normalized_wav(b'synthetic-original-webm','ffmpeg')),'rb') as output:
        assert (output.getnchannels(),output.getsampwidth(),output.getframerate(),output.getnframes()) == (1,2,16000,16000)


@pytest.mark.parametrize('result',[SimpleNamespace(returncode=1,stdout=b''),
                                 SimpleNamespace(returncode=0,stdout=b'odd'),
                                 SimpleNamespace(returncode=0,stdout=b'x' * (180 * 16000 * 2 + 2))])
def test_conversion_failure_prevents_any_provider_call(monkeypatch,result):
    monkeypatch.setattr('app.speech.subprocess.run',lambda *args,**kwargs:result)
    def forbidden(request):
        raise AssertionError('must fail before HTTP')
    provider = CloudRuSpeechProvider('synthetic-secret','synthetic-stt',base_url=PROGRAM_BASE_URL,
                                    normalize_audio=True,transport=httpx.MockTransport(forbidden))
    with pytest.raises(ProviderError,match='stt_audio_conversion_failed'):
        provider.transcribe(io.BytesIO(b'original'),media_type='audio/ogg')


@pytest.mark.skipif(sys.platform != 'linux' or not shutil.which('ffmpeg'),reason='Linux decoder required')
def test_real_decoder_preserves_original_and_produces_valid_bounded_wav():
    original = io.BytesIO()
    with wave.open(original,'wb') as source:
        source.setnchannels(2)
        source.setsampwidth(2)
        source.setframerate(48000)
        source.writeframes(b'\x00\x00\x00\x00' * 48000)
    before = original.getvalue()
    converted = normalized_wav(before,'ffmpeg')
    assert original.getvalue() == before
    with wave.open(io.BytesIO(converted),'rb') as output:
        assert (output.getnchannels(),output.getframerate(),output.getnframes()) == (1,16000,16000)


def test_error_diagnostics_never_return_provider_body_or_key():
    provider = CloudRuSpeechProvider('synthetic-secret','synthetic-stt',base_url=PROGRAM_BASE_URL,
        transport=httpx.MockTransport(lambda _: httpx.Response(400,json={
            'error':{'message':'unsupported file format; synthetic-secret; private recording content'},
        })))
    with pytest.raises(ProviderError) as caught:
        provider.transcribe(io.BytesIO(b'fixture'),media_type='audio/wav')
    assert caught.value.diagnostic_stage == 'audio_format'
    assert str(caught.value) == 'stt_http_400' and caught.value.http_status == 400
