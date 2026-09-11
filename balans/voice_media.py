"""Decode bounded Telegram audio locally; only normalized WAV reaches the API."""
from io import BytesIO
import subprocess
import sys
import wave
from balans.receipt_media import MAX_FILE_BYTES, MediaError

MAX_SECONDS = 180
RATE = 16000


def prepare_voice(data):
    if not data or len(data) > MAX_FILE_BYTES:
        raise MediaError('Пустая запись или файл больше 15 МБ.')
    try:
        result = subprocess.run([sys.executable, '-m', 'balans.voice_media'], input=data,
                                capture_output=True, timeout=25)
    except subprocess.TimeoutExpired:
        raise MediaError('Не удалось обработать запись за отведённое время. Отправьте более короткое сообщение.') from None
    if result.returncode:
        raise MediaError('Нужна читаемая голосовая запись до 3 минут. Пустую, повреждённую или слишком длинную запись обработать нельзя.')
    return result.stdout


def decode(data):
    import av
    import array
    pcm = bytearray()
    with av.open(BytesIO(data)) as container:
        if len(container.streams.audio) != 1 or container.streams.video:
            raise ValueError('Audio stream required')
        resampler = av.AudioResampler(format='s16', layout='mono', rate=RATE)
        for frame in container.decode(audio=0):
            if frame.samples > 1_000_000 or frame.sample_rate > 192000:
                raise ValueError('Frame limit')
            for out in resampler.resample(frame):
                pcm.extend(bytes(out.planes[0])[:out.samples * 2])
                if len(pcm) > MAX_SECONDS * RATE * 2:
                    raise ValueError('Duration limit')
        for out in resampler.resample(None):
            pcm.extend(bytes(out.planes[0])[:out.samples * 2])
    if not RATE // 5 * 2 <= len(pcm) <= MAX_SECONDS * RATE * 2:
        raise ValueError('Duration limit')
    samples = array.array('h', pcm)
    if max(abs(s) for s in samples) < 100:
        raise ValueError('Silent audio')
    output = BytesIO()
    with wave.open(output, 'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(RATE); f.writeframes(pcm)
    return output.getvalue()


if __name__ == '__main__':
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    if sys.platform == 'linux':
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    try:
        sys.stdout.buffer.write(decode(sys.stdin.buffer.read(MAX_FILE_BYTES + 1)))
    except Exception:
        sys.exit(1)
