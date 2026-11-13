FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MCP_PORT=18765
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY awsbench_workspace /app/awsbench_workspace
EXPOSE 18765
CMD ["python", "-m", "awsbench_workspace.server"]
