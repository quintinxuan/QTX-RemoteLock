# -*- coding: utf-8 -*-
"""
QTX-RemoteLock - 远程锁屏/解锁管理软件
GUI 包装已验证的 SSH + tscon 机制，机器清单可随时增删改。
配置文件位于 %APPDATA%/QTX-RemoteLock/machines.json
"""
import os
import sys
import json
import time
import shutil
import tempfile
import random
import string
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
import tkinter as tk
from tkinter import messagebox

import base64
import ttkbootstrap as ttkb
from ttkbootstrap.constants import *
from app_icon import APP_ICON_B64, LOCK_CLOSED_B64, LOCK_OPEN_B64, DETECT_B64

try:
    import paramiko
except Exception:  # noqa
    paramiko = None

# ----------------------------------------------------------------------------
# 品牌与版本
# ----------------------------------------------------------------------------
APP_NAME = "QTX-RemoteLock"
APP_VERSION = "1.0.16"
APP_TITLE = "%s v%s" % (APP_NAME, APP_VERSION)

_ICON_TMP = None
def set_window_icon(win):
    """为 tk/ttk 窗口设置标题栏与任务栏图标（运行时生效，与 EXE 外壳图标无关）。

    图标以 base64 内嵌在 app_icon.py，解码后写入临时 .ico 并设置；只写一次并缓存。
    """
    global _ICON_TMP
    if _ICON_TMP is None:
        try:
            data = base64.b64decode(APP_ICON_B64)
            fd, _ICON_TMP = tempfile.mkstemp(suffix=".ico", prefix="qtxlock_")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except Exception:
            return
    try:
        win.wm_iconbitmap(_ICON_TMP)
    except Exception:
        pass

# ----------------------------------------------------------------------------
# 配置路径：放在 %APPDATA% 下，保证单文件 EXE 也能持久化读写
# 旧版本目录为 %APPDATA%/RemoteLock，首次运行自动迁移（不删除旧目录）
# ----------------------------------------------------------------------------
APP_DIR = os.path.join(os.path.expandvars("%APPDATA%"), APP_NAME)
LEGACY_DIR = os.path.join(os.path.expandvars("%APPDATA%"), "RemoteLock")
CONFIG_PATH = os.path.join(APP_DIR, "machines.json")
os.makedirs(APP_DIR, exist_ok=True)


def _migrate_legacy():
    """把旧版 %APPDATA%/RemoteLock 下的配置复制到新目录。仅复制，不删旧目录。"""
    if os.path.exists(CONFIG_PATH) or not os.path.isdir(LEGACY_DIR):
        return False
    old = os.path.join(LEGACY_DIR, "machines.json")
    if not os.path.isfile(old):
        return False
    try:
        shutil.copy2(old, CONFIG_PATH)
        return True
    except Exception:  # noqa
        return False


MIGRATED = _migrate_legacy()

# ----------------------------------------------------------------------------
# 隐藏子进程控制台窗口（ssh/scp 为控制台程序，默认每次调用都会闪一个黑窗口）
# ----------------------------------------------------------------------------
if os.name == "nt":
    CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    _si = subprocess.STARTUPINFO()
    _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _si.wShowWindow = subprocess.SW_HIDE
    HIDE_KW = {"creationflags": CREATE_NO_WINDOW, "startupinfo": _si}
else:
    HIDE_KW = {}

DEFAULT_CONFIG = {
    "sshKeyPath": "%USERPROFILE%\\.ssh\\id_ed25519",
    "theme": "system",          # system / light / dark
    "columnWidths": {},         # 列宽持久化：{列id: 像素宽}
    "sortColumn": "",           # 排序列 id
    "sortDesc": False,          # 是否降序
    "machines": [
        {"name": "PC-01", "ip": "192.168.1.101", "sshUser": "admin",
         "rdp": True, "notes": "示例条目，可直接编辑或删除"},
    ],
}


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg.get("machines"), list):
                cfg["machines"] = []
            if "sshKeyPath" not in cfg or not cfg["sshKeyPath"]:
                cfg["sshKeyPath"] = DEFAULT_CONFIG["sshKeyPath"]
            # 补齐新版本新增字段，兼容旧配置文件
            for k in ("theme", "columnWidths", "sortColumn", "sortDesc"):
                if k not in cfg:
                    cfg[k] = json.loads(json.dumps(DEFAULT_CONFIG[k]))
            if cfg.get("theme") not in ("system", "light", "dark"):
                cfg["theme"] = "system"
            if not isinstance(cfg.get("columnWidths"), dict):
                cfg["columnWidths"] = {}
            return cfg
        except Exception:
            pass
    # 首次运行：写入默认配置
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    save_config(cfg)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------------
# 主题：亮 / 暗 / 跟随系统
#   Windows 主题取自 HKCU\...\Themes\Personalize\AppsUseLightTheme
#   0 = 暗色应用，1 = 亮色应用
# ----------------------------------------------------------------------------
DARK_THEME = "darkly"
LIGHT_THEME = "cosmo"
THEME_LABELS = [("跟随系统", "system"), ("亮色", "light"), ("暗色", "dark")]

# 日志配色：暗色 / 亮色两套
# dim 为次要/细节日志（如 tscon 明细、主题切换提示）。
# 暗色下原 #888888 在 darkly 输入区(#2b2b2b)对比度仅 ~4.1，低于 WCAG AA 4.5，提亮到 #b3b3b3(~6.9)。
# 亮色下 #888888 在白底对比度 ~3.3，调深到 #6f6f6f(~4.6) 提升可读性。
LOG_COLORS = {
    True: {"ok": "#2ecc71", "err": "#ff6b6b", "warn": "#f1c40f",
           "dim": "#b3b3b3", "info": "#dddddd"},
    False: {"ok": "#1e8449", "err": "#c0392b", "warn": "#b7791f",
            "dim": "#6f6f6f", "info": "#222222"},
}


def system_is_dark():
    """读注册表判断系统是否为暗色模式。读取失败时默认暗色。"""
    if os.name != "nt":
        return True
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        try:
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        finally:
            winreg.CloseKey(key)
        return int(val) == 0
    except Exception:  # noqa
        return True


def resolve_dark(mode):
    """把主题模式解析成 是否暗色。"""
    if mode == "light":
        return False
    if mode == "dark":
        return True
    return system_is_dark()


def theme_name(dark):
    return DARK_THEME if dark else LIGHT_THEME


