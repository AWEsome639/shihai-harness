# Python base plus environment/requirements.txt, the evaluator's requirements; the
# world is staged at /app/files and /filesystem, and WORKDIR is the world root. environment/ is the build context.
FROM python:3.11-slim

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY filesystem/ /app/files/
RUN ln -s /app/files /filesystem

WORKDIR /app/files/dc_model__hall_seed000_d000__mep_routing
