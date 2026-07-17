# API Hub Manager · 中转站渠道整合管理

面向 **NewAPI / one-api / new-api** 与 **Sub2API** 上游站点的本地集中监控与管理面板。

统一管理多个中转站账号，一站式查看余额、消耗、分组倍率；支持兑换码、低价扫描、按需推送到自有 Sub2API Hub，以及连通性探活（延迟色块历史 + 可用性统计）。

数据默认只保存在本机 `data/` 目录，**不会上传到第三方**（除你主动调用的上游站点 / Hub / CapSolver）。

---

## 目录

- [快速开始](#快速开始)
- [功能总览](#功能总览)
- [页面说明](#页面说明)
- [密钥体系（重要）](#密钥体系重要)
- [连通性探活](#连通性探活)
- [扫描器与按需同步](#扫描器与按需同步)
- [与 Sub2API Hub 联动](#与-sub2api-hub-联动)
- [设置项](#设置项)
- [本项目 HTTP API](#本项目-http-api)
- [上游站点 API 对照](#上游站点-api-对照)
- [数据文件](#数据文件)
- [技术栈与性能](#技术栈与性能)
- [常见问题](#常见问题)
- [功能状态](#功能状态)

---

## 快速开始

### 环境要求

- Python **3.11+**
- 可访问各中转站 / Hub 的网络

### 安装与启动

```bash
cd api-hub-manager

# 安装依赖
pip install -r requirements.txt

# 启动服务（默认 0.0.0.0:8899，无热重载，更稳定）
python main.py
```

Windows 也可双击 `start.bat`。

启动后浏览器访问：**http://localhost:8899**

> 修改代码后请 **彻底结束旧 python 进程** 再启动，否则可能仍在跑旧逻辑。  
> 前端有缓存时请 **Ctrl+Shift+R** 强制刷新。

### 最小使用路径

1. **添加渠道**：填站点地址 + Cookie/登录 或 Token  
2. 仪表盘点 **强制** 拉取余额与分组  
3. （可选）配置 **上游 sk- Key** → 扫描器 / 探活 / 推 Hub  
4. （可选）设置里配置 **Sub2API Hub** → 扫描器按需同步  

---

## 功能总览

| 模块 | 能力 |
|------|------|
| **仪表盘** | 总余额 / 今日消耗 / 累计消耗 / 异常数；渠道卡片；消费对比图；最低倍率表；磁盘缓存秒开 |
| **渠道管理** | NewAPI + Sub2API；登录 / Token / Cookie / User API Key；充值比归一化；兑换码；Token 刷新 |
| **扫描器** | 低价扫描；状态分桶筛分；多选按需配置到 Hub；倍率同步；映射表；上游密钥一览 |
| **探活** | 自定义渠道+分组+模型；延迟展示；3×10 历史色块；可用性 %；自动探活开关 |
| **设置** | CapSolver；Hub 邮箱密码 / Admin API Key；超低价阈值；模型分类关键词 |

---

## 页面说明

### 1. 仪表盘

- **刷新**：读内存/磁盘缓存（毫秒级）  
- **强制**：并发拉取所有渠道最新数据（单渠道超时约 12s）  
- **自动刷新**：可选 5 / 10 / 30 分钟强制刷新（浏览器需保持打开）  
- 渠道卡片展示：余额、今日/累计消耗、分组倍率、Cookie/上游 Key 标签、充值比  
- 支持搜索过滤渠道  

首次打开若已有 `data/dashboard_cache.json`，会立刻显示上次结果，无需等待上游。

### 2. 添加渠道

| 字段 | 说明 |
|------|------|
| 平台 | `newapi` 或 `sub2api` |
| 认证 | 登录（用户名密码）或 Token/Cookie |
| 凭据类型 | cookie / token / bearer / user_api_key |
| User ID | NewAPI Cookie 模式常需要 `New-Api-User` |
| 充值比例 | 1:1 / 1:10 / …，用于跨站倍率归一化 |
| **上游 API Key** | `sk-...`，用于探活与推送到 Sub2API（**不能用 Cookie**） |

登录成功后，NewAPI 会记为 `credential_type=cookie`，Sub2API 记为 `bearer`。

### 3. 扫描器

见下文 [扫描器与按需同步](#扫描器与按需同步)。

### 4. 探活

见下文 [连通性探活](#连通性探活)。

### 5. 设置

- CapSolver API Key（Turnstile 自动打码）  
- Sub2API Hub 地址 / 管理员邮箱密码 / **Admin API Key（推荐）**  
- 超低价阈值（默认 0.6）  
- 模型分类关键词（Claude-官号 / Claude-Kiro / GPT / Gemini / Grok）  

---

## 密钥体系（重要）

本工具里有两类完全不同的「密钥」：

| 类型 | 用途 | 能否探活 / 推 Hub |
|------|------|-------------------|
| **Cookie / Session / JWT**（登录态） | 查余额、分组、消耗 | ❌ 不能 |
| **上游 sk- API Key** | 调 `/v1/chat/completions`、写入 Sub2API 上游 | ✅ 需要 |

### 解析优先级（探活 / provision）

```
1. 分组级密钥  data/group_keys.json   （扫描器「设置密钥」）
2. 渠道级 upstream_key                 （添加渠道时填写）
3. access_token 仅当其本身是 sk- 形态
```

Cookie / `session=` / JWT（`eyJ...`）会被拒绝。

### 如何查看「哪个分组配了 Key」

打开 **扫描器** 页底部 **「上游密钥一览」**：

- **渠道级上游 Key**：哪些渠道配置了 `upstream_key`  
- **分组级密钥**：`渠道 + 分组 + 脱敏 sk-****`，可更换/删除  

扫描结果表 **Key** 列也会显示：

- `✓ 分组密钥` / `✓ 渠道Key` / `⚠ 设置密钥`  

---

## 连通性探活

向 `{base_url}/v1/chat/completions` 发送轻量 chat 请求，检测上游是否可用（风格类似 Sub2API 探活）。

### 添加目标

1. 点 **「＋ 添加探活目标」**（弹窗填写，非常驻表单）  
2. 选择 **渠道**、**分组**（可选）、填写 **模型**  
3. **保存并添加**，或 **仅测一次（不保存）**  

中文分组名安全：不会写入 HTTP Header（避免 ascii 编码错误），分组信息走 JSON body。

### 延迟展示

- 大号显示最近延迟（ms / s）  
- 颜色参考：快 &lt;800ms 绿；中 800–2000 黄；慢 &gt;2000 红（列表强调色）  
- 汇总：最近成功数、失败数、平均延迟  

### 历史色块（3 行 × 10 列）

每条探活目标下方为 **最多 30 次** 历史格子（3×10），紧密 **正方形** 色块：

| 颜色 | 含义 |
|------|------|
| 🟢 绿 | 成功且延迟 **&lt; 1000 ms** |
| 🟡 黄 | 成功且 **1000–15000 ms** |
| 🟠 深黄 | 成功且 **&gt; 15000 ms** |
| 🔴 红 | **连接失败** |

- 悬浮显示：`时间 · xxx ms`（失败显示原因）  
- **可用性** = 成功次数 / 总次数 × 100%  
- 整体可用性在汇总区展示  

### 自动探活

探活页右上角开关（与仪表盘自动刷新类似）：

| 项 | 说明 |
|----|------|
| 开关 | 开启/关闭自动探活 |
| 间隔 | 1 / 3 / 5 / 10 / 30 分钟 |
| 倒计时 | 下次批量探活剩余时间 |

- 只跑 **已启用** 的目标  
- 配置保存在浏览器 `localStorage`  
- **需保持浏览器标签页打开**（前端定时器）  

---

## 扫描器与按需同步

### 扫描

1. 强制刷新仪表盘数据  
2. 找出有效倍率 **&lt; 超低价阈值** 的分组（已按充值比归一化）  
3. 标注：已映射 / 倍率变化 / 缺密钥 / 更低价 / 分类最优  

### 筛分

- 状态 chips：全部 / 未配置 / 已配置 / 倍率已变 / 缺密钥 / 更低价 / 分类最优  
- 搜索、平台、分类、排序  

### 按需上传（不会默认全量）

| 操作 | 说明 |
|------|------|
| 勾选 + **配置所选** | 只把勾选的推到 Hub |
| **同步所选倍率** | 只同步勾选的已映射项 |
| **配置可见未映射** | 当前筛分结果里的未映射项 |
| 单项「配置到 Hub / 重新配置」 | 单条操作；重新配置 `force=true` |

### 上游密钥一览

扫描器底部固定展示渠道级 / 分组级密钥配置情况。

---

## 与 Sub2API Hub 联动

可将其它中转站作为你的 Sub2API 上游，实现：

**监控 → 扫描低价 → 按需建上游账号 → 同步 rate_multiplier**

### 1. 配置 Hub

「设置」→ Sub2API Hub：

| 字段 | 说明 |
|------|------|
| Hub 地址 | 如 `https://my-hub.example.com` |
| 管理员邮箱 / 密码 | 登录鉴权 |
| Admin API Key | **推荐**；`x-api-key`，优先于邮箱密码 |
| 超低价阈值 | 默认 0.6 |

点 **测试连接** 验证。

### 2. 准备上游 sk-

- 添加渠道时填 **上游 API Key**，或  
- 扫描器给具体分组 **设置密钥**  

### 3. 同步方式

- **推荐**：扫描 → 筛分勾选 → **配置所选**  
- 可选：一键全自动同步（仍会跳过无 Key、已映射且倍率未变的项）  

### 4. 映射表

本地文件：`data/provision_mappings.json`  

- 同步倍率、删除本地映射（**不**自动删除 Hub 上账号/分组）  

### 5. 流程示意

```
监控上游 (NewAPI/Sub2API)
        │  定时/强制刷新
        ▼
扫描有效倍率 < 阈值
        │  用户勾选 / 自动同步
        ▼
Sub2API Hub 创建 apikey 上游 + 分组
        │
        ▼
倍率变化 → 更新 Hub rate_multiplier
```

---

## 设置项

### CapSolver

用于站点开启 Cloudflare Turnstile 时的自动打码。  
未配置时可用 Cookie/Token 方式添加渠道，避开登录打码。

### 模型分类关键词

仪表盘最低倍率表、扫描器分类标签依赖关键词匹配。  
可在设置中自定义；需与前端 `Claude-官号` / `Claude-Kiro` 等分类一致。

---

## 本项目 HTTP API

前缀均为 `/api`。

### 仪表盘与渠道

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/dashboard` | 汇总；无 force 时优先缓存 |
| GET | `/dashboard?force=true` | 强制并发拉取 |
| GET | `/dashboard/status` | 是否正在刷新 |
| GET | `/accounts` | 渠道列表（含 has_upstream_key 等） |
| POST | `/accounts` | 添加渠道（可带 upstream_key） |
| DELETE | `/accounts/{id}` | 删除 |
| GET | `/accounts/{id}/balance` | 余额 |
| GET | `/accounts/{id}/groups` | 分组 |
| GET | `/accounts/{id}/models` | 模型 |
| GET | `/accounts/{id}/usage` | 消耗 |
| GET | `/accounts/{id}/overview` | 概览 |
| GET | `/accounts/{id}/ratio_history` | 倍率历史 |
| POST | `/accounts/{id}/refresh` | 重新登录刷新凭据 |
| POST | `/accounts/{id}/redeem` | 兑换码 |
| PATCH | `/accounts/{id}/upstream_key` | 更新上游 Key |
| PATCH | `/accounts/{id}/user_id` | 更新 User ID |
| POST | `/check-turnstile` | 检测站点 Turnstile |

### 设置 / Hub

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST | `/settings` | 读写设置 |
| GET | `/settings/capsolver-balance` | CapSolver 余额 |
| POST | `/hub/test` | 测试 Hub 连接 |

### 扫描器

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/scanner/scan` | 扫描低价候选 |
| POST | `/scanner/provision` | 配置到 Hub（body 可 `force`） |
| POST | `/scanner/sync-ratio` | 同步单条倍率 |
| POST | `/scanner/auto-sync` | 一键同步 |
| GET/DELETE | `/scanner/mappings` | 映射列表 / 删除 |
| GET/POST/DELETE | `/group-keys` | 分组密钥 |

### 探活

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST | `/probe/profiles` | 列表 / 新建 |
| PUT/DELETE | `/probe/profiles/{id}` | 更新 / 删除 |
| POST | `/probe/run` | 即时探活（不保存） |
| POST | `/probe/profiles/{id}/run` | 跑单条并写入历史 |
| POST | `/probe/run-all` | 跑全部已启用 |

探活历史保存在 profile 的 `history`（最多 30 条）与 `stats`（可用性等）。

---

## 上游站点 API 对照

### NewAPI

| 接口 | 用途 |
|------|------|
| GET `/api/status` | quota_per_unit、Turnstile |
| POST `/api/user/login` | 登录 |
| GET `/api/user/self` | 用户信息 |
| GET `/api/user/self/groups` | 分组倍率 |
| GET `/api/log/self/stat` | 今日消耗 |
| POST `/api/user/topup` | 兑换码 |
| POST `/v1/chat/completions` | 探活 / 上游调用 |

### Sub2API（用户侧）

| 接口 | 用途 |
|------|------|
| POST `/api/v1/auth/login` | 登录 |
| GET `/api/v1/auth/me` | 余额等 |
| GET `/api/v1/groups/available` | 可用分组 |
| GET `/api/v1/groups/rates` | 倍率 |
| GET `/api/v1/usage/dashboard/stats` | 消耗 |
| POST `/api/v1/redeem` | 兑换码 |

### Sub2API（管理端 · Hub）

| 接口 | 用途 |
|------|------|
| POST `/api/v1/auth/login` 或 `x-api-key` | 鉴权 |
| GET `/api/v1/admin/groups/all` | 分组列表 |
| POST `/api/v1/admin/groups` | 创建分组 |
| PUT `/api/v1/admin/groups/{id}` | 更新倍率等 |
| POST `/api/v1/admin/accounts` | 创建上游（`type=apikey` + `credentials`） |

参考实现：[Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api)

---

## 数据文件

路径均在 `data/`（敏感文件已在 `.gitignore`，**不要提交到公开仓库**）。

| 文件 | 内容 |
|------|------|
| `accounts.json` | 渠道账号、Cookie/Token、upstream_key |
| `settings.json` | CapSolver、Hub、阈值 |
| `dashboard_cache.json` | 仪表盘磁盘缓存 |
| `ratio_history.json` | 倍率历史快照 |
| `group_keys.json` | 分组级 sk- |
| `provision_mappings.json` | 上游→Hub 映射 |
| `probe_profiles.json` | 探活目标 + 历史色块数据 |
| `accounts.sample.json` | 示例结构（可提交） |

---

## 技术栈与性能

- **后端**：Python 3.11+、FastAPI、Uvicorn、httpx 异步  
- **前端**：Jinja2 + 纯 HTML/CSS/JS，Chart.js  
- **性能要点**：  
  - 共享 SSL context，避免 Windows 上反复建 SSL 卡住事件循环  
  - 仪表盘渠道并发拉取 + 单渠道超时  
  - 内存缓存 + 磁盘缓存，打开即显  
  - 默认 `reload=False` 降低双进程问题  

---

## 常见问题

### 1. 前端一直「加载中」

- 确认服务已启动  
- **Ctrl+Shift+R** 强刷（曾有前端 JS 语法错误导致整页脚本不执行）  
- 结束所有旧 `python.exe` 后重启  

### 2. 探活 `ascii codec can't encode`

- 旧版本把中文分组名放进 HTTP 头导致  
- **请升级并重启服务**；当前版本中文分组只走 JSON body  

### 3. 探活 / 推 Hub 报缺少上游 Key

- Cookie 不能当 sk-  
- 在添加渠道时填上游 Key，或扫描器给分组「设置密钥」  
- 在「上游密钥一览」确认是否已配置  

### 4. 扫描后不要全部同步

- 使用筛分 + 勾选 + **配置所选**  
- 不要误点「一键全自动」除非确实要批量  

### 5. 余额为 0 不显示进总计

- 已修复（`0` 会正确计入）  

### 6. GitHub 推送超时

- 可尝试：`git -c http.version=HTTP/1.1 push origin main`  

---

## 功能状态

- ✅ 余额 / 消耗 / 分组倍率  
- ✅ 兑换码、Token 刷新  
- ✅ Turnstile + CapSolver  
- ✅ 仪表盘自动刷新、磁盘缓存  
- ✅ 消费对比图、倍率历史  
- ✅ 模型分类可自定义  
- ✅ 充值比归一化  
- ✅ 扫描筛分 + 多选按需 provision  
- ✅ 上游密钥一览  
- ✅ Sub2API Hub 联动（credentials 建号、倍率同步）  
- ✅ 探活（弹窗添加、延迟、3×10 色块、可用性、自动探活）  
- ❌ 暂无 Telegram / Webhook 通知  
- ❌ 暂无服务端后台定时任务（依赖浏览器打开）  

---

## 许可证与声明

- 仅供个人/团队管理自有账号使用  
- 请遵守各上游站点服务条款  
- 仓库请勿提交 `data/accounts.json` 等含密码与 Token 的文件  
