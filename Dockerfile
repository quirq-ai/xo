# The xo server (`xo serve`). Nothing here deploys it: run the image on a platform that terminates
# HTTPS in front of it and gives it CLERK_SECRET_KEY at run time. See docs/server.md.
# TODO(suraj): pin the base image by digest once the deploy target is chosen.
FROM python:3.14.6-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /app/src

# Never root. CLERK_SECRET_KEY is never an ARG or ENV here: the platform supplies it at run time.
RUN useradd --system --uid 10001 --no-create-home xo
USER xo

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4)"]
# 0.0.0.0 only inside the container; the HTTPS proxy in front is the only way in.
CMD ["xo", "serve", "--host", "0.0.0.0", "--port", "8787"]
