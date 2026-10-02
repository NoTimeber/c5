# C5 武器箱监控扫货

基于 [C5GAME 开放平台](https://opendoc.c5game.com) 的官方接口：轮询监控列表里的箱子，在售最低价跌到目标价以内就自动买入，直到买满数量或用完预算。附带网页看板，能对比 Steam 价算出能拿到几折的 Steam 余额。

## 准备

1. **app-key**：C5GAME 官网 → 个人中心 → API 管理 申请。
2. **IP 白名单**：同一页面把运行脚本的机器的公网 IP 加进白名单。`listing` 策略用到的在售查询是内测接口，必须有白名单。
3. **Steam 交易链接**：收货账号的交易链接。先在官网确认这个 Steam 账号已绑定且状态正常。
4. **余额**：购买走 C5 账户余额，先充值。

## 安装与配置

```bash
cd c5
uv sync
copy .env.example .env
copy watchlist.example.toml watchlist.toml
```

- `.env`：填 `C5_APP_KEY`，其余看文件里的注释。
- `watchlist.toml`：要监控的箱子。`name` 是 Steam 市场英文名，`max_price` 是目标价，`max_qty` 是累计最多买几个。

## 使用

按这个顺序走一遍，每一步都通了再进下一步：

```bash
uv run python -m c5bot check                        # 1. 连通性、app-key、余额、Steam 账号状态
uv run python -m c5bot prices                       # 2. 监控列表当前行情，核对价格和官网一致
uv run python -m c5bot compare                      # 3. C5 价 vs Steam 价，算能拿到几折的 Steam 余额
uv run python -m c5bot listings "Kilowatt Case"     # 4. 目标价以内的在售（验证内测接口权限和白名单）
uv run python -m c5bot run                          # 5. 启动监控和看板。C5_MODE=dry 时只模拟不花钱
uv run python -m c5bot orders                       # 同步订单状态、看购买流水
```

### compare：几折的 Steam 余额

只读，不下单。每个箱子取 C5 在售最低价和 Steam 市场当前最低挂单价（人民币），按 Steam 的手续费算法（Steam 5% + CS2 10%，各按分向下取整、最低 0.01）算出挂这个价卖掉后到手多少，再算 `C5 价 / 到手` 就是花 1 元能换到多少 Steam 余额的成本——`7.35折` 表示 7.35 元换 10 元余额。同时按 `max_price` 算一列，看目标价能拿到几折。

```bash
uv run python -m c5bot compare --every 30           # 每 30 分钟重算一次，结果追加到 data/compare.csv
```

注意：C5 买到的饰品要 7 天后才能挂 Steam 市场卖，折扣是按今天的 Steam 价算的。`--every` 跑几天、看 `compare.csv` 里 Steam 价的波动，能大致估计这 7 天的风险。Steam 市场接口对未登录请求限流很严（约每分钟 20 次），脚本每次请求间隔 3 秒；被 429 后 5 分钟内不再请求 Steam，连续被限流退避时间翻倍（最多 1 小时），期间沿用上次的价。

### 网页看板

`run` 启动时自动开看板，浏览器打开 http://127.0.0.1:8766 ：

- 汇总：已花费 / 预算进度、已买件数、C5 余额（实盘）、Steam 价更新时间、Steam 汇率、目标汇率、当前到价的箱子。
- 监控表：每个箱子的状态（监控中 / 到价 / 冷却 / 已买满 / 无行情 / 等 Steam 价）、C5 最低价、目标价、在售数、求购最高价、Steam 最低价与净到手、按 C5 最低价算的折扣和汇率、按目标价算的折扣、已买 / 上限、已花。Steam 价每 `STEAM_REFRESH_SEC` 秒刷新一次，也会追加到 `data/compare.csv`。
- 购买流水和最近日志。
- 按钮：**暂停扫货 / 继续**（暂停时照常看行情，不下单）、**刷新 Steam 价**、**重载 watchlist**（改了 `watchlist.toml` 后不用重启）、**同步订单**（实盘）。

只想看行情不想买，用 `run --paused` 启动，在看板上点“继续扫货”才开始买。`C5_UI_PORT=0` 关闭看板。

### 目标汇率：目标价随 Steam 价自动算

圈子里说的“汇率”是 1 美元的 Steam 余额花多少人民币。Steam 自己有一个人民币/美元换算率（同一件饰品的人民币价 ÷ 美元价，约等于官方汇率，看板叫 **Steam 汇率**，每次刷新 Steam 价时用一件固定的参照饰品分别查人民币价和美元价算出，默认 AK-47 | Redline (Field-Tested)，可用 `STEAM_RATE_ITEM` 换。参照饰品要贵：Steam 换算后向上取整到分，几十美元的饰品误差在 0.03% 以内，几毛钱的箱子会差百分之几）。你买到余额的实际汇率 = 折 × Steam 汇率。

在看板“监控”栏填 **目标汇率**（比如 `5.20`）点“应用”之后：

- 折扣 = 目标汇率 ÷ Steam 汇率。Steam 汇率 7.2、目标 5.2 就是 7.22 折。
- 每个箱子的目标价 = Steam 最低价扣完手续费的净到手 × 折扣，向下取整到分。Steam 价每次刷新都重算，`watchlist.toml` 里的 `max_price` 不再使用（`max_qty`、`max_spend` 照常生效）。
- 某个箱子还没拿到 Steam 价、或 Steam 价超过 3 个刷新周期没更新成功，它的状态显示“等 Steam 价”，这期间不买它。
- 目标汇率存在 `data/dashboard.json`，重启不丢。点“清除”恢复用 `max_price`。

`compare` 命令也会打印 Steam 汇率和“汇率(C5最低)”列，`compare.csv` 多了 `steam_rate`、`rate_at_lowest`、`rate_at_target` 三列。

### 美元区钱包

卖货的 Steam 账号是美元区就把 `.env` 里 `STEAM_CURRENCY` 改成 `1`。之后 Steam 价、挂单价、净到手都是美元，不再量 Steam 汇率，“折”两列换成“汇率(C5最低)”和“汇率(目标价)”：C5 人民币价 ÷ 到手美元，就是 1 美元余额花多少人民币。目标汇率填 5.20 表示目标价 = 到手美元 × 5.20。登录的 Steam 账号也应是美元区，成交历史才不用换算。

### 登录 Steam：按成交历史定挂单价

默认按 Steam 当前最低挂单价算净到手，适合“挂最低价秒出”。如果你是挂一个高一点的价等成交，在看板的 **Steam 账号** 栏登录 Steam，之后每个箱子的 **挂单价** 按最近 `STEAM_SELL_WINDOW_DAYS`（默认 3）天的成交历史算（就是市场页那张价格图的数据，每小时一个成交中位价和成交量）：把这些小时按价从高到低累计成交量，累计到总量的 `STEAM_SELL_VOLUME_SHARE`（默认 0.3）比例时的价就是挂单价，意思是最近几天有 30% 的成交在这个价或更高价成交，挂上去能走。只取最高价会排队等价格回来，所以不这么做；比例调小价更高但更难卖，调大更稳。净到手、折扣、汇率、按目标汇率算的目标价全部改按挂单价算。监控表“挂单价”列下面标比例和窗口内最高价。

- 登录走 Steam 官方的网页登录接口：输入账号、密码，再输手机令牌 / 邮箱验证码，或直接在手机 Steam App 上点确认。密码只用来向 Steam 换登录令牌，不保存、不进日志。
- 拿到的刷新令牌存在 `data/steam_session.json`（权限 600），访问令牌一天一换，脚本自动续，几个月内不用再登录；过期后看板会提示重新登录。这个文件等于登录态，别外传。
- 查成交历史不需要是收货账号，用一个没有库存和余额的小号最稳妥。成交历史的币种跟登录账号的钱包区走：人民币区直接用；美元区按 Steam 汇率换算成人民币再用（看板会标出原始美元价，美元只到分，换算后精度略低）；其它币种退回按最低价算并标出来。
- 历史拉不到（限流、登录态失效）时退回按最低价算，只会让目标价偏低少买，不会多付。
- 看板没有登录保护，所以这个功能更要求看板只监听 127.0.0.1、远程只走 SSH 隧道。

dry 跑一段时间，确认日志里的模拟买入符合预期后，再在 `.env` 里改成实盘：

```
C5_MODE=live
C5_LIVE_CONFIRM=yes
C5_TRADE_URL=...
C5_MAX_TOTAL_SPEND=500
```

## 工作流程

每轮（默认 3 秒）：

1. 一个请求批量查所有箱子的在售最低价。
2. 某个箱子最低价 ≤ `max_price` 时触发：
   - `listing` 策略：查该箱子 `max_price` 以内的在售，从便宜的开始挑，批量下单。
   - `quick` 策略：调“快速购买”，平台自己挑最低价，一次一件，买到平台说没有为止。
3. 买入结果写进 `data/c5bot.sqlite`，数量和预算从这里累计，重启不清零。

拿不到内测在售接口权限时用 `C5_STRATEGY=quick`。

## 风控

| 配置 | 作用 |
| --- | --- |
| `max_price` / `max_qty` / `max_spend` | 单个箱子的价格上限、累计数量上限、累计花费上限 |
| `C5_MAX_TOTAL_SPEND` | 所有箱子的总预算，实盘必填 |
| `C5_MAX_BUY_PER_CYCLE` | 每轮最多下几单，防止瞬间打光预算 |
| `C5_MIN_BALANCE_RESERVE` | 账户余额保留额 |
| `C5_ERROR_COOLDOWN_SEC` | 下单被拒后该箱子暂停的时间 |

**结果未知的单**：下单请求超时或断连时，不知道平台有没有成交。这类单标记为 `unknown`，先按已花费占住额度（宁可少买），之后按商户单号自动向平台对账：查到就转为成交，超过 2 分钟平台仍没有这笔单就按未成交释放额度。对账查询本身一直失败的，保持 `unknown`，去官网核对后手工标记：

```bash
uv run python -m c5bot resolve <商户单号> ok
uv run python -m c5bot resolve <商户单号> failed
```

**被取消的订单**（卖家不发货等）：脚本定期刷新未完结订单，取消的自动释放数量和预算。

## Docker 部署与更新

流程：本地 `git push` → GitHub Actions 构建镜像推到注册表 → 服务器一条命令拉新镜像换容器。服务器上不 build、不装 git；`.env`、`watchlist.toml`、`data/` 都在容器外，更新不影响。

### 一次性：代码推到 GitHub

在 `c5` 目录（`.env`、`data/`、`.venv/` 已在 `.gitignore` 里，推之前 `git status` 看一眼）。**先把 [deploy/install.sh](deploy/install.sh) 顶部的 `REPO` 改成你的仓库**，然后：

```bash
git init
git add .
git commit -m "c5 扫货"
git remote add origin git@github.com:你的账号/c5.git
git push -u origin main
```

推上去后 Actions 自动跑 [c5-image.yml](.github/workflows/c5-image.yml)，镜像在 `ghcr.io/<账号小写>/c5bot`，标签 `latest` 和 `sha-xxxxxxx`。GHCR 默认是私有包，服务器拉取要一个只有 `read:packages` 的 token：GitHub → Settings → Developer settings → Personal access tokens。

`curl` 拉脚本走的是 `raw.githubusercontent.com`，仓库公开时直接可用；仓库私有的话 `curl` 要加 `-H "Authorization: token <有 repo 权限的 token>"`，并把同一个 token 以 `GITHUB_TOKEN=` 环境变量传给脚本。

### 服务器：安装

```bash
curl -sSL https://raw.githubusercontent.com/你的账号/c5/main/deploy/install.sh | \
  GHCR_USER=你的账号 GHCR_TOKEN=ghp_xxx sudo -E bash -s -- install
```

它会：没有 Docker 就装；建 `/opt/c5bot`，下载 `compose.yaml` 和两个配置模板；生成 `.env`（把镜像名写进去）和 `watchlist.toml`；`docker login`；拉镜像。此时 `.env` 里还没有 `C5_APP_KEY`，所以**不会启动**，按提示编辑 `/opt/c5bot/.env` 和 `watchlist.toml`，再跑一次 `upgrade` 就启动了。

### 服务器：更新

本地改完 `git push`，等 Actions 跑完（约 1 分钟，仓库 Actions 页能看到），服务器上：

```bash
curl -sSL https://raw.githubusercontent.com/你的账号/c5/main/deploy/install.sh | sudo bash -s -- upgrade
```

做的事：重新下载 `compose.yaml`（compose 有改动也一起带上）→ `docker compose pull` → `up -d --no-deps --force-recreate` → 清旧镜像 → 打印状态和最近日志。`data/` 里的 sqlite 流水原样保留，重启后数量和预算接着算。

其它子命令：`check`（用同一套镜像和配置跑一次连通性检查）、`status`、`logs`、`restart`（改了 `.env` 后用）、`stop`。可用环境变量：`C5BOT_DIR`（安装目录）、`C5BOT_IMAGE`（换注册表）、`C5BOT_BRANCH`。

回滚：`.env` 里把 `C5BOT_IMAGE` 的 `latest` 换成版本号（如 `0.1.0`）或 Actions 日志里的 `sha-xxxxxxx`，再跑 `upgrade`。

### 发版

版本号在 [c5bot/__init__.py](c5bot/__init__.py) 和 `pyproject.toml` 里（测试会检查两边一致），看板头部和启动日志都会显示，`python -m c5bot --version` 也能看。改动记在 [CHANGELOG.md](CHANGELOG.md)。发版：

```bash
git tag v0.2.0 && git push origin main --tags
```

Actions 会在 `latest` 之外多推一个 `ghcr.io/notimeber/c5bot:0.2.0`。

想完全自动（不登服务器）：跑一个 [watchtower](https://containrrr.dev/watchtower/) 容器，它定时检查注册表有没有新的 `latest` 并自动重建：

```bash
docker run -d --name watchtower --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock -v /root/.docker/config.json:/config.json:ro \
  containrrr/watchtower --interval 300 --cleanup c5bot
```

### 注意

- **`curl | sudo bash` 是以 root 执行仓库里的脚本**，只对自己的仓库这么干；仓库被人改了脚本就等于拿到了服务器 root。
- **看板端口只映射到宿主机的 127.0.0.1。** 看板没有登录，上面有暂停 / 继续扫货的按钮，绝对不要开到公网。远程用隧道看：`ssh -L 8766:127.0.0.1:8766 user@server`，然后本地开 http://127.0.0.1:8766 。
- C5 内测在售接口校验 IP 白名单，填的要是服务器的公网出口 IP。
- 服务器在国内拉不动 `ghcr.io` 的话，改推阿里云容器镜像服务（个人版免费）：在 GitHub 仓库 Settings → Secrets and variables → Actions 设 Variables `C5_REGISTRY`、`C5_IMAGE` 和 Secrets `C5_REGISTRY_USER`、`C5_REGISTRY_PASSWORD`；服务器安装时传 `C5BOT_IMAGE=<阿里云镜像名>` 和阿里云账号的 `GHCR_USER` / `GHCR_TOKEN`（变量名沿用，登录的是镜像名里的那个注册表）。
- `compose.yaml` 由脚本管理，每次 `upgrade` 覆盖，别在服务器上手改；所有自定义都放 `.env`。改 `watchlist.toml` 不用重启，看板上点“重载 watchlist”；改 `.env` 跑 `restart`。
- 只看行情不买：`.env` 里 `C5_START_PAUSED=yes`，在看板上点“继续扫货”才开始买。
- Steam 市场接口对匿名请求限流严，云服务器的 IP 更容易被 429。被限流后看板 5 分钟内不再请求 Steam（连续被限翻倍到最多 1 小时），期间沿用旧价，“Steam 价”卡片会显示限流中并自动重试；Steam 汇率每小时才重新量一次，手动“刷新 Steam 价”至少间隔 1 分钟。频繁被限流就配 `STEAM_PROXY`，推荐轮转住宅代理（`http://用户名:密码@代理地址:端口`，每个请求换出口 IP，退避时间也会自动缩短到 30 秒），或调大 `STEAM_REFRESH_SEC`。看板上登录 Steam 和续期不走这个代理。
- `docker stop` 发 SIGTERM，程序按 Ctrl+C 同样的路径退出；正在发的下单请求最坏也就是记成“结果未知”，下次启动自动对账。
- 不经过 CI 在本机 build：`docker compose -f compose.yaml -f compose.build.yaml up -d --build`。
- 本机没装 Docker 也没有 bash，Dockerfile、workflow、install.sh 都没有实际跑过；第一次失败的话把输出贴出来。

## 上实盘前要核对的

接口文档有几处没写清楚，代码里按最合理的理解实现，但没有真实账号验证过：

- **价格单位**：在售查询写的是“元”，订单详情写的是“美元”。用 `prices` 和 `listings` 对比官网页面上的价格，确认单位一致再开实盘。
- **在售查询的排序**：文档没说返回是否按价格排序。脚本在本地排序，单次最多取 50 条，一轮买不完的下一轮继续。
- **“订单不存在”的错误码**：文档没给，对账时按返回文案里有没有“不存在”判断。判断不出来的保持 `unknown`，不会误释放额度。
- **实盘首次运行**：建议先用很小的预算（`C5_MAX_TOTAL_SPEND` 设成一两个箱子的钱）跑通一笔。

另外 CS2 库存上限 1000 件，扫货前留意收货账号的库存余量。

## 用到的接口

| 用途 | 接口 |
| --- | --- |
| 余额 | `GET /merchant/account/v2/balance` |
| Steam 账号 | `GET /merchant/account/v1/steamInfo` |
| 批量行情 | `POST /merchant/market/v2/item/stat/hash/name` |
| 在售查询（内测） | `POST /merchant/market/v2/products/search` |
| 批量购买 | `POST /merchant/trade/v1/batch/buy` |
| 快速购买 | `POST /merchant/trade/v2/quick-buy` |
| 订单详情 | `GET /merchant/order/v2/buy/detail` |

所有请求在 query 里带 `app-key`，请求头带 `Accept-Encoding: gzip, br, zstd, deflate`（官方要求）。默认限流 50 qps。

Steam 价格来自 `https://steamcommunity.com/market/priceoverview/`（`currency=23` 人民币），不需要登录。

## 测试

```bash
uv run pytest
```
