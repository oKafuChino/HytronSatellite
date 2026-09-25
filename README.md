# HytronSatellite

HytronSatellite 是一个运行在 Ubuntu Docker 环境中的私人 Telegram Bot，用于监控 [HyVPS](https://hyvps.hytron.io) 节点库存。

它会定时读取 HyVPS 库存接口，并在你加入白名单的节点发生“有货/无货”变化时，通过 Telegram 私聊通知你。

## 功能特点

- 只有你指定的 Telegram 用户可以使用 Bot。
- 只接受 Telegram 私聊，群聊中的命令会被忽略。
- 默认不会主动推送任何库存变化。
- 只有加入白名单的节点才会被监控。
- 白名单和库存快照保存在 SQLite 数据库中，容器重启后不会丢失。
- 使用 Telegram 长轮询，不需要配置域名、HTTPS 或公网端口。
- 使用 Docker Compose 部署，支持自动重启。

## 一、准备条件

你需要准备：

- 一台 Ubuntu 服务器，建议 Ubuntu 22.04 或更新版本。
- 服务器可以访问互联网。
- 一个 Telegram 账号。
- 一个 GitHub 仓库地址，或者直接下载本项目代码。

建议为项目使用单独目录，例如 `/opt/hytronsatellite`。

## 二、创建 Telegram Bot

1. 在 Telegram 中搜索 `@BotFather`。
2. 发送 `/newbot`。
3. 按提示输入 Bot 名称和用户名。
4. BotFather 会返回一串 Token，格式类似：

   ```text
   123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```

5. 保存这个 Token。

Token 相当于 Bot 的密码，不要提交到 GitHub，也不要发送给其他人。

## 三、获取你的 Telegram 用户 ID

可以使用 Telegram 中的 `@userinfobot`：

1. 搜索并打开 `@userinfobot`。
2. 发送 `/start`。
3. 记录它返回的数字 `Id`。

这个数字就是后面 `.env` 文件中的 `OWNER_TELEGRAM_ID`。

不要填写 Telegram 用户名，必须填写数字用户 ID。

## 四、安装 Docker

在 Ubuntu 服务器执行：

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin git
sudo systemctl enable --now docker
```

确认 Docker 可用：

```bash
docker --version
docker compose version
```

如果希望当前用户不使用 `sudo` 执行 Docker，可以运行：

```bash
sudo usermod -aG docker "$USER"
```

执行后退出 SSH 并重新登录一次，再检查：

```bash
docker ps
```

## 五、下载项目

如果项目已经上传到 GitHub，执行：

```bash
sudo mkdir -p /opt/hytronsatellite
sudo chown -R "$USER":"$USER" /opt/hytronsatellite
cd /opt/hytronsatellite
git clone https://github.com/你的用户名/你的仓库名.git .
```

如果代码已经通过其他方式上传到服务器，直接进入项目目录即可：

```bash
cd /opt/hytronsatellite
```

确认目录中存在以下文件：

```text
Dockerfile
compose.yaml
.env.example
requirements.txt
src/
```

## 六、配置环境变量

复制配置文件：

```bash
cp .env.example .env
nano .env
```

填写以下内容：

```dotenv
TELEGRAM_BOT_TOKEN=从BotFather获取的Token
OWNER_TELEGRAM_ID=你的数字Telegram用户ID
POLL_INTERVAL_SECONDS=60
DATABASE_PATH=/data/hytron.db
LOG_LEVEL=INFO
```

参数说明：

| 参数 | 说明 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather 创建 Bot 时返回的 Token |
| `OWNER_TELEGRAM_ID` | 你的 Telegram 数字用户 ID |
| `POLL_INTERVAL_SECONDS` | 库存检查间隔，默认 60 秒，最小 15 秒 |
| `DATABASE_PATH` | 容器内数据库路径，保持 `/data/hytron.db` 即可 |
| `LOG_LEVEL` | 日志级别，通常使用 `INFO` |

保护配置文件：

```bash
chmod 600 .env
```

## 七、启动 Bot

首次使用 bind mount 时，需要让容器用户 `10001` 拥有数据目录的读写权限。请先执行：

```bash
mkdir -p data
sudo chown -R 10001:10001 data
sudo chmod 750 data
```

如果之前使用 `sudo docker compose` 启动过，`data` 目录或数据库文件可能属于 root；上面的命令可以修复这种情况。

在项目目录执行：

```bash
docker compose up -d --build
```

查看容器状态：

```bash
docker compose ps
```

查看实时日志：

```bash
docker compose logs -f --tail=100
```

如果看到容器持续运行，并且没有反复出现错误，说明 Bot 已经启动。

按 `Ctrl+C` 退出日志查看，不会停止 Bot。

## 八、首次使用

在 Telegram 中打开你刚创建的 Bot 私聊，依次发送：

```text
/start
/nodes
```

`/nodes` 会返回 HyVPS 节点名称、地区和库存状态，例如：

```text
有货 | HKG | Hytron-HK-BDXHK2-1-Server-1
```

复制需要监控的完整节点名称，然后发送：

```text
/watch 节点名称
```

例如：

```text
/watch Hytron-HK-BDXHK2-1-Server-1
```

添加成功后，Bot 会记录当前状态作为初始基线。之后只有该节点从“无货”变为“有货”，或从“有货”变为“无货”时，才会主动通知你。

添加节点时不会把当前状态重复当作库存变化通知。

## 九、常用命令

| 命令 | 功能 |
|---|---|
| `/start` | 显示帮助信息 |
| `/nodes` | 获取最新节点列表和库存状态 |
| `/watch <节点名称>` | 将节点加入白名单并建立初始基线 |
| `/unwatch <节点名称>` | 从白名单移除节点 |
| `/watchlist` | 查看当前白名单 |
| `/status` | 查看运行状态、白名单数量和最近检查时间 |
| `/pause` | 暂停主动库存通知，但保留白名单 |
| `/resume` | 恢复通知并重新建立库存基线 |

白名单为空时，Bot 不会主动推送库存变化。

## 十、更新 Bot

进入项目目录，拉取最新代码并重新构建：

```bash
cd /opt/hytronsatellite
git pull
docker compose up -d --build
```

查看更新后的日志：

```bash
docker compose logs -f --tail=100
```

数据库位于项目目录的 `data/hytron.db`，更新代码不会删除它。

## 十一、备份和恢复

备份数据库：

```bash
cd /opt/hytronsatellite
cp data/hytron.db "data/hytron.db.$(date +%Y%m%d-%H%M%S).backup"
```

恢复数据库前先停止容器：

```bash
docker compose down
cp data/hytron.db.备份文件 data/hytron.db
docker compose up -d
```

建议定期将备份复制到服务器之外保存。

## 十二、停止和重启

停止 Bot：

```bash
docker compose down
```

启动 Bot：

```bash
docker compose up -d
```

重启 Bot：

```bash
docker compose restart
```

查看容器资源占用：

```bash
docker stats hytron-satellite-bot
```

## 十三、常见问题

### 1. 容器启动后马上退出

查看完整日志：

```bash
docker compose logs --tail=200
```

最常见原因是 `.env` 中的 Token 或用户 ID 未填写，或者用户 ID 不是数字。

如果日志中出现 `sqlite3.OperationalError: unable to open database file`，修复数据目录权限：

```bash
cd /opt/hytronsatellite
sudo mkdir -p data
sudo chown -R 10001:10001 data
sudo chmod 750 data
docker compose up -d --build --force-recreate
```

这里的 `10001` 是镜像中 `botuser` 的用户 ID。不要把数据库目录改成只读。

检查配置文件：

```bash
cat .env
```

不要把 Token 粘贴到公开位置。检查完成后可以清理终端历史记录。

### 2. Bot 不回复我的命令

请确认：

- 你是在 Bot 的私聊窗口中发送命令。
- `OWNER_TELEGRAM_ID` 是你的数字用户 ID。
- `.env` 已经修改并重新执行了 `docker compose up -d`。
- 日志中没有 Telegram 网络错误。

可以查看日志：

```bash
docker compose logs -f --tail=100
```

### 3. Bot 回复 `/start`，但不推送库存

这是默认行为。Bot 只有在你执行 `/watch <节点名称>` 后，才会监控并推送该节点。

检查白名单：

```text
/watchlist
```

### 4. `/nodes` 获取库存失败

检查服务器是否可以访问外网：

```bash
curl -I https://hyvps.hytron.io/api/v1/nodes/catalog/grouped
```

然后查看 Bot 日志：

```bash
docker compose logs --tail=200
```

接口短暂失败时，Bot 会保留上一次有效状态，不会把接口错误误判为“无货”。

### 5. 修改 `.env` 后没有生效

修改环境变量后需要重新创建容器：

```bash
docker compose up -d --force-recreate
```

如果同时修改了代码或依赖，使用：

```bash
docker compose up -d --build --force-recreate
```

## 十四、安全注意事项

- 不要将 `.env` 提交到 GitHub。
- 不要将 Bot Token 写入 README、Issue、日志或截图。
- 不要公开 `data/hytron.db`，里面包含白名单和运行状态。
- 建议服务器使用 SSH 密钥登录，并关闭密码登录。
- 定期更新 Ubuntu、Docker 和项目代码。
- 如果 Token 泄露，立即在 BotFather 中使用 `/revoke` 生成新 Token，然后更新 `.env` 并重启容器。

项目中的 `.gitignore` 已默认忽略 `.env` 和 `data/`。