def apply_titlebar(win, dark):
    """Win10 1809+ / Win11：让窗口标题栏跟随暗色，否则深色界面顶着白标题栏。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        if not hwnd:
            return
        value = ctypes.c_int(1 if dark else 0)
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE（旧版本为 19）
        for attr in (20, 19):
            res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
            if res == 0:
                break
    except Exception:  # noqa
        pass


# ----------------------------------------------------------------------------
# 自绘对话框：tkinter 原生 messagebox 是 Tk 内置窗口，不受 ttk 主题控制，
# 在暗色界面下永远是亮色，造成"亮暗混杂"。这里统一用 ttkb.Toplevel 重写。
# ----------------------------------------------------------------------------
_ICONS = {"info": ("i", "info"), "ok": ("OK", "success"),
          "warn": ("!", "warning"), "err": ("X", "danger"),
          "ask": ("?", "primary")}


def _center_on(win, parent):
    win.update_idletasks()
    try:
        pw, ph = parent.winfo_width(), parent.winfo_height()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        if pw <= 1:
            raise ValueError
    except Exception:  # noqa
        pw = ph = px = py = 0
    w, h = win.winfo_width(), win.winfo_height()
    if pw > 1:
        x, y = px + (pw - w) // 2, py + (ph - h) // 3
    else:
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 3
    win.geometry("+%d+%d" % (max(x, 0), max(y, 0)))


def show_dialog(parent, title, message, kind="info", ask=False,
                width=None, dark=True, scroll=False):
    """统一主题化对话框。ask=True 时返回 True/False，否则返回 None。"""
    if parent is None:
        # 兜底：主窗口尚未建立时退回原生弹窗，保证异常不会静默丢失
        if ask:
            return messagebox.askyesno(title, message)
        messagebox.showinfo(title, message)
        return None

    win = ttkb.Toplevel(parent)
    set_window_icon(win)
    win.title(title)
    win.transient(parent)
    win.resizable(False, False)
    apply_titlebar(win, dark)

    glyph, style = _ICONS.get(kind, _ICONS["info"])
    body = ttkb.Frame(win, padding=(18, 16, 18, 10))
    body.pack(fill=BOTH, expand=YES)

    top = ttkb.Frame(body)
    top.pack(fill=BOTH, expand=YES)
    ttkb.Label(top, text=glyph, bootstyle=style,
               font=("Segoe UI", 18, "bold"), width=3,
               anchor=CENTER).pack(side=LEFT, anchor=N, padx=(0, 10))

    if scroll:
        txt = ttkb.ScrolledText(top, height=18, width=68, autohide=True,
                                font=("Segoe UI", 9))
        txt.pack(side=LEFT, fill=BOTH, expand=YES)
        txt.text.insert("1.0", message)
        txt.text.configure(state=DISABLED)
        win.resizable(True, True)
    else:
        ttkb.Label(top, text=message, justify=LEFT, anchor=W,
                   wraplength=(width or 420)).pack(side=LEFT, fill=BOTH,
                                                   expand=YES)

    res = {"v": False}

    def close(v):
        res["v"] = v
        win.destroy()

    btns = ttkb.Frame(body)
    btns.pack(fill=X, pady=(14, 0))
    if ask:
        ttkb.Button(btns, text="确定", bootstyle="danger", width=10,
                    command=lambda: close(True)).pack(side=RIGHT, padx=(6, 0))
        ttkb.Button(btns, text="取消", bootstyle="secondary", width=10,
                    command=lambda: close(False)).pack(side=RIGHT)
        win.bind("<Escape>", lambda e: close(False))
    else:
        b = ttkb.Button(btns, text="确定", bootstyle=style, width=10,
                        command=lambda: close(True))
        b.pack(side=RIGHT)
        b.focus_set()
        win.bind("<Return>", lambda e: close(True))
        win.bind("<Escape>", lambda e: close(True))

    _center_on(win, parent)
    win.grab_set()
    win.wait_window()
    return res["v"] if ask else None


# ----------------------------------------------------------------------------
# 底层 SSH / SCP 封装（与 .ps1 机制一致）
# ----------------------------------------------------------------------------
def ssh_opts(key_path):
    return ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes", "-i", key_path]


# SSH 主机密钥变更的报错关键字；命中后自动移除旧记录并重试，免去用户手动执行 ssh-keygen -R
_HOST_KEY_ERR = "REMOTE HOST IDENTIFICATION HAS CHANGED"


def _try_fix_host_key(ip, log):
    """主机密钥变更时，移除 known_hosts 中该主机的旧记录，使下次连接能自动接受新密钥。
    仅删除该 IP 对应的条目（ssh-keygen -R），不影响其他主机，也不删除任何密钥文件。"""
    try:
        subprocess.run(
            ["ssh-keygen", "-R", ip],
            capture_output=True, text=True, timeout=20,
            stdin=subprocess.DEVNULL, **HIDE_KW)
        if log:
            log(f"[SSH] {ip} 主机密钥已变更，已自动移除旧信任记录并重试连接", "warn")
        return True
    except Exception as e:  # noqa
        if log:
            log(f"[SSH] 自动移除 {ip} 旧密钥记录失败: {e}", "err")
        return False


def run_ssh(user, ip, cmd, key, timeout=40, log=None):
    """返回 (returncode, output)。cmd 作为单个参数传给远程 shell。
    若遇到主机密钥变更 (REMOTE HOST IDENTIFICATION HAS CHANGED)，自动移除旧记录并重试一次，
    无需用户手动执行 ssh-keygen -R。"""
    def _once():
        proc = subprocess.run(
            ["ssh"] + ssh_opts(key) + [f"{user}@{ip}", cmd],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, **HIDE_KW)
        return proc, (proc.stdout or "") + (proc.stderr or "")
    try:
        proc, out = _once()
        if _HOST_KEY_ERR in out:
            if _try_fix_host_key(ip, log):
                proc, out = _once()
        return proc.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    except Exception as e:  # noqa
        return -1, str(e)


def run_scp(local, remote, user, ip, key, timeout=30, log=None):
    """上传文件。遇到主机密钥变更同样自动移除旧记录并重试一次。"""
    def _once():
        proc = subprocess.run(
            ["scp"] + ssh_opts(key) + [local, f"{user}@{ip}:{remote}"],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, **HIDE_KW)
        return proc, (proc.stdout or "") + (proc.stderr or "")
    try:
        proc, out = _once()
        if _HOST_KEY_ERR in out:
            if _try_fix_host_key(ip, log):
                proc, out = _once()
        return proc.returncode
    except Exception:  # noqa
        return -1


def rdp_session_id(user, ip, key, log=None):
    """返回当前 RDP 会话的 SID（如 '1'），无则空字符串。

    注意：query session 在「连上但未登录」时该行没有用户名，列会左移，
    例如 'rdp-tcp#0   1   Connected'，因此不能固定取第 3 段。
    正确做法：找 rdp-tcp# 之后的第一个纯数字 token 作为会话 ID。
    """
    rc, out = run_ssh(user, ip, 'query session 2>nul | findstr rdp-tcp#', key, log=log)
    for line in (out or "").splitlines():
        line = line.strip()
        if "rdp-tcp#" not in line:
            continue
        for tok in line.split()[1:]:
            if tok.isdigit():
                return tok
    return ""


def rdp_session_authed(user, ip, key, log=None):
    """RDP 会话是否已登录（有用户名）。

    已登录:   'rdp-tcp#0   USER   1   Active'（rdp-tcp# 后首个 token 是用户名）
    仅连接未登录: 'rdp-tcp#0   1   Connected'（rdp-tcp# 后首个 token 是数字 ID，无用户名）
    tscon 只能把「已登录」的 RDP 会话切回控制台实现解锁；未登录（无凭据）无法解锁。
    """
    rc, out = run_ssh(user, ip, 'query session 2>nul | findstr rdp-tcp#', key, log=log)
    for line in (out or "").splitlines():
        line = line.strip()
        if "rdp-tcp#" not in line:
            continue
        for tok in line.split()[1:]:
            if tok.isdigit():
                return False
            return True
    return False


# ----------------------------------------------------------------------------
# 业务操作
# ----------------------------------------------------------------------------
DET_CMD = (
    'powershell -NoProfile -Command '
    '"if(Get-Process -Name logonui -ErrorAction SilentlyContinue)'
    '{Write-Output RL_LOCKED}else{Write-Output RL_UNLOCKED}"'
)


def detect_state(m, key, log):
    """检测远程机器当前是否处于锁屏状态。

    返回 (rc, state)，state 取值：
        'locked'   - logonui.exe 存在，判断为已锁屏
        'unlocked' - logonui.exe 不存在，判断为未锁屏
        'offline'  - SSH 连接失败/超时
        'unknown'  - 输出无法识别

    说明：Windows 锁屏时会拉起 logonui.exe 显示安全桌面；用户已登录且
    未锁屏时 logonui.exe 通常不存在。此为经验性检测，若与实际情况不符
    请反馈。
    """
    user, ip = m["sshUser"], m["ip"]
    rc, out = run_ssh(user, ip, DET_CMD, key, timeout=15, log=log)
    if rc != 0:
        return rc, "offline"
    if "RL_LOCKED" in out:
        return rc, "locked"
    if "RL_UNLOCKED" in out:
        return rc, "unlocked"
    return rc, "unknown"


def lock_machine(m, key, log):
    """内联 SSH 一次性下发 schtasks，不再上传 bat，被控机零文件落地。"""
    name, ip, user = m["name"], m["ip"], m["sshUser"]
    task = "QLock_" + "".join(random.choices(string.digits, k=8))
    cmd = (
        f'schtasks /create /sc once /st 00:00 /tn {task} '
        f'/tr "rundll32.exe user32.dll,LockWorkStation" /f >nul 2>&1 '
        f'&& schtasks /run /tn {task} >nul 2>&1 '
        f'&& echo LOCK_OK '
        f'& ping -n 3 127.0.0.1 >nul '
        f'& schtasks /delete /tn {task} /f >nul 2>&1'
    )
    log(f"[{name}] 下发锁屏指令...")
    rc, out = run_ssh(user, ip, cmd, key, timeout=25, log=log)
    out = out or ""
    if "LOCK_OK" in out:
        log(f"[{name}] 已锁屏 OK", "ok")
        return True
    if rc in (-1, 255) and not out.strip():
        log(f"[{name}] 离线 (SSH 不可达)", "warn")
        return False
    log(f"[{name}] 锁屏失败 rc={rc} {out.strip()[:80]}", "err")
    return False


def unlock_machine(m, key, log):
    name, ip, user = m["name"], m["ip"], m["sshUser"]
    if not m.get("rdp", True):
        log(f"[{name}] 跳过 - 无 RDP (家庭版)", "warn")
        return None
    remote_batch = "C:\\Windows\\Temp\\unlock_tscon.bat"

    # 1. 关闭 NLA
    log(f"[{name}] [1] 关闭 NLA...")
    cmd1 = ('reg add "HKLM\\SYSTEM\\CurrentControlSet\\Control\\Terminal Server\\WinStations\\RDP-Tcp" '
            '/v UserAuthentication /t REG_DWORD /d 0 /f >nul 2>&1 & '
            'reg add "HKLM\\SYSTEM\\CurrentControlSet\\Control\\Terminal Server\\WinStations\\RDP-Tcp" '
            '/v SecurityLayer /t REG_DWORD /d 0 /f >nul 2>&1 & echo DONE')
    rc, out = run_ssh(user, ip, cmd1, key, log=log)
    if "DONE" in out:
        log(f"[{name}]     NLA 已关闭", "ok")
    else:
        log(f"[{name}]     NLA 警告: {out[:80]}", "warn")

    # 2. 部署 tscon 包装 bat
    log(f"[{name}] [2] 部署 tscon bat...")
    tscon_bat = (
        "@echo off\r\n"
        "setlocal\r\n"
        "if exist C:\\Windows\\Sysnative\\tscon.exe (set TC=C:\\Windows\\Sysnative\\tscon.exe) "
        "else (set TC=C:\\Windows\\System32\\tscon.exe)\r\n"
        "%TC% %1 /dest:console > C:\\Windows\\Temp\\tscon_result.txt 2>&1\r\n"
        "echo TSCON_RC=%errorlevel% >> C:\\Windows\\Temp\\tscon_result.txt\r\n"
    )
    # 并发解锁时每台机器必须使用独立的本地临时文件，
    # 否则多线程会轮流写/删同一个 unlock_tscon.bat，导致其他线程 scp 读不到文件而失败。
    fd, local_bat = tempfile.mkstemp(suffix=".bat", prefix=f"unlock_tscon_{ip.replace('.', '_')}_")
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="") as f:
            f.write(tscon_bat)
        rc = run_scp(local_bat, remote_batch, user, ip, key, log=log)
    finally:
        try: os.remove(local_bat)
        except Exception: pass
    if rc != 0:
        log(f"[{name}]     离线 (scp 失败)", "warn")
        return False
    log(f"[{name}]     tscon bat OK", "ok")

    # 3. 启动 mstsc（若弹出凭据框请登录）
    log(f"[{name}] [3] 启动 mstsc（如需凭据请登录）...")
    try:
        mstsc = subprocess.Popen(["mstsc", f"/v:{ip}", "/w:1920", "/h:1080"])
    except Exception as e:  # noqa
        log(f"[{name}]     无法启动 mstsc: {e}", "err")
        run_ssh(user, ip, f"del /q {remote_batch}", key, log=log)
        return False
    time.sleep(0.8)
    if mstsc.poll() is not None:
        log(f"[{name}]     mstsc 已退出", "err")
        run_ssh(user, ip, f"del /q {remote_batch}", key, log=log)
        return False

    # 4. 轮询 RDP 会话
    log(f"[{name}] [4] 等待 RDP 会话...")
    sid = ""
    for _ in range(20):
        sid = rdp_session_id(user, ip, key, log=log)
        if sid:
            break
        time.sleep(0.6)
    if not sid:
        log(f"[{name}]     RDP 未连上，解锁失败", "err")
        try: mstsc.terminate()
        except Exception: pass
        run_ssh(user, ip, f"del /q {remote_batch}", key, log=log)
        return False
    log(f"[{name}]     会话 SID={sid}", "ok")
    if not sid.isdigit():
        log(f"[{name}]     RDP 会话 SID 解析异常: {sid!r}，解锁失败", "err")
        try: mstsc.terminate()
        except Exception: pass
        run_ssh(user, ip, f"del /q {remote_batch}", key, log=log)
        return False
    # RDP 会话必须已登录（有用户名）才能 tscon 解锁；仅连上未登录（无凭据）无法解锁
    if not rdp_session_authed(user, ip, key, log=log):
        log(f"[{name}]     RDP 会话未登录（多半无 RDP 凭据），无法解锁。"
            f"请先「部署密钥」或手动 mstsc /v:{ip} 登录一次并保存凭据", "err")
        try: mstsc.terminate()
        except Exception: pass
        run_ssh(user, ip, f"del /q {remote_batch}", key, log=log)
        return False

    # 5. schtasks 触发 tscon
    log(f"[{name}] [5] 触发 tscon (schtasks /ru SYSTEM)...")
    sch = (f'schtasks /create /sc once /st 00:00 /tn TSCON_UNLOCK '
           f'/tr "{remote_batch} {sid}" /ru SYSTEM /f >nul 2>&1 & '
           f'schtasks /run /tn TSCON_UNLOCK >nul 2>&1 & echo SCHTASKS_OK')
    rc, out = run_ssh(user, ip, sch, key, log=log)
    if "SCHTASKS_OK" in out:
        log(f"[{name}]     已触发", "ok")
    else:
        log(f"[{name}]     警告: {out[:80]}", "warn")
    # 等 tscon 真正执行完并写出结果（schtasks 异步，且切换会话本身需要时间）
    time.sleep(2.0)

    # 读取 tscon 结果（重试，避免 schtasks 尚未写出就被读到空）
    tscon_str = ""
    for _ in range(4):
        rc, out2 = run_ssh(user, ip, "type C:\\Windows\\Temp\\tscon_result.txt 2>nul", key, log=log)
        tscon_str = (out2 or "").strip()
        if tscon_str:
            break
        time.sleep(0.7)
    if tscon_str:
        log(f"[{name}]     [tscon] {tscon_str}", "dim")
    # bat 已结束，再删 schtasks 任务（提前删可能杀掉正在写结果的 bat）
    run_ssh(user, ip, "schtasks /delete /tn TSCON_UNLOCK /f >nul 2>&1", key, log=log)

    # 6. 验证会话已切换
    log(f"[{name}] [6] 验证会话切换...")
    switched = False
    for _ in range(8):
        time.sleep(0.8)
        if not rdp_session_id(user, ip, key, log=log):
            switched = True
            break

    # 7. 关闭 mstsc
    log(f"[{name}] [7] 关闭 mstsc...")
    try:
        mstsc.terminate()
        time.sleep(0.5)
        if mstsc.poll() is None:
            mstsc.kill()
    except Exception:
        pass

    # 8. 最终判定
    log(f"[{name}] [8] 最终判定...")
    time.sleep(0.5)
    cleanup = f"del /q {remote_batch} C:\\Windows\\Temp\\tscon_result.txt"
    # 解析 TSCON_RC（bat 末尾 echo TSCON_RC=<n>）
    tscon_rc = None
    for _line in tscon_str.splitlines():
        if _line.strip().startswith("TSCON_RC="):
            try:
                tscon_rc = int(_line.strip().split("=", 1)[1])
            except Exception:
                pass
            break

    success = False
    if tscon_rc == 0:
        success = True
        log(f"[{name}] 解锁成功 (tscon rc=0)", "ok")
    elif tscon_rc is not None:
        log(f"[{name}] 解锁失败: tscon rc={tscon_rc}（{tscon_str}）", "err")
    elif switched:
        # 无 RC 文本但 RDP 会话已消失，视为切换成功
        success = True
        log(f"[{name}] 解锁成功 (rdp-tcp# 已消失)", "ok")
    else:
        log(f"[{name}] 解锁失败: tscon 无结果且会话未切换 (tscon={tscon_str!r})", "err")
    run_ssh(user, ip, cleanup, key, log=log)
    return success


# ----------------------------------------------------------------------------
# 一键部署：用密码登录一次，把控制端公钥写入被控机，之后全程走密钥
# 密码仅存在于内存，绝不写入 machines.json
# ----------------------------------------------------------------------------
def ensure_local_keypair(key_path, log):
    """确保本地存在密钥对，缺失则自动生成。返回公钥文本，失败返回 None。"""
    key_path = os.path.expandvars(key_path)
    pub_path = key_path + ".pub"
    if not os.path.exists(key_path):
        log(f"本地私钥不存在，自动生成: {key_path}", "warn")
        os.makedirs(os.path.dirname(key_path), exist_ok=True)
        try:
            proc = subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", "", "-q"],
                capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL, **HIDE_KW)
            if proc.returncode != 0:
                log(f"ssh-keygen 失败: {(proc.stderr or proc.stdout)[:120]}", "err")
                return None
        except Exception as e:  # noqa
            log(f"ssh-keygen 异常: {e}", "err")
            return None
        log("密钥对已生成", "ok")
    if not os.path.exists(pub_path):
        log(f"找不到公钥文件: {pub_path}", "err")
        return None
    try:
        with open(pub_path, "r", encoding="utf-8", errors="ignore") as f:
            pub = f.read().strip()
    except Exception as e:  # noqa
        log(f"读取公钥失败: {e}", "err")
        return None
    if not pub.startswith("ssh-"):
        log("公钥内容格式异常", "err")
        return None
    return pub


def _ps_encoded(script):
    """把 PowerShell 脚本编码为 -EncodedCommand 参数。
    补空格使字符数为 3 的倍数，令 base64 无 '=' 填充（'=' 在 cmd 中是参数分隔符）。
    """
    while len(script) % 3 != 0:
        script += " "
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


_PS_AUTOSTART = (
    "if ($isAdmin) { Set-Service sshd -StartupType Automatic; "
    "$svcState = if ($?) { 'AUTO' } else { 'FAIL' } }"
)
_PS_ENABLE_RDP = (
    "if ($isAdmin -and $rdpCapable) { "
    "Set-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Terminal Server' "
    "-Name fDenyTSConnections -Value 0; "
    "Get-NetFirewallRule -Group '@FirewallAPI.dll,-28752' | Enable-NetFirewallRule; "
    "$rdpState = 'ENABLED' } elseif (-not $rdpCapable) { $rdpState = 'NO_RDP' }"
)


def _deploy_script(pub, set_autostart, enable_rdp):
    """生成被控端执行的 PowerShell 部署脚本。"""
    pub_esc = pub.replace("'", "''")
    ps_autostart = _PS_AUTOSTART if set_autostart else ""
    ps_rdp = _PS_ENABLE_RDP if enable_rdp else ""
    return f"""
