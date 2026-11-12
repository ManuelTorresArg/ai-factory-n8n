FROM python:3.12-slim

# git para worktrees; nodejs opcional si tus proyectos son JS (los tests corren DENTRO de este contenedor)
ARG WITH_NODE=false
ARG WITH_PLAYWRIGHT=false
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && if [ "$WITH_NODE" = "true" ]; then apt-get install -y --no-install-recommends nodejs npm; fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$WITH_PLAYWRIGHT" = "true" ]; then pip install --no-cache-dir playwright && playwright install --with-deps chromium; fi

COPY app ./app
COPY tests ./tests

# El contenedor corre con tu UID (ver compose): HOME escribible y git sin quejarse de "dubious ownership"
ENV HOME=/tmp PYTHONUNBUFFERED=1 \
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=*

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
