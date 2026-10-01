#!/usr/bin/env bash
#
# 软考报名提醒 —— 一键部署到 Ubuntu / Debian 服务器
#
# 用法：
#   sudo bash deploy/setup.sh ruankao.example.com you@example.com
#
# 用 bash 显式调用，不要依赖可执行位——脚本从 Windows 拷过来时那个位常常会丢。
#
# 参数：
#   $1  站点域名（必填）
#   $2  证书到期提醒邮箱（必填，Let's Encrypt 用来在证书过期前通知你）
#
# 这个脚本会做：
#   1. 装依赖（nginx / certbot / python3-venv）
#   2. 建独立运行用户，把代码放到 /srv/ruankao，建虚拟环境装依赖
#   3. 生成 .env（SECRET_KEY 与 ADMIN_TOKEN 用随机值）
#   4. 写 nginx 站点配置（不影响机器上已有的其它站点）
#   5. 用 Let's Encrypt 免费证书签发 HTTPS 并配好自动跳转
#   6. 装 systemd 服务并启动
#   7. 跑一次体检
#
# 可重复执行：已有的 .env、已有的证书都不会被覆盖。
#
# 唯一需要你事后手工填的是 .env 里的 MAIL_* —— 没有它邮件发不出去。

set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"
APP_DIR="/srv/ruankao"
SERVICE_USER="ruankao"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
step()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

if [[ -z "$DOMAIN" || -z "$EMAIL" ]]; then
    red "用法：sudo $0 <域名> <证书通知邮箱>"
    red "例如：sudo $0 ruankao.example.com you@example.com"
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    red "需要 root：用 sudo 运行。"
    exit 1
fi

if [[ "$DOMAIN" == *"_"* ]]; then
    red "域名里不能有下划线（RFC 1123）。若是从别处复制的，请换成连字符版本。"
    exit 1
fi

# ---------------------------------------------------------------------------
step "1/7 检查前置条件"

# 域名必须已经解析到本机，否则 ACME 校验必然失败
RESOLVED="$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1 || true)"
if [[ -z "$RESOLVED" ]]; then
    red "域名 $DOMAIN 解析不出地址。请先在 DNS 服务商添加 A 记录，等生效后再跑。"
    exit 1
fi
green "  $DOMAIN → $RESOLVED"

# 80 端口必须能从公网访问，HTTP-01 校验走它
if ! curl -sS -o /dev/null --max-time 10 "http://$DOMAIN/" 2>/dev/null; then
    red "无法通过 http://$DOMAIN/ 访问到本机。"
    red "可能原因：安全组没放行 80 端口、或服务器在境内但域名未完成 ICP 备案。"
    red "（境内服务器未备案时 80/443 会被拦截，这种情况必须先备案。）"
    exit 1
fi
green "  80 端口可达，Let's Encrypt 的 HTTP-01 校验可用"

# ---------------------------------------------------------------------------
step "2/7 安装依赖"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx python3-venv python3-pip certbot python3-certbot-nginx curl
green "  nginx $(nginx -v 2>&1 | grep -oE '[0-9.]+' | head -1) / certbot $(certbot --version 2>&1 | grep -oE '[0-9.]+' | head -1)"

# ---------------------------------------------------------------------------
step "3/7 布置应用"

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    green "  已创建运行用户 $SERVICE_USER"
else
    green "  运行用户 $SERVICE_USER 已存在"
fi

mkdir -p "$APP_DIR"
# 只同步代码与模板，不覆盖已有的 .env 和 data/
for item in ruankao tests cli.py run.py requirements.txt; do
    rm -rf "${APP_DIR:?}/$item"
    cp -r "$REPO_DIR/$item" "$APP_DIR/"
done
green "  代码已同步到 $APP_DIR"

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    python3 -m venv "$APP_DIR/.venv"
    green "  已创建虚拟环境"
fi
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
green "  依赖已安装"

# ---------------------------------------------------------------------------
step "4/7 生成配置"