$ErrorActionPreference = 'SilentlyContinue'
$pub = '{pub_esc}'
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {{
  $dir = 'C:\\ProgramData\\ssh'
  $file = 'C:\\ProgramData\\ssh\\administrators_authorized_keys'
}} else {{
  $dir = Join-Path $env:USERPROFILE '.ssh'
  $file = Join-Path $dir 'authorized_keys'
}}
if (-not (Test-Path $dir)) {{ New-Item -ItemType Directory -Path $dir -Force | Out-Null }}
$existing = ''
if (Test-Path $file) {{ $existing = [IO.File]::ReadAllText($file) }}
if ($existing -eq $null) {{ $existing = '' }}
if ($existing.Contains($pub)) {{
  $keyState = 'EXISTS'
}} else {{
  $new = $existing
  if ($new.Length -gt 0 -and -not $new.EndsWith("`n")) {{ $new += "`r`n" }}
  $new += $pub + "`r`n"
  [IO.File]::WriteAllText($file, $new, (New-Object Text.UTF8Encoding $false))
  $keyState = 'ADDED'
}}
if ($isAdmin) {{
  icacls $file /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null
}} else {{
  icacls $file /inheritance:r /grant ('{{0}}:F' -f $env:USERNAME) /grant '*S-1-5-18:F' | Out-Null
}}
$aclState = if ($?) {{ 'ACL_OK' }} else {{ 'ACL_WARN' }}
$svcState = 'SKIP'
{ps_autostart}
$edition = (Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion' -Name EditionID).EditionID
if (-not $edition) {{ $edition = 'Unknown' }}
$rdpCapable = -not ($edition -like 'Core*')
$rdpState = 'SKIP'
{ps_rdp}
$ds = (Get-ItemProperty 'HKLM:\\SOFTWARE\\OpenSSH' -Name DefaultShell).DefaultShell
if (-not $ds) {{ $ds = 'cmd' }}
Write-Output ('RL_RESULT|' + $keyState + '|' + $aclState + '|' + $svcState + '|' + $rdpState + '|' + $edition + '|' + $ds + '|admin=' + $isAdmin)
"""


def deploy_key(m, password, key_path, log, set_autostart=True, enable_rdp=True):
    """用密码登录被控机并部署公钥。返回 dict 结果，失败返回 None。"""
    name, ip, user = m["name"], m["ip"], m["sshUser"]
    if paramiko is None:
        log(f"[{name}] paramiko 未安装，无法部署", "err")
        return None
    pub = ensure_local_keypair(key_path, log)
    if not pub:
        return None

    log(f"[{name}] [1] 密码登录 {user}@{ip} ...")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(ip, port=22, username=user, password=password,
                    timeout=10, banner_timeout=15, auth_timeout=15,
                    allow_agent=False, look_for_keys=False)
    except Exception as e:  # noqa
        et = type(e).__name__
        if "Authentication" in et:
            log(f"[{name}] 密码认证失败（账号或密码错误）", "err")
        elif "NoValidConnections" in et or "timed out" in str(e).lower():
            log(f"[{name}] 连不上 22 端口（SSH 服务未开 / 防火墙未放通）", "err")
        else:
            log(f"[{name}] 登录失败: {et} {str(e)[:80]}", "err")
        try: cli.close()
        except Exception: pass
        return None
    log(f"[{name}]     登录成功", "ok")

    log(f"[{name}] [2] 写入公钥并配置 ...")
    script = _deploy_script(pub, set_autostart, enable_rdp)
    cmd = "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand " + _ps_encoded(script)
    if len(cmd) > 7500:  # Windows cmd 命令行上限 8191
        log(f"[{name}] 警告: 部署命令过长 ({len(cmd)})，公钥可能过大，建议改用 ed25519 密钥", "warn")
    try:
        stdin, stdout, stderr = cli.exec_command(cmd, timeout=60)
        out = stdout.read().decode("utf-8", "ignore")
        err = stderr.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa
        log(f"[{name}] 执行部署脚本失败: {e}", "err")
        try: cli.close()
        except Exception: pass
        return None
    finally:
        try: cli.close()
        except Exception: pass

    line = next((l for l in (out or "").splitlines() if l.startswith("RL_RESULT|")), "")
    if not line:
        log(f"[{name}] 部署脚本无有效返回: {(out + err).strip()[:120]}", "err")
        return None
    parts = line.strip().split("|")
    res = {
        "key": parts[1] if len(parts) > 1 else "?",
        "acl": parts[2] if len(parts) > 2 else "?",
        "svc": parts[3] if len(parts) > 3 else "?",
        "rdp": parts[4] if len(parts) > 4 else "?",
        "edition": parts[5] if len(parts) > 5 else "?",
        "shell": parts[6] if len(parts) > 6 else "?",
        "admin": parts[7] if len(parts) > 7 else "?",
    }
    log(f"[{name}]     公钥 {res['key']} / {res['acl']} / 版本 {res['edition']}", "ok")
    if res["svc"] != "SKIP":
        log(f"[{name}]     sshd 自启: {res['svc']}", "ok" if res["svc"] == "AUTO" else "warn")
    if res["rdp"] != "SKIP":
        log(f"[{name}]     RDP: {res['rdp']}", "ok" if res["rdp"] == "ENABLED" else "warn")
    if "powershell" in (res["shell"] or "").lower():
        log(f"[{name}]     注意: 默认 shell 为 PowerShell，锁/解锁命令按 cmd 语法编写，"
            f"如失败请将 HKLM\\SOFTWARE\\OpenSSH\\DefaultShell 设为 cmd.exe", "warn")

    log(f"[{name}] [3] 用密钥验证连接 ...")
    rc, vout = run_ssh(user, ip, "echo RL_KEY_OK", os.path.expandvars(key_path), timeout=15)
    if "RL_KEY_OK" in (vout or ""):
        log(f"[{name}] 部署成功，密钥登录已生效", "ok")
        res["verified"] = True
    else:
        log(f"[{name}] 公钥已写入，但密钥登录验证失败 rc={rc} {(vout or '')[:80]}", "err")
        res["verified"] = False
    res["rdpCapable"] = not (res["edition"] or "").startswith("Core")
    return res


def store_rdp_credential(ip, user, password, log=None):
    """把 RDP 凭据写入 Windows 凭据管理器，使 mstsc 解锁时能自动登录。

    必须用 /generic 写入 LegacyGeneric(普通) 类型：mstsc 在 `mstsc /v:<ip>` 时只读取
    LegacyGeneric 类型的 TERMSRV/<ip> 凭据；用 /add 写的是 Domain(域密码) 类型，
    mstsc 完全不认，会导致连接时停在远程登录界面、无法自动登录（v1.0.13 的坑）。
    写入前先 /delete 清掉该目标可能残留的旧凭据（含 Domain 类型），避免类型冲突。
    密码仅在本次调用以命令行参数传给 cmdkey，不写入任何配置文件（machines.json 等）。
    """
    target = "TERMSRV/" + ip
    try:
        # 清掉可能残留的旧条目（Domain 类型 mstsc 不读，留着无用且易混淆）
        try:
            subprocess.run(["cmdkey", "/delete:" + target], capture_output=True,
                           text=True, timeout=15, **HIDE_KW)
        except Exception:  # noqa
            pass
        proc = subprocess.run(
            ["cmdkey", "/generic:" + target, "/user:" + user, "/pass:" + password],
            capture_output=True, text=True, timeout=15, **HIDE_KW)
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0:
            if log:
                log(f"  RDP 凭据已存入凭据管理器（{user}@{ip}，LegacyGeneric），解锁将自动登录", "ok")
            return True
        if log:
            log(f"  RDP 凭据存储失败 rc={proc.returncode}: {out.strip()[:160]}", "err")
        return False
    except Exception as e:  # noqa
        if log:
            log(f"  RDP 凭据存储异常: {e}", "err")
        return False


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
class ScrollableFrame(ttkb.Frame):
    """Canvas + 竖向滚动条容器，内部 self.inner 用网格逐行装载机器。

    ttkbootstrap 没有 .scrolled 模块，这里自绘等价容器。
    """

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        self.vsb = ttkb.Scrollbar(self, orient=VERTICAL,
                                  command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side=RIGHT, fill=Y)
        self.canvas.pack(side=LEFT, fill=BOTH, expand=YES)
        self.inner = ttkb.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor=NW)
        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfig(self._win, width=e.width))
        # 仅当光标在列表区时响应滚轮，避免影响其他控件
        self.canvas.bind("<Enter>",
                         lambda e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>",
                         lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class RemoteLockApp:
    # 列定义：(id, 标题, 像素宽, 是否可排序)。"选择"和"操作"列不参与排序。
    LIST_COLS = [
        ("sel",    "选择",   46, False),  # 不参与排序
        ("name",   "名称",  100, True),
        ("ip",     "IP",    120, True),
        ("user",   "SSH用户", 90, True),
        ("rdp",    "RDP",    46, True),
        ("status", "状态",   80, True),
        ("ops",    "操作",  112, False), # 不参与排序；放备注前，避免被挤到显示区外
        ("notes",  "备注",  160, True),  # 最后一列，弹性填充
    ]
    COL_W = {cid: w for cid, _, w, _ in LIST_COLS}
    COL_IDX = {}
    for _i, (_cid, _l, _w, _s) in enumerate(LIST_COLS):
        COL_IDX[_cid] = _i

    def __init__(self, root):
        self.root = root
        set_window_icon(root)
        self.cfg = load_config()
        self.selection = {}        # name -> bool（勾选用于本次操作）
        self.status = {}           # name -> 文本
        self.busy = False
        self.cells = {}            # name -> {col_id: widget}
        self.sel_vars = {}         # name -> BooleanVar
        self.focused_row = None    # 当前单击选中的行（用于编辑/删除）
        self._hl = None            # 已高亮的行
        self._normal_bg = "SystemButtonFace"

        # 主题状态
        self.theme_mode = self.cfg.get("theme", "system")
        self.dark = resolve_dark(self.theme_mode)
        self._last_sys_dark = system_is_dark()

        self._hl_bg = "#dbeafe" if self.dark else "#33415c"

        # 排序状态
        self.sort_col = self.cfg.get("sortColumn", "") or ""
        self.sort_desc = bool(self.cfg.get("sortDesc", False))

        root.title(APP_TITLE + " - 远程锁屏管理")
        root.geometry("980x640")
        root.minsize(820, 560)

        # 顶部标题
        hdr = ttkb.Frame(root)
        hdr.pack(fill=X, padx=10, pady=(10, 4))
        ttkb.Label(hdr, text=APP_NAME, font=("Segoe UI", 16, "bold"),
                   bootstyle="warning").pack(side=LEFT)
        ttkb.Label(hdr, text="v" + APP_VERSION, font=("Segoe UI", 10, "bold"),
                   bootstyle="info").pack(side=LEFT, padx=(6, 0), pady=(6, 0))
        ttkb.Label(hdr, text="远程锁屏 / 解锁管理", font=("Segoe UI", 10),
                   bootstyle="secondary").pack(side=LEFT, padx=(10, 0), pady=(6, 0))

        # 主题切换（右上角）
        ttkb.Button(hdr, text="关于", bootstyle="link",
                    command=self.show_about).pack(side=RIGHT)
        self.theme_var = tk.StringVar(
            value=next(l for l, v in THEME_LABELS if v == self.theme_mode))
        cb = ttkb.Combobox(hdr, textvariable=self.theme_var, width=9,
                           state="readonly",
                           values=[l for l, _ in THEME_LABELS])
        cb.pack(side=RIGHT, padx=(4, 10))
        cb.bind("<<ComboboxSelected>>", self.on_theme_change)
        ttkb.Label(hdr, text="主题:", bootstyle="secondary").pack(side=RIGHT)

        # 工具栏
        bar = ttkb.Frame(root)
        bar.pack(fill=X, padx=10, pady=4)
        ttkb.Button(bar, text="新增", bootstyle="success-outline", width=8,
                    command=self.add_machine).pack(side=LEFT, padx=2)
        ttkb.Button(bar, text="编辑", bootstyle="info-outline", width=8,
                    command=self.edit_selected).pack(side=LEFT, padx=2)
        ttkb.Button(bar, text="删除", bootstyle="danger-outline", width=8,
                    command=self.delete_selected).pack(side=LEFT, padx=2)
        ttkb.Button(bar, text="测试连接", bootstyle="secondary", width=10,
                    command=self.test_selected).pack(side=LEFT, padx=2)
        ttkb.Button(bar, text="部署密钥", bootstyle="primary", width=10,
                    command=self.deploy_selected).pack(side=LEFT, padx=2)
        ttkb.Button(bar, text="全选", bootstyle="secondary", width=8,
                    command=lambda: self.set_all_selection(True)).pack(side=LEFT, padx=2)
        ttkb.Button(bar, text="全不选", bootstyle="secondary", width=8,
                    command=lambda: self.set_all_selection(False)).pack(side=LEFT, padx=2)
        ttkb.Button(bar, text="部署指引", bootstyle="link",
                    command=self.show_deploy_help).pack(side=RIGHT, padx=2)

        # 机器列表（自定义行列表，替代 Treeview；支持点击表头排序，每行带独立锁定/解锁按钮）
        self.lock_closed_img = tk.PhotoImage(data=LOCK_CLOSED_B64)
        self.lock_open_img = tk.PhotoImage(data=LOCK_OPEN_B64)
        self.detect_img = tk.PhotoImage(data=DETECT_B64)

        # 图标按钮紧凑化：彩色填充样式减小内边距，使图标按钮接近方形
        _st = ttkb.Style()
        for _bs in ("warning.TButton", "success.TButton", "info.TButton"):
            _st.configure(_bs, padding=(3, 3))

        # 列宽持久化（沿用旧版 columnWidths 配置键）
        saved_w = self.cfg.get("columnWidths", {})
        for cid in saved_w:
            if cid in self.COL_W:
                self.COL_W[cid] = int(saved_w[cid])

        # 滚动容器：Canvas + 竖向滚动条，内部 self.inner 用网格布局逐行装载
        self.list_frame = ScrollableFrame(root)
        self.list_frame.pack(fill=BOTH, expand=YES, padx=10, pady=4)
        self.inner = self.list_frame.inner
        for c, (cid, _label, _w, _s) in enumerate(self.LIST_COLS):
            self.inner.columnconfigure(
                c, minsize=self.COL_W[cid], weight=(1 if cid == "notes" else 0))
        self._build_header()
        self._hl_bg = "#dbeafe" if self.dark else "#33415c"

        # 操作按钮区（锁定/解锁/检测；白图标配彩色填充按钮，避免顺色）
        act = ttkb.Frame(root)
        act.pack(fill=X, padx=10, pady=4)
        ttkb.Button(act, text="锁定选中", image=self.lock_closed_img,
                    compound=LEFT, bootstyle="warning", width=14,
                    command=lambda: self.run_action("lock", "selected")).pack(side=LEFT, padx=4)
        ttkb.Button(act, text="解锁选中", image=self.lock_open_img,
                    compound=LEFT, bootstyle="warning", width=14,
                    command=lambda: self.run_action("unlock", "selected")).pack(side=LEFT, padx=4)
        ttkb.Button(act, text="锁定全部", image=self.lock_closed_img,
                    compound=LEFT, bootstyle="success", width=14,
                    command=lambda: self.run_action("lock", "all")).pack(side=LEFT, padx=4)
        ttkb.Button(act, text="解锁全部", image=self.lock_open_img,
                    compound=LEFT, bootstyle="success", width=14,
                    command=lambda: self.run_action("unlock", "all")).pack(side=LEFT, padx=4)
        ttkb.Button(act, text="检测状态", image=self.detect_img,
                    compound=LEFT, bootstyle="info", width=14,
                    command=lambda: self.run_action("detect", "selected")).pack(side=LEFT, padx=4)

        # 设置：SSH 密钥
        setf = ttkb.Frame(root)
        setf.pack(fill=X, padx=10, pady=(2, 4))
        ttkb.Label(setf, text="SSH 私钥:", bootstyle="secondary").pack(side=LEFT)
        self.key_var = tk.StringVar(value=self.cfg.get("sshKeyPath", ""))
        self.key_entry = ttkb.Entry(setf, textvariable=self.key_var, width=70)
        self.key_entry.pack(side=LEFT, padx=4, fill=X, expand=YES)
        ttkb.Button(setf, text="保存设置", bootstyle="info", width=10,
                    command=self.save_settings).pack(side=LEFT, padx=4)

        # 日志面板
        ttkb.Label(root, text="运行日志", bootstyle="secondary").pack(anchor=W, padx=12)
        self.log = ttkb.ScrolledText(root, height=10, autohide=True,
                                     font=("Consolas", 9))
        self.log.pack(fill=BOTH, expand=YES, padx=10, pady=(2, 8))
        self.log.text.configure(state=DISABLED)

        self.rebuild_rows()

    # ---- 列表操作（自定义行列表，替代 Treeview）----
    def _sort_key(self, cid):
        """返回用于排序的 key 函数。IP 按数值段排，避免 .10 排在 .9 前面。"""
        def ip_key(m):
            try:
                return tuple(int(p) for p in m["ip"].split("."))
            except Exception:  # noqa
                return (0, 0, 0, 0)

        return {
            "name": lambda m: m.get("name", "").lower(),
            "ip": ip_key,
            "user": lambda m: m.get("sshUser", "").lower(),
            "rdp": lambda m: not m.get("rdp", True),
            "status": lambda m: self.status.get(m["name"], "-"),
            "notes": lambda m: m.get("notes", "").lower(),
        }.get(cid, lambda m: m.get("name", ""))

    def sort_by(self, cid):
        """点击表头排序：同列再点切换升/降序。'选择'/'操作'列不可排序。"""
        sortable = next((s for c, _, _, s in self.LIST_COLS if c == cid), False)
        if not sortable:
            return
        if self.sort_col == cid:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = cid, False
        self.cfg["sortColumn"] = self.sort_col
        self.cfg["sortDesc"] = self.sort_desc
        save_config(self.cfg)
        self.rebuild_rows()

    def _update_header(self):
        for cid, lbl in self.header.items():
            label = next(t for c, t, _, _ in self.LIST_COLS if c == cid)
            text = label
            if cid == self.sort_col:
                text += "  ▼" if self.sort_desc else "  ▲"
            lbl.configure(text=text)

    def _build_header(self):
        self.header = {}
        for c, (cid, label, _w, sortable) in enumerate(self.LIST_COLS):
            lbl = ttkb.Label(self.inner, text=label, anchor=CENTER,
                             font=("Segoe UI", 9, "bold"), bootstyle="secondary")
            lbl.grid(row=0, column=c, sticky="ew", padx=1, pady=3)
            if sortable:
                lbl.configure(cursor="hand2")
                lbl.bind("<Button-1>", lambda e, cid=cid: self.sort_by(cid))
            self.header[cid] = lbl

    def rebuild_rows(self):
        """销毁现有数据行并依排序重建；表头（row 0）保持不变。"""
        for name, widgets in self.cells.items():
            for w in widgets.values():
                try:
                    w.destroy()
                except Exception:  # noqa
                    pass
        self.cells = {}
        self.sel_vars = {}
        self.focused_row = None
        self._hl = None
        rows = list(self.cfg["machines"])
        if self.sort_col:
            try:
                rows.sort(key=self._sort_key(self.sort_col),
                          reverse=self.sort_desc)
            except Exception:  # noqa
                pass
        self._update_header()
        for r, m in enumerate(rows, start=1):
            self._build_row(m, r)

    def _build_row(self, m, r):
        name = m["name"]
        self.selection.setdefault(name, True)
        var = tk.BooleanVar(value=self.selection[name])
        self.sel_vars[name] = var
        cells = {}
        CI = self.COL_IDX

        cb = ttkb.Checkbutton(self.inner, variable=var, bootstyle="round-toggle",
                              command=lambda n=name: self._on_check(n))
        cb.grid(row=r, column=CI["sel"], sticky="ew", padx=1)
        cells["sel"] = cb
        cb.bind("<Button-1>", lambda e, n=name: self.on_row_click(n))

        name_l = ttkb.Label(self.inner, text=name, anchor=W)
        name_l.grid(row=r, column=CI["name"], sticky="ew", padx=3)
        cells["name"] = name_l

        ip_l = ttkb.Label(self.inner, text=m["ip"], anchor=W)
        ip_l.grid(row=r, column=CI["ip"], sticky="ew", padx=3)
        cells["ip"] = ip_l

        user_l = ttkb.Label(self.inner, text=m["sshUser"], anchor=W)
        user_l.grid(row=r, column=CI["user"], sticky="ew", padx=3)
        cells["user"] = user_l

        rdp_l = ttkb.Label(self.inner, text=("是" if m.get("rdp", True) else "否"),
                           anchor=CENTER)
        rdp_l.grid(row=r, column=CI["rdp"], sticky="ew", padx=1)
        cells["rdp"] = rdp_l

        st = self.status.get(name, "-")
        status_l = ttkb.Label(self.inner, text=st, anchor=CENTER)
        status_l.grid(row=r, column=CI["status"], sticky="ew", padx=1)
        cells["status"] = status_l

        ops = ttkb.Frame(self.inner)
        ops.grid(row=r, column=CI["ops"], sticky="ew", padx=1)
        # 操作按钮：无文字、仅图标、彩色填充、紧凑方形
        ttkb.Button(ops, image=self.lock_closed_img, bootstyle="warning",
                    command=lambda n=name: self.run_action("lock", "one", n)).pack(side=LEFT, padx=1)
        ttkb.Button(ops, image=self.lock_open_img, bootstyle="success",
                    command=lambda n=name: self.run_action("unlock", "one", n)).pack(side=LEFT, padx=1)
        ttkb.Button(ops, image=self.detect_img, bootstyle="info",
                    command=lambda n=name: self.run_action("detect", "one", n)).pack(side=LEFT, padx=1)
        cells["ops"] = ops

        notes_l = ttkb.Label(self.inner, text=m.get("notes", ""), anchor=W)
        notes_l.grid(row=r, column=CI["notes"], sticky="ew", padx=3)
        cells["notes"] = notes_l

        for cid in ("name", "ip", "user", "rdp", "status", "notes"):
            cells[cid].bind("<Button-1>", lambda e, n=name: self.on_row_click(n))

        self.cells[name] = cells

    def _on_check(self, name):
        self.focused_row = name
        self.selection[name] = self.sel_vars[name].get()
        self._highlight(name)
        if self.sort_col == "sel":
            self.rebuild_rows()

    def on_row_click(self, name):
        self.focused_row = name
        self._highlight(name)

    def _highlight(self, name):
        prev = getattr(self, "_hl", None)
        if prev and prev in self.cells:
            for w in self.cells[prev].values():
                try:
                    w.configure(background=self._normal_bg)
                except Exception:  # noqa
                    pass
        if name in self.cells:
            for w in self.cells[name].values():
                try:
                    w.configure(background=self._hl_bg)
                except Exception:  # noqa
                    pass
        self._hl = name

    def set_all_selection(self, val):
        for m in self.cfg["machines"]:
            self.selection[m["name"]] = val
        for name, var in self.sel_vars.items():
            var.set(val)

    # ---- 主题 ----
    def on_theme_change(self, _event=None):
        label = self.theme_var.get()
        mode = next((v for l, v in THEME_LABELS if l == label), "system")
        self.theme_mode = mode
        self.cfg["theme"] = mode
        save_config(self.cfg)
        self.apply_theme()
        self.log_msg("主题已切换为：%s" % label)

    def apply_theme(self):
        dark = resolve_dark(self.theme_mode)
        self.dark = dark
        try:
            self.root.style.theme_use(theme_name(dark))
        except Exception as e:  # noqa
            self.log_msg("主题切换失败: %s" % e, "err")
            return
        apply_titlebar(self.root, dark)
        # ScrolledText 是原生 Text，主题切换不会自动重绘，需手动同步配色
        try:
            colors = self.root.style.colors
            self.log.text.configure(background=colors.inputbg,
                                    foreground=colors.inputfg,
                                    insertbackground=colors.inputfg)
        except Exception:  # noqa
            pass

    def _watch_system_theme(self):
        """跟随系统模式下，轮询系统主题变化并实时切换。"""
        try:
            if self.theme_mode == "system":
                cur = system_is_dark()
                if cur != self._last_sys_dark:
                    self._last_sys_dark = cur
                    self.apply_theme()
                    self.log_msg("检测到系统主题变化，已切换为%s模式"
                                 % ("暗色" if cur else "亮色"), "dim")
        except Exception:  # noqa
            pass
        self.root.after(3000, self._watch_system_theme)

    # ---- 主题化弹窗封装 ----
    def info(self, msg, title="提示"):
        show_dialog(self.root, title, msg, "info", dark=self.dark)

    def warn(self, msg, title="注意"):
        show_dialog(self.root, title, msg, "warn", dark=self.dark)

    def error(self, msg, title="错误"):
        show_dialog(self.root, title, msg, "err", dark=self.dark)

    def ask(self, msg, title="确认"):
        return show_dialog(self.root, title, msg, "ask", ask=True,
                           dark=self.dark)

    def show_about(self):
        show_dialog(
            self.root, "关于 " + APP_NAME,
            "%s\n版本 %s\n\n"
            "远程批量锁屏 / 解锁 Windows 工作机。\n"
            "锁屏：SSH 下发 schtasks 调用 LockWorkStation\n"
            "解锁：RDP 连接后 tscon 将会话切回控制台\n\n"
            "配置目录：%s\n"
            "项目地址：github.com/quintinxuan/QTX-RemoteLock"
            % (APP_NAME, APP_VERSION, APP_DIR),
            "info", dark=self.dark, width=460)

    # ---- CRUD ----
    def machine_dialog(self, title, data=None):
        win = ttkb.Toplevel(self.root)
        set_window_icon(win)
        win.title(title)
        win.transient(self.root)
        win.resizable(False, False)
        apply_titlebar(win, self.dark)

        frm = ttkb.Frame(win, padding=(16, 14))
        frm.pack(fill=BOTH, expand=YES)
        fields = [("名称", "name"), ("IP", "ip"), ("SSH用户", "sshUser"),
                  ("备注", "notes")]
        vars_ = {}
        for i, (label, key) in enumerate(fields):
            ttkb.Label(frm, text=label).grid(row=i, column=0, padx=(0, 10),
                                             pady=6, sticky=W)
            v = tk.StringVar(value=(data or {}).get(key, ""))
            vars_[key] = v
            ent = ttkb.Entry(frm, textvariable=v, width=30)
            ent.grid(row=i, column=1, pady=6, sticky=EW)
            if i == 0:
                ent.focus_set()
        frm.columnconfigure(1, weight=1)
        rdp_var = tk.BooleanVar(value=(data or {}).get("rdp", True))
        ttkb.Checkbutton(frm, text="支持 RDP（解锁需要）", variable=rdp_var,
                         bootstyle="round-toggle").grid(
            row=4, column=0, columnspan=2, pady=(10, 4), sticky=W)

        result = {}

        def ok():
            result["data"] = {
                "name": vars_["name"].get().strip(),
                "ip": vars_["ip"].get().strip(),
                "sshUser": vars_["sshUser"].get().strip(),
                "notes": vars_["notes"].get().strip(),
                "rdp": rdp_var.get(),
            }
            win.destroy()

        btnf = ttkb.Frame(frm)
        btnf.grid(row=5, column=0, columnspan=2, pady=(12, 0), sticky=EW)
        ttkb.Button(btnf, text="确定", bootstyle="success", width=10,
                    command=ok).pack(side=RIGHT, padx=(6, 0))
        ttkb.Button(btnf, text="取消", bootstyle="secondary", width=10,
                    command=win.destroy).pack(side=RIGHT)
        win.bind("<Return>", lambda e: ok())
        win.bind("<Escape>", lambda e: win.destroy())
        _center_on(win, self.root)
        win.grab_set()
        win.wait_window()
        return result.get("data")

    def add_machine(self):
        data = self.machine_dialog("新增机器")
        if not data or not data["name"] or not data["ip"]:
            return
        if any(m["name"] == data["name"] for m in self.cfg["machines"]):
            self.error(f"名称 {data['name']} 已存在")
            return
        self.cfg["machines"].append(data)
        save_config(self.cfg)
        self.selection[data["name"]] = True
        self.rebuild_rows()
        self.log_msg(f"已新增机器 {data['name']} ({data['ip']})")

    def edit_selected(self):
        # 优先用单击选中的行，其次回退到第一个被勾选的
        name = getattr(self, "focused_row", None)
        if not name or not any(x["name"] == name for x in self.cfg["machines"]):
            picked = [n for n, v in self.selection.items() if v]
            name = picked[0] if picked else None
        if not name:
            self.info("请先单击要编辑的机器行")
            return
        m = next((x for x in self.cfg["machines"] if x["name"] == name), None)
        if not m:
            return
        data = self.machine_dialog("编辑机器", m)
        if not data or not data["name"] or not data["ip"]:
            return
        if data["name"] != name and any(x["name"] == data["name"] for x in self.cfg["machines"]):
            self.error(f"名称 {data['name']} 已存在")
            return
        m.update(data)
        save_config(self.cfg)
        self.rebuild_rows()
        self.log_msg(f"已更新机器 {name}")

    def delete_selected(self):
        name = getattr(self, "focused_row", None)
        if not name or not any(x["name"] == name for x in self.cfg["machines"]):
            picked = [n for n, v in self.selection.items() if v]
            name = picked[0] if picked else None
        if not name:
            self.info("请先单击要删除的机器行")
            return
        if not self.ask(f"确定要删除机器 {name} 吗？\n此操作只影响本地清单，不会改动被控机。"):
            return
        self.cfg["machines"] = [x for x in self.cfg["machines"] if x["name"] != name]
        self.selection.pop(name, None)
        self.status.pop(name, None)
        save_config(self.cfg)
        self.rebuild_rows()
        self.log_msg(f"已删除机器 {name}")

    def save_settings(self):
        self.cfg["sshKeyPath"] = self.key_var.get().strip()
        save_config(self.cfg)
        self.log_msg(f"设置已保存：SSH 私钥 = {self.cfg['sshKeyPath']}")

    # ---- 连接测试 ----
    def test_selected(self):
        name = getattr(self, "focused_row", None)
        targets = [name] if name else [n for n, v in self.selection.items() if v]
        if not targets:
            self.info("请先选中要测试的机器")
            return
        self.run_in_thread(self._do_test, targets)

    def _do_test(self, names):
        key = os.path.expandvars(self.cfg["sshKeyPath"])
        for name in names:
            m = next((x for x in self.cfg["machines"] if x["name"] == name), None)
            if not m:
                continue
            rc, out = run_ssh(m["sshUser"], m["ip"], "echo OK", key, timeout=10)
            if rc == 0 and "OK" in out:
                self.set_status(name, "在线")
                self.log_msg(f"[{name}] 在线", "ok")
            else:
                self.set_status(name, "离线")
                self.log_msg(f"[{name}] 离线 ({out[:60]})", "warn")

    # ---- 一键部署密钥 ----
    def deploy_selected(self):
        if self.busy:
            self.info("上一批操作正在进行，请稍候")
            return
        targets = [n for n, v in self.selection.items() if v]
        targets = [n for n in targets
                   if any(m["name"] == n for m in self.cfg["machines"])]
        if not targets:
            self.info("请先勾选要部署的机器（点击第一列的方框）")
            return
        if paramiko is None:
            self.error("paramiko 未打包进程序，无法使用密码部署功能", "缺少组件")
            return
        params = self.deploy_dialog(targets)
        if not params:
            return
        self.run_in_thread(self._do_deploy, params)

    def deploy_dialog(self, names):
        """返回 {'items': [(name, password)...], 'autostart': bool,
                  'rdp': bool, 'syncRdp': bool} 或 None"""
        win = ttkb.Toplevel(self.root)
        set_window_icon(win)
        win.title("一键部署 SSH 密钥")
        win.transient(self.root)
        apply_titlebar(win, self.dark)
        win.geometry("580x%d" % min(680, 320 + 34 * len(names)))
        win.grab_set()

        ttkb.Label(win, text="用密码登录被控机一次，写入控制端公钥；之后全程走密钥。",
                   bootstyle="warning").pack(anchor=W, padx=14, pady=(12, 2))
        ttkb.Label(win, text="密码仅在本次操作的内存中使用，不会写入任何配置文件。",
                   bootstyle="secondary").pack(anchor=W, padx=14, pady=(0, 8))

        # 统一密码
        topf = ttkb.Frame(win)
        topf.pack(fill=X, padx=14, pady=(0, 6))
        ttkb.Label(topf, text="统一密码:").pack(side=LEFT)
        bulk_var = tk.StringVar()
        ttkb.Entry(topf, textvariable=bulk_var, show="*", width=24).pack(side=LEFT, padx=6)

        pw_vars = {}

        def fill_all():
            for v in pw_vars.values():
                v.set(bulk_var.get())

        ttkb.Button(topf, text="填入全部", bootstyle="info-outline", width=10,
                    command=fill_all).pack(side=LEFT, padx=4)

        # 机器列表
        body = ttkb.Frame(win)
        body.pack(fill=BOTH, expand=YES, padx=14, pady=4)
        try:
            from ttkbootstrap.scrolled import ScrolledFrame
            holder = ScrolledFrame(body, autohide=True)
            holder.pack(fill=BOTH, expand=YES)
        except Exception:
            holder = body

        for i, n in enumerate(names):
            m = next(x for x in self.cfg["machines"] if x["name"] == n)
            row = ttkb.Frame(holder)
            row.pack(fill=X, pady=2)
            ttkb.Label(row, text=n, width=12).pack(side=LEFT)
            ttkb.Label(row, text=m["ip"], width=16, bootstyle="secondary").pack(side=LEFT)
            ttkb.Label(row, text=m["sshUser"], width=10, bootstyle="secondary").pack(side=LEFT)
            v = tk.StringVar()
            pw_vars[n] = v
            ttkb.Entry(row, textvariable=v, show="*", width=20).pack(side=LEFT, padx=4)

        # 选项
        optf = ttkb.Frame(win)
        optf.pack(fill=X, padx=14, pady=(8, 2))
        auto_var = tk.BooleanVar(value=True)
        rdp_var = tk.BooleanVar(value=True)
        sync_var = tk.BooleanVar(value=True)
        ttkb.Checkbutton(optf, text="设置 sshd 服务开机自启", variable=auto_var,
                         bootstyle="round-toggle").pack(anchor=W, pady=1)
        ttkb.Checkbutton(optf, text="启用远程桌面并放通防火墙（家庭版自动跳过）",
                         variable=rdp_var, bootstyle="round-toggle").pack(anchor=W, pady=1)
        ttkb.Checkbutton(optf, text="根据系统版本自动回填 RDP 支持状态",
                         variable=sync_var, bootstyle="round-toggle").pack(anchor=W, pady=1)
        rdp_cred_var = tk.BooleanVar(value=True)
        ttkb.Checkbutton(optf, text="部署后保存 RDP 凭据到凭据管理器（解锁免输密码）",
                         variable=rdp_cred_var, bootstyle="round-toggle").pack(anchor=W, pady=1)
        rdpcf = ttkb.Frame(win)
        rdpcf.pack(fill=X, padx=14, pady=(2, 2))
        ttkb.Label(rdpcf, text="RDP 用户名（留空=与 SSH 用户相同）:",
                   bootstyle="secondary").pack(side=LEFT)
        rdp_user_var = tk.StringVar()
        ttkb.Entry(rdpcf, textvariable=rdp_user_var, width=22).pack(side=LEFT, padx=6)
        ttkb.Label(win, text="RDP 密码默认使用上方各机器的部署密码（即 Windows 登录密码）。",
                   bootstyle="secondary").pack(anchor=W, padx=14, pady=(0, 6))

        result = {}

        def ok():
            items = [(n, pw_vars[n].get()) for n in names]
            miss = [n for n, p in items if not p]
            if miss:
                show_dialog(win, "缺少密码",
                            "以下机器未填密码，将被跳过：\n" + ", ".join(miss),
                            "warn", dark=self.dark)
            items = [(n, p) for n, p in items if p]
            if not items:
                return
            result["v"] = {"items": items, "autostart": auto_var.get(),
                           "rdp": rdp_var.get(), "syncRdp": sync_var.get(),
                           "rdpCred": rdp_cred_var.get(),
                           "rdpUser": rdp_user_var.get().strip()}
            win.destroy()

        btnf = ttkb.Frame(win)
        btnf.pack(fill=X, padx=14, pady=10)
        ttkb.Button(btnf, text="开始部署", bootstyle="success", width=12,
                    command=ok).pack(side=LEFT)
        ttkb.Button(btnf, text="取消", bootstyle="secondary", width=10,
                    command=win.destroy).pack(side=LEFT, padx=8)
        win.wait_window()
        return result.get("v")

    def _do_deploy(self, params):
        key = self.cfg.get("sshKeyPath", "")
        ok = fail = 0
        changed = False
        for name, pw in params["items"]:
            m = next((x for x in self.cfg["machines"] if x["name"] == name), None)
            if not m:
                continue
            self.set_status(name, "部署中")
            try:
                res = deploy_key(m, pw, key, self.log_msg,
                                 set_autostart=params["autostart"],
                                 enable_rdp=params["rdp"])
            except Exception as e:  # noqa
                res = None
                self.log_msg(f"[{name}] 部署异常: {e}", "err")
            if res and res.get("verified"):
                ok += 1
                self.set_status(name, "已部署")
                if params.get("rdpCred"):
                    rdp_user = params.get("rdpUser") or m["sshUser"]
                    if store_rdp_credential(m["ip"], rdp_user, pw, self.log_msg):
                        self.log_msg(f"[{name}] RDP 凭据已保存，解锁将自动登录", "ok")
                if params["syncRdp"] and m.get("rdp", True) != res.get("rdpCapable", True):
                    m["rdp"] = res.get("rdpCapable", True)
                    changed = True
                    self.log_msg(f"[{name}] RDP 支持状态已回填为 "
                                 f"{'是' if m['rdp'] else '否'}", "ok")
            else:
                fail += 1
                self.set_status(name, "部署失败")
        # 密码引用尽快释放
        params["items"] = []
        if changed:
            save_config(self.cfg)
            self.root.after(0, self.rebuild_rows)
        self.log_msg(f"=== 部署完成: 成功 {ok}  失败 {fail} ===", "ok" if fail == 0 else "warn")

    # ---- 执行锁/解锁/检测 ----
    def run_action(self, action, scope, name=None):
        if self.busy:
            self.info("上一批操作正在进行，请稍候")
            return
        targets = []
        if scope == "all":
            targets = [m["name"] for m in self.cfg["machines"]]
        elif scope == "one":
            targets = [name] if name else []
        else:
            targets = [n for n, v in self.selection.items() if v]
            # 检测按钮便捷语义：未勾选任何机器时直接检测全部
            if not targets and action == "detect":
                targets = [m["name"] for m in self.cfg["machines"]]
        if not targets:
            self.info("没有选中的机器")
            return
        self.run_in_thread(self._do_action, action, targets)

    def _do_action(self, action, names):
        key = os.path.expandvars(self.cfg["sshKeyPath"])

        def work(name):
            m = next((x for x in self.cfg["machines"] if x["name"] == name), None)
            if not m:
                return name, "skip", None
            if action == "detect":
                self.set_status(name, "检测中")
            else:
                self.set_status(name, "执行中")
            try:
                if action == "lock":
                    return name, "lock", lock_machine(m, key, self.log_msg)
                if action == "detect":
                    return name, "detect", detect_state(m, key, self.log_msg)
                return name, "unlock", unlock_machine(m, key, self.log_msg)
            except Exception as e:  # noqa
                return name, "exc", e

        ok = fail = skip = 0
        with ThreadPoolExecutor(max_workers=len(names) or 1) as ex:
            futs = {ex.submit(work, n): n for n in names}
            for fut in as_completed(futs):
                name, kind, res = fut.result()
                if kind == "skip":
                    skip += 1
                    self.set_status(name, "跳过")
                elif kind == "exc":
                    fail += 1
                    self.set_status(name, "异常")
                    self.log_msg(f"[{name}] 异常: {res}", "err")
                elif kind == "lock":
                    if res:
                        ok += 1
                        self.set_status(name, "已锁屏")
                    else:
                        fail += 1
                        self.set_status(name, "失败")
                elif kind == "detect":
                    state = res[1] if isinstance(res, tuple) else "unknown"
                    if state == "locked":
                        ok += 1
                        self.set_status(name, "已锁")
                    elif state == "unlocked":
                        ok += 1
                        self.set_status(name, "未锁")
                    elif state == "offline":
                        fail += 1
                        self.set_status(name, "离线")
                    else:
                        fail += 1
                        self.set_status(name, "未知")
                else:  # unlock
                    if res is None:
                        skip += 1
                        self.set_status(name, "跳过")
                    elif res:
                        ok += 1
                        self.set_status(name, "已解锁")
                    else:
                        fail += 1
                        self.set_status(name, "失败")
        self.log_msg(f"=== 完成: 成功 {ok}  失败 {fail}  跳过 {skip} ===", "ok")

    # ---- 线程调度 ----
    def run_in_thread(self, fn, *args):
        self.busy = True
        self.set_controls_state(DISABLED)

        def worker():
            try:
                fn(*args)
            except Exception as e:  # noqa
                self.log_msg(f"线程异常: {e}", "err")
            finally:
                self.busy = False
                self.root.after(0, lambda: self.set_controls_state(NORMAL))

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def set_controls_state(self, state):
        for w in self.root.winfo_children():
            self._set_state(w, state)

    def _set_state(self, widget, state):
        try:
            widget.configure(state=state)
        except Exception:
            pass
        for c in widget.winfo_children():
            self._set_state(c, state)

    # ---- UI 更新（线程安全）----
    def set_status(self, name, text):
        self.status[name] = text
        self.root.after(0, lambda: self._update_status_cell(name, text))

    def _update_status_cell(self, name, text):
        w = self.cells.get(name, {}).get("status")
        if w:
            try:
                w.configure(text=text)
            except Exception:  # noqa
                pass

    def log_msg(self, msg, level="info"):
        palette = LOG_COLORS[bool(getattr(self, "dark", True))]
        tag_color = palette.get(level, palette["info"])
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"

        def _insert():
            self.log.text.configure(state=NORMAL)
            self.log.text.insert(END, line)
            # 粗略着色：整行用一种颜色
            self.log.text.tag_add(f"c{id(line)}", "end-2l linestart", "end-1l lineend")
            self.log.text.tag_config(f"c{id(line)}", foreground=tag_color)
            self.log.text.see(END)
            self.log.text.configure(state=DISABLED)

        self.root.after(0, _insert)

    def show_deploy_help(self):
        show_dialog(
            self.root, "新机器上线流程",
            "【你需要在新机器上手动做的（仅两步）】\n"
            "1. 安装并启动 OpenSSH Server\n"
            "   设置 - 应用 - 可选功能 - 添加 OpenSSH 服务器\n"
            "   或管理员 PowerShell:\n"
            "   Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0\n"
            "   Start-Service sshd\n"
            "2. 放通防火墙 22 端口（多数情况安装时会自动加规则）\n\n"
            "【其余全部交给本软件】\n"
            "3. 在本软件「新增」里填名称 / IP / SSH 用户\n"
            "4. 勾选该机器 -> 点「部署密钥」-> 输入该机的登录密码\n"
            "   软件会自动完成：\n"
            "     - 本地无密钥时自动生成 ed25519 密钥对\n"
            "     - 写入公钥到 administrators_authorized_keys\n"
            "     - 修正该文件 ACL（Windows OpenSSH 强制要求）\n"
            "     - 设置 sshd 开机自启\n"
            "     - 启用远程桌面并放通 RDP 防火墙\n"
            "     - 识别系统版本，家庭版自动标记为无 RDP\n"
            "     - 用密钥重连验证，确认免密生效\n"
            "   密码仅在内存中使用一次，不写入任何配置文件。\n\n"
            "解锁功能需被控机支持 RDP（专业版/企业版）。\n"
            "家庭版无 RDP，只能锁屏，部署时会自动标记。",
            "info", dark=self.dark, scroll=True)


# ----------------------------------------------------------------------------
def _selftest():
    """打包完整性自检：QTX-RemoteLock.exe --selftest
    结果写入 %TEMP%\\QTX-RemoteLock_selftest.txt（windowed 模式无控制台，只能落文件）。
    """
    out = os.path.join(tempfile.gettempdir(), "QTX-RemoteLock_selftest.txt")
    lines = ["app=%s v%s" % (APP_NAME, APP_VERSION),
             "python=" + sys.version.split()[0],
             "frozen=" + str(getattr(sys, "frozen", False)),
             "appdir=" + APP_DIR,
             "sysdark=" + str(system_is_dark())]
    lines.append("paramiko=" + (getattr(paramiko, "__version__", "?")
                                if paramiko is not None else "MISSING"))
    for mod in ("tkinter", "ttkbootstrap", "cryptography"):
        try:
            m = __import__(mod)
            lines.append(f"{mod}={getattr(m, '__version__', 'OK')}")
        except Exception as e:  # noqa
            lines.append(f"{mod}=FAIL {e}")
    try:
        from ttkbootstrap.themes.standard import STANDARD_THEMES
        lines.append("themes=%d (%s=%s, %s=%s)"
                     % (len(STANDARD_THEMES),
                        DARK_THEME, DARK_THEME in STANDARD_THEMES,
                        LIGHT_THEME, LIGHT_THEME in STANDARD_THEMES))
    except Exception as e:  # noqa
        lines.append(f"themes=FAIL {e}")
    try:
        import ttkbootstrap as _tb
        assets = os.path.join(os.path.dirname(_tb.__file__), "localization")
        lines.append("ttkb_pkgdir_exists=" + str(os.path.isdir(os.path.dirname(_tb.__file__))))
        lines.append("localization_exists=" + str(os.path.isdir(assets)))
    except Exception as e:  # noqa
        lines.append(f"assets=FAIL {e}")
    try:
        s = _deploy_script("ssh-ed25519 AAAATEST test@ctrl", True, True)
        enc = _ps_encoded(s)
        lines.append("deploy_script=OK len=%d enc=%d pad=%s"
                     % (len(s), len(enc), "=" in enc))
    except Exception as e:  # noqa
        lines.append(f"deploy_script=FAIL {e}")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    if "--selftest" in sys.argv:
        _selftest()
        return

    # 异常弹窗，避免 windowed 模式静默崩溃
    def excepthook(exc_type, exc_value, exc_tb):
        try:
            import traceback
            msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            messagebox.showerror("程序错误", msg)
        except Exception:
            pass

    sys.excepthook = excepthook
    startup_dark = resolve_dark(load_config().get("theme", "system"))
    root = ttkb.Window(themename=theme_name(startup_dark))
    app = RemoteLockApp(root)
    apply_titlebar(root, app.dark)
    app.apply_theme()
    root.after(3000, app._watch_system_theme)
    app.log_msg("%s 已启动（配置目录：%s）" % (APP_TITLE, APP_DIR))
    if MIGRATED:
        app.log_msg("已从旧版目录 %s 迁移配置（旧目录保留未删除）"
                    % LEGACY_DIR, "warn")
    root.mainloop()


if __name__ == "__main__":
    main()
