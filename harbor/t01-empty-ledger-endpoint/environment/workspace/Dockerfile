# The base image by digest and every Python package by hash (requirements.txt), so AgentEnv and the Harbor package
# build the same server.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MCP_PORT=18765
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements.txt
COPY awsbench_workspace /app/awsbench_workspace
EXPOSE 18765
CMD ["python", "-m", "awsbench_workspace.server"]
