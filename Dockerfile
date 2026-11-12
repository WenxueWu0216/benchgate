# One image per commit: the C++ workloads built at that commit plus the
# Python runner/orchestrator. Bench pods and the orchestrator share it.
FROM ubuntu:24.04 AS build
RUN apt-get update && apt-get install -y --no-install-recommends cmake g++ make \
    && rm -rf /var/lib/apt/lists/*
COPY workloads /src/workloads
RUN cmake -S /src/workloads -B /build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /build -j"$(nproc)" \
    && cd /build && ctest --output-on-failure

FROM python:3.12-slim
ARG KUBECTL_VERSION=v1.31.2
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && apt-get purge -y curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY benchgate ./benchgate
COPY trace ./trace
RUN pip install --no-cache-dir .
COPY --from=build /build/benchgate_workloads /build/trace_parser /opt/benchgate/bin/
ENV BENCHGATE_BINARY=/opt/benchgate/bin/benchgate_workloads \
    BENCHGATE_TRACE_PARSER=/opt/benchgate/bin/trace_parser \
    PYTHONUNBUFFERED=1
RUN useradd -u 10001 -m benchgate
USER 10001
ENTRYPOINT ["python", "-m", "benchgate"]
CMD ["serve"]
