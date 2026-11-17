FROM python:3.13-slim-bookworm
WORKDIR /opt/zhiyu
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY data ./data
COPY web ./web
COPY zhiyu ./zhiyu
COPY scripts/models.py ./scripts/models.py
RUN python scripts/models.py
EXPOSE 8765/tcp
CMD ["python", "-m", "uvicorn", "zhiyu.app:api", "--host", "0.0.0.0", "--port", "8765"]
