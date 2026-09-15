FROM python:3.12-slim@sha256:9d3abd9fc11d06998ccdbdd93b4dd49b5ad7d67fcbbc11c016eb0eb2c2194891
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /requirements.txt
ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 HOME=/tmp/home HERMES_HOME=/tmp/home/.hermes TIRITH_ENABLED=false HERMES_DISABLE_LAZY_INSTALL=1
WORKDIR /candidate
