# 中转站渠道整合管理工具

面向 NewAPI / Sub2API 上游站点的集中监控与管理面板。

统一管理多个 NewAPI / Sub2API 中转站账号，一站式查看余额、消耗、分组倍率，支持兑换码兑换。

## 快速开始

```bash
cd api-hub-manager

# 安装依赖
pip install -r requirements.txt

# 启动服务
python main.py
```

或双击 `start.bat` 启动。启动后访问 **http://localhost:8899**

## 功能

### 仪表盘
- 总余额汇总、今日消耗、累计消耗
- 异常渠道数量统计
- 所有渠道概览卡片

### 渠道管理
- 支持 **NewAPI** (one-api/new-api 系列) 和 **Sub2API** 两类上游
- 支持 Token/Cookie 认证和账号密码登录两种方式
- Token 刷新（重新登录获取新凭据）
- 删除渠道

### 数据查询
- **余额查询**: 自动从 `/api/status` 获取 `quota_per_unit` 精确换算
- **分组倍率**: NewAPI 使用 `/api/user/self/groups`，Sub2API 使用 `/api/v1/groups/available` + `/api/v1/groups/rates`
- **消耗统计**: 今日消耗 + 累计消耗（分别调用日志统计和用户信息接口）
- **模型列表**: 获取可用模型

### 兑换码
- NewAPI: 调用 `/api/user/topup`
- Sub2API: 调用 `/api/v1/redeem`

## API 端点对照

### NewAPI 端点
| 接口 | 用途 |
|------|------|
| GET /api/status | 获取 quota_per_unit、Turnstile 配置 |
| POST /api/user/login | 用户名 + 密码登录 |
| GET /api/user/self | 用户信息（quota、used_quota、group） |
| GET /api/user/self/groups | 分组列表及倍率 |
| GET /api/log/self/stat | 日志统计（今日消耗） |
| POST /api/user/topup | 兑换码兑换 |

### Sub2API 端点
| 接口 | 用途 |
|------|------|
| POST /api/v1/auth/login | 邮箱 + 密码登录 → access_token |
| POST /api/v1/auth/refresh | 刷新 token |
| GET /api/v1/auth/me | 用户信息（balance） |
| GET /api/v1/groups/available | 可用分组 |
| GET /api/v1/groups/rates | 分组倍率覆盖 |
| GET /api/v1/usage/dashboard/stats | 消耗统计 |
| GET /api/v1/announcements | 公告 |
| POST /api/v1/redeem | 兑换码兑换 |

## 本项目 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/dashboard | 仪表盘汇总 |
| GET | /api/accounts | 列出所有渠道 |
| POST | /api/accounts | 添加渠道 |
| DELETE | /api/accounts/{id} | 删除渠道 |
| GET | /api/accounts/{id}/balance | 余额 |
| GET | /api/accounts/{id}/groups | 分组倍率 |
| GET | /api/accounts/{id}/models | 模型列表 |
| GET | /api/accounts/{id}/usage | 消耗统计 |
| GET | /api/accounts/{id}/overview | 完整概览 |
| POST | /api/accounts/{id}/refresh | 刷新 Token |
| POST | /api/accounts/{id}/redeem | 兑换码 |

## 技术栈

- Python 3.11+
- FastAPI + Uvicorn
- httpx（异步 HTTP）
- Jinja2 模板
- 前端纯 HTML/CSS/JS（无框架）

## 功能状态

- ✅ 余额/消耗/分组倍率查询
- ✅ 兑换码兑换
- ✅ Token 刷新
- ✅ Cloudflare Turnstile 自动打码（CapSolver）
- ✅ 自动刷新（5/10/30 分钟可选）
- ✅ 消费对比图表
- ✅ 模型分类关键词可自定义
- ✅ 持久缓存（打开即用，手动刷新才更新）
- ✅ **全自动联动（扫描→推送→倍率同步）**
- ✅ **充值比例归一化（跨渠道公平对比）**
- ❌ 暂无通知推送（Telegram/Webhook 等）

## 与 Sub2API 全自动联动教程

此工具可将其他中转站作为你的 Sub2API 上游，实现「扫描低价 → 自动建账号 → 自动更新倍率」的闭环。

### 1. 配置 Hub

打开「设置」→「Sub2API Hub 配置」，填写：

| 字段 | 说明 |
|------|------|
| Hub 地址 | 你的 Sub2API 站点，如 `https://my-hub.example.com` |
| 管理员邮箱 | Sub2API 管理员登录邮箱 |
| 管理员密码 | Sub2API 管理员密码 |
| 超低价阈值 | 低于此倍率触发扫描，默认 0.6x |

点「测试连接」验证。

### 2. 添加上游渠道 + 上游 API Key

添加你要监控的中转站渠道时：

- **凭据类型**推荐选「Cookie」（从浏览器复制 session），并填写 User ID
- **上游 API Key**（可选但建议填）：这个 Key 用于调用上游的 API，会被自动配置到 Sub2API 作为上游凭据
- **充值比例**：如果此站是 1:10 充值，选 1:10，工具会自动归一化倍率

> ⚠️ 渠道卡片加了 `API Key` 标签说明凭据类型；非 1:1 充值会显示比例标签。

### 3. 一键全自动同步

打开「扫描器」→ 点 **「⚡ 一键全自动同步」**，工具会：

1. **强制刷新**所有渠道的最新数据
2. **扫描**所有低于阈值的分组
3. **检查映射表**：已配置过的跳过（不会重复创建）
4. **新建**：为每个新低价分组在 Sub2API Hub 中创建上游账号，绑定到「超低价自动化」分组
5. **更新倍率**：如果上游分组倍率发生变化，自动调用 Hub API 更新对应分组的 `rate_multiplier`
6. **记录映射**：所有配置关系存入 `data/provision_mappings.json`，下次同步不会重复

### 4. 查看和管理映射

扫描器页面底部展示**已配置映射表**：

- **同步倍率**：手动触发单条倍率同步
- **删除**：删除映射记录（不删除 Hub 上已有的账号/分组）

### 5. 工作流程图

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  监控上游渠道     │ ──→ │  扫描低价分组      │ ──→ │  Sub2API Hub    │
│  (NewAPI/Sub2API)│     │  (有效倍率 < 阈值)  │     │  创建账号+分组    │
└─────────────────┘     └──────────────────┘     └─────────────────┘
        │                        │                        │
        └────── 定时刷新 ────────┘                        │
                                                         │
        ┌────────────────────────────────────────────────┘
        │  倍率变化检测
        ▼
  ┌──────────────────┐
  │  更新 Hub 分组     │
  │  rate_multiplier  │
  └──────────────────┘
```

### 6. 定时全自动运行

在仪表盘打开「自动刷新」（5/10/30 分钟），配合**浏览器保持页面打开**。每次定时刷新都会更新数据，但不会自动触发全同步。建议：

- 每天手动点一次「⚡ 一键全自动同步」
- 或发现倍率变化标记（扫描结果中 ⚠ 标记）时点「同步倍率」
