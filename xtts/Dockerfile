# GPU sidecar for Vexa's multilingual voice (Coqui XTTS-v2).
# Torch + CUDA come from the base image; only the TTS stack is layered on top.
FROM pytorch/pytorch:2.5.1-cuda12.1-cudnn9-runtime

ENV PYTHONUNBUFFERED=1 \
    # Model weights land here, on a named volume, so a rebuilt container does not
    # re-download ~2 GB from Hugging Face.
    TTS_HOME=/models \
    HF_HOME=/models/hf \
    NUMBA_CACHE_DIR=/tmp/numba

RUN apt-get update && apt-get install -y --no-install-recommends \
    espeak-ng \
    libsndfile1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "coqui-tts>=0.25.1" \
    # coqui-tts asks for transformers>=4.57 but does not cap it, and 5.x dropped
    # `isin_mps_friendly`, which XTTS's GPT2 inference still imports — without the
    # cap the model fails to load with an ImportError.
    "transformers>=4.57,<5" \
    "fastapi>=0.100.0" \
    "uvicorn>=0.22.0" \
    "anyio>=4.0.0" \
    # Renders the English reference clip XTTS clones its voice from, so the
    # multilingual voice matches the Piper voice the stack already used.
    "piper-tts>=1.6.0"

COPY xtts/server.py /srv/server.py

EXPOSE 8700

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=600s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8700/health', timeout=3).read()"

CMD ["uvicorn", "server:app", "--app-dir", "/srv", "--host", "0.0.0.0", "--port", "8700", "--no-access-log"]