if [[ ! -f "$APP_DIR/.env" ]]; then
    cp "$REPO_DIR/.env.example" "$APP_DIR/.env"
    SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
    ADMIN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
    sed -i "s|^SITE_URL=.*|SITE_URL=https://$DOMAIN|" "$APP_DIR/.env"
    sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$SECRET|"     "$APP_DIR/.env"
    sed -i "s|^ADMIN_TOKEN=.*|ADMIN_TOKEN=$ADMIN|"    "$APP_DIR/.env"
    sed -i "s|^ALERT_EMAIL=.*|ALERT_EMAIL=$EMAIL|"    "$APP_DIR/.env"
    green "  已生成 .env（SECRET_KEY 与 ADMIN_TOKEN 为随机值）"
    green "  后台口令：$ADMIN    ← 请抄下来，后台登录要用"
    green "  ⚠ 还差 MAIL_* 未填，邮件此时发不出去"
else
    green "  .env 已存在，保持不动"
fi

mkdir -p "$APP_DIR/data"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"
green "  data/ 已就绪，.env 权限收紧为 600"

# ---------------------------------------------------------------------------
step "5/7 配置 nginx"

NGINX_SITE="/etc/nginx/sites-available/ruankao-$DOMAIN"
# 示例配置只监听 80，443 交给 certbot 补——这样中间不会出现「证书还没有
# 但配置里已经写了 ssl」导致 nginx 起不来的窗口
sed -e "s|ruankao\.agentctxs\.com|$DOMAIN|g" \
    -e "s|/srv/ruankao|$APP_DIR|g" \
    "$REPO_DIR/deploy/nginx.conf.example" > "$NGINX_SITE"

ln -sf "$NGINX_SITE" "/etc/nginx/sites-enabled/ruankao-$DOMAIN"
nginx -t
systemctl reload nginx
green "  站点已启用（80 端口）"

# ---------------------------------------------------------------------------
step "6/7 申请 Let's Encrypt 免费证书（90 天，自动续期）"

if [[ -d "/etc/letsencrypt/live/$DOMAIN" ]]; then
    green "  证书已存在，跳过签发"
else
    certbot --nginx -d "$DOMAIN" \
        --non-interactive --agree-tos --redirect \
        --email "$EMAIL" \
        --keep-until-expiring
    green "  证书已签发"
fi

# 放开 443 的 server 块。certbot 已经把证书路径写进了它自己生成的那份配置，
# 这里以 certbot 的结果为准，不再覆盖。
nginx -t && systemctl reload nginx
green "  HTTPS 已生效"

# 续期是 certbot 的 systemd timer 自动做的，验证一下它确实在工作
systemctl list-timers --all 2>/dev/null | grep -q certbot \
    && green "  自动续期定时器已就绪" \
    || echo "  提示：未发现 certbot 定时器，请确认 certbot.timer 已启用"

# ---------------------------------------------------------------------------
step "7/7 启动服务并体检"

sed "s|/srv/ruankao|$APP_DIR|g; s|^User=.*|User=$SERVICE_USER|; s|^Group=.*|Group=$SERVICE_USER|" \
    "$REPO_DIR/deploy/ruankao.service" > /etc/systemd/system/ruankao.service
systemctl daemon-reload
systemctl enable --now ruankao
sleep 2
systemctl --no-pager --lines=0 status ruankao | head -5

echo
# 用服务用户跑体检，避免在 data/ 里留下 root 属主的文件——
# 那种文件会让服务启动时因权限不足而失败
runuser -u "$SERVICE_USER" -- bash -c \
    "cd '$APP_DIR' && '$APP_DIR/.venv/bin/python' cli.py doctor" 2>&1 \
    | grep -vE '^20[0-9]{2}-' | head -20 || true

cat <<EOF

$(green "部署完成")

  站点        https://$DOMAIN
  后台        https://$DOMAIN/admin
  后台口令    见 $APP_DIR/.env 的 ADMIN_TOKEN
  应用目录    $APP_DIR
  日志        journalctl -u ruankao -f

$(red "还差两件事，否则功能不完整：")

  1. 填 $APP_DIR/.env 里的 MAIL_* （SMTP 授权码，不是登录密码），
     然后 systemctl restart ruankao
  2. 在 DNS 侧配好 SPF / DKIM / DMARC —— 不配的话邮件大概率进垃圾箱，
     而这个服务一旦进垃圾箱就等于不存在

证书续期是自动的，但建议每月顺手确认一次：
  certbot renew --dry-run
EOF
