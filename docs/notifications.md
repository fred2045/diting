# 系统通知

> 返回 [README](../README.md)

重要的事来找你，而不是你去找。通知失败只记日志，不影响主流程。

## 触发事件

| 事件 | 点击通知的去向 |
| ---- | -------------- |
| L3 点名（分级判出重点条目） | 直达阅读器那一条并自动展开正文 |
| 日报生成完成 / 失败 | 直达那天的日报页 / `/summary` |
| 周报生成完成 / 失败 | 直达周报页 / `/summary` |

失败也通知（独立开关、独立提示音）——静默失败会造成 FOMO。

## 平台后端

- **macOS**：走 [terminal-notifier](https://github.com/julienXX/terminal-notifier)，
  需先安装：`brew install terminal-notifier`。支持图标与 `-open` 点击直达。
- **Windows**：走 winotify（已写进 `reader/requirements.txt`，PEP 508 标记，
  仅 win32 自动安装，mac/linux 跳过），由随包的 `core/notify_win.py` shim 转发。
- 其他系统：不发通知，只记日志。

`NOTIFY_BACKEND=auto` 时按平台自动选择，一般不用改。

## 开关（`.env` 的 `NOTIFY_*` 段）

| 键 | 默认 | 说明 |
| -- | ---- | ---- |
| `NOTIFY_ENABLE` | `1` | 总开关 |
| `NOTIFY_L3` | `1` | L3 点名（不想要就关，只留日报） |
| `NOTIFY_DAILY` | `1` | 日报生成完成 |
| `NOTIFY_DAILY_FAIL` | `1` | 日报生成失败（排查时可只关它） |
| `NOTIFY_SOUND` | `default` | 提示音；留空 = 静音 |
| `NOTIFY_FAIL_SOUND` | `Basso` | 失败专用提示音；留空 = 失败静音 |
| `NOTIFY_IGNORE_DND` | `0` | `1` = 穿透勿扰/专注模式 |
| `NOTIFY_READER_BASE` | `http://127.0.0.1:8099` | 通知点击跳转的基地址 |
| `NOTIFY_APP_ICON` | 自动用 `static/favicon-256.png` | 留空即默认 |