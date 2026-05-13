FROM python:3.11-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests

RUN uv sync --no-dev

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "competitionops.main:app", "--host", "0.0.0.0", "--port", "8000"]
