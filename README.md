# DSHLuncher 2

面向 Windows 的 Python 桌面维护器。新版把「备份与恢复」「部署新环境」「DSH 管理」分成独立页面，避免把不同风险和结果的操作堆在同一张面板里。

## 启动

```powershell
python launcher.py
```

或双击 `启动.cmd`。依赖为 Python 3.10+、PySide6、7-Zip 和 Windows 自带的 robocopy。

## 当前数据位置

- 新版配置和守护状态：`data/`
- 备份数据仍读取 `data/config.json` 中 `paths.backup_root` 指向的旧备份库；现在不搬动原始备份。
- Harness、`.dsh` 和 workspace 继续使用配置中的路径；pnpm store 由独立增量镜像维护在 `<backup_root>\store`，当前机器的源路径是 `D:\.pnpm-store\v11`。
- DSH 管理端口设为 `3025`。新版会从当前 Harness 的 `start.cmd` 生成一个独立启动副本，不改写原 Harness；此副本将服务启动参数改成 3025。

## 页面

- **运行概览**：网关状态、最新基线和会话、备份库空间、快速备份。
- **备份与恢复**：基线 / 会话 / 工作区快照，以及固定的 pnpm store 增量镜像；支持恢复单份到原路径或新目录。历史表下方的四条时间泳道可以点选对应记录。
- **部署新环境**：分别选择基线和会话快照，可选工作区；重定位到新目录，把 store 镜像复制到目标盘本地 `.pnpm-store\v11` 后离线安装依赖、重建目录联接并生成使用配置端口的 `start-dsh.cmd`。
- **DSH 管理**：启动、停止、重启、打开本地界面和运行期间守护。

自动备份由新版运行期间调度：会话按 15 分钟检查，基线每日按配置时间检查；store 在新基线完成后跟随增量同步；工作区只手动备份。基线与会话的保留上限均为 15 天，store 镜像不轮转。

pnpm 未显式指定全局 store 时会按所在盘符选择本地仓库；本机 `pnpm store path` 返回 `D:\.pnpm-store\v11`。因此部署到其他盘时，新版沿用此盘符相对路径，例如目标盘为 `E:` 就准备 `E:\.pnpm-store\v11`，避免离线安装依赖只读 D 盘仓库。

新版将备份核心放在 `vendor/dshbackup`，旧版目录和旧版未提交修改保持不动。`data/config.json` 是新版自己的配置副本；备份根目录仍指向现有备份，因此启动新版不会复制或删除历史包。
