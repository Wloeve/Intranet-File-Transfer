<div align="center">

# 内网文件互传服务器

**Intranet File Transfer**

局域网内 iPad / 手机 / 电脑之间互传大文件——单文件即可运行，浏览器打开同一个网址就能互相传。

[![Version](https://img.shields.io/badge/version-3.5.1-4f46e5)](https://github.com/Wloeve/Intranet-File-Transfer/releases/latest)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%20%2F%2011%20%C2%B7%20macOS%20%C2%B7%20Linux-lightgrey)](README.md#-快速开始)
[![Python](https://img.shields.io/badge/Python-3.7%2B-3776ab?logo=python&logoColor=white)](https://www.python.org)
[![Dependencies](https://img.shields.io/badge/%E8%BF%90%E8%A1%8C%E6%97%B6%E4%BE%9D%E8%B5%96-0-brightgreen)](src/file_transfer_server.py)

</div>

---

## ✨ 功能特性

- **双向传输**：手机/平板 → 电脑上传；电脑上传后，其他设备打开同一网址即可下载
- **超大文件**：单个文件支持 20G 及以上
- **断点续传**：网络中断、页面刷新、服务重启后，重选同一文件自动从断点继续
- **多连接并行**：1~4 条 WebSocket 长连接同时传输，绕开单条 TCP 流的窗口限制
- **实时速率**：显示瞬时速率、峰值速率、平均速率与预计剩余时间
- **下载续传**：大文件下载支持 HTTP Range 断点续传，中断后从断点继续
- **深色模式**：跟随系统自动切换明暗主题

## 🚀 快速开始

> **免安装版下载**：到 [Releases](https://github.com/Wloeve/Intranet-File-Transfer/releases/latest)
> 下载 `IntranetFileTransfer.exe`，复制到任意 Windows 10/11 电脑双击即可运行，无需安装 Python。

### Windows

双击工程根目录的 **`start_server.bat`**（即「启动服务器」）。脚本会自动在本机寻找可用的 Python 3
（含 py 启动器、PATH、常见安装目录），找不到时会引导安装并可直接打开下载页。

启动后窗口会显示本机的局域网地址（例如 `http://192.168.31.x:8899`），
在 iPad / 手机的浏览器打开这个地址即可。

首次运行如弹出防火墙提示，请勾选「专用网络」并允许访问。

### 从源码打包独立 exe

在没有装 Python 的电脑上运行，可以先打一个独立 exe：

1. 在装有 Python 的电脑上双击 **`build_exe.bat`**（即「构建 exe」，首次会自动下载 PyInstaller）
2. 生成的 `dist\IntranetFileTransfer.exe`（约 9 MB）是单文件程序
3. 把这一个 exe 复制到任何 Windows 10/11 电脑，双击即可运行，无需安装 Python

exe 旁会自动生成 `接收的文件/`、`.uploads_tmp/`、`server.log`，
把 exe 放在哪个文件夹，文件就落在哪个文件夹。

推送 `v*` 标签时，GitHub Actions 会自动完成打包并挂到对应 Release（见
[`.github/workflows/release.yml`](.github/workflows/release.yml)）。

### macOS / Linux

在工程根目录执行：

```bash
python3 src/file_transfer_server.py
```

## 📖 使用方法

### 上传

1. 在浏览器打开服务器地址
2. 点击上传区域选择文件（支持多选），电脑上也可以直接把文件拖进去
3. 进度条走完显示「已完成」后，文件就保存在电脑的 `接收的文件/` 文件夹里

### 下载

在任意设备中把文件上传完成后，另一设备打开同一网址，在「电脑上的文件」列表里点「下载」。

### 页面参数

| 参数               | 说明                             | 建议                                                |
| ------------------ | -------------------------------- | --------------------------------------------------- |
| **连接数**   | 并行 WebSocket 条数              | 2~3 通常最快；如果反复卡住，调回**1（最稳）** |
| **停滞判定** | 多久没有分片完成就判定卡死并重发 | iPad 默认 2 秒；经常出现「重发中」可调到 5 秒       |

两个参数改动即时生效，并会记住你的选择（存于浏览器 localStorage）。

## 🧠 设计要点

- **零依赖**：只用 Python 标准库，一个 `.py` 文件跑起来，不需要 `pip install` 任何东西
- **双通道上传**：WebSocket 长连接为主通道（整文件一条连接，消除每片一次 TCP 握手与慢启动），
  HTTP 分片接口保留为兜底
- **并行传输**：1~4 条 WebSocket 同时发分片，绕开单条 TCP 流的窗口限制；逐条看门狗 +
  故障隔离 + 自动降级到 1 条，防止越传越卡
- **全链路续传**：分片位图落盘（`.uploads_tmp/`），断网、刷新、服务重启都能续；
  下载侧用字节级发送重试（容忍约 8 分钟 Wi-Fi 停顿）+ `ETag` / `If-Range` 校验，
  续传不会拼错内容
- **死连接自愈**：读写超时回收静默死连接（Wi-Fi 抖动 / NAT 断开），配合停滞判定自动重发

## 📁 目录结构

```
start_server.bat              入口，双击启动（自动找本机 Python）
build_exe.bat                 可选：打包免安装的独立 exe
src/file_transfer_server.py   全部源码（单文件，含内嵌网页）
CHANGELOG.md                  版本变更记录
.github/workflows/            CI：推送 v* 标签自动打包 Release
接收的文件/                   上传的文件落在这里
.uploads_tmp/                 未完成的分片，断点续传用；删掉只是无法续传
server.log                    运行日志
.gitignore / .gitattributes   仓库配置
```

`接收的文件/`、`.uploads_tmp/`、`server.log` 均为运行时产生的数据，不入版本库。

## ⚙️ 配置项

打开 `src/file_transfer_server.py` 顶部的「配置区」按需修改：

| 常量                  | 默认值   | 说明                                   |
| --------------------- | -------- | -------------------------------------- |
| `PORT`              | `8899` | 服务端口                               |
| `AUTO_OPEN_BROWSER` | `True` | 启动时在本机自动打开浏览器             |
| `SOCKET_TIMEOUT`    | `120`  | 单次网络读写超时（秒），用于回收死连接 |
| `TMP_KEEP_DAYS`     | `7`    | 未完成分片的保留天数（期间可续传）     |
| `CHUNK_SIZE`        | `4 MB` | 默认分片大小（iPad 会自动改用 2MB）    |

## ❓ 常见问题

### 启动时提示「无法在程序目录写入文件」

程序被放进了 `C:\Program Files`、`C:\Windows` 等没有写入权限的系统目录。
把整个文件夹移动到桌面、文档或 D 盘等普通目录即可。

### 下载时容易断 / 中断后要重头下

服务端已支持断点续传（HTTP Range + ETag），连接中断后 Safari 重新请求会从断点继续。
使用时注意：

- 下载大文件时和上传一样，**保持 iPad 亮屏并停留在页面上**——锁屏或切走后
  iOS 会暂停网络，服务端最多容忍约 8 分钟，之后才断开这条连接
- 若 Safari 提示下载失败，在 Safari 右下角的**下载列表（箭头图标）里点重试**，
  会自动从断点继续，不用重头下载
- `server.log` 里会记录每次下载中断的位置（`下载中断 xxx 已发 n/m 字节`），方便排查

### 传输速度不理想

速度上限主要取决于 **Wi-Fi 实际带宽**，其次才是代码。按性价比排查：

1. **电脑用网线接路由器**——电脑若也走 Wi-Fi，等于同一无线信道收发两跳，带宽直接减半
2. **连 5GHz 频段**，别用 2.4GHz（实际吞吐常只有 5~10 MB/s）
3. **确认文件已下载到 iPad 本地**——iCloud Drive 里的文件是边下边传，速度被 iCloud 卡死
4. **关掉低电量模式**（会限制网络与性能）
5. 页面上把**连接数**调到 2~3 试试；若 2 条和 1 条速度差不多，瓶颈在射频或频段，不在代码

### 上传时 iPad 提示中断 / 反复重发

- 页面会自动重连并从断点继续，一般不需要人工干预
- 大文件传输时请**保持亮屏并停留在本页面**——iPad 锁屏或切走后 Safari 会暂停网络
- 若状态栏频繁出现「停滞 X 秒，重发中…」且进度不涨，把「停滞判定」调大到 5 秒

### 上传了一半，能续传吗

能。重新打开页面、重新选择**同一个文件**即可自动续传。
`.uploads_tmp/` 里保存着分片位图，删除它只会导致从头重传。

### 端口被占用

修改 `src/file_transfer_server.py` 顶部的 `PORT` 后重启。

## 📜 版本历史

详见 [CHANGELOG.md](CHANGELOG.md)。

## 📄 许可证

本项目基于 [MIT License](LICENSE) 开源。

---

<div align="center">

**开发者**

### **Wloeve**

[GitHub @Wloeve](https://github.com/Wloeve) · [Releases 下载](https://github.com/Wloeve/Intranet-File-Transfer/releases/latest)

如果这个项目对你有帮助，欢迎点一个 Star ⭐

</div>
