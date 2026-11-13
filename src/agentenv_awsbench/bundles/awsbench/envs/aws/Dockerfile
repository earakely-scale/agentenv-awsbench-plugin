FROM python:3.12-slim

ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates dnsutils \
    && arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && curl -fsSL -o /tmp/aws-emulator.deb \
       "https://downloads.aws.com/aws-emulator/packages/aws-emulator/LTS/latest/aws-emulator_${arch}.deb" \
    && apt-get install -y --no-install-recommends /tmp/aws-emulator.deb \
    && rm -rf /tmp/aws-emulator.deb /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY awsbench_aws /app/awsbench_aws

ENV MCP_PORT=18765 \
    PYTHONUNBUFFERED=1 \
    AWSBENCH_EMULATOR_HOME=/data/emulator
RUN mkdir -p /data/emulator && chmod -R 777 /data
EXPOSE 18765
CMD ["python", "-m", "awsbench_aws.server"]
