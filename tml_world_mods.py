# -*- coding: utf-8 -*-
"""
TMLWorldMods — tModLoader 世界级 Mod 集自动管理器
====================================================
核心思路：
- tModLoader 的 mod 启用集由 <存档>/Mods/enabled.json 唯一决定
- mod 在游戏启动时加载，早于选世界 —— 所以自动切换只能从外部做
- 本工具维护「世界 → mod 集」映射：
    启动时写入目标集合 → 拉起游戏 → 退出后按策略结算
- 自动模式与手动模式并存，随开随关
"""

import gzip
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

STEAM_APPID = "1281930"  # tModLoader
APP_NAME = "TMLWorldMods"
CONFIG_VERSION = 1

# ---------------------------------------------------------------- 路径工具


def get_documents_dir():
    """取「文档」真实路径（尊重系统重定向，如 E:\19645\Documents）。"""
    if os.name == "nt":
        try:
            import ctypes
            # CSIDL_PERSONAL = 0x0005（我的文档），SHGetFolderPathW 尊重重定向
            buf = ctypes.create_unicode_buffer(300)
            r = ctypes.windll.shell32.SHGetFolderPathW(None, 0x0005, None,
                                                       0, buf)
            if r == 0 and buf.value:
                return buf.value
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def get_tml_save_dir():
    """定位 tModLoader 存档目录。"""
    candidates = [
        os.path.join(get_documents_dir(), "My Games", "Terraria", "tModLoader"),
        os.path.join(os.environ.get("USERPROFILE", ""), "Documents", "My Games",
                     "Terraria", "tModLoader"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    # 兜底：多盘浅层扫描（覆盖文档重定向到任意盘符的情况）
    deep = find_save_dirs_deep()
    if deep:
        return deep[0]
    return candidates[0]


def _drive_top_dirs():
    """各盘符的根目录条目（只扫一层，代价极低）。"""
    out = []
    for d in "CDEFGH":
        root = f"{d}:\\"
        if not os.path.isdir(root):
            continue
        try:
            with os.scandir(root) as it:
                out += [e.path for e in it if e.is_dir()]
        except OSError:
            continue
    return out


def find_save_dirs_deep():
    """多盘浅层扫描：X:\\<用户目录>\\Documents(文档)\\My Games\\Terraria\\tModLoader"""
    out = []
    for c in _drive_top_dirs():
        for doc in (os.path.join(c, "Documents"),
                    os.path.join(c, "文档"),
                    c):
            p = os.path.join(doc, "My Games", "Terraria", "tModLoader")
            if os.path.isdir(os.path.join(p, "Worlds")) or \
                    os.path.isdir(os.path.join(p, "Mods")):
                if p.lower() not in {x.lower() for x in out}:
                    out.append(p)
    return out


def find_tml_installs_deep():
    """多盘浅层扫描 tML 安装目录：X:\\<库目录>\\steamapps\\common\\tModLoader"""
    out = []
    for s in _drive_top_dirs():
        p = os.path.join(s, "steamapps", "common", "tModLoader")
        if os.path.isfile(os.path.join(p, "start-tModLoader.bat")):
            out.append(p)
    return out


def get_steam_path():
    """从注册表取 Steam 安装目录（任意机器自适应）。"""
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Valve\Steam") as k:
            v, _ = winreg.QueryValueEx(k, "SteamPath")
            return v.replace("/", "\\")
    except OSError:
        return None


def parse_libraryfolders_vdf(vdf_path):
    """解析 libraryfolders.vdf，返回全部 Steam 库目录。"""
    try:
        with open(vdf_path, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
    except OSError:
        return []
    libs = re.findall(r'"path"\s+"([^"]+)"', txt)
    return [l.replace("\\\\", "\\") for l in libs]


def get_steam_libraries():
    """全部 Steam 库：注册表主目录 + vdf 声明的库 + 常见位置兜底。"""
    out = []
    sp = get_steam_path()
    if sp:
        out.append(sp)
        out += parse_libraryfolders_vdf(
            os.path.join(sp, "steamapps", "libraryfolders.vdf"))
    for d in "CDEFGH":
        out.append(rf"{d}:/SteamLibrary")
    out.append(r"C:/Program Files (x86)/Steam")
    seen, res = set(), []
    for p in out:
        p = os.path.normpath(p)
        if p and p.lower() not in seen and os.path.isdir(p):
            seen.add(p.lower())
            res.append(p)
    return res


def get_workshop_dirs():
    """各 Steam 库里 tML 的 Workshop 内容目录。"""
    dirs = []
    for lib in get_steam_libraries():
        ws = os.path.join(lib, "steamapps", "workshop", "content",
                          STEAM_APPID)
        if os.path.isdir(ws):
            dirs.append(ws)
    return dirs


def get_tml_install_dir():
    """定位 tModLoader 安装目录（含 start-tModLoader.bat）。"""
    for lib in get_steam_libraries():
        inst = os.path.join(lib, "steamapps", "common", "tModLoader")
        if os.path.isfile(os.path.join(inst, "start-tModLoader.bat")):
            return inst
    # 兜底：多盘浅层扫描（Steam 装在冷门位置 / vdf 损坏）
    deep = find_tml_installs_deep()
    if deep:
        return deep[0]
    return None


def get_steam_protocol_ok():
    """steam:// 协议是否已注册（Steam 未运行也可用，会自动拉起 Steam）。"""
    if os.name != "nt":
        return False
    try:
        import winreg
        for hive in (winreg.HKEY_CLASSES_ROOT, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"steam\shell\open\command"):
                    return True
            except OSError:
                continue
    except Exception:
        pass
    return False


def resolve_save_dir(cfg):
    """存档目录：用户指定的优先，否则自动检测。"""
    override = cfg.get("save_dir")
    if override and os.path.isdir(override):
        return override
    return get_tml_save_dir()


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path():
    return os.path.join(app_dir(), APP_NAME + ".json")


# ---------------------------------------------------------------- .twld 解析
# .twld 整体为 gzip 压缩。内部是节结构：
#   - "usedMods" 节 = 上次保存时启用的完整 mod 列表（正是游戏弹窗对比的那份）
#   - 各 mod 的数据块里也散落着 mod 名（仅「写过数据」的 mod）
# 名字均为 7bit 编码长度前缀 + UTF8 字符串，且名字前有一枚 \x00 分隔。

_MODNAME_RE = re.compile(r"^[A-Za-z0-9_. +'\[\]-]{1,96}$")


def _read_twld_bytes(twld_path):
    try:
        with open(twld_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if data[:2] == b"\x1f\x8b":  # gzip
        try:
            import zlib
            data = gzip.decompress(data)
        except (OSError, zlib.error, EOFError):
            return None
    return data


def _read_varint(data, pos):
    result = shift = 0
    while pos < len(data) and shift < 35:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
    raise ValueError("bad varint")


def extract_used_mods_from_twld(twld_path):
    """解析 usedMods 节 = 世界上次保存时的完整启用列表。失败返回 None。"""
    data = _read_twld_bytes(twld_path)
    if data is None:
        return None
    i = data.find(b"usedMods")
    if i < 0:
        return None
    try:
        pos = i + len(b"usedMods")
        # 头部 4 字节（观测为 \x08\x00\x00\x00，先跳过再做内容校验兜底）
        pos += 4
        count, pos = _read_varint(data, pos)
        if not 1 <= count <= 4096:
            return None
        names = []
        for _ in range(count):
            if pos < len(data) and data[pos] == 0:
                pos += 1
            n, pos = _read_varint(data, pos)
            if not 1 <= n <= 96 or pos + n > len(data):
                return None
            name = data[pos:pos + n].decode("utf-8", "replace")
            pos += n
            if not _MODNAME_RE.match(name):
                return None
            names.append(name)
        return names
    except (ValueError, IndexError):
        return None


def extract_mod_names_from_twld(twld_path, known_names):
    """旧方法：在解压后的数据里做「长度前缀」匹配，找写过数据的 mod。"""
    data = _read_twld_bytes(twld_path)
    if data is None:
        return []
    found = []
    for name in known_names:
        enc = name.encode("utf-8", "ignore")
        n = len(enc)
        if n == 0 or n >= 128:
            if n >= 128:
                # 变长前缀为多字节，简化处理：跳过极长名
                continue
        start = 0
        while True:
            i = data.find(enc, start)
            if i < 0:
                break
            if i > 0 and data[i - 1] == n:
                found.append(name)
                break
            start = i + 1
    return found


# ---------------------------------------------------------------- 主应用


class App:
    def __init__(self, root, save_dir=None, guide=True):
        self.root = root
        self.cfg_path = config_path()
        self.cfg = self.load_config()
        # 存档目录：显式参数 > 配置覆盖 > 自动检测（含多盘浅层扫描兜底）
        self.save_dir = save_dir or resolve_save_dir(self.cfg)
        self._apply_paths()
        self.proc = None      # 启动监听进程（PowerShell watcher）
        self.pre_state = None  # 启动前 enabled.json 快照
        self.launching = False
        self.world_map = {}   # 显示名 -> wld 路径
        self.session_netplay = False  # 本次会话是否检测到联机
        self.exit_mode_var = tk.StringVar(
            value=self.cfg.get("exit_mode", "writeback"))

        root.title("泰拉瑞亚 tModLoader 工具 — 按世界自动切换 Mod")
        root.geometry("760x470")
        root.minsize(680, 430)
        base_font = ("Microsoft YaHei UI", 10)
        root.option_add("*Font", base_font)

        self.build_ui()
        self.refresh_all()
        if guide and not self.save_dir_valid:
            self.root.after(200, self.first_run_guide)

    def first_run_guide(self):
        """扫不到存档目录时主动引导，不让用户对着空列表发呆。"""
        deep = find_save_dirs_deep()
        if deep and messagebox.askyesno(
                APP_NAME,
                "自动检测未直接命中存档目录，但深度扫描找到了：\n"
                + deep[0] + "\n\n使用它吗？"):
            self.cfg["save_dir"] = deep[0]
            self.save_config()
            self.save_dir = deep[0]
            self._apply_paths()
            self.refresh_all()
            return
        if messagebox.askyesno(
                APP_NAME,
                "未能自动找到 tModLoader 存档目录（Worlds/Mods 所在处）。\n"
                "常见原因：文档重定向到特殊位置、tML 未在本机运行过。\n\n"
                "现在手动指定吗？（也可稍后点「⚙ 高级」）"):
            p = filedialog.askdirectory(title="选择 tModLoader 存档目录"
                                        "（含 Mods 与 Worlds）")
            if p:
                self.cfg["save_dir"] = p
                self.save_config()
                self.save_dir = p
                self._apply_paths()
                self.refresh_all()
                return
        self.status("未找到存档目录——点「⚙ 高级」手动指定，"
                    "或点「⚙ 高级」里的「🔍 体检」排查")

    def _apply_paths(self):
        self.mods_dir = os.path.join(self.save_dir, "Mods")
        self.enabled_json = os.path.join(self.mods_dir, "enabled.json")
        self.worlds_dir = os.path.join(self.save_dir, "Worlds")
        self.save_dir_valid = os.path.isdir(
            os.path.join(self.save_dir, "Worlds"))

    # ---------------- 配置读写

    def load_config(self):
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError
        except Exception:
            cfg = {}
        cfg.setdefault("config_version", CONFIG_VERSION)
        cfg.setdefault("worlds", {})          # 世界名 -> {"mods": [...]}
        cfg.setdefault("pre_state", None)     # 启动前快照
        cfg.setdefault("exit_mode", "writeback")  # writeback / restore / none
        cfg.setdefault("save_dir", "")        # 存档目录覆盖（可移植用）
        cfg.setdefault("tml_bat", "")         # 非 Steam 启动脚本
        return cfg

    def save_config(self):
        try:
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except OSError as e:
            self.status(f"配置保存失败：{e}")

    # ---------------- 数据读取

    def list_worlds(self):
        """返回 [(显示名, wld路径)]，排除 .bak。"""
        out = []
        try:
            for fn in os.listdir(self.worlds_dir):
                if fn.lower().endswith(".wld"):
                    out.append((os.path.splitext(fn)[0],
                                os.path.join(self.worlds_dir, fn)))
        except OSError:
            pass
        out.sort(key=lambda x: x[0])
        return out

    def installed_mod_names(self):
        """本地 Mods/*.tmod + Workshop 子目录内的 .tmod 名。"""
        names = set()
        try:
            for fn in os.listdir(self.mods_dir):
                if fn.lower().endswith(".tmod"):
                    names.add(os.path.splitext(fn)[0])
        except OSError:
            pass
        for wdir in get_workshop_dirs():
            # Workshop 结构：<modID>/<tML版本>/ModName.tmod，需递归
            for root, _dirs, files in os.walk(wdir):
                for fn in files:
                    if fn.lower().endswith(".tmod"):
                        names.add(os.path.splitext(fn)[0])
        return sorted(names)

    def read_enabled(self):
        try:
            with open(self.enabled_json, "r", encoding="utf-8") as f:
                v = json.load(f)
            if isinstance(v, dict):  # 兼容 {"EnabledMods": [...]}
                v = v.get("EnabledMods", [])
            if isinstance(v, list):
                return [str(x) for x in v]
        except Exception:
            pass
        return []

    def write_enabled(self, mods):
        """写 enabled.json（写入前做滚动备份；过滤未安装的 mod）。"""
        installed = set(self.installed_mod_names())
        valid = [m for m in mods if m in installed]
        dropped = [m for m in mods if m not in installed]
        os.makedirs(self.mods_dir, exist_ok=True)
        if os.path.isfile(self.enabled_json):
            bak = self.enabled_json + ".bak"
            try:
                os.replace(self.enabled_json, bak)
            except OSError:
                pass
        with open(self.enabled_json, "w", encoding="utf-8") as f:
            json.dump(valid, f, ensure_ascii=False, indent=2)
        return valid, dropped

    # ---------------- UI

    def build_ui(self):
        pad = {"padx": 6, "pady": 4}

        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True, **pad)

        # 左：世界列表（主操作入口）；不 expand——宽度贴内容，杜绝中间死空白
        left = ttk.LabelFrame(main, text=" 世界（点选查看 · 双击直接启动） ")
        left.pack(side="left", fill="y", padx=(0, 6))
        # 按钮行先固定在底部，列表区再填满剩余空间（顺序反了会被挤到侧边）
        wf = ttk.Frame(left)
        wf.pack(side="bottom", fill="x", padx=4, pady=(4, 4))
        ttk.Button(wf, text="⟳ 扫描", command=self.refresh_all,
                   width=8).pack(side="left")
        ttk.Button(wf, text="⚙ 高级…", command=self.open_advanced,
                   width=10).pack(side="left", padx=4)
        list_area = ttk.Frame(left)
        list_area.pack(side="top", fill="both", expand=True)
        self.world_list = tk.Listbox(list_area, activestyle="none", width=17,
                                     font=("Microsoft YaHei UI", 11))
        self.world_list.pack(side="left", fill="both", expand=True,
                             padx=(4, 0), pady=4)
        sb = ttk.Scrollbar(list_area, command=self.world_list.yview)
        sb.pack(side="left", fill="y", pady=4)
        self.world_list.config(yscrollcommand=sb.set)
        self.world_list.bind("<<ListboxSelect>>", lambda e: self.on_select_world())
        self.world_list.bind("<Double-Button-1>",
                             lambda e: self.launch_world())

        # 右：所选世界卡片（简介 + 居中内容 + 新手引导）
        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True)
        card = ttk.LabelFrame(right, text=" 所选世界 ")
        card.pack(fill="both", expand=True)

        tag = ttk.Label(card, foreground="#999", wraplength=310,
                        justify="center",
                        text="泰拉瑞亚 tModLoader 模组管理："
                             "不同世界自动切换对应的 Mod 组合，"
                             "退出游戏自动恢复原状")
        tag.pack(pady=(8, 0))

        mid = ttk.Frame(card)
        mid.pack(expand=True)  # 垂直居中，消除异常留白
        self.sel_name_lbl = ttk.Label(mid, text="（未选择世界）",
                                      font=("Microsoft YaHei UI", 15, "bold"))
        self.sel_name_lbl.pack(pady=(0, 4))
        self.sel_info_lbl = ttk.Label(mid, text="在左边点一个世界看看\n"
                                      "双击世界名可以直接启动",
                                      foreground="#666", wraplength=310,
                                      justify="center")
        self.sel_info_lbl.pack(padx=10, pady=6)
        big = ttk.Frame(mid)
        big.pack(pady=(14, 0))
        self.launch_btn = tk.Button(
            big, text="▶ 启 动", font=("Microsoft YaHei UI", 14, "bold"),
            bg="#f8bbd0", activebackground="#f48fb1", activeforeground="#5a1a33",
            fg="#5a1a33", relief="flat", width=12, pady=6, cursor="hand2",
            command=self.launch_world)
        self.launch_btn.pack()

        ttk.Button(card, text="❓ 第一次用？点这里看使用说明",
                   command=self.show_help).pack(pady=(0, 10))

        self.status_lbl = ttk.Label(self.root, text="就绪",
                                    foreground="#444", anchor="w")
        self.status_lbl.pack(fill="x", padx=10, pady=(0, 6))

    def show_help(self):
        """内置使用说明（人话版）。"""
        if getattr(self, "help_win", None) is not None and \
                self.help_win.winfo_exists():
            self.help_win.deiconify()
            self.help_win.lift()
            return
        dlg = tk.Toplevel(self.root)
        self.help_win = dlg
        dlg.title("使用说明")
        dlg.transient(self.root)
        dlg.geometry("600x640")
        txt = tk.Text(dlg, wrap="word", padx=16, pady=12, relief="flat",
                      font=("Microsoft YaHei UI", 10), cursor="arrow")
        txt.pack(fill="both", expand=True)
        txt.tag_config("h", font=("Microsoft YaHei UI", 11, "bold"),
                       foreground="#ad1457", spacing1=10, spacing3=4)
        txt.tag_config("dim", foreground="#666")
        sections = [
            ("h", "【这是什么】"),
            ("n", "这是给泰拉瑞亚 tModLoader 用的辅助工具。"
                  "不同世界要用不同的 mod 组合，这个工具会在你启动某个世界时，"
                  "自动把 mod 开关调成它的配置；退出游戏后自动结算。"
                  "从此不用再进 mod 列表手动开关。"),
            ("h", "【三步上手】"),
            ("n", "1. 左边点选一个世界（会自动读取它需要的 mod）\n"
                  "2. 点粉色「▶ 启动」（或直接双击世界名）\n"
                  "3. 正常游玩，退出游戏后自动处理，不用管"),
            ("h", "【常见问题】"),
            ("n", "Q：高级设置里的「启动脚本」是什么？\n"
                  "A：备用启动方式，平时留空就行。只有 Steam 打不开游戏时"
                  "（比如没装 Steam、steam:// 协议损坏）才会用到。"
                  "它指的不是存档，而是游戏本体安装目录"
                  "（…\\steamapps\\common\\tModLoader）里的 start-tModLoader.bat。"),
            ("n", "Q：存档目录在哪？扫不到怎么办？\n"
                  "A：世界和 mod 设置存放的地方，一般是：\n"
                  "    文档\\My Games\\Terraria\\tModLoader\n"
                  "（里面 Worlds 放世界、Mods 放启用设置）。"
                  "自动扫不到时会弹窗引导你选择；也可在「⚙ 高级」底部手动指定。"),
            ("n", "Q：进世界时还提示缺少/增添 mod？\n"
                  "A：工具已按该世界存档里记录的完整列表写入，"
                  "正常情况不会再弹。若弹窗提到未安装的 mod，装上它即可"
                  "（联机时缺的 mod 会由服务器自动同步下载）。"),
            ("n", "Q：联机（服务器在别人电脑）会怎样？\n"
                  "A：随便连。联机时 mod 由服务器说了算，游戏会自动同步；"
                  "工具检测到联机后，退出时只恢复你的原有状态，"
                  "不会把服务器的 mod 集写进任何世界配置。"),
            ("n", "Q：换电脑怎么用？\n"
                  "A：把 exe 单独拷过去双击即可，配置跟着 exe 走。"
                  "如果它扫不到路径，会弹窗一步步引导你；"
                  "也可以在「⚙ 高级」里点「🔍 体检」，"
                  "自动生成一份健康报告，哪里不正常写得明明白白。"),
            ("n", "Q：想手动开关 mod 还可以吗？\n"
                  "A：随时可以。「⚙ 高级」里能直接编辑每个世界的 mod 列表、"
                  "一键应用或恢复——自动和手动互不打架。"),
            ("dim", "—— 状态栏会随时告诉你正在发生什么；"
                    "有疑问先看这里，解决不了再找爱莉希雅~"),
        ]
        for tag, s in sections:
            txt.insert("end", s + "\n", tag if tag != "n" else ())
        txt.config(state="disabled")

    def open_advanced(self):
        """高级窗口：启用集编辑 / 退出策略 / 手动操作 / 路径设置。"""
        if getattr(self, "adv_win", None) is not None and \
                self.adv_win.winfo_exists():
            self.adv_win.deiconify()
            self.adv_win.lift()
            return
        dlg = tk.Toplevel(self.root)
        self.adv_win = dlg
        dlg.title("高级")
        dlg.transient(self.root)
        dlg.geometry("540x860")
        dlg.protocol("WM_DELETE_WINDOW", dlg.withdraw)  # 关闭=隐藏，引用不失效
        f = ttk.Frame(dlg, padding=10)
        f.pack(fill="both", expand=True)

        # 1. 启用集编辑
        cur = ttk.LabelFrame(f, text=" 所选世界的启用集（每行一个 mod，可编辑） ")
        cur.pack(fill="both", expand=True)
        self.mods_txt = tk.Text(cur, height=6, width=46, undo=True,
                                wrap="none")
        self.mods_txt.pack(side="left", fill="both", expand=True,
                           padx=(4, 0), pady=4)
        sb2 = ttk.Scrollbar(cur, command=self.mods_txt.yview)
        sb2.pack(side="left", fill="y", pady=4)
        self.mods_txt.config(yscrollcommand=sb2.set)

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="从世界数据提取", width=14,
                   command=self.fill_from_world).pack(side="left", padx=(0, 4))
        ttk.Button(btns, text="抓取当前启用集", width=14,
                   command=self.capture_current).pack(side="left", padx=4)
        ttk.Button(btns, text="保存配置", width=10,
                   command=self.save_world_cfg).pack(side="left", padx=4)
        ttk.Button(btns, text="清空", width=8,
                   command=lambda: self.set_mods_text([])).pack(side="left",
                                                                padx=4)

        # 2. 退出策略
        modef = ttk.LabelFrame(f, text=" 游戏退出后 ")
        modef.pack(fill="x", pady=(8, 0))
        for val, txt in (("writeback", "回写为世界配置"),
                         ("restore", "恢复启动前状态"),
                         ("none", "不做处理")):
            ttk.Radiobutton(modef, text=txt, value=val,
                            variable=self.exit_mode_var).pack(side="left",
                                                              padx=6, pady=4)
        self.exit_mode_var.trace_add("write", lambda *_: self._persist_exit_mode())

        # 3. 手动操作
        opf = ttk.LabelFrame(f, text=" 手动操作 ")
        opf.pack(fill="x", pady=(8, 0))
        ttk.Button(opf, text="仅应用配置", width=12,
                   command=self.apply_only).pack(side="left", padx=4, pady=4)
        ttk.Button(opf, text="恢复启动前状态", width=14,
                   command=self.restore_pre_state).pack(side="left", padx=4)
        ttk.Button(opf, text="🔍 体检", width=10,
                   command=self.run_check_gui).pack(side="left", padx=4)

        # 4. 路径设置（自动检测失败时才需要动）
        pth = ttk.LabelFrame(f, text=" 路径（换机扫不到时手动指定） ")
        pth.pack(fill="x", pady=(8, 0))

        ttk.Label(pth, foreground="#666", wraplength=440, justify="left",
                  text="存档目录 = 世界（Worlds）和 mod 设置（Mods）存放的地方，\n"
                       "一般是「文档\\My Games\\Terraria\\tModLoader」").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=4)
        ttk.Label(pth, text="存档目录").grid(
            row=1, column=0, sticky="w", padx=4)
        e1 = ttk.Entry(pth, width=46)
        e1.grid(row=2, column=0, sticky="we", padx=4)
        e1.insert(0, self.cfg.get("save_dir") or "")

        def browse_dir():
            p = filedialog.askdirectory(title="选择 tModLoader 存档目录")
            if p:
                e1.delete(0, "end")
                e1.insert(0, p)

        ttk.Button(pth, text="浏览", command=browse_dir).grid(
            row=2, column=1, padx=(4, 4))

        ttk.Label(pth, foreground="#666", wraplength=440, justify="left",
                  text="启动脚本 = Steam 打不开游戏时才用的备用启动器"
                       "（游戏本体目录里的 start-tModLoader.bat），平时留空即可"
                  ).grid(row=3, column=0, columnspan=2, sticky="w", padx=4,
                         pady=(6, 0))
        ttk.Label(pth, text="启动脚本 start-tModLoader.bat").grid(
            row=4, column=0, sticky="w", padx=4)
        e2 = ttk.Entry(pth, width=46)
        e2.grid(row=5, column=0, sticky="we", padx=4)
        e2.insert(0, self.cfg.get("tml_bat") or "")

        def browse_bat():
            p = filedialog.askopenfilename(
                title="选择 start-tModLoader.bat",
                filetypes=[("启动脚本", "*.bat"), ("所有文件", "*.*")])
            if p:
                e2.delete(0, "end")
                e2.insert(0, p)

        ttk.Button(pth, text="浏览", command=browse_bat).grid(
            row=5, column=1, padx=(4, 4))

        def save_paths():
            sd = e1.get().strip()
            bt = e2.get().strip()
            self.cfg["save_dir"] = sd if os.path.isdir(sd) else ""
            self.cfg["tml_bat"] = bt if (not bt or os.path.isfile(bt)) else bt
            self.save_config()
            self.save_dir = resolve_save_dir(self.cfg)
            self._apply_paths()
            self.refresh_all()
            self.status("路径已保存并重新扫描")

        ttk.Button(pth, text="保存路径", command=save_paths).grid(
            row=6, column=0, sticky="w", padx=4, pady=(6, 4))

        info = ttk.Label(pth, foreground="#666", wraplength=440,
                         justify="left",
                         text=f"自动检测结果：\n存档：{self.save_dir}\n"
                              f"Steam：{get_steam_path() or '未找到'}\n"
                              f"安装目录：{get_tml_install_dir() or '未找到'}")
        info.grid(row=7, column=0, columnspan=2, sticky="w", padx=4,
                  pady=(4, 4))

        # 打开时同步当前选择
        info_cur = self.current_world()
        if info_cur:
            self.on_select_world()

    def _persist_exit_mode(self):
        self.cfg["exit_mode"] = self.exit_mode_var.get()
        self.save_config()

    def status(self, msg):
        self.status_lbl.config(text=msg)

    def set_mods_text(self, mods):
        if getattr(self, "mods_txt", None) is None:
            return  # 高级窗口未打开时无可编辑区，跳过
        self.mods_txt.delete("1.0", "end")
        self.mods_txt.insert("1.0", "\n".join(mods))

    def get_mods_text(self):
        if getattr(self, "mods_txt", None) is None:
            return []
        raw = self.mods_txt.get("1.0", "end")
        seen, out = set(), []
        for line in raw.splitlines():
            m = line.strip()
            if m and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    # ---------------- 世界选择 / 填充

    def refresh_all(self, keep_selection=False):
        if not self.save_dir_valid:
            self.world_list.delete(0, "end")
            self.world_map = {}
            self.status("未找到 tModLoader 存档目录——点「⚙ 高级」手动指定")
            return
        sel_name = None
        if keep_selection:
            info = self.current_world()
            sel_name = info[0] if info else None
        self.world_map = dict(self.list_worlds())
        self.world_list.delete(0, "end")
        for idx, name in enumerate(self.world_map):
            mark = "● " if name in self.cfg["worlds"] else "  "
            self.world_list.insert("end", mark + name)
            if name == sel_name:
                self.world_list.selection_set(idx)
        self.status(f"发现 {len(self.world_map)} 个世界 · 配置文件：{self.cfg_path}")

    def current_world(self):
        sel = self.world_list.curselection()
        if not sel:
            return None
        name = self.world_list.get(sel[0]).strip("● ")
        return name, self.world_map.get(name)

    def on_select_world(self):
        info = self.current_world()
        if not info:
            return
        name, _ = info
        saved = self.cfg["worlds"].get(name)
        if saved:
            mods = saved.get("mods", [])
            self.set_mods_text(mods)
            self.sel_info_lbl.config(
                text=f"已保存配置 · {len(mods)} 个 mod\n启动时自动写入，退出后自动结算")
            self.status(f"已载入「{name}」的配置（{len(mods)} 个 mod）")
        else:
            detected = self.detect_for_world(name)
            self.set_mods_text(detected)
            if detected:
                self.sel_info_lbl.config(
                    text=f"首次使用 · 已自动从世界数据提取 {len(detected)} 个 mod\n"
                         "点启动即自动生效（含依赖库）")
            else:
                self.sel_info_lbl.config(
                    text="原版世界（无模组数据）\n可在「⚙ 高级」里手动抓取当前启用集")
            tip = (f"「{name}」暂无配置——已自动从世界数据提取 {len(detected)} 个 mod"
                   if detected else
                   f"「{name}」暂无配置（原版世界？）——"
                   f"可在「⚙ 高级」手动设置")
            if getattr(self, "last_detect_fallback", False) and detected:
                tip += "（usedMods 解析失败，走了降级匹配）"
            self.status(tip)
        self.sel_name_lbl.config(text=name)

    def detect_for_world(self, name):
        wld = self.world_map.get(name)
        if not wld:
            return []
        twld = os.path.splitext(wld)[0] + ".twld"
        if not os.path.isfile(twld):
            return []
        # 首选：usedMods 节（= 游戏弹窗对比的那份「上次启用列表」，含不写数据的依赖库）
        self.last_detect_fallback = False
        used = extract_used_mods_from_twld(twld)
        if used is not None:
            return used
        # 兜底：匹配写过数据的 mod（可能缺依赖库，导致首次进世界仍有提示）
        self.last_detect_fallback = True
        known = self.installed_mod_names()
        return extract_mod_names_from_twld(twld, known)

    def fill_from_world(self):
        info = self.current_world()
        if not info:
            messagebox.showinfo(APP_NAME, "先在左侧选择一个世界")
            return
        det = self.detect_for_world(info[0])
        self.set_mods_text(det)
        self.status(f"已提取 {len(det)} 个 mod（检测依赖库可能不全，可手动补充）")

    def capture_current(self):
        cur = self.read_enabled()
        self.set_mods_text(cur)
        self.status(f"已抓取当前启用集（{len(cur)} 个 mod）")

    def save_world_cfg(self):
        info = self.current_world()
        if not info:
            messagebox.showinfo(APP_NAME, "先在左侧选择一个世界")
            return
        name = info[0]
        mods = self.get_mods_text()
        if not mods:
            if not messagebox.askyesno(APP_NAME, f"「{name}」的启用集为空"
                                       "（原版世界？），确定保存？"):
                return
        self.cfg["worlds"][name] = {"mods": mods,
                                    "updated": datetime.now().isoformat(
                                        timespec="seconds")}
        self.save_config()
        self.refresh_all(keep_selection=True)
        self.status(f"已保存「{name}」配置（{len(mods)} 个 mod）")

    # ---------------- 写入与启动

    def game_running(self):
        """轻量检测 tML 是否正在运行（dotnet.exe 且命令行含 tModLoader）。"""
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Process -Filter \"Name='dotnet.exe'"
                 " OR Name='tModLoader.exe'\" | Where-Object "
                 "{ $_.CommandLine -match 'tModLoader' } | "
                 "Measure-Object).Count"],
                capture_output=True, text=True, timeout=20,
                creationflags=0x08000000)  # CREATE_NO_WINDOW
            return int(out.stdout.strip() or 0) > 0
        except Exception:
            return False

    def _precheck(self):
        if not self.save_dir_valid:
            messagebox.showwarning(APP_NAME, "未找到 tModLoader 存档目录，"
                                   "请先在「⚙ 高级」中指定")
            return False
        if not self.current_world():
            messagebox.showinfo(APP_NAME, "先在左侧选择一个世界")
            return False
        if self.game_running():
            messagebox.showwarning(APP_NAME, "检测到 tModLoader 正在运行，"
                                   "请先退出游戏再操作（否则配置会被游戏覆盖）")
            return False
        return True

    def apply_config_for(self, name):
        mods = self.cfg["worlds"].get(name, {}).get("mods")
        if mods is None:
            mods = self.get_mods_text()
        if not mods:
            # 高级窗口未打开、也无保存配置 → 直接从世界数据提取
            mods = self.detect_for_world(name)
        if not mods:
            messagebox.showwarning(APP_NAME, f"「{name}」没有配置启用集")
            return False
        valid, dropped = self.write_enabled(mods)
        msg = f"已写入 enabled.json：{len(valid)} 个 mod（原文件已备份为 .bak）"
        if dropped:
            msg += f" · 未安装已跳过 {len(dropped)} 个：{'、'.join(dropped)}"
        self.status(msg)
        return True

    def launch_world(self):
        info = self.current_world()
        if not info:
            messagebox.showinfo(APP_NAME, "先在左侧选择一个世界")
            return
        name = info[0]
        if not self.cfg["worlds"].get(name) and not self.get_mods_text():
            self.on_select_world()
        if not self._precheck():
            return
        # 快照启动前状态（必须在写配置之前！）
        self.pre_state = self.read_enabled()
        self.cfg["pre_state"] = {"mods": self.pre_state,
                                 "world": name,
                                 "time": datetime.now().isoformat(
                                     timespec="seconds")}
        self.save_config()
        if not self.apply_config_for(name):
            return

        # 拉起游戏（Steam 优先，找不到 Steam 则退回启动脚本）
        if not self._do_launch():
            return

        self.launching = True
        self.session_netplay = False
        self.launch_btn.config(state="disabled")
        self.status(f"等待 tModLoader 启动（世界：{name}）……启动后请正常选世界游玩")
        self.start_exit_watcher(name)

    def _do_launch(self):
        """拉起游戏。返回 False 表示启动失败（已提示用户）。"""
        bat = self.cfg.get("tml_bat")
        if bat and os.path.isfile(bat):
            try:
                subprocess.Popen(
                    [bat], cwd=os.path.dirname(bat),
                    creationflags=0x08000000)  # CREATE_NO_WINDOW
                return True
            except OSError as e:
                messagebox.showerror(APP_NAME, f"启动脚本执行失败：{e}")
                return False
        if get_steam_path() and get_steam_protocol_ok():
            try:
                os.startfile(f"steam://rungameid/{STEAM_APPID}")
                return True
            except OSError as e:
                messagebox.showerror(APP_NAME, f"Steam 启动失败：{e}")
                return False
        # Steam 未装/协议失效：优先用自动找到的启动脚本，否则让用户选
        inst = get_tml_install_dir()
        if inst:
            bat = os.path.join(inst, "start-tModLoader.bat")
            self.cfg["tml_bat"] = bat
            self.save_config()
            return self._do_launch()
        if messagebox.askyesno(
                APP_NAME, "未检测到 Steam，也没有配置启动脚本。\n"
                "现在选择 tModLoader 目录下的 start-tModLoader.bat 吗？"):
            p = filedialog.askopenfilename(
                title="选择 start-tModLoader.bat",
                filetypes=[("启动脚本", "*.bat"), ("所有文件", "*.*")])
            if p:
                self.cfg["tml_bat"] = p
                self.save_config()
                return self._do_launch()
        return False

    def start_exit_watcher(self, world_name):
        ps_script = r"""
$f=$false; $dl=(Get-Date).AddSeconds(300)
while((Get-Date) -lt $dl){
  $p=Get-CimInstance Win32_Process -Filter "Name='dotnet.exe' OR Name='tModLoader.exe'" |
     Where-Object { $_.CommandLine -match 'tModLoader' }
  if($p){$f=$true; break}
  Start-Sleep -Milliseconds 1500
}
if($f){
  Write-Output 'FOUND'
  $net=$false
  while($true){
    $p=Get-CimInstance Win32_Process -Filter "Name='dotnet.exe' OR Name='tModLoader.exe'" |
       Where-Object { $_.CommandLine -match 'tModLoader' }
    if(-not $p){ Write-Output 'EXITED'; break }
    if(-not $net){
      $srv = Get-Process tModLoaderServer -ErrorAction SilentlyContinue
      if(-not $srv){
        $conn = Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue |
                Where-Object { $_.RemotePort -eq 7777 }
      }
      if($srv -or $conn){ $net=$true; Write-Output 'NETPLAY' }
    }
    Start-Sleep -Seconds 3
  }
}else{ Write-Output 'TIMEOUT' }
"""
        try:
            self.proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", ps_script],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8",
                creationflags=0x08000000)
        except OSError as e:
            self.status(f"监听进程启动失败：{e}（自动结算不可用，可手动恢复）")
            return
        # 后台线程读 watcher 输出 → 队列；主循环定时取，绝不阻塞界面
        self.watch_q = queue.Queue()
        self._watch_thread = threading.Thread(
            target=self._read_watcher, daemon=True)
        self._watch_thread.start()
        self._drain_queue(world_name)

    def _read_watcher(self):
        try:
            for line in self.proc.stdout:
                self.watch_q.put(line.strip())
        except Exception:
            pass
        self.watch_q.put("WATCHER_DEAD")

    def _drain_queue(self, world_name):
        try:
            while True:
                sig = self.watch_q.get_nowait()
                if sig == "FOUND":
                    self.status("游戏运行中……退出后将自动结算")
                elif sig == "NETPLAY":
                    self.session_netplay = True
                    self.status("检测到联机会话（连服或本机开服）——退出后将"
                                "恢复启动前状态，不会回写污染世界配置")
                elif sig == "EXITED":
                    self.on_game_exit(world_name)
                    return
                elif sig in ("TIMEOUT", "WATCHER_DEAD"):
                    if sig == "TIMEOUT":
                        self.status("300 秒内未检测到游戏启动——已恢复启动前状态，"
                                    "请确认 Steam 能正常登录")
                        if self.pre_state is not None:
                            self.write_enabled(self.pre_state)
                    else:
                        self.status("监听进程意外退出（自动结算不可用，可手动恢复）")
                    self._finish_launch()
                    return
        except queue.Empty:
            pass
        if self.launching:
            self.root.after(400, lambda: self._drain_queue(world_name))

    def on_game_exit(self, world_name):
        mode = self.exit_mode_var.get()
        self.cfg["exit_mode"] = mode
        final = self.read_enabled()
        # 联机会话保护：启用集被服务器改过，回写会污染世界配置 → 强制恢复
        if mode == "writeback" and self.session_netplay:
            mode = "restore"
        if mode == "writeback":
            self.cfg["worlds"][world_name] = {
                "mods": final,
                "updated": datetime.now().isoformat(timespec="seconds")}
            self.save_config()
            self.refresh_all(keep_selection=True)
            self.status(f"游戏已退出：本次会话实际启用集（{len(final)} 个）"
                        f"已回写为「{world_name}」的配置")
        elif mode == "restore":
            if self.cfg.get("pre_state"):
                self.write_enabled(self.cfg["pre_state"]["mods"])
            reason = ("（联机会话，已自动改用恢复模式，"
                      "未回写世界配置）" if self.session_netplay else "")
            self.status(f"游戏已退出：已恢复启动前状态{reason}")
        else:
            self.status(f"游戏已退出（未做处理）；当前启用集 {len(final)} 个 mod")
        self._finish_launch()

    def _finish_launch(self):
        self.launching = False
        self.launch_btn.config(state="normal")
        self.proc = None

    # ---------------- 手动操作

    def apply_only(self):
        info = self.current_world()
        if not info or not self._precheck():
            return
        if self.apply_config_for(info[0]):
            messagebox.showinfo(APP_NAME,
                                "配置已写入。下次启动 tModLoader 生效。")

    def run_check_gui(self):
        """体检按钮：生成健康报告并自动打开。"""
        if self.game_running():
            messagebox.showwarning(APP_NAME, "游戏正在运行，先退出再体检")
            return
        try:
            ok = run_selfcheck()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"体检失败：{e}")
            return
        self.status("体检完成：" + ("一切正常 ✔" if ok else
                                   "发现问题，详见已打开的报告"))

    def restore_pre_state(self):
        pre = self.cfg.get("pre_state")
        if not pre:
            messagebox.showinfo(APP_NAME, "没有可恢复的启动前快照")
            return
        if self.game_running():
            messagebox.showwarning(APP_NAME, "游戏正在运行，先退出再恢复")
            return
        self.write_enabled(pre["mods"])
        self.cfg["pre_state"] = None
        self.save_config()
        self.status(f"已恢复 {pre['time']} 启动前状态（世界：{pre.get('world')}）")


