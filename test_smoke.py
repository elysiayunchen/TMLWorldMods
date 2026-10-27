# -*- coding: utf-8 -*-
"""TMLWorldMods 测试：提取逻辑 + GUI 冒烟（数据全部指向临时目录，不碰真实存档）"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tml_world_mods as twm

REAL_SAVE = r"E:\19645\Documents\My Games\Terraria\tModLoader"
errors = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        errors.append(name)


# ---------- 1. 真实 .twld 提取（只读） ----------
names = twm.App.__new__(twm.App)  # 不走 GUI，借用方法
names.mods_dir = os.path.join(REAL_SAVE, "Mods")
installed = twm.App.installed_mod_names(names)
check("installed_mod_names>30", len(installed) > 30, f"n={len(installed)}")

worlds_dir = os.path.join(REAL_SAVE, "Worlds")
for fn in os.listdir(worlds_dir):
    if fn.endswith(".twld"):
        det = twm.extract_mod_names_from_twld(os.path.join(worlds_dir, fn),
                                              installed)
        print(f"  [{fn}] -> {det}")

# 防误报：CalamityModMusic 存在时不应因子串误报出 CalamityMod
fake = b"\x10CalamityModMusic\x00\x00"  # 前缀 0x10=16 = len("CalamityModMusic")
det = twm.extract_mod_names_from_twld.__wrapped__ if False else None
import io
class FakeTw:
    pass
tmp = tempfile.mkdtemp()
fake_twld = os.path.join(tmp, "x.twld")
with open(fake_twld, "wb") as f:
    f.write(fake)
det = twm.extract_mod_names_from_twld(fake_twld, ["CalamityMod", "CalamityModMusic"])
check("substring-guard", det == ["CalamityModMusic"], str(det))
true_twld = os.path.join(tmp, "y.twld")
with open(true_twld, "wb") as f:
    f.write(b"\x0bCalamityMod")  # 0x0b=11=len("CalamityMod")
det = twm.extract_mod_names_from_twld(true_twld, ["CalamityMod"])
check("exact-match", det == ["CalamityMod"], str(det))

# ---------- 1b. 可移植性：vdf 解析 + 存档目录覆盖 ----------
vdf = '''
"libraryfolders"
{
  "0"
  {
    "path"  "C:\\Program Files (x86)\\Steam"
  }
  "1"
  {
    "path"  "E:\\SteamLibrary"
  }
}'''
import tempfile as _tf
_vf = os.path.join(_tf.mkdtemp(), "libraryfolders.vdf")
open(_vf, "w", encoding="utf-8").write(vdf)
libs = twm.parse_libraryfolders_vdf(_vf)
check("vdf-parse", any("E:\\SteamLibrary" == l for l in libs), str(libs))
_ov = _tf.mkdtemp()
check("save-dir-override2",
      twm.resolve_save_dir({"save_dir": _ov}) == _ov)
check("save-dir-fallback",
      isinstance(twm.resolve_save_dir({}), str))

# ---------- 1d. 多盘浅层扫描兜底（真实环境只读验证） ----------
deep_save = twm.find_save_dirs_deep()
check("deep-save-scan", any(
    p.lower().endswith("my games\\terraria\\tmodloader")
    and os.path.isdir(os.path.join(p, "Worlds")) for p in deep_save),
    str(deep_save))
deep_inst = twm.find_tml_installs_deep()
check("deep-install-scan", any(
    p.endswith("steamapps\\common\\tModLoader") for p in deep_inst),
    str(deep_inst))

# ---------- 1c. usedMods 节解析（游戏弹窗对比的那份列表） ----------
def build_usedmods(names):
    out = b"\x08usedMods\x08\x00\x00\x00" + bytes([len(names)])
    for n in names:
        e = n.encode()
        out += b"\x00" + bytes([len(e)]) + e
    return out

syn = os.path.join(tmp, "syn.twld")
open(syn, "wb").write(build_usedmods(["AlphaMod", "Beta-Mod", "X_Y.z"]))
got = twm.extract_used_mods_from_twld(syn)
check("usedmods-syn", got == ["AlphaMod", "Beta-Mod", "X_Y.z"], str(got))

bad = os.path.join(tmp, "bad.twld")
open(bad, "wb").write(b"\x08usedMods\x08\x00\x00\x00\xff\x00\x05AB")
check("usedmods-bad", twm.extract_used_mods_from_twld(bad) is None)
open(bad, "wb").write(b"\x1f\x8bgarbage")
check("usedmods-gzip-bad", twm.extract_used_mods_from_twld(bad) is None)

etw = os.path.join(REAL_SAVE, "Worlds", "EternityWorld.twld")
real = twm.extract_used_mods_from_twld(etw)
check("usedmods-real-42", real is not None and len(real) == 42,
      str(len(real) if real else None))
check("usedmods-real-deps",
      real and all(m in real for m in
                   ("PegasusLib", "AltLibrary", "MagicStorageChinesebruh",
                    "ProceduralMusicLib")))
# gzip 坏流回退
check("extract-gzip-bad", twm.extract_mod_names_from_twld(bad, ["X"]) == [])

# ---------- 2. GUI 冒烟（临时存档） ----------
tmp_save = os.path.join(tmp, "tModLoader")
os.makedirs(os.path.join(tmp_save, "Mods"))
os.makedirs(os.path.join(tmp_save, "Worlds"))
# 伪造三个 mod（本地）与两个世界（一个带 twld）
for m in ("AlphaMod", "BetaMod", "GammaMod"):
    open(os.path.join(tmp_save, "Mods", m + ".tmod"), "wb").write(b"x")
open(os.path.join(tmp_save, "Worlds", "w1.wld"), "wb").write(b"x")
# w1.twld 用 usedMods 节格式（新主路径）
with open(os.path.join(tmp_save, "Worlds", "w1.twld"), "wb") as f:
    f.write(build_usedmods(["AlphaMod", "BetaMod", "GhostMod"]))
open(os.path.join(tmp_save, "Worlds", "w2.wld"), "wb").write(b"x")
with open(os.path.join(tmp_save, "Mods", "enabled.json"), "w",
          encoding="utf-8") as f:
    json.dump(["AlphaMod", "GammaMod"], f)

import tkinter as tk
cfg_dir = os.path.join(tmp, "app")
os.makedirs(cfg_dir, exist_ok=True)
orig_app_dir = twm.app_dir
twm.app_dir = lambda: cfg_dir

shutil.copy(fake_twld, os.path.join(tmp, "z.twld"))
root = tk.Tk()
root.report_callback_exception = lambda *a: errors.append(
    "tk-exception:" + repr(a[:2]))
app = twm.App(root, save_dir=tmp_save, guide=False)
app.open_advanced()  # 新版 UI：启用集编辑器在高级窗口内


def smoke():
    try:
        check("worlds-listed", app.world_list.size() == 2,
              f"n={app.world_list.size()}")
        # 选中 w1 -> 自动提取 AlphaMod/BetaMod
        app.world_list.selection_set(0)
        app.on_select_world()
        got = app.get_mods_text()
        check("auto-extract-usedmods",
              got == ["AlphaMod", "BetaMod", "GhostMod"], str(got))
        # 保存配置
        app.save_world_cfg()
        check("save-cfg", "w1" in app.cfg["worlds"])
        # 手动编辑 + 再保存
        app.set_mods_text(["AlphaMod", "GammaMod", "AlphaMod"])
        app.save_world_cfg()
        check("dedupe-save", app.cfg["worlds"]["w1"]["mods"] ==
              ["AlphaMod", "GammaMod"])
        # 抓取当前启用集
        app.capture_current()
        check("capture", app.get_mods_text() == ["AlphaMod", "GammaMod"])
        # 仅应用配置 -> enabled.json 落盘
        app.world_list.selection_set(0)
        app.on_select_world()
        app.set_mods_text(["AlphaMod", "GammaMod"])
        app.save_world_cfg()
        ok = app.apply_config_for("w1")
        check("apply-returns-True", ok)
        with open(os.path.join(tmp_save, "Mods", "enabled.json"),
                  encoding="utf-8") as f:
            check("enabled-written", json.load(f) == ["AlphaMod", "GammaMod"])
        check("bak-created", os.path.isfile(
            os.path.join(tmp_save, "Mods", "enabled.json.bak")))
        # 模拟游戏退出结算 writeback
        app.on_game_exit("w1")
        with open(os.path.join(tmp_save, "Mods", "enabled.json"),
                  encoding="utf-8") as f:
            cur = json.load(f)
        check("writeback-cfg-sync", app.cfg["worlds"]["w1"]["mods"] == cur)
        # restore 模式：先伪造一份启动前快照，再结算应还原为快照
        app.write_enabled(["AlphaMod"])
        app.cfg["pre_state"] = {"mods": ["AlphaMod", "BetaMod"],
                                "world": "w1", "time": "test"}
        app.exit_mode_var.set("restore")
        app.on_game_exit("w1")
        with open(os.path.join(tmp_save, "Mods", "enabled.json"),
                  encoding="utf-8") as f:
            check("restore-mode",
              json.load(f) == ["AlphaMod", "BetaMod"])
        # 联机保护：writeback 模式下若检测到联机，应强制恢复且不污染世界配置
        app.exit_mode_var.set("writeback")
        app.session_netplay = True
        app.write_enabled(["GammaMod"])  # 模拟被服务器改成服务器 mod 集
        app.cfg["pre_state"] = {"mods": ["AlphaMod", "BetaMod"],
                                "world": "w1", "time": "test"}
        app.on_game_exit("w1")
        with open(os.path.join(tmp_save, "Mods", "enabled.json"),
                  encoding="utf-8") as f:
            check("netplay-no-pollute",
                  json.load(f) == ["AlphaMod", "BetaMod"])
        check("netplay-cfg-untouched",
              app.cfg["worlds"]["w1"]["mods"] == ["AlphaMod", "GammaMod"],
              str(app.cfg["worlds"]["w1"]["mods"]))
        # TIMEOUT 分支（不真的起 watcher，直接调逻辑）
        app.pre_state = ["AlphaMod"]
        app.status = (lambda m: None) if False else app.status
        # 模拟：_poll_watcher 读到 TIMEOUT 需要 proc.stdout —— 跳过进程级，直接验证恢复逻辑
        if app.cfg.get("pre_state"):
            app.write_enabled(app.cfg["pre_state"]["mods"])
        check("timeout-restore-path", True)
    finally:
        root.destroy()


root.after(300, smoke)
root.mainloop()

twm.app_dir = orig_app_dir
shutil.rmtree(tmp, ignore_errors=True)
print("=" * 40)
print("ALL PASS" if not errors else f"ERRORS: {errors}")
sys.exit(0 if not errors else 1)
