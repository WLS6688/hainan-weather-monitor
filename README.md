# 海南天气预警监控 · GitHub Actions

这套方案部署到 GitHub Actions，**无需电脑开机、无需 WorkBuddy 常开**，由云端定时运行。

## 监控范围与规则

- **监控区域**：海口、三亚、澄迈 三城
- **天气日报**：只报澄迈（每天 09:00 推送）
- **预警推送触发**：预警影响 **澄迈或海口** 时推送
  - 仅三亚受影响（澄迈/海口无影响）→ 不推预警，仅在日报里提示
- **影响判断**：预警提到具体城市→影响该城；全省/全岛→影响三城；其他市县→不影响三城

## 一、你需要准备

- 一个 GitHub 账号（免费注册：https://github.com ）
- 企业微信群机器人 webhook（就是之前那个 `qyapi...webhook/send?key=...`）

## 二、部署步骤（全程 5 分钟）

### 第 1 步：创建 GitHub 仓库

1. 登录 GitHub，点右上角 `+` → `New repository`
2. 仓库名随意，如 `hainan-weather-monitor`
3. 选 **Private（私有）**（因为要存 webhook，别公开）
4. 不要勾选任何初始化选项（README 等），点 `Create repository`

### 第 2 步：上传代码

本地打开终端，进入本目录（`.workbuddy/github-actions`），执行：

```bash
git init
git add .
git commit -m "init weather monitor"
git branch -M main
git remote add origin https://github.com/你的用户名/hainan-weather-monitor.git
git push -u origin main
```

> 把 `你的用户名` 换成你的 GitHub 用户名。

### 第 3 步：配置 webhook 密钥

1. 打开你的仓库页面 → `Settings` → `Secrets and variables` → `Actions`
2. 点 `New repository secret`
3. Name 填：`WECOM_WEBHOOK`
4. Value 填你的 webhook：`https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=6cbed4be-c980-41cb-8e1b-625f4ea8e017`
5. 点 `Add secret`

### 第 4 步：启用工作流

1. 仓库页面 → `Actions` 标签
2. 如果提示 `I understand my workflows, go ahead and enable them`，点它启用
3. 找到 `海南天气预警监控` 工作流

### 第 5 步：测试

在 `Actions` 页点工作流右侧的 `Run workflow` → `Run workflow`，手动触发一次，验证能收到群消息。

## 三、定时规则说明

工作流里的 cron 用的是 **UTC 时间**（北京时间 - 8 小时）：

| 任务 | 北京时间 | UTC cron |
|------|---------|----------|
| 每日首检 + 天气日报 | 每天 09:00 | `0 1 * * *` |
| 预警轮询更新 | 每小时 30 分 | `30 * * * *` |

## 四、常见问题

**Q：GitHub Actions 免费吗？**
免费。公开仓库无限用；私有仓库每月 2000 分钟，你这个任务每小时 1 次、每次几十秒，完全够用。

**Q：定时任务会准时吗？**
GitHub Actions 的 schedule 可能有几分钟到十几分钟的延迟（尤其在高峰），但基本可靠。若需严格准点，可改用付费方案或自建服务器。

**Q：换群怎么办？**
只需更新仓库里的 Secret `WECOM_WEBHOOK` 的值即可，代码不用动。

**Q：怎么停止？**
仓库 `Settings` → `Actions` → `General` → 关闭 Actions，或删除仓库。

## 五、文件说明

```
.github/workflows/weather-monitor.yml   # 定时任务定义
weather_monitor.py                      # 监控主脚本
state.json                              # 状态文件（自动生成，记录预警等级）
```
