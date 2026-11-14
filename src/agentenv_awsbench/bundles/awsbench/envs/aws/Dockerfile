# Everything the server runs is pinned, so AgentEnv and the Harbor package build the same one: the base image by digest,
# the AWS Emulator by sha256 and every Python package by hash (requirements.txt). AWS serves the emulator only at
# LTS/latest, so a build stops when that URL no longer serves the pinned package; moving the pin means requalifying the
# tasks that run this server (T01's QC in harbor/qc).
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ARG TARGETARCH
ARG AWS_EMULATOR_VERSION=1.0.353-1
ARG AWS_EMULATOR_SHA256_AMD64=d457c3057273b5dcb2017c187c1b85fdccc1e0dcbe874455be39def857d4a2b6
ARG AWS_EMULATOR_SHA256_ARM64=e1cd7e84b4cf3856d5d87912380dc34f4747c19b77b949502fa10fc52cec3cb7
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates dnsutils \
    && arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && case "$arch" in \
         amd64) want="$AWS_EMULATOR_SHA256_AMD64" ;; \
         arm64) want="$AWS_EMULATOR_SHA256_ARM64" ;; \
         *) echo "no pinned AWS Emulator for $arch" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /tmp/aws-emulator.deb \
       "https://downloads.aws.com/aws-emulator/packages/aws-emulator/LTS/latest/aws-emulator_${arch}.deb" \
    && got="$(sha256sum /tmp/aws-emulator.deb | cut -d' ' -f1)" \
    && if [ "$got" != "$want" ]; then \
         echo "LTS/latest no longer serves AWS Emulator $AWS_EMULATOR_VERSION for $arch (sha256 $got, pinned $want)" >&2; \
         exit 1; \
       fi \
    && apt-get install -y --no-install-recommends /tmp/aws-emulator.deb \
    && test "$(dpkg-query -W -f='${Version}' aws-emulator)" = "$AWS_EMULATOR_VERSION" \
    && rm -rf /tmp/aws-emulator.deb /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes --no-deps -r /app/requirements.txt
COPY awsbench_aws /app/awsbench_aws

ENV MCP_PORT=18765 \
    PYTHONUNBUFFERED=1 \
    AWSBENCH_EMULATOR_HOME=/data/emulator
RUN mkdir -p /data/emulator && chmod -R 777 /data
EXPOSE 18765
CMD ["python", "-m", "awsbench_aws.server"]
