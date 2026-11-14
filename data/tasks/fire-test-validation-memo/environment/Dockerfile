# An office toolchain for the agent; the world is staged at /app/files and /filesystem. environment/ is the build context.
FROM python:3.11-slim@sha256:d1e9ca7c4e78d1e8ecadb5d44bfc8e956e7a65b659a9950f569f243d72b326d0
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-calc libreoffice-writer libreoffice-impress fonts-dejavu-core poppler-utils \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir openpyxl==3.1.5 python-docx==1.2.0 python-pptx==1.0.2 pypdf==6.19.0 pandas==3.0.6 \
        numpy==2.4.6 lxml==6.1.3 Pillow==12.3.0 XlsxWriter==3.2.9 et-xmlfile==2.0.0 python-dateutil==2.9.0.post0 \
        six==1.17.0 typing_extensions==4.16.0

COPY filesystem/ /app/files/
RUN ln -s /app/files /filesystem

WORKDIR /app/files
