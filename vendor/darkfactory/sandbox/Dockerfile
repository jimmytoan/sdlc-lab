# Sandbox for the coding agents and for grading. No network at run time.
FROM python:3.13-slim
RUN pip install --no-cache-dir pytest==8.4.2 pytest-timeout==2.4.0 && useradd -m -u 1000 dev
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER dev
WORKDIR /work
