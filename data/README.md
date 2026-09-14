# 推送历史与溯源记录

本目录由 `monitor.py` 自动维护，用于留存**所有已推送的消息**，便于事后溯源、统计与导出。
每次有推送发生时，脚本会把记录追加写入这里，并随 `state.json` 一起提交回仓库（因此可从 Git 历史完整追溯）。

| 文件 | 说明 |
|------|------|
| `push_log.jsonl` | **主审计日志**，每行一条 JSON，字段：时间、类型(kind)、预警类型/等级/区域、发布单位、标题、消息格式(msgtype)、附加信息（是否附图、持续提醒、剩余小时数、本次合并条数 `batch` 等）。多条预警合并推送时，**每条预警各记一行**（`batch` 为同批条数），保证逐条可溯源 |
| `archive/YYYY-MM.md` | **月度可读归档**，按自然月分文件，人可读、便于逐月翻查 |
| `history.csv` | 导出文件（`--mode export` 生成），UTF-8 BOM，Excel 可直接打开 |
| `history.md` | 导出文件（`--mode export --format md` 生成），Markdown 表格 |

`kind` 取值：`alert`（红/橙即时预警）、`daily`（每日报告）、`daily_image`（每日报告附图）、
`imminent`（台风逼近/趋向）、`lifted`（预警解除）、`error`（抓取异常）、`recovered`（恢复）。

## 导出方式

```bash
python3 monitor.py --mode export              # 生成 data/history.csv（Excel 友好）
python3 monitor.py --mode export --format md  # 生成 data/history.md（Markdown 表格）
```
