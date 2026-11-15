FROM golang:1.27.3 AS substrate-cli
ARG SUBSTRATE_REVISION=a46464663fa076221ae059e1fd4a273edd2d8b09
ENV GOMAXPROCS=2
RUN git clone --filter=blob:none https://github.com/agent-substrate/substrate.git /src
WORKDIR /src
RUN git checkout "$SUBSTRATE_REVISION" \
    && CGO_ENABLED=0 go build -p=2 -o /out/kubectl-ate ./cmd/kubectl-ate

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY companest ./companest
RUN pip install --no-cache-dir ".[server,gateway,scheduler,s3]" python-telegram-bot
COPY --from=substrate-cli /out/kubectl-ate /usr/local/bin/kubectl-ate
EXPOSE 8000
ENTRYPOINT ["python", "-m", "companest"]
CMD ["serve", "-c", "/etc/companest/config.md"]
