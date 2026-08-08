# QTX-RemoteLock

远程锁屏 / 解锁管理工具（Windows）。图形界面批量管理多台 Windows 机器，
通过 SSH + 远程桌面（RDP / tscon）实现一键锁定与解锁，无需逐台操作。

当前版本：**v1.0.4**

## 功能

- 机器清单管理：增删改、SSH 用户、是否启用 RDP、备注
- 一键锁定 / 解锁选中或全部机器
- 主题：亮色 / 暗色 / 跟随系统（默认跟随系统）
- 表格支持按字段一键排序（点击列头切换升/降序）
- 列宽可手动拖拽，宽度与排序状态持久化保存
- 配置自动迁移：旧版 `RemoteLock` 配置目录会自动迁移到 `QTX-RemoteLock`

## 快速开始

### 方式一：直接使用安装包

下载 `QTX-RemoteLock-v1.0.4.exe`，双击即运行，无需安装 Python。

### 方式二：从源码运行

环境要求：Windows + Python 3.11。

```bash
pip install ttkbootstrap paramiko
python remote_lock_gui.py
```

## 配置

配置文件位于：`%APPDATA%\QTX-RemoteLock\machines.json`

首次运行会生成默认配置（含示例机器 `PC-01`）。参见 `machines.example.json` 了解字段结构：

| 字段        | 说明                                                  |
| ----------- | ----------------------------------------------------- |
| `name`      | 机器显示名                                            |
| `ip`        | 内网 IP                                               |
| `sshUser`   | SSH 登录用户名                                        |
| `rdp`       | 是否启用远程桌面。`false` 的机器仅支持锁定，不支持解锁 |
| `notes`     | 备注                                                  |
| `sshKeyPath`| SSH 私钥路径（默认 `%USERPROFILE%\.ssh\id_ed25519`）   |
| `theme`     | `system` / `light` / `dark`                           |

> 本仓库为公开仓库，不含任何真实机器配置。请将真实 `machines.json` 放在
> 上述用户目录下，不要提交到代码库。

## 打包

```bash
pip install pyinstaller ttkbootstrap paramiko
pyinstaller --onefile --windowed --name QTX-RemoteLock-v1.0.4 \
  --collect-data ttkbootstrap --hidden-import paramiko \
  --version-file version_info.txt remote_lock_gui.py
```

生成的安装包位于 `dist/QTX-RemoteLock-v1.0.4.exe`。

## 说明

- 锁定：通过 SSH 触发 `LockWorkStation`（SYSTEM 身份，被控机零文件落地）。
- 解锁：通过 RDP 连接 + `tscon` 将会话切到控制台，断开后控制台保持解锁。
- 单会话机器判定解锁成功标准：`rdp-tcp#` 会话消失。

## 许可证

仅供内部使用。
