#!/usr/bin/env bash
# Development only: run the pinned upstream installer in a dedicated kind cluster.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REVISION="$(cat "$ROOT/deploy/substrate/upstream-revision")"
STATE="${COMPANEST_SUBSTRATE_STATE_DIR:-$ROOT/.companest/substrate}"
mkdir -p "$STATE"
STATE="$(cd "$STATE" && pwd)"
UPSTREAM="$STATE/upstream"
export KUBECONFIG="$STATE/kubeconfig"
export KIND_CLUSTER_NAME=companest-substrate
export ATE_INSTALL_KIND=true
export GOPATH="$STATE/go"
export GOCACHE="$STATE/go-cache"
export GOBIN="$STATE/bin"
export GOMAXPROCS="${GOMAXPROCS:-2}"

for tool in git go docker kubectl; do
    command -v "$tool" >/dev/null || { echo "Missing required tool: $tool" >&2; exit 1; }
done
docker version --format '{{.Server.Version}}'

if [[ ! -d "$UPSTREAM" ]]; then
    git clone --filter=blob:none --no-checkout https://github.com/agent-substrate/substrate.git "$UPSTREAM"
    git -C "$UPSTREAM" checkout --detach "$REVISION"
fi
[[ "$(git -C "$UPSTREAM" rev-parse HEAD)" == "$REVISION" ]] || {
    echo "Upstream checkout does not match upstream-revision; use a fresh state directory." >&2
    exit 1
}
[[ -z "$(git -C "$UPSTREAM" status --porcelain)" ]] || {
    echo "Upstream checkout has local changes; refusing to deploy." >&2
    exit 1
}
cd "$UPSTREAM"
# Download/check the toolchain before creating infrastructure.
go version

nodes="$(docker ps -a --filter "label=io.x-k8s.kind.cluster=$KIND_CLUSTER_NAME" --format '{{.Names}}')"
if [[ -z "$nodes" ]]; then
    # The upstream script replaces a same-named cluster and can replace its
    # fixed-name registry. Refuse either collision before calling it.
    registry="$(docker ps -a --filter 'name=^/kind-registry$' --format '{{.Names}}')"
    [[ -z "$registry" ]] || {
        echo "kind-registry already exists; inspect it before using the upstream creation script." >&2
        exit 1
    }
    hack/create-kind-cluster.sh
else
    [[ -f "$KUBECONFIG" ]] || {
        echo "Cluster already exists without this state directory's kubeconfig; refusing to replace it." >&2
        exit 1
    }
    kubectl --context="kind-$KIND_CLUSTER_NAME" get nodes
fi

# A Go installation running through Rosetta can report amd64 on an arm64
# Docker daemon. Build for the actual kind node, not the installer's GOARCH.
NODE_ARCH="$(kubectl --context="kind-$KIND_CLUSTER_NAME" get nodes -o jsonpath='{.items[0].status.nodeInfo.architecture}')"
case "$NODE_ARCH" in
    amd64|arm64) ;;
    *) echo "Unsupported kind node architecture: $NODE_ARCH" >&2; exit 1 ;;
esac

go run ./cmd/ate-setup deploy ate-system \
    --ko-default-platforms="linux/$NODE_ARCH" \
    --sandbox-classes=gvisor \
    --credential-provider='{"name":"k8s.io"}' \
    --ingress-allowed-spiffe-ids=spiffe://cluster.local/ns/ate-system/sa/atenet-client
go run ./cmd/ate-setup deploy demo counter --ko-default-platforms="linux/$NODE_ARCH"
go install ./cmd/kubectl-ate
echo "Substrate and the upstream counter demo installed in kind-$KIND_CLUSTER_NAME."
