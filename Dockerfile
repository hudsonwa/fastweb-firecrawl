# fastweb-firecrawl image: the API (default command) and, with `python -m fastweb_firecrawl.egress`, the browser's egress proxy.
# Build context: the repository root (see .dockerignore).
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# glibc: few arenas, give freed memory back early; extraction itself runs in recycled processes.
ENV MALLOC_ARENA_MAX=2 MALLOC_TRIM_THRESHOLD_=131072 MALLOC_MMAP_THRESHOLD_=131072
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -r -u 10001 fastweb
COPY pyproject.toml README.md LICENSE ./
COPY fastweb_firecrawl ./fastweb_firecrawl
# The pinned requirements are already installed; this only installs the package itself.
RUN pip install --no-cache-dir --no-deps . && rm -rf /app/fastweb_firecrawl /app/build
USER fastweb
EXPOSE 3002
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3002/health', timeout=4)"
CMD ["uvicorn", "fastweb_firecrawl.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "3002", "--no-access-log", \
     "--limit-concurrency", "512"]
