#!/usr/bin/env bash
# c5bot 一条命令安装 / 更新，在服务器上以 root 跑：
#   curl -sSL https://raw.githubusercontent.com/<账号>/c5/main/deploy/install.sh | sudo bash -s -- install
#   curl -sSL https://raw.githubusercontent.com/<账号>/c5/main/deploy/install.sh | sudo bash -s -- upgrade
# 其它子命令：check（连通性）/ status / logs / restart / stop
#
# 可选环境变量（写在命令前面，如 GHCR_USER=x GHCR_TOKEN=y sudo -E bash -s -- install）：
#   C5BOT_REPO     GitHub 仓库 owner/repo，默认下面的 REPO
#   C5BOT_BRANCH   分支，默认 main
#   C5BOT_DIR      安装目录，默认 /opt/c5bot
#   C5BOT_IMAGE    镜像，默认 ghcr.io/<owner>/c5bot:latest，install 时写进 .env，之后以 .env 为准
#   GHCR_USER / GHCR_TOKEN   镜像是私有包时用来 docker login（token 只需 read:packages）
#   GITHUB_TOKEN   仓库是私有的时候用来下载文件（curl 本脚本时也要带 -H "Authorization: token ..."）
set -euo pipefail

REPO="${C5BOT_REPO:-CHANGE_ME/c5}"   # <- 推到 GitHub 前改成你的仓库
BRANCH="${C5BOT_BRANCH:-main}"
DIR="${C5BOT_DIR:-/opt/c5bot}"
RAW="https://raw.githubusercontent.com/${REPO}/${BRANCH}"
OWNER_LC=$(printf '%s' "${REPO%%/*}" | tr '[:upper:]' '[:lower:]')
IMAGE="${C5BOT_IMAGE:-ghcr.io/${OWNER_LC}/c5bot:latest}"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 跑（... | sudo bash -s -- ...）"
case "$REPO" in CHANGE_ME/*) die "install.sh 里的 REPO 还是占位符，改成你的 GitHub 仓库再推" ;; esac

fetch() {  # fetch <仓库根目录下的文件名>
  local url="$RAW/$1" dest="$DIR/$1"
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    curl -fsSL -H "Authorization: token $GITHUB_TOKEN" "$url" -o "$dest"
  else
    curl -fsSL "$url" -o "$dest"
  fi || die "下载失败: $url（仓库是私有的话要设 GITHUB_TOKEN）"
}

need_docker() {
  if ! docker compose version >/dev/null 2>&1; then
    say "没有 docker compose，用官方脚本安装 Docker"
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker >/dev/null 2>&1 || true
  fi
}

compose() { docker compose --project-directory "$DIR" "$@"; }

login_registry() {
  if [ -n "${GHCR_USER:-}" ] && [ -n "${GHCR_TOKEN:-}" ]; then
    local registry="${IMAGE%%/*}"
    say "登录 $registry"
    printf '%s' "$GHCR_TOKEN" | docker login "$registry" -u "$GHCR_USER" --password-stdin
  fi
}

cmd_install() {
  need_docker
  mkdir -p "$DIR/data"
  say "下载 compose 和配置模板到 $DIR"
  fetch compose.yaml
  fetch .env.example
  fetch watchlist.example.toml
  if [ ! -f "$DIR/.env" ]; then
    cp "$DIR/.env.example" "$DIR/.env"
    chmod 600 "$DIR/.env"
  fi
  grep -q '^C5BOT_IMAGE=' "$DIR/.env" || printf '\nC5BOT_IMAGE=%s\n' "$IMAGE" >> "$DIR/.env"
  [ -f "$DIR/watchlist.toml" ] || cp "$DIR/watchlist.example.toml" "$DIR/watchlist.toml"
  login_registry
  say "拉镜像"
  compose pull c5bot
  if grep -Eq '^C5_APP_KEY=\S' "$DIR/.env"; then
    compose up -d
    say "已启动。看板：ssh -L 8766:127.0.0.1:8766 <服务器>，然后本地打开 http://127.0.0.1:8766"
  else
    say "镜像就绪，还没启动。接下来："
    echo "  1. 编辑 $DIR/.env，填 C5_APP_KEY（实盘再填交易链接、预算等）"
    echo "  2. 编辑 $DIR/watchlist.toml"
    echo "  3. 再跑一次本命令，子命令换成 upgrade，就会启动"
  fi
}

cmd_upgrade() {
  [ -f "$DIR/.env" ] || die "$DIR 还没安装，先跑 install"
  fetch compose.yaml   # compose.yaml 由脚本管理，每次升级覆盖；自定义都放 .env
  login_registry
  say "拉新镜像"
  compose pull c5bot
  say "重建容器"
  compose up -d --no-deps --force-recreate c5bot
  docker image prune -f >/dev/null
  compose ps
  compose logs --tail 20 c5bot
}

cmd_check()   { compose run --rm -T c5bot python -m c5bot check; }
cmd_status()  { compose ps; }
cmd_logs()    { compose logs -f --tail 100 c5bot; }
cmd_restart() { compose up -d --force-recreate c5bot; }
cmd_stop()    { compose down; }

case "${1:-}" in
  install) cmd_install ;;
  upgrade) cmd_upgrade ;;
  check)   cmd_check ;;
  status)  cmd_status ;;
  logs)    cmd_logs ;;
  restart) cmd_restart ;;
  stop)    cmd_stop ;;
  *) echo "用法: ... | sudo bash -s -- install | upgrade | check | status | logs | restart | stop" >&2; exit 1 ;;
esac
