FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates coreutils file jq poppler-utils unzip zip libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/corrino-requirements.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: -r /tmp/corrino-requirements.txt \
    && python -m pip check
COPY . /tests
RUN chmod 0755 /tests/test.sh
WORKDIR /filesystem
