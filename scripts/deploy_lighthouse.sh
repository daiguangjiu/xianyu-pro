#!/usr/bin/env bash
# 腾讯云轻量应用服务器（Docker 镜像）一键部署。
# 在服务器上执行：bash scripts/deploy_lighthouse.sh [--with-https]
#   --with-https  额外启动 Caddy 自动申请证书（需要有已解析到本机的域名；
#                 大陆地域服务器还需域名已完成 ICP 备案，否则 80/443 会被拦截）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
WITH_HTTPS="no"
[[ "${1:-}" == "--with-https" ]] && WITH_HTTPS="yes"

echo "==> 1/5 检查 Docker 环境"
if ! command -v docker >/dev/null 2>&1; then
    echo "未检测到 docker，正在安装（腾讯云 Docker 基础镜像一般已内置）"
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
fi
if ! docker compose version >/dev/null 2>&1; then
    apt-get update -y && apt-get install -y docker-compose-plugin
fi

echo "==> 2/5 配置腾讯云容器镜像加速（解决拉镜像慢）"
if [[ ! -f /etc/docker/daemon.json ]] || ! grep -q "mirror.ccs.tencentyun.com" /etc/docker/daemon.json 2>/dev/null; then
    mkdir -p /etc/docker
    cat > /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": ["https://mirror.ccs.tencentyun.com"]
}
EOF
    systemctl restart docker || true
fi

echo "==> 3/5 检查 .env"
if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "已生成 .env，请编辑后再重新执行本脚本：vi $ROOT/.env"
    exit 1
fi
# shellcheck disable=SC1091
set -a; source .env; set +a
missing=()
[[ -z "${POSTGRES_PASSWORD:-}" ]] && missing+=("POSTGRES_PASSWORD")
[[ -z "${ADMIN_USERNAME:-}" || -z "${ADMIN_PASSWORD:-}" ]] && missing+=("ADMIN_USERNAME/ADMIN_PASSWORD")
[[ "$WITH_HTTPS" == "yes" && -z "${DOMAIN:-}" ]] && missing+=("DOMAIN")
if ((${#missing[@]})); then
    echo "以下配置缺失，请先写入 .env：${missing[*]}"
    exit 1
fi

echo "==> 4/5 构建并启动"
if [[ "$WITH_HTTPS" == "yes" ]]; then
    docker compose --profile https up -d --build
else
    docker compose up -d --build
fi

echo "==> 5/5 等待服务就绪"
for i in {1..30}; do
    if curl -sf -o /dev/null -u "${ADMIN_USERNAME}:${ADMIN_PASSWORD}" http://127.0.0.1:8000/api/health; then
        break
    fi
    sleep 2
done

docker compose ps
PUBLIC_IP="$(curl -sf --max-time 5 http://metadata.tencentyun.com/latest/meta-data/public-ipv4 2>/dev/null || true)"
echo
echo "部署完成。接下来请确认："
echo "  1) 腾讯云控制台 → 防火墙，放行 8000${WITH_HTTPS:+、80、443} 端口"
[[ -n "$PUBLIC_IP" ]] && echo "  2) 访问地址：http://$PUBLIC_IP:8000 （账号 $ADMIN_USERNAME）"
echo "  3) .env 里 PUBLIC_BASE_URL 改成实际访问地址，飞书回调填 <访问地址>/feishu/event"
[[ "$WITH_HTTPS" == "yes" ]] && echo "  4) 域名需已备案并解析到本机，否则 80/443 会被拦截"
echo "  5) 配置备份：0 3 * * * $ROOT/scripts/backup.sh >> /var/log/orderhub-backup.log 2>&1"