def acquire_single_instance():
    """单实例互斥锁。返回 False 表示已有实例在运行。"""
    if os.name != "nt":
        return True
    import ctypes
    # use_last_error=True：错误码进 ctypes 私有缓存，杜绝中间调用冲掉
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW(None, False, "Global\\TMLWorldMods_SingleInstance")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def run_selfcheck(save_dir=None):
    """一键体检：环境探测 + 全部世界解析 + 启用集一致性。
    报告写入 <exe旁>/TMLWorldMods-check.txt 并打开。"""
    lines = []
    ok = True

    def add(s):
        lines.append(s)

    add(f"=== {APP_NAME} 自检报告  {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    sd = save_dir or resolve_save_dir({})
    add(f"[环境] 存档目录: {sd} {'OK' if os.path.isdir(sd) else '未找到!'}")
    if not os.path.isdir(sd):
        ok = False
    add(f"[环境] Steam: {get_steam_path() or '未找到(可改用启动脚本)'}")
    add(f"[环境] steam://协议: "
        f"{'已注册 OK（Steam 未运行也会自动拉起）' if get_steam_protocol_ok() else '未注册! 将走启动脚本'}")
    inst = get_tml_install_dir()
    add(f"[环境] tML安装: {inst or '未找到!'}")
    if not inst:
        deep_inst = find_tml_installs_deep()
        add(f"[环境] 深度扫描安装目录: "
            f"{'; '.join(deep_inst) if deep_inst else '无结果'}")
        ok = bool(deep_inst)
    ws = get_workshop_dirs()
    add(f"[环境] Workshop目录: {len(ws)} 个")

    app = App.__new__(App)
    app.save_dir = sd
    app._apply_paths()
    app.cfg_path = config_path()
    app.cfg = {"worlds": {}, "save_dir": "", "tml_bat": ""}
    installed = app.installed_mod_names()
    add(f"[环境] 识别到已安装 mod: {len(installed)} 个")

    add("")
    worlds = app.list_worlds()
    if not worlds:
        add("[世界] 未发现任何 .wld！")
        ok = False
    for name, wld in worlds:
        twld = os.path.splitext(wld)[0] + ".twld"
        if not os.path.isfile(twld):
            add(f"[世界] {name}: 无 .twld（原版世界，可手动抓取当前启用集）")
            continue
        used = extract_used_mods_from_twld(twld)
        if used is None:
            known = installed
            used = extract_mod_names_from_twld(twld, known)
            add(f"[世界] {name}: usedMods 解析失败，降级匹配到 {len(used)} 个"
                "（把此报告发给开发者）")
            ok = False
        else:
            missing = [m for m in used if m not in installed]
            add(f"[世界] {name}: {len(used)} 个 mod"
                + (f"，其中未安装 {len(missing)} 个: {'、'.join(missing)}"
                   if missing else "，全部已安装 OK"))
    add("")
    cur = app.read_enabled()
    miss = [m for m in cur if m not in installed]
    add(f"[当前启用集] {len(cur)} 个"
        + (f"，其中未安装 {len(miss)} 个: {'、'.join(miss)}" if miss
           else "，全部已安装 OK"))
    add("")
    add("结论: " + ("一切正常 ✔" if ok else "存在问题，见上方 [!] 标记"))

    report = os.path.join(app_dir(), APP_NAME + "-check.txt")
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    text = "\n".join(lines)
    print(text)
    if getattr(sys, "frozen", False):
        try:
            os.startfile(report)
        except OSError:
            pass
    return ok


def main():
    if "--check" in sys.argv:
        run_selfcheck()
        return
    if not acquire_single_instance():
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, APP_NAME + " 已经在运行了哦~", APP_NAME, 0x40)
        return
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
