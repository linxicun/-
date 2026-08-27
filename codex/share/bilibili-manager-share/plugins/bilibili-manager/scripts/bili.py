#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bili.py - B站数据同步/知识库工具 (bilibili-manager 插件)

功能: 拉取收藏夹/关注列表/UP主投稿/字幕并缓存为本地 JSON,
供 AI Agent 生成 Markdown 知识库使用。对 B站 只读
(仅 create-folder / apply-plan 两个写操作, 且默认干跑预览)。
纯标准库实现, 无第三方依赖。

用法: python bili.py --data-dir <数据目录> <子命令> ...
数据目录: 优先环境变量 BILI_DATA_DIR, 其次当前目录下的 bili-data,
         也可用 --data-dir 显式指定。
"""
import argparse
import csv
import hashlib
import html as _html
import io
import json
import os
import random
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API = "https://api.bilibili.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
REFERER = "https://www.bilibili.com/"
DELAY_DEFAULT = 0.8
WBI_TTL = 6 * 3600  # WBI 密钥缓存时长(秒)

# WBI 混淆表 (bilibili-API-collect 公开算法)
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43,
    5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16,
    24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59,
    6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def cache_path(data_dir, *parts):
    p = os.path.join(data_dir, "cache", *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def read_text(path):
    """读取文本, 自动剥掉 UTF-8 BOM(某些编辑器保存的卡片首行会有 BOM)。"""
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read()


def load_config(data_dir):
    """敏感配置: SESSDATA/bili_jct 等登录态, 仅存本地。"""
    return load_json(os.path.join(data_dir, "config.json"), {}) or {}


def save_config(data_dir, cfg):
    save_json(os.path.join(data_dir, "config.json"), cfg)


def load_settings(data_dir):
    """非敏感配置: 重点领域、复习间隔等, 可与 config.json 分离备份。"""
    s = load_json(os.path.join(data_dir, "settings.json"), {}) or {}
    # 迁移旧版 config.json 中的 focus 字段
    if not s.get("focus"):
        cfg = load_config(data_dir)
        legacy = cfg.get("focus")
        if legacy:
            s["focus"] = legacy
            cfg.pop("focus", None)
            save_config(data_dir, cfg)
            save_settings(data_dir, s)
    return s


def save_settings(data_dir, s):
    save_json(os.path.join(data_dir, "settings.json"), s)


def cookie_of(cfg):
    sd = cfg.get("sessdata", "")
    return f"SESSDATA={sd}" if sd else ""


def rate_sleep(delay=DELAY_DEFAULT):
    """带随机抖动的限速, 避免节奏性请求被风控识别。"""
    time.sleep(max(0.0, delay) + random.uniform(0.0, 0.3))


def _http_json(url, params=None, body=None, cookie="", referer=REFERER, retries=3):
    """GET/POST JSON 接口, 遇到风控(-412)/繁忙(-509)自动退避重试。"""
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    headers = {
        "User-Agent": UA,
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.bilibili.com",
    }
    data = None
    if body is not None:
        data = urllib.parse.urlencode(body).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        headers["Cookie"] = cookie
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read()
            j = json.loads(raw.decode("utf-8", errors="replace"))
            if j.get("code") in (-412, -509):
                wait = 3.0 * (attempt + 1)
                sys.stderr.write(f"[warn] 风控/繁忙 code={j.get('code')}, {wait:.0f}s 后重试\n")
                time.sleep(wait)
                last = j
                continue
            return j
        except urllib.error.HTTPError as e:
            last = {"code": -e.code, "message": f"HTTP {e.code}"}
            if e.code in (412, 403):
                time.sleep(3.0 * (attempt + 1))
                continue
            break
        except Exception as e:
            last = {"code": -9998, "message": str(e)}
            time.sleep(2.0 * (attempt + 1))
    return last or {"code": -9999, "message": "请求失败"}


def http_get(url, params=None, cookie="", referer=REFERER, retries=3):
    return _http_json(url, params=params, cookie=cookie, referer=referer, retries=retries)


def http_post(url, fields, cookie, referer=REFERER, retries=3):
    return _http_json(url, body=fields, cookie=cookie, referer=referer, retries=retries)


def get_wbi_keys(data_dir, cookie):
    """获取 WBI 签名密钥, 带本地缓存(WBI_TTL 内不重复请求)。"""
    cache = cache_path(data_dir, "wbi.json")
    j = load_json(cache) or {}
    if j.get("img_key") and j.get("fetched_at") and time.time() - j["fetched_at"] < WBI_TTL:
        return j["img_key"], j["sub_key"]
    d = http_get(f"{API}/x/web-interface/nav", cookie=cookie)
    if d.get("code") not in (0, -101):
        raise RuntimeError(f"nav 接口异常: code={d.get('code')} msg={d.get('message')}")
    wbi = (d.get("data") or {}).get("wbi_img") or {}
    img_key = wbi["img_url"].rsplit("/", 1)[1].split(".")[0]
    sub_key = wbi["sub_url"].rsplit("/", 1)[1].split(".")[0]
    save_json(cache, {"img_key": img_key, "sub_key": sub_key, "fetched_at": time.time()})
    return img_key, sub_key


def mixin_key_of(img_key, sub_key):
    orig = img_key + sub_key
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def wbi_sign(params, mkey):
    params = dict(params)
    params["wts"] = int(time.time())
    items = sorted((k, re.sub(r"[!'()*]", "", str(v))) for k, v in params.items())
    qs = urllib.parse.urlencode(items)
    params["w_rid"] = hashlib.md5((qs + mkey).encode()).hexdigest()
    return params


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def fail(msg, **extra):
    out = {"ok": False, "error": msg}
    out.update(extra)
    emit(out)
    sys.exit(1)

# ---------------- 重点领域与进度 ----------------

FOCUS_CATEGORIES = ["电子", "嵌入式", "机械"]
FOCUS_KEYWORDS = [
    "电路", "电子", "模电", "数电", "模拟电路", "数字电路", "单片机", "嵌入式",
    "STM32", "ESP32", "ESP8266", "Arduino", "FPGA", "PCB", "硬件", "芯片",
    "半导体", "传感器", "示波器", "万用表", "焊接", "电源", "运放", "滤波",
    "信号", "频谱", "射频", "物联网", "IoT", "RTOS",
    "机械", "结构", "力学", "机械设计", "CAD", "SolidWorks", "制图",
    "齿轮", "轴承", "3D打印", "数控", "CNC", "PLC", "控制", "自动化",
    "电机", "机器人", "ROS", "仿真", "MATLAB", "Simulink", "工科", "工程",
]


def get_focus(data_dir):
    s = load_settings(data_dir)
    f = s.get("focus") or {}
    return {
        "categories": f.get("categories") or list(FOCUS_CATEGORIES),
        "keywords": f.get("keywords") or list(FOCUS_KEYWORDS),
    }


def focus_hit(text, keywords):
    t = (text or "").lower()
    return any(k.lower() in t for k in keywords)


def load_progress(data_dir):
    return load_json(cache_path(data_dir, "progress.json"), {}) or {}


def save_progress(data_dir, prog):
    save_json(cache_path(data_dir, "progress.json"), prog)


def library_done_bvids(data_dir):
    r"""扫描 library\分类\ 下的知识卡片, 提取已建档 BV (带 mtime 缓存)。

    只扫 分类\ 目录: 博主视频列表/思维导图/索引里的 BV 链接不算“已建档”,
    避免把尚未建档的收藏误判为已完成。
    """
    lib = os.path.join(data_dir, "library", "分类")
    cache_file = cache_path(data_dir, "library_scan_cache.json")
    j = load_json(cache_file) or {}
    files = {}
    if os.path.isdir(lib):
        for root, _d, names in os.walk(lib):
            for fn in names:
                if fn.lower().endswith(".md"):
                    p = os.path.join(root, fn)
                    files[os.path.relpath(p, lib)] = int(os.path.getmtime(p))
    if j.get("files") == files:
        return set(j.get("bvids") or [])
    done = set()
    for rel in files:
        try:
            done.update(re.findall(r"BV[0-9A-Za-z]{10}", read_text(os.path.join(lib, rel))))
        except Exception:
            pass
    save_json(cache_file, {"files": files, "bvids": sorted(done)})
    return done


def card_files(data_dir):
    """分类目录下所有知识卡片 md (排除 列表.md 汇总行, 它们不是卡片)。"""
    base = os.path.join(data_dir, "library", "分类")
    out = []
    if os.path.isdir(base):
        for root, _d, names in os.walk(base):
            for fn in sorted(names):
                if fn.lower().endswith(".md") and fn.lower() != "列表.md":
                    out.append(os.path.join(root, fn))
    return out


def parse_card_meta(path):
    """读取卡片基础字段: 标题/BV/学习状态/下次复习。"""
    try:
        text = read_text(path)
    except Exception:
        return None
    meta = {"file": path}
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m:
        meta["title"] = m.group(1).strip()
    m = re.search(r"^-\s*BV号:\s*(\S+)", text, re.M)
    if m:
        meta["bvid"] = m.group(1).strip()
    m = re.search(r"^-\s*学习状态:\s*(\S+)", text, re.M)
    if m:
        meta["status"] = m.group(1).strip()
    m = re.search(r"^-\s*下次复习:\s*(\S+)", text, re.M)
    if m:
        meta["next_review"] = m.group(1).strip()
    return meta


def parse_card_full(path):
    """读取卡片完整结构化字段(兼容新旧两种格式, 全角/半角冒号)。

    新格式(SKILL 模板): - 领域: X | 子领域: Y  /  - 知识点标签: #a #b
    旧格式(早期归档):   - UP主：xxx | 时长：xx | 分类：yyy
    """
    try:
        text = read_text(path)
    except Exception:
        return {}
    meta = {"file": path}
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m:
        meta["title"] = m.group(1).strip()
    m = re.search(r"^-\s*BV号\s*[:：]\s*(\S+)", text, re.M)
    if m:
        meta["bvid"] = m.group(1).strip()
    m = re.search(r"^-\s*UP主\s*[:：]([^\n]+)", text, re.M)
    if m:
        meta["upper"] = m.group(1).split("|")[0].strip()
        dm = re.search(r"时长\s*[:：]\s*([0-9:]+)", m.group(1))
        if dm:
            meta["duration"] = dm.group(1)
    # 领域: 优先独立「领域」行, 其次独立「分类」行, 最后 UP主 行里的分类
    m = re.search(r"^-\s*领域\s*[:：]\s*([^|]+?)\s*\|\s*子领域\s*[:：]\s*(.+)$", text, re.M)
    if m:
        meta["domain"] = m.group(1).strip()
        meta["subdomain"] = m.group(2).strip()
    if "domain" not in meta:
        m = re.search(r"^-\s*分类\s*[:：]\s*([^|]+)", text, re.M)
        if m:
            meta["domain"] = m.group(1).strip()
    if "domain" not in meta:
        m = re.search(r"^-\s*UP主\s*[:：]([^\n]+)", text, re.M)
        if m:
            dm = re.search(r"分类\s*[:：]\s*([^|\n]+)", m.group(1))
            if dm:
                meta["domain"] = dm.group(1).strip()
    m = re.search(r"^-\s*知识点标签\s*[:：]\s*(.+)$", text, re.M)
    if m:
        meta["tags"] = re.findall(r"#([^\s#]+)", m.group(1))
    # 内容总结/概要(优先取第一段), 供交互式 HTML 的视频介绍展示
    m = re.search(r"^##\s*内容(?:总结|概要)\s*$", text, re.M)
    if m:
        body = text[m.end():]
        nxt = re.search(r"^##\s+", body, re.M)
        if nxt:
            body = body[:nxt.start()]
        lines = [re.sub(r"^\s*>\s*", "", ln).strip()
                 for ln in body.splitlines() if ln.strip()]
        if lines:
            summary = re.sub(r"\s+", " ", " ".join(lines)).strip()
            # 去掉"根据标题(与/和)简介推断，仅供参考(…)。"之类的前缀套话
            summary = re.sub(
                r"^(根据标题(?:与|和)?(?:简介)?推断[，,]?\s*仅供参考[。.!！]?\s*"
                r"(?:（[^）]*）\s*[。.!！]?\s*)?)+", "", summary).strip()
            if summary:
                meta["summary"] = summary[:240]
    return meta


def split_subdomains(s):
    s = re.sub(r"^\s*(如|例如)\s*", "", s or "")
    parts = re.split(r"[/、，,;；]", s)
    out = []
    for p in parts:
        p = p.strip().strip("（）()[]【】")
        if p and p not in out:
            out.append(p)
    return out


def mermaid_safe(t, maxlen=26):
    """清理节点文字: 只保留中文/字母/数字/空格, 去掉 Mermaid 形状符号。"""
    t = re.sub(r"[()\[\]{}<>`]", " ", t or "")
    t = re.sub(r"\s+", " ", t).strip()
    return (t[:maxlen].strip() or "?")


def set_card_field(path, field, value):
    """在知识卡片中写入/更新一行 `- <field>: <value>` (保留其余内容)。"""
    if not value:
        return False
    text = read_text(path)
    pattern = re.compile(rf"^-\s*{re.escape(field)}:\s*.*$", re.M)
    new = f"- {field}: {value}"
    if pattern.search(text):
        text2 = pattern.sub(new, text, count=1)
    else:
        anchor = re.compile(r"^##\s+", re.M)
        m = anchor.search(text)
        if m:
            text2 = text[:m.start()] + new + "\n" + text[m.start():]
        else:
            text2 = text.rstrip("\n") + "\n" + new + "\n"
    if text2 != text:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text2)
        return True
    return False

# ---------------- 子命令 ----------------

def cmd_setup(args, data_dir):
    cfg = load_config(data_dir)
    cfg["sessdata"] = args.sessdata.strip()
    if args.bili_jct:
        cfg["bili_jct"] = args.bili_jct.strip()
    cookie = cookie_of(cfg)
    d = http_get(f"{API}/x/web-interface/nav", cookie=cookie)
    if d.get("code") == -101:
        fail("SESSDATA 无效或已过期（-101 未登录），请重新从浏览器复制")
    if d.get("code") != 0:
        fail(f"nav 接口异常: code={d.get('code')} msg={d.get('message')}")
    info = d["data"]
    cfg.update({
        "uid": info.get("mid"),
        "uname": info.get("uname"),
        "setup_at": now_iso(),
    })
    save_config(data_dir, cfg)
    emit({"ok": True, "uid": cfg["uid"], "uname": cfg["uname"],
          "message": "登录态已保存", "config": os.path.join(data_dir, "config.json")})


def cmd_status(args, data_dir):
    cfg = load_config(data_dir)
    out = {"ok": True, "data_dir": data_dir,
           "has_sessdata": bool(cfg.get("sessdata")),
           "has_bili_jct": bool(cfg.get("bili_jct")),
           "config": os.path.join(data_dir, "config.json"),
           "settings": os.path.join(data_dir, "settings.json")}
    if cfg.get("sessdata"):
        d = http_get(f"{API}/x/web-interface/nav", cookie=cookie_of(cfg))
        out["login"] = {
            "is_login": bool((d.get("data") or {}).get("isLogin")),
            "code": d.get("code"),
            "uname": cfg.get("uname"), "uid": cfg.get("uid"),
        }
    caches = {}
    for name in ("fav_folders.json", "fav_videos.json", "followings.json"):
        p = cache_path(data_dir, name)
        j = load_json(p)
        if j:
            caches[name] = {"synced_at": j.get("synced_at"), "items": j.get("count")}
    up_dir = cache_path(data_dir, "uploader_videos")
    up_count = len([x for x in os.listdir(up_dir) if x.endswith(".json")]) if os.path.isdir(up_dir) else 0
    caches["uploader_videos"] = {"uploader_count": up_count}
    out["cache"] = caches
    # 知识库统计
    lib = os.path.join(data_dir, "library")
    md_count = 0
    bvids = set()
    if os.path.isdir(lib):
        for root, _dirs, files in os.walk(lib):
            for fn in files:
                if fn.lower().endswith(".md"):
                    md_count += 1
                    try:
                        with open(os.path.join(root, fn), encoding="utf-8") as fh:
                            bvids.update(re.findall(r"BV[0-9A-Za-z]{10}", fh.read()))
                    except Exception:
                        pass
    out["library"] = {"md_files": md_count, "referenced_bvids": len(bvids)}
    emit(out)


def fetch_folder_videos(folder, cookie, delay):
    """拉取一个收藏夹的全部视频。返回 (videos, error)。"""
    media_id = folder["id"]
    total = folder.get("media_count", 0)
    videos = []
    error = None
    pn = 1
    while True:
        d = http_get(f"{API}/x/v3/fav/resource/list",
                     {"media_id": media_id, "pn": pn, "ps": 20, "platform": "web"},
                     cookie=cookie)
        if d.get("code") != 0:
            error = f"第{pn}页 code={d.get('code')} msg={d.get('message')}"
            sys.stderr.write(f"[warn] 收藏夹<{folder.get('title')}> {error}\n")
            break
        data = d.get("data") or {}
        medias = data.get("medias") or []
        for m in medias:
            videos.append({
                "bvid": m.get("id") if m.get("type") == 2 else None,
                "type": m.get("type"),
                "title": m.get("title"),
                "intro": m.get("intro") or "",
                "upper": (m.get("upper") or {}).get("name"),
                "upper_mid": (m.get("upper") or {}).get("mid"),
                "duration": m.get("duration"),
                "fav_time": m.get("fav_time"),
                "pubtime": m.get("pubtime"),
                "attr": m.get("attr"),
                "cover": m.get("cover"),
                "play": ((m.get("cnt_info") or {}).get("play")),
                "collect": ((m.get("cnt_info") or {}).get("collect")),
            })
        if not data.get("has_more") or not medias or pn * 20 >= total + 40:
            break
        pn += 1
        rate_sleep(delay)
    return videos, error


def cmd_sync_favorites(args, data_dir):
    cfg = load_config(data_dir)
    cookie = cookie_of(cfg)
    if not cookie:
        fail("尚未配置登录态，请先运行: setup --sessdata <值>")
    nav = http_get(f"{API}/x/web-interface/nav", cookie=cookie)
    if not (nav.get("data") or {}).get("isLogin"):
        fail(f"未登录或 SESSDATA 过期 (nav code={nav.get('code')})，请重新 setup")
    uid = nav["data"]["mid"]
    d = http_get(f"{API}/x/v3/fav/folder/created/list-all", {"up_mid": uid}, cookie=cookie)
    if d.get("code") != 0:
        fail(f"获取收藏夹列表失败: code={d.get('code')} msg={d.get('message')}")
    folders = []
    for f_ in ((d.get("data") or {}).get("list") or []):
        folders.append({
            "id": f_["id"], "fid": f_.get("fid"), "title": f_.get("title"),
            "media_count": f_.get("media_count", 0), "attr": f_.get("attr"),
            "mtime": f_.get("mtime"),
        })
    if args.folder:
        folders = [x for x in folders if x["id"] == args.folder]
        if not folders:
            fail(f"未找到 media_id={args.folder} 的收藏夹")
    all_videos = {}
    summary = []
    fetch_errors = []
    for folder in folders:
        vids, err = fetch_folder_videos(folder, cookie, args.delay)
        if err:
            fetch_errors.append({"folder": folder["title"], "media_id": folder["id"],
                                 "error": err, "fetched": len(vids)})
        ok = 0
        for v in vids:
            if v["type"] != 2 or not v["bvid"]:
                continue
            if v["bvid"] in all_videos:
                all_videos[v["bvid"]]["folder_ids"].append(folder["id"])
                continue
            v["folder_ids"] = [folder["id"]]
            all_videos[v["bvid"]] = v
            ok += 1
        summary.append({"folder": folder["title"], "media_id": folder["id"],
                        "declared": folder["media_count"], "fetched": len(vids), "videos": ok})
        sys.stderr.write(f"[info] 收藏夹<{folder['title']}> {len(vids)} 条\n")
        rate_sleep(args.delay)
    prev = load_json(cache_path(data_dir, "fav_videos.json"), {}) or {}
    merged = dict((prev.get("videos") or {}))
    # 新数据整体替换旧条目: 重新收藏的视频会自然清除 removed 标记
    for bv, v in all_videos.items():
        merged[bv] = v
    removed_this_run = 0
    if fetch_errors:
        sys.stderr.write(f"[warn] {len(fetch_errors)} 个收藏夹拉取失败, "
                         "本次跳过“已取消收藏”清理以免误删\n")
    elif args.no_prune or args.folder:
        # 部分同步(--folder)时不知道其余收藏夹现状, 跳过清理以免误标 removed
        pass
    else:
        now_ids = set(all_videos)
        for bv, v in merged.items():
            if bv not in now_ids and not v.get("removed"):
                v["removed"] = True
                v["removed_at"] = now_iso()
                removed_this_run += 1
    save_json(cache_path(data_dir, "fav_folders.json"),
              {"synced_at": now_iso(), "uid": uid, "count": len(folders), "folders": folders,
               "summary": summary})
    save_json(cache_path(data_dir, "fav_videos.json"),
              {"synced_at": now_iso(), "uid": uid, "count": len(merged), "videos": merged})
    emit({"ok": True, "folders": summary, "total_videos": len(merged),
          "new_this_run": len(all_videos), "removed_this_run": removed_this_run,
          "fetch_errors": fetch_errors,
          "cache_file": cache_path(data_dir, "fav_videos.json")})


def cmd_sync_followings(args, data_dir):
    cfg = load_config(data_dir)
    cookie = cookie_of(cfg)
    if not cookie:
        fail("尚未配置登录态，请先运行: setup --sessdata <值>")
    img_key, sub_key = get_wbi_keys(data_dir, cookie)
    mkey = mixin_key_of(img_key, sub_key)
    users = []
    page = 1
    total = None
    errors = []
    while True:
        params = wbi_sign({"page": page, "ps": 50}, mkey)
        d = http_get(f"{API}/x/relation/sublist", params, cookie=cookie)
        if d.get("code") != 0:
            errors.append({"page": page, "code": d.get("code"), "message": d.get("message")})
            if not args.continue_on_error:
                fail(f"关注列表接口失败: code={d.get('code')} msg={d.get('message')}",
                     hint="若持续失败可能是接口风控, 稍后重试或更新脚本",
                     partial=len(users))
            break
        data = d.get("data") or {}
        batch = data.get("list") or []
        total = data.get("total", 0)
        for u in batch:
            users.append({
                "mid": u.get("mid"), "uname": u.get("uname"),
                "sign": u.get("sign") or "",
                "official": ((u.get("official_verify") or {}).get("desc")) or "",
            })
        if not batch or len(users) >= total or page >= 10:
            break
        page += 1
        rate_sleep(args.delay)
    # 去重
    seen, uniq = set(), []
    for u in users:
        if u["mid"] in seen:
            continue
        seen.add(u["mid"])
        uniq.append(u)
    save_json(cache_path(data_dir, "followings.json"),
              {"synced_at": now_iso(), "count": len(uniq), "total_reported": total,
               "users": uniq})
    emit({"ok": len(errors) == 0, "count": len(uniq), "total_reported": total,
          "partial": bool(errors), "errors": errors,
          "cache_file": cache_path(data_dir, "followings.json"),
          "note": "B站网页端关注列表上限通常为 250, 超出部分无法通过此接口获取"})


def fetch_uploader_videos(mid, uname, cookie, mkey, max_videos, delay):
    videos = []
    pn = 1
    total = None
    referer = f"https://space.bilibili.com/{mid}/video"
    while len(videos) < max_videos:
        params = wbi_sign({"mid": mid, "pn": pn, "ps": 30, "order": "pubdate",
                           "order_avoided": "true"}, mkey)
        d = http_get(f"{API}/x/space/wbi/arc/search", params, cookie=cookie, referer=referer)
        if d.get("code") != 0:
            return {"error": f"code={d.get('code')} msg={d.get('message')}",
                    "videos": videos, "total": total}
        data = d.get("data") or {}
        lst = (data.get("list") or {}).get("vlist") or []
        total = (data.get("page") or {}).get("count", total)
        for v in lst:
            videos.append({
                "bvid": v.get("bvid"), "title": v.get("title"),
                "desc": v.get("description") or "",
                "created": v.get("created"), "length": v.get("length"),
                "play": v.get("play"), "comment": v.get("comment"),
            })
        if not lst or (total is not None and pn * 30 >= total):
            break
        pn += 1
        rate_sleep(delay)
    return {"uname": uname, "total": total, "videos": videos[:max_videos]}


def cmd_sync_uploader_videos(args, data_dir):
    cfg = load_config(data_dir)
    cookie = cookie_of(cfg)
    img_key, sub_key = get_wbi_keys(data_dir, cookie)
    mkey = mixin_key_of(img_key, sub_key)
    targets = []
    if args.mid:
        targets = [{"mid": args.mid, "uname": args.uname or str(args.mid)}]
    else:
        fj = load_json(cache_path(data_dir, "followings.json"))
        if not fj:
            fail("请先运行 sync-followings 获取关注列表, 或用 --mid 指定单个 UP 主")
        targets = fj.get("users") or []
        if args.limit:
            targets = targets[:args.limit]
    results = []
    for i, u in enumerate(targets):
        r = fetch_uploader_videos(u["mid"], u.get("uname"), cookie, mkey,
                                  args.max_videos, args.delay)
        out = {"mid": u["mid"], "uname": u.get("uname"), "synced_at": now_iso(),
               "sign": u.get("sign", ""), **r}
        save_json(cache_path(data_dir, "uploader_videos", f"{u['mid']}.json"), out)
        results.append({"mid": u["mid"], "uname": u.get("uname"),
                        "videos": len(out.get("videos") or []),
                        "error": out.get("error")})
        sys.stderr.write(f"[info] ({i+1}/{len(targets)}) {u.get('uname')} "
                         f"{len(out.get('videos') or [])} 条投稿"
                         + (f" [错误: {out['error']}]" if out.get("error") else "") + "\n")
        rate_sleep(args.delay)
    errors = [r for r in results if r.get("error")]
    emit({"ok": len(errors) == 0, "uploaders": len(results), "results": results,
          "errors": errors})


def cmd_subtitles(args, data_dir):
    cfg = load_config(data_dir)
    cookie = cookie_of(cfg)
    img_key, sub_key = get_wbi_keys(data_dir, cookie)
    mkey = mixin_key_of(img_key, sub_key)
    view = http_get(f"{API}/x/web-interface/view", {"bvid": args.bvid}, cookie=cookie)
    if view.get("code") != 0:
        fail(f"视频信息获取失败: code={view.get('code')} msg={view.get('message')}")
    vdata = view["data"]
    cid = args.cid or (vdata.get("pages") or [{}])[0].get("cid")
    params = wbi_sign({"bvid": args.bvid, "cid": cid}, mkey)
    d = http_get(f"{API}/x/player/wbi/v2", params, cookie=cookie)
    if d.get("code") != 0:
        fail(f"字幕接口失败: code={d.get('code')} msg={d.get('message')}")
    subs = ((d.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    if not subs:
        emit({"ok": False, "bvid": args.bvid, "title": vdata.get("title"),
              "error": "该视频没有可用字幕（AI字幕需登录且并非所有视频都有）"})
        return
    pick = None
    for s in subs:
        if s.get("lan", "").startswith(args.lang):
            pick = s
            break
    pick = pick or subs[0]
    url = pick["subtitle_url"]
    if url.startswith("//"):
        url = "https:" + url
    sd = http_get(url, cookie=cookie)
    lines = [x.get("content", "") for x in (sd.get("body") or [])]
    text = "\n".join(lines)
    save_path = cache_path(data_dir, "subtitles", f"{args.bvid}.txt")
    with open(save_path, "w", encoding="utf-8") as fh:
        fh.write(f"# {vdata.get('title')}\n# UP: {vdata.get('owner', {}).get('name')}"
                 f"\n# 字幕语言: {pick.get('lan_doc', pick.get('lan'))}\n\n")
        fh.write(text)
    emit({"ok": True, "bvid": args.bvid, "title": vdata.get("title"),
          "lan": pick.get("lan"), "chars": len(text), "saved_to": save_path,
          "available": [{"lan": s.get("lan"), "doc": s.get("lan_doc")} for s in subs],
          "preview": text[:args.preview_chars]})


def cmd_pending(args, data_dir):
    """分批输出未完成的收藏视频或未建档的 UP 主；自动跳过已完成/已取消收藏项。"""
    focus = get_focus(data_dir)
    prog = load_progress(data_dir)
    fv = load_json(cache_path(data_dir, "fav_videos.json"))
    fj = load_json(cache_path(data_dir, "fav_folders.json"))
    fo = load_json(cache_path(data_dir, "followings.json"))
    if args.kind == "uploaders":
        if not fo:
            fail("请先运行 sync-followings")
        up_dir = cache_path(data_dir, "uploader_videos")
        have = set()
        if os.path.isdir(up_dir):
            have = {int(x[:-5]) for x in os.listdir(up_dir) if x.endswith(".json")}
        done = {int(k) for k in (prog.get("uploaders_done") or {}).keys()}
        users = [u for u in fo.get("users", []) if u["mid"] not in done]
        total = len(users)
        users = users[args.offset:args.offset + args.limit]
        emit({"ok": True, "kind": "uploaders", "pending_total": total,
              "shown": len(users),
              "resume_after": prog.get("last_uploader"),
              "users": [{"mid": u["mid"], "uname": u["uname"],
                         "sign": (u.get("sign") or "")[:120],
                         "has_video_cache": u["mid"] in have} for u in users]})
        return
    if not fv:
        fail("请先运行 sync-favorites")
    folder_names = {x["id"]: x["title"] for x in (fj or {}).get("folders", [])}
    vids = fv.get("videos") or {}
    skip = set((prog.get("videos_done") or {}).keys()) | library_done_bvids(data_dir)
    pend = []
    removed_total = 0
    for bv, v in sorted(vids.items(), key=lambda kv: kv[1].get("fav_time") or 0):
        if v.get("removed"):
            removed_total += 1
            continue
        if bv in skip or not v.get("bvid"):
            continue
        pend.append(v)
    total = len(pend)
    pend = pend[args.offset:args.offset + args.limit]
    out = []
    for v in pend:
        ft = v.get("fav_time")
        text = " ".join([v.get("title") or "", v.get("intro") or "", v.get("upper") or ""])
        out.append({
            "bvid": v["bvid"],
            "title": v.get("title"),
            "intro": (v.get("intro") or "")[:150],
            "upper": v.get("upper"),
            "attr": v.get("attr"),
            "folders": [folder_names.get(x, str(x)) for x in v.get("folder_ids", [])],
            "fav_time": datetime.fromtimestamp(ft).strftime("%Y-%m-%d") if ft else None,
            "duration_sec": v.get("duration"),
            "focus_hint": focus_hit(text, focus["keywords"]),
        })
    emit({"ok": True, "kind": "videos", "pending_total": total, "shown": len(out),
          "resume_after": prog.get("last_video"),
          "focus_categories": focus["categories"],
          "removed_total": removed_total,
          "videos": out})


def cmd_mark_done(args, data_dir):
    """每建档一个视频/UP主后立即调用, 写入断点进度。"""
    prog = load_progress(data_dir)
    if args.kind == "video":
        if not args.bvid:
            fail("video 需要 --bvid")
        prog.setdefault("videos_done", {})[args.bvid] = {
            "title": args.title or "", "at": now_iso()}
        prog["last_video"] = {"bvid": args.bvid, "title": args.title or "",
                              "at": now_iso()}
    else:
        if not args.mid:
            fail("uploader 需要 --mid")
        prog.setdefault("uploaders_done", {})[str(args.mid)] = {
            "uname": args.uname or "", "at": now_iso()}
        prog["last_uploader"] = {"mid": args.mid, "uname": args.uname or "",
                                 "at": now_iso()}
    save_progress(data_dir, prog)
    emit({"ok": True, "kind": args.kind,
          "videos_done": len(prog.get("videos_done", {})),
          "uploaders_done": len(prog.get("uploaders_done", {}))})


def cmd_focus(args, data_dir):
    s = load_settings(data_dir)
    if args.reset:
        s["focus"] = {"categories": list(FOCUS_CATEGORIES),
                      "keywords": list(FOCUS_KEYWORDS)}
        save_settings(data_dir, s)
    out = {"ok": True}
    out.update(get_focus(data_dir))
    emit(out)


def cmd_report(args, data_dir):
    cfg = load_config(data_dir)
    out = {"ok": True, "data_dir": data_dir,
           "logged_in": bool(cfg.get("sessdata")), "uname": cfg.get("uname")}
    prog = load_progress(data_dir)
    out["focus"] = get_focus(data_dir)
    out["progress"] = {
        "videos_done": len(prog.get("videos_done") or {}),
        "uploaders_done": len(prog.get("uploaders_done") or {}),
        "last_video": prog.get("last_video"),
        "last_uploader": prog.get("last_uploader"),
    }
    fj = load_json(cache_path(data_dir, "fav_folders.json"))
    fv = load_json(cache_path(data_dir, "fav_videos.json"))
    fo = load_json(cache_path(data_dir, "followings.json"))
    if fj:
        out["fav_folders"] = {"synced_at": fj.get("synced_at"), "count": fj.get("count"),
                              "folders": [{"title": x["title"], "media_id": x["id"],
                                           "count": x.get("media_count", 0)} for x in fj.get("folders", [])]}
    if fv:
        vids = fv.get("videos") or {}
        bvids = set(vids.keys())
        done = library_done_bvids(data_dir) | set((prog.get("videos_done") or {}).keys())
        removed = {k for k, v in vids.items() if v.get("removed")}
        pending = sorted(bvids - done - removed)
        out["fav_videos"] = {"synced_at": fv.get("synced_at"), "total": len(bvids),
                             "documented": len(bvids & done),
                             "removed": len(removed),
                             "pending": len(pending), "pending_sample": pending[:50]}
    if fo:
        users = fo.get("users") or []
        up_dir = cache_path(data_dir, "uploader_videos")
        have = set()
        if os.path.isdir(up_dir):
            have = {int(x[:-5]) for x in os.listdir(up_dir) if x.endswith(".json")}
        up_done = {int(k) for k in (prog.get("uploaders_done") or {}).keys()}
        out["followings"] = {"synced_at": fo.get("synced_at"), "total": len(users),
                             "uploader_videos_synced": len(have),
                             "uploader_pending": [u["mid"] for u in users
                                                  if u["mid"] not in have
                                                  and u["mid"] not in up_done][:50]}
    # 复习到期数
    today = datetime.now().date()
    due_cards = 0
    total_cards = 0
    for p in card_files(data_dir):
        total_cards += 1
        m = parse_card_meta(p)
        if not m:
            continue
        st = m.get("status")
        if st == "未学":
            due_cards += 1
        elif st and m.get("next_review"):
            try:
                if datetime.strptime(m["next_review"], "%Y-%m-%d").date() <= today:
                    due_cards += 1
            except Exception:
                pass
    out["review"] = {"cards_total": total_cards, "due": due_cards,
                     "today": today.isoformat()}
    emit(out)


# ---------------- 学习功能: 复习 / Anki / 索引 / 搜索 / 统计 ----------------

def parse_review_date(v):
    """把 --next 值解析为 YYYY-MM-DD: 支持字面日期与 +N d/w/m/y 相对格式。"""
    v = (v or "").strip()
    if not v:
        return None
    m = re.fullmatch(r"\+(\d+)([dwmy])", v.lower())
    if m:
        days = int(m.group(1)) * {"d": 1, "w": 7, "m": 30, "y": 365}[m.group(2)]
        return (datetime.now().date() + timedelta(days=days)).isoformat()
    try:
        datetime.strptime(v, "%Y-%m-%d")
        return v
    except Exception:
        return None


def cmd_review(args, data_dir):
    """查看到期(未学或到期)的知识卡片; --set 更新某张卡片的学习状态。"""
    cards = []
    for p in card_files(data_dir):
        m = parse_card_meta(p)
        if m:
            cards.append(m)
    today = datetime.now().date()
    if args.set:
        if not args.bvid:
            fail("--set 需要 --bvid")
        if not args.status and not args.next:
            fail("--set 需要 --status 或 --next 至少其一")
        target = next((m for m in cards if m.get("bvid") == args.bvid), None)
        if not target:
            fail(f"未找到 BV{args.bvid} 的知识卡片(先建档或检查 BV 号)")
        next_date = parse_review_date(args.next) if args.next else None
        if args.next and not next_date:
            fail("--next 需为 YYYY-MM-DD 或相对格式(如 +7d/+2w/+1m)")
        changed = set_card_field(target["file"], "学习状态", args.status) if args.status else False
        changed = set_card_field(target["file"], "下次复习", next_date) or changed
        emit({"ok": True, "bvid": args.bvid, "updated": changed,
              "next": next_date, "file": target["file"]})
        return
    enriched = []
    for m in cards:
        due = False
        st = m.get("status")
        if st == "未学":
            due = True
        elif st and m.get("next_review"):
            try:
                due = datetime.strptime(m["next_review"], "%Y-%m-%d").date() <= today
            except Exception:
                due = False
        m["due"] = due
        enriched.append(m)
    shown = enriched if args.all else [m for m in enriched if m["due"]]
    emit({"ok": True, "today": today.isoformat(),
          "cards_total": len(enriched),
          "due_count": sum(1 for m in enriched if m["due"]),
          "cards": [{"bvid": m.get("bvid"), "title": m.get("title"),
                     "status": m.get("status") or "未标记",
                     "next_review": m.get("next_review"), "due": m["due"],
                     "file": m["file"]} for m in shown]})


def cmd_export_anki(args, data_dir):
    """把知识卡片的“核心知识点”导出为 Anki 可导入的 CSV (UTF-8 BOM)。"""
    cards = []
    for p in card_files(data_dir):
        try:
            text = read_text(p)
        except Exception:
            continue
        title = None
        bvid = None
        tags = []
        kps = []
        m = re.search(r"^#\s+(.+)$", text, re.M)
        if m:
            title = m.group(1).strip()
        m = re.search(r"^-\s*BV号:\s*(\S+)", text, re.M)
        if m:
            bvid = m.group(1).strip()
        m = re.search(r"^-\s*知识点标签:\s*(.+)$", text, re.M)
        if m:
            tags = re.findall(r"#([^\s#]+)", m.group(1))
        m = re.search(r"^-\s*领域:\s*([^|]+)", text, re.M)
        domain = m.group(1).strip() if m else ""
        sec = re.search(r"^##\s*核心知识点\s*$", text, re.M)
        if sec:
            body = text[sec.end():]
            nxt = re.search(r"^##\s+", body, re.M)
            if nxt:
                body = body[:nxt.start()]
            for line in body.splitlines():
                mm = re.match(r"^\s*\d+\.\s*\*\*(.+?)\*\*\s*[:：]?\s*(.*)$", line.strip())
                if mm:
                    kps.append((mm.group(1).strip(), mm.group(2).strip()))
        if title and kps:
            cards.append({"title": title, "bvid": bvid, "tags": tags, "kps": kps,
                          "file": p, "domain": domain})
    if args.domain:
        # 优先匹配卡片「领域」字段, 字段缺失时退回路径匹配
        cards = [c for c in cards
                 if args.domain in (c.get("domain") or c["file"].replace("\\", "/"))]
    rows = [["Front", "Back", "Tags"]]
    for c in cards:
        link = f"https://www.bilibili.com/video/{c['bvid']}" if c.get("bvid") else ""
        for kp_title, kp_body in c["kps"]:
            front = f"{kp_title}（《{c['title']}》）"
            back = (f"{kp_body}<br>来源: <a href=\"{link}\">{link}</a><br>"
                    f"卡片: {c['file']}") if kp_body else (
                f"来源: <a href=\"{link}\">{link}</a><br>卡片: {c['file']}")
            rows.append([front, back, " ".join(c["tags"])])
    out = args.out or os.path.join(data_dir, "library", "anki_export.csv")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerows(rows)
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(buf.getvalue())
    emit({"ok": True, "out": out, "cards": len(cards), "flashcards": len(rows) - 1})


PLOT_COLORS = [
    "#E15759", "#4E79A7", "#59A14F", "#F28E2B", "#B07AA1",
    "#76B7B2", "#EDC948", "#FF9DA7", "#9C755F", "#BAB0AC",
]

# 工科教学话题分类规则: (话题名, 关键词列表)。按顺序匹配, 先命中先用。
TOPIC_RULES = [
    ("嵌入式/单片机", ["stm32", "esp32", "esp8266", "arduino", "单片机", "嵌入式",
                     "rtos", "freertos", "rt-thread", "寄存器", "gpio", "串口",
                     "uart", "i2c", "spi", "adc", "dac", "pwm", "定时器", "中断",
                     "cortex", "arm", "裸机", "hal库", "micropython", "点灯"]),
    ("电子电路", ["模电", "数电", "电路", "pcb", "原理图", "焊接", "示波器", "万用表",
                "运放", "电源", "滤波", "信号", "射频", "芯片", "半导体", "传感器",
                "三极管", "mos管", "二极管", "led", "开关电源", "嘉立创", "立创eda",
                "buck", "boost"]),
    ("机器学习/AI", ["机器学习", "深度学习", "神经网络", "pytorch", "tensorflow",
                   "大模型", "gpt", "llm", "transformer", "cnn", "rnn",
                   "强化学习", "数据挖掘", "人工智能", "aigc", "chatgpt",
                   "ai绘画", "ai工具", "ai模型", "智能"]),
    ("计算机基础", ["数据结构", "算法", "操作系统", "计算机网络", "数据库", "编译",
                  "内存", "线程", "进程", "linux", "git", "docker", "kubernetes",
                  "k8s", "nginx", "redis", "mysql", "分布式", "网络编程"]),
    ("编程语言", ["python", "c语言", "c++", "java", "javascript", "typescript",
                "go语言", "golang", "rust", "kotlin", "swift", "shell", "sql",
                "正则", "html", "css", "编程", "代码", "函数", "面向对象"]),
    ("硬件开发", ["硬件", "开发板", "树莓派", "raspberry", "香橙派", "jetson",
                "外设", "驱动", "fpga", "verilog", "vhdl"]),
    ("机械/制造", ["solidworks", "cad", "机械", "3d打印", "cnc", "数控", "齿轮",
                 "轴承", "电机", "结构设计", "力学", "plc", "自动化", "matlab",
                 "simulink"]),
    ("机器人/ROS", ["ros", "机器人", "机械臂", "四足", "无人机", "飞控", "moveit"]),
]

TEACHING_HINTS = ["教程", "入门", "实战", "详解", "原理", "基础", "从零", "速通",
                  "课程", "学会", "教你", "保姆级", "逐行", "教学", "讲解", "指南",
                  "手册", "笔记", "系列"]


def classify_engineering(text):
    """按关键词把标题/简介归类为工科话题; 不命中返回 None。"""
    t = (text or "").lower()
    for topic, keys in TOPIC_RULES:
        if any(k in t for k in keys):
            return topic
    return None


def is_teaching(title):
    """弱信号判断是否教学类(教程/入门/实战等字眼), 供分析参考。"""
    t = title or ""
    return any(h in t for h in TEACHING_HINTS)


def build_knowledge_tree(data_dir):
    """扫描知识卡片, 返回 (structured, tree, dom_subs, flat_by_domain)。

    兼容新旧格式: 带「知识点标签」的卡片进入结构化树(领域→子领域→知识点→视频);
    只有「领域/分类」没有标签的旧卡片进入 flat_by_domain(领域→视频 扁平渲染)。
    """
    cards_all = []
    for p in card_files(data_dir):
        meta = parse_card_full(p)
        if meta.get("domain"):
            cards_all.append(meta)
    structured = [c for c in cards_all if c.get("tags")]
    flat_by_domain = {}
    for c in cards_all:
        if not c.get("tags"):
            flat_by_domain.setdefault(c["domain"], []).append(c)
    tree = {}
    for c in structured:
        subs = split_subdomains(c.get("subdomain") or "") or ["综合"]
        for sub in subs:
            for kp in c["tags"]:
                tree.setdefault((c["domain"], sub, kp), []).append(c)
    dom_subs = {}
    for (dom, sub, kp), cs in tree.items():
        dom_subs.setdefault(dom, {}).setdefault(sub, {})[kp] = cs
    return structured, tree, dom_subs, flat_by_domain


def _mix(hex_color, white_ratio):
    """把十六进制颜色往白色方向混合, 得到更浅的变体。"""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    r = int(r + (255 - r) * white_ratio)
    g = int(g + (255 - g) * white_ratio)
    b = int(b + (255 - b) * white_ratio)
    return f"#{r:02x}{g:02x}{b:02x}"


def _wrap(text, max_chars):
    text = (text or "").strip()
    lines = []
    while len(text) > max_chars:
        lines.append(text[:max_chars])
        text = text[max_chars:]
    if text:
        lines.append(text)
    return lines or [""]


def _draw_node(ax, x, y, w, h, text, fc, ec, tc, fontsize, max_chars=12):
    from matplotlib.patches import FancyBboxPatch
    box = FancyBboxPatch((x - w / 2, y - h / 2), w, h,
                         boxstyle="round,pad=0.02,rounding_size=5",
                         fc=fc, ec=ec, lw=1.2, mutation_scale=1.0)
    ax.add_patch(box)
    lines = _wrap(text, max_chars)
    n = len(lines)
    for i, ln in enumerate(lines):
        ax.text(x, y + (n / 2 - i - 0.5) * (fontsize + 2), ln,
                ha="center", va="center", fontsize=fontsize, color=tc)


def _draw_link(ax, x1, y1, x2, y2, color, lw=1.2):
    from matplotlib.path import Path
    from matplotlib.patches import PathPatch
    ctrl = (x1 + x2) / 2
    verts = [(x1, y1), (ctrl, y1), (ctrl, y2), (x2, y2)]
    codes = [Path.MOVETO, Path.CURVE4, Path.CURVE4, Path.CURVE4]
    ax.add_patch(PathPatch(Path(verts, codes), fill=False,
                           edgecolor=color, lw=lw))


def cmd_analyze(args, data_dir):
    """分析知识库: 找出工科教学类视频, 按话题归类, 统计分类/UP主分布。"""
    all_cards = []
    for p in card_files(data_dir):
        meta = parse_card_full(p)
        if meta.get("title"):
            all_cards.append(meta)
    eng = []
    for c in all_cards:
        # 只按标题判断(不含分类名/UP主名, 避免噪音误匹配)
        topic = classify_engineering(c.get("title") or "")
        if topic:
            c["topic"] = topic
            c["teaching"] = is_teaching(c.get("title") or "")
            eng.append(c)
    by_cat, by_topic, by_upper = {}, {}, {}
    for c in eng:
        cat = c.get("domain") or "未分类"
        by_cat[cat] = by_cat.get(cat, 0) + 1
        by_topic[c["topic"]] = by_topic.get(c["topic"], 0) + 1
        up = c.get("upper") or "未知UP主"
        by_upper[up] = by_upper.get(up, 0) + 1
    shown = eng
    if args.top:
        shown = sorted(eng, key=lambda x: (x["topic"], x.get("title") or ""))[:args.top]
    emit({"ok": True,
          "total_cards": len(all_cards),
          "engineering_cards": len(eng),
          "teaching_cards": sum(1 for c in eng if c["teaching"]),
          "by_category": dict(sorted(by_cat.items(), key=lambda x: -x[1])),
          "by_topic": dict(sorted(by_topic.items(), key=lambda x: -x[1])),
          "top_uploaders": [{"uname": k, "count": v} for k, v in
                            sorted(by_upper.items(), key=lambda x: -x[1])[:15]],
          "sample": [{"bvid": c.get("bvid"), "title": c.get("title"),
                      "topic": c["topic"], "category": c.get("domain"),
                      "teaching": c["teaching"]} for c in shown],
          "hint": "用 render-mindmap --engineering --html 生成工科教学交互式脑图"})


def _leaf_node(c, intro_map=None):
    """视频叶节点数据: 标题/UP主/时长/链接/简介。简介优先卡片内容总结, 其次B站缓存简介。"""
    intro_map = intro_map or {}
    node = {"name": c.get("title") or "?", "up": c.get("upper") or ""}
    if c.get("duration"):
        node["dur"] = c["duration"]
    if c.get("bvid"):
        node["url"] = f"https://www.bilibili.com/video/{c['bvid']}"
    intro = c.get("summary") or intro_map.get(c.get("bvid")) or ""
    intro = re.sub(r"\s+", " ", intro).strip()
    if intro:
        node["intro"] = intro[:160]
    return node


def _tree_payload(block_list, dom_blocks, doms, colors, intro_map=None):
    """把渲染块转换为交互式 HTML 的树数据(分类→话题/知识点→视频)。"""
    root = {"name": "知识库", "children": []}
    for dom in doms:
        dnode = {"name": dom, "color": colors[dom], "children": []}
        subs = {}
        for i in dom_blocks[dom]:
            d, sub, kp, cs, flat = block_list[i]
            if flat:
                for c in cs:
                    dnode["children"].append(_leaf_node(c, intro_map))
            elif kp is None:
                snode = subs.get(sub)
                if snode is None:
                    snode = {"name": f"{sub}（{len(cs)}）", "children": []}
                    subs[sub] = snode
                    dnode["children"].append(snode)
                for c in cs:
                    snode["children"].append(_leaf_node(c, intro_map))
            else:
                snode = subs.get(sub)
                if snode is None:
                    snode = {"name": sub, "children": []}
                    subs[sub] = snode
                    dnode["children"].append(snode)
                knode = {"name": f"{kp}（{len(cs)}）", "children": []}
                for c in cs:
                    knode["children"].append(_leaf_node(c, intro_map))
                snode["children"].append(knode)
        if dnode["children"]:
            root["children"].append(dnode)
    return root


# 交互式 HTML 思维导图模板(单文件、离线可用、可折叠/搜索/跳B站)
_MINDMAP_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html,body { height:100%; font-family:"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",sans-serif; background:#f7f8fa; }
  #bar { position:fixed; top:0; left:0; right:0; height:52px; background:#fff; border-bottom:1px solid #e3e6ea;
         display:flex; align-items:center; gap:16px; padding:0 18px; z-index:10; }
  #bar .t { font-size:15px; font-weight:bold; color:#222; white-space:nowrap; }
  #bar .s { font-size:12px; color:#888; white-space:nowrap; }
  #bar input { flex:1; max-width:340px; height:30px; border:1px solid #d5d9de; border-radius:6px; padding:0 10px;
               font-size:13px; outline:none; }
  #bar input:focus { border-color:#FB7299; }
  #bar .btns { display:flex; gap:6px; }
  #bar button { height:30px; padding:0 12px; border:1px solid #d5d9de; border-radius:6px; background:#fff; color:#444; font-size:12px; cursor:pointer; }
  #bar button:hover { border-color:#FB7299; color:#FB7299; }
  #wrap { position:fixed; top:52px; left:0; right:0; bottom:34px; overflow:hidden; cursor:grab; }
  #wrap.drag { cursor:grabbing; }
  svg { width:100%; height:100%; display:block; }
  #legend { position:fixed; left:0; right:0; bottom:0; height:34px; background:#fff; border-top:1px solid #e3e6ea;
            display:flex; align-items:center; gap:14px; padding:0 18px; font-size:12px; color:#555;
            overflow:hidden; white-space:nowrap; }
  .lg { display:inline-flex; align-items:center; gap:5px; margin-right:10px; }
  .sw { width:12px; height:12px; border-radius:3px; display:inline-block; }
  .node rect { stroke-width:1.2; }
  .node .leaf { cursor:pointer; }
  .node .leaf:hover rect { stroke:#FB7299; stroke-width:1.8; }
  .node .group { cursor:pointer; }
  .hl rect { stroke:#FB7299; stroke-width:2.4 !important; }
  .mark text { fill:#FB7299; font-weight:bold; }
  .hint { position:fixed; right:18px; bottom:40px; font-size:11px; color:#aaa; z-index:10; }
  #wrap svg, #wrap .node { user-select:none; -webkit-user-select:none; -webkit-user-drag:none; }
</style>
</head>
<body>
<div id="bar">
  <span class="t">__TITLE__</span>
  <span class="s" id="stats"></span>
  <span class="btns"><button id="fit" type="button">适应窗口</button></span>
  <input id="q" type="text" placeholder="搜索视频/话题/UP主… Enter 定位">
</div>
<div id="wrap"><svg id="map"></svg></div>
<div id="legend"><span>图例:</span><span id="lgbox"></span></div>
<div class="hint">滚轮滚动 · Ctrl/滚轮缩放 · 拖拽平移 · 点击节点折叠/展开 · 点击视频打开B站</div>
<script>
const DATA = __DATA__;
(function(){
  const svg = document.getElementById('map');
  const NS = 'http://www.w3.org/2000/svg';
  const root = DATA.root;
  const collapsed = new Set();
  const matches = new Set();
  let g = null;
  let vp = {x:40, y:20, s:1};
  function walk(n, k, depth){
    n._k = k; n._depth = depth;
    if (n.children) n.children.forEach((c,i)=>{ c.parent = n; walk(c, k+'-'+i, depth+1); });
  }
  walk(root, '0', 0);
  // 默认折叠: 除根节点外, 所有带子节点的节点都收起来(打开即见分类概览)
  (function initCollapse(n){
    if (n !== root && n.children && n.children.length) collapsed.add(n._k);
    if (n.children) n.children.forEach(initCollapse);
  })(root);
  function visible(){
    const out = [];
    (function dfs(n){
      out.push(n);
      if (n.children && !collapsed.has(n._k)) n.children.forEach(dfs);
    })(root);
    return out;
  }
  const GH = 36, COL = 250, BOXW = 220, X0 = 30, Y0 = 24, GAP = 6;
  let ymap = {};
  let lastNodes = [];
  function applyView(){ if (g) g.setAttribute('transform', 'translate('+vp.x+','+vp.y+') scale('+vp.s+')'); }
  function viewSize(){ return { w: svg.clientWidth || 1200, h: svg.clientHeight || 700 }; }
  function syncViewBox(){
    const v = viewSize();
    svg.setAttribute('viewBox', '0 0 '+v.w+' '+v.h);
  }
  function reveal(n){
    // 展开后把该节点第一个子节点滚动进视野
    if (!lastNodes.length) return;
    let firstY = null;
    for (let i=0;i<lastNodes.length;i++){ if (lastNodes[i].parent === n) { firstY = ymap[lastNodes[i]._k]; break; } }
    if (firstY === null) return;
    const v = viewSize();
    const winBottom = -vp.y/vp.s + v.h/vp.s;
    if (firstY > winBottom - 60) { vp.y = -(firstY*vp.s) + 80; applyView(); }
  }
  function fitView(){
    if (!lastNodes.length) return;
    let x1=Infinity, y1=Infinity, x2=-Infinity, y2=-Infinity;
    lastNodes.forEach(n=>{
      const h = nodeH(n);
      x1 = Math.min(x1, X0 + n._depth*COL); y1 = Math.min(y1, ymap[n._k] - h/2);
      x2 = Math.max(x2, X0 + n._depth*COL + BOXW); y2 = Math.max(y2, ymap[n._k] + h/2);
    });
    const v = viewSize();
    const s = Math.max(0.2, Math.min(v.w/(x2-x1+40), v.h/(y2-y1+40), 1.5));
    vp.s = s;
    vp.x = v.w/2 - ((x1+x2)/2)*s;
    vp.y = v.h/2 - ((y1+y2)/2)*s;
    applyView();
  }
  function blend(hex, w){
    const r=parseInt(hex.slice(1,3),16), gg=parseInt(hex.slice(3,5),16), b=parseInt(hex.slice(5,7),16);
    const f=v=>Math.round(v+(255-v)*w);
    return '#'+[f(r),f(gg),f(b)].map(v=>v.toString(16).padStart(2,'0')).join('');
  }
  function leafLines(n){
    const out = [];
    out.push((n.name || '?').slice(0,26));
    out.push(((n.up || '') + (n.dur ? ' · ' + n.dur : '')).slice(0,32));
    out.push(n.url ? n.url.slice(0,44) : '');
    const intro = n.intro || '';
    if (intro) {
      const s = intro.length > 52 ? intro.slice(0,52)+'…' : intro;
      for (let i=0;i<s.length;i+=26) out.push(s.slice(i,i+26));
    }
    return out;
  }
  function nodeH(n){
    if (n.children && n.children.length) return GH;
    return 16 + leafLines(n).length * 13 + 6;
  }
  function render(){
    svg.innerHTML = '';
    g = document.createElementNS(NS,'g');
    g.setAttribute('transform', 'translate('+vp.x+','+vp.y+') scale('+vp.s+')');
    svg.appendChild(g);
    const nodes = visible();
    ymap = {};
    let yy = Y0;
    nodes.forEach(n=>{ ymap[n._k] = yy + nodeH(n)/2; yy += nodeH(n) + GAP; });
    lastNodes = nodes;
    const links = document.createElementNS(NS,'g');
    const boxes = document.createElementNS(NS,'g');
    g.appendChild(links); g.appendChild(boxes);
    nodes.forEach(n=>{
      if (n.parent && ymap[n.parent._k] !== undefined) {
        const x1 = X0 + n.parent._depth*COL + BOXW/2 + 10;
        const y1 = ymap[n.parent._k];
        const x2 = X0 + n._depth*COL - 10;
        const y2 = ymap[n._k];
        const cx = (x1+x2)/2;
        const p = document.createElementNS(NS,'path');
        p.setAttribute('d', 'M'+x1+','+y1+' C'+cx+','+y1+' '+cx+','+y2+' '+x2+','+y2);
        p.setAttribute('stroke', n.color || (n.parent.color) || '#c9ccd1');
        p.setAttribute('fill','none'); p.setAttribute('stroke-width','1.3');
        links.appendChild(p);
      }
    });
    nodes.forEach(n=>{
      const y = ymap[n._k];
      const x = X0 + n._depth*COL;
      const isLeaf = !n.children || n.children.length===0;
      const h = nodeH(n);
      const yTop = y - h/2;
      const color = n.color || (n.parent? n.parent.color : '#FB7299');
      const group = document.createElementNS(NS,'g');
      const r = document.createElementNS(NS,'rect');
      const bg = n===root ? '#FB7299' : (n._depth===1 ? color : (isLeaf ? '#ffffff' : blend(color,0.85)));
      const tc = (n===root || n._depth===1) ? '#ffffff' : '#333333';
      r.setAttribute('x', x); r.setAttribute('y', yTop);
      r.setAttribute('width', BOXW); r.setAttribute('height', h);
      r.setAttribute('rx','6');
      r.setAttribute('fill', bg); r.setAttribute('stroke', color);
      group.appendChild(r);
      group.setAttribute('class', isLeaf ? 'node leaf' : 'node group');
      if (isLeaf) {
        // 视频详情卡片: 标题 / UP主·时长 / 链接 / 简介
        const lines = leafLines(n);
        lines.forEach((ln, i)=>{
          const t = document.createElementNS(NS,'text');
          t.setAttribute('x', x+8);
          t.setAttribute('y', yTop + 14 + i*13);
          t.setAttribute('font-size', i===0 ? 10.5 : (i===1 ? 9 : 8.5));
          t.setAttribute('fill', i===0 ? '#222222' : (i===2 ? '#2f6fd0' : '#777777'));
          t.setAttribute('font-weight', i===0 ? 'bold' : 'normal');
          t.textContent = ln;
          group.appendChild(t);
        });
        group.addEventListener('click', ()=>{
          if (suppressClick) { suppressClick=false; return; }
          if (n.url) window.open(n.url,'_blank');
        });
      } else {
        const t = document.createElementNS(NS,'text');
        const label = (n.name||'').length>16 ? n.name.slice(0,16)+'…' : (n.name||'');
        t.setAttribute('x', x+10); t.setAttribute('y', y+5);
        t.setAttribute('font-size', n===root?15:(n._depth===1?13:11));
        t.setAttribute('fill', tc);
        t.setAttribute('font-weight', n._depth<=1 ? 'bold' : 'normal');
        t.textContent = label;
        group.appendChild(t);
        const c = document.createElementNS(NS,'circle');
        c.setAttribute('cx', x+BOXW-14); c.setAttribute('cy', y);
        c.setAttribute('r','8'); c.setAttribute('fill','#fff'); c.setAttribute('stroke', color);
        group.appendChild(c);
        const m = document.createElementNS(NS,'text');
        m.setAttribute('x', x+BOXW-14); m.setAttribute('y', y+3.5);
        m.setAttribute('font-size','11'); m.setAttribute('fill', color); m.setAttribute('text-anchor','middle');
        m.textContent = collapsed.has(n._k) ? '+' : '-';
        group.appendChild(m);
        group.addEventListener('click', ()=>{
          if (suppressClick) { suppressClick=false; return; }
          const wasCollapsed = collapsed.has(n._k);
          if (wasCollapsed) collapsed.delete(n._k); else collapsed.add(n._k);
          render();
          if (wasCollapsed) reveal(n);
        });
      }
      if (matches.has(n._k)) {
        r.setAttribute('class','hl');
        t.setAttribute('class','mark');
      }
      boxes.appendChild(group);
    });
  }
  const q = document.getElementById('q');
  function doSearch(){
    const s = q.value.trim().toLowerCase();
    matches.clear();
    if (!s) { render(); return; }
    const found = [];
    (function dfs(n){
      if ((n.name||'').toLowerCase().indexOf(s)>=0 || (n.up||'').toLowerCase().indexOf(s)>=0) found.push(n);
      if (n.children) n.children.forEach(dfs);
    })(root);
    found.forEach(n=>{
      let p = n.parent;
      while (p) { collapsed.delete(p._k); p = p.parent; }
      let q2 = n;
      while (q2) { matches.add(q2._k); q2 = q2.parent; }
    });
    render();
    if (found.length && ymap[found[0]._k] !== undefined) {
      vp.y = -(ymap[found[0]._k]*vp.s) + window.innerHeight/2 - 60;
      if (g) g.setAttribute('transform', 'translate('+vp.x+','+vp.y+') scale('+vp.s+')');
    }
  }
  q.addEventListener('input', doSearch);
  q.addEventListener('keydown', e=>{ if (e.key==='Enter') { e.preventDefault(); doSearch(); } });
  const wrap = document.getElementById('wrap');
  let dragging=false, moved=false, suppressClick=false, sx=0, sy=0, ox=0, oy=0;
  wrap.addEventListener('mousedown', e=>{ dragging=true; moved=false; suppressClick=false; sx=e.clientX; sy=e.clientY; ox=vp.x; oy=vp.y; wrap.classList.add('drag'); });
  window.addEventListener('mousemove', e=>{
    if (!dragging) return;
    const dx = e.clientX - sx, dy = e.clientY - sy;
    if (!moved && Math.abs(dx) + Math.abs(dy) > 4) { moved = true; suppressClick = true; }
    if (moved) { e.preventDefault(); vp.x = ox + dx; vp.y = oy + dy; applyView(); }
  });
  window.addEventListener('mouseup', ()=>{ dragging=false; wrap.classList.remove('drag'); });
  // 滚轮: 默认滚动; Ctrl/Shift+滚轮 = 以鼠标为中心缩放
  wrap.addEventListener('wheel', e=>{
    e.preventDefault();
    if (e.ctrlKey || e.shiftKey) {
      const v = viewSize();
      const rect = svg.getBoundingClientRect();
      const mx = e.clientX - rect.left, my = e.clientY - rect.top;
      const fx = (mx - vp.x)/vp.s, fy = (my - vp.y)/vp.s;
      const ns = Math.min(3, Math.max(0.2, vp.s * (e.deltaY>0 ? 0.9 : 1.1)));
      vp.x = mx - fx*ns; vp.y = my - fy*ns; vp.s = ns;
    } else {
      vp.y -= e.deltaY;
    }
    applyView();
  }, {passive:false});
  document.getElementById('fit').addEventListener('click', ()=>{ fitView(); });
  window.addEventListener('resize', ()=>{ syncViewBox(); });
  // 统计与图例
  let vids = 0;
  (function count(n){ if (!n.children || n.children.length===0) vids++; if (n.children) n.children.forEach(count); })(root);
  const groups = root.children ? root.children.length : 0;
  document.getElementById('stats').textContent = groups+' 个分类 · '+vids+' 个视频';
  const lg = document.getElementById('lgbox');
  (root.children||[]).forEach(d=>{
    const s = document.createElement('span');
    s.className = 'lg';
    const sw = document.createElement('span'); sw.className='sw'; sw.style.background = d.color;
    s.appendChild(sw);
    s.appendChild(document.createTextNode(d.name));
    lg.appendChild(s);
  });
  render();
  syncViewBox();
})();
</script>
</body>
</html>
"""


def cmd_render_mindmap(args, data_dir):
    """用 matplotlib 把知识库绘制成图片思维导图(默认输出 PNG+SVG)。

    兼容新旧卡片格式: 带「知识点标签」的走 领域→子领域→知识点→视频 四层;
    只有分类的旧卡片走 分类→视频 扁平结构。
    """
    cards, _tree, dom_subs, flat_by_domain = build_knowledge_tree(data_dir)
    if not dom_subs and not flat_by_domain and not args.engineering:
        fail("没有可绘制的知识卡片（需要带 领域/分类 字段）")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except Exception as e:
        fail(f"绘图需要 matplotlib，请先安装: pip install matplotlib ({e})")
    for name in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC",
                 "WenQuanYi Zen Hei", "PingFang SC"):
        if any(name in f.name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.sans-serif"] = [name]
            break
    plt.rcParams["axes.unicode_minus"] = False

    max_videos = max(0, args.max_videos)
    eng_mode = bool(args.engineering)
    if eng_mode and "失效视频" not in (args.exclude or []):
        args.exclude = (args.exclude or []) + ["失效视频"]

    # 组装块: (dom, sub, kp, cards, is_flat)
    block_list = []
    if eng_mode:
        # 工科教学模式: 只保留命中工程话题的卡片, 用话题合成子领域(分类→话题→视频)
        all_cards = []
        for p in card_files(data_dir):
            meta = parse_card_full(p)
            if meta.get("title"):
                all_cards.append(meta)
        groups = {}
        for c in all_cards:
            # 只按标题判断(同 analyze)
            topic = classify_engineering(c.get("title") or "")
            if not topic:
                continue
            dom = c.get("domain") or "未分类"
            groups.setdefault((dom, topic), []).append(c)
        for (dom, topic), cs in sorted(groups.items()):
            block_list.append((dom, topic, None,
                               sorted(cs, key=lambda x: x.get("title") or ""), False))
        doms = sorted(set(b[0] for b in block_list))
        colors = {d: PLOT_COLORS[i % len(PLOT_COLORS)] for i, d in enumerate(doms)}
        if args.exclude:
            exclude = set(args.exclude)
            block_list = [b for b in block_list if b[0] not in exclude]
            doms = sorted(set(b[0] for b in block_list))
        if not block_list:
            fail("没有匹配到工科教学类卡片（可先运行 analyze 查看话题分布）")
    else:
        doms = sorted(set(list(dom_subs) + list(flat_by_domain)))
        colors = {d: PLOT_COLORS[i % len(PLOT_COLORS)] for i, d in enumerate(doms)}
        if args.exclude:
            exclude = set(args.exclude)
            doms = [d for d in doms if d not in exclude]
        for dom in doms:
            for sub in sorted(dom_subs.get(dom, {})):
                for kp in sorted(dom_subs[dom][sub]):
                    cs = sorted(dom_subs[dom][sub][kp], key=lambda x: x.get("title") or "")
                    block_list.append((dom, sub, kp, cs, False))
            flat = sorted(flat_by_domain.get(dom, []), key=lambda x: x.get("title") or "")
            if flat:
                block_list.append((dom, None, None, flat, True))
        if not block_list:
            fail("没有可绘制的节点（可能被 --exclude 全部排除了）")

    leaf_h = 30
    kp_gap = 6
    sub_gap = 26
    dom_gap = 46

    shown_list = []
    for (dom, sub, kp, cs, flat) in block_list:
        shown_list.append(cs[:max_videos] if max_videos else [])
    block_h = [max(1, len(s)) * leaf_h for s in shown_list]
    dom_blocks = {}
    for i, b in enumerate(block_list):
        dom_blocks.setdefault(b[0], []).append(i)

    def dom_h(dom):
        idxs = dom_blocks[dom]
        return sum(block_h[i] for i in idxs) + sub_gap * (len(idxs) - 1)

    margin = 70
    content_h = sum(dom_h(d) for d in doms) + dom_gap * (len(doms) - 1)
    height = max(500, content_h + margin * 2)
    width = 1240

    X = {"root": 90, "dom": 330, "sub": 560, "kp": 790, "leaf": 1030}
    W = {"root": 180, "dom": 200, "sub": 200, "kp": 220, "leaf": 250}

    y_root = height / 2
    y_dom, y_sub, y_kp, y_blk = {}, {}, {}, {}
    top = margin
    for dom in doms:
        idxs = dom_blocks[dom]
        h = dom_h(dom)
        y_dom[dom] = top + h / 2
        b_top = top
        for i in idxs:
            y_blk[i] = b_top + block_h[i] / 2
            b_top += block_h[i] + sub_gap
        top += h + dom_gap
    # 子领域/知识点节点位置(结构化块)
    for i, b in enumerate(block_list):
        dom, sub, kp, cs, flat = b
        if flat:
            continue
        if (dom, sub) not in y_sub:
            same = [j for j in dom_blocks[dom] if block_list[j][1] == sub]
            y_sub[(dom, sub)] = sum(y_blk[j] for j in same) / len(same)
        y_kp[(dom, sub, kp)] = y_blk[i]

    fig, ax = plt.subplots(figsize=(width / 100, height / 100), dpi=100)
    ax.set_xlim(0, width)
    ax.set_ylim(0, height)
    ax.axis("off")
    total_videos = sum(len(b[3]) for b in block_list)
    mode_label = "工科教学类 · " if eng_mode else ""
    ax.text(width / 2, height - 24,
            f"B站知识库 · {mode_label}思维导图（{len(doms)} 个领域 · {len(block_list)} 组 · {total_videos} 个视频）",
            ha="center", va="center", fontsize=15, color="#333333", fontweight="bold")

    # 连线(先画, 节点盖住端点)
    for dom in doms:
        _draw_link(ax, X["dom"] - W["dom"] / 2, y_dom[dom],
                   X["root"] + W["root"] / 2, y_root, colors[dom], lw=2.0)
        for i in dom_blocks[dom]:
            d, sub, kp, cs, flat = block_list[i]
            n_show = len(shown_list[i])
            base_y = y_blk[i] - block_h[i] / 2
            if flat:
                for j in range(n_show):
                    ly = base_y + leaf_h * (j + 0.5)
                    _draw_link(ax, X["leaf"] - W["leaf"] / 2, ly,
                               X["dom"] + W["dom"] / 2, y_dom[dom], colors[dom], lw=0.8)
            else:
                _draw_link(ax, X["sub"] - W["sub"] / 2, y_sub[(dom, sub)],
                           X["dom"] + W["dom"] / 2, y_dom[dom], colors[dom])
                if kp is not None:
                    fx, fy = X["kp"] + W["kp"] / 2, y_kp[(dom, sub, kp)]
                    _draw_link(ax, X["kp"] - W["kp"] / 2, fy,
                               X["sub"] + W["sub"] / 2, y_sub[(dom, sub)], colors[dom])
                else:
                    fx, fy = X["sub"] + W["sub"] / 2, y_sub[(dom, sub)]
                for j in range(n_show):
                    ly = base_y + leaf_h * (j + 0.5)
                    _draw_link(ax, X["leaf"] - W["leaf"] / 2, ly, fx, fy,
                               colors[dom], lw=0.8)

    # 节点
    _draw_node(ax, X["root"], y_root, W["root"], 52, "知识库",
               "#FB7299", "#FB7299", "#ffffff", 16, max_chars=8)
    for dom in doms:
        n_cards = len(flat_by_domain.get(dom, [])) + sum(
            len(v) for s in dom_subs.get(dom, {}).values() for v in s.values())
        label = f"{dom}（{n_cards}）" if n_cards else dom
        _draw_node(ax, X["dom"], y_dom[dom], W["dom"], 34, label,
                   colors[dom], colors[dom], "#ffffff", 12, max_chars=10)
        for i in dom_blocks[dom]:
            d, sub, kp, cs, flat = block_list[i]
            n_show = len(shown_list[i])
            base_y = y_blk[i] - block_h[i] / 2
            if not flat:
                _draw_node(ax, X["sub"], y_sub[(dom, sub)], W["sub"], 30, sub,
                           _mix(colors[dom], 0.72), colors[dom], "#333333", 11, max_chars=12)
                if kp is not None:
                    _draw_node(ax, X["kp"], y_kp[(dom, sub, kp)], W["kp"],
                               min(block_h[i], leaf_h * 4), kp,
                               _mix(colors[dom], 0.86), colors[dom], "#333333", 11, max_chars=14)
            for j, c in enumerate(shown_list[i]):
                ly = base_y + leaf_h * (j + 0.5)
                title = c.get("title") or "?"
                upper = c.get("upper") or ""
                _draw_node(ax, X["leaf"], ly, W["leaf"], leaf_h - 4,
                           f"⭐{title}", "#FFFFFF", "#CCCCCC", "#333333", 9, max_chars=18)
                if upper:
                    ax.text(X["leaf"] + W["leaf"] / 2 - 8, ly,
                            upper[:12], ha="right", va="center",
                            fontsize=7.5, color="#999999")
            extra_n = len(cs) - n_show
            if extra_n > 0:
                ey = base_y + block_h[i] - leaf_h / 2
                chip = f"… 等{extra_n}个" if flat else f"… 等{extra_n}个 见知识点索引"
                _draw_node(ax, X["leaf"], ey, W["leaf"], leaf_h - 4,
                           chip, "#FAFAFA", "#D8D8D8", "#999999", 8, max_chars=22)

    # 领域图例
    ly0 = 26
    ax.text(20, ly0, "领域:", fontsize=10, color="#666666", va="center")
    lx = 70
    for dom in doms:
        ax.add_patch(plt.Rectangle((lx, ly0 - 8), 14, 14, fc=colors[dom], ec="none"))
        ax.text(lx + 20, ly0, dom, fontsize=10, color="#333333", va="center")
        lx += 26 + len(dom) * 15 + 14
        if lx > width - 60:
            break

    out_png = args.out or os.path.join(data_dir, "library", "思维导图", "知识地图.png")
    out_svg = os.path.splitext(out_png)[0] + ".svg"
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    fig.savefig(out_png, dpi=args.dpi, bbox_inches="tight", facecolor="white")
    fig.savefig(out_svg, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    out_html = None
    if args.html:
        # 简介映射: bvid -> B站缓存里的视频简介(卡片内容总结优先, 在 _leaf_node 里处理)
        intro_map = {}
        fv = load_json(cache_path(data_dir, "fav_videos.json"))
        if fv:
            for bv, v in (fv.get("videos") or {}).items():
                iv = (v.get("intro") or "").strip()
                if iv:
                    intro_map[bv] = iv
        # 模板里用 DATA.root 访问根节点, 因此包一层 root
        payload = {"root": _tree_payload(block_list, dom_blocks, doms, colors,
                                         intro_map)}
        title = f"B站知识库 · {'工科教学类 · ' if eng_mode else ''}思维导图"
        data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        html_text = (_MINDMAP_HTML.replace("__TITLE__", _html.escape(title))
                     .replace("__DATA__", data_json))
        out_html = os.path.splitext(out_png)[0] + ".html"
        write_text(out_html, html_text)
    emit({"ok": True, "png": out_png, "svg": out_svg, "html": out_html,
          "cards": sum(len(b[3]) for b in block_list),
          "groups": len(block_list),
          "domains": doms, "videos": total_videos,
          "height_px": int(height)})


def cmd_build_index(args, data_dir):
    """扫描知识卡片, 确定性重建 知识地图.md 与 知识点索引.md。"""
    mindmap_dir = os.path.join(data_dir, "library", "思维导图")
    os.makedirs(mindmap_dir, exist_ok=True)
    cards, tree, dom_subs, _flat = build_knowledge_tree(data_dir)
    if not cards:
        fail("分类目录下没有带“领域”和“知识点标签”的知识卡片，无法生成索引")
    # 1) 知识点索引
    idx = ["# 知识点索引", "",
           "> 由 build-index 自动生成：知识卡片 → 知识点 → 视频/卡片。手工改动会在下次重建时被覆盖。",
           "> 条目格式：标题(链接) — UP主 ⭐ | 知识卡片(相对路径)"]
    for key in sorted(tree):
        dom, sub, kp = key
        idx.append("")
        idx.append(f"## {kp}")
        for c in sorted(tree[key], key=lambda x: x.get("title") or ""):
            link = f"https://www.bilibili.com/video/{c['bvid']}" if c.get("bvid") else ""
            rel = os.path.relpath(c["file"], mindmap_dir).replace("\\", "/")
            idx.append(f"- [{c.get('title') or '无标题'}]({link}) — "
                       f"{c.get('upper') or '未知UP主'} ⭐ | [知识卡片]({rel})")
    write_text(os.path.join(mindmap_dir, "知识点索引.md"), "\n".join(idx) + "\n")
    # 2) 知识地图 (领域 -> 子领域 -> 知识点 -> 视频)
    # 知识点节点数(每个 子领域×知识点 组合算一个节点)超过 30 才拆分子图
    big = [d for d, subs in dom_subs.items()
           if sum(len(s) for s in subs.values()) > 30]
    main = ["# 知识地图", "",
            "> 由 build-index 自动生成。层级：领域 → 子领域 → 知识点 → 视频。",
            "",
            "```mermaid", "mindmap", "  root((知识地图))"]
    for dom in sorted(dom_subs):
        if dom in big:
            main.append(f"    {mermaid_safe(dom)} 见{dom}子图")
            sub_lines = ["# 知识地图：" + dom, "", "```mermaid", "mindmap",
                         f"  root(({mermaid_safe(dom)}))"]
            _mindmap_body(sub_lines, dom_subs[dom])
            sub_lines.append("```")
            write_text(os.path.join(mindmap_dir, f"{dom}.md"), "\n".join(sub_lines) + "\n")
        else:
            main.append(f"    {mermaid_safe(dom)}")
            _mindmap_body(main, dom_subs[dom])
    main.append("```")
    write_text(os.path.join(mindmap_dir, "知识地图.md"), "\n".join(main) + "\n")
    emit({"ok": True, "cards": len(cards), "knowledge_points": len(tree),
          "domains": sorted(dom_subs), "big_domains": big,
          "knowledge_index": os.path.join(mindmap_dir, "知识点索引.md"),
          "mindmap": os.path.join(mindmap_dir, "知识地图.md")})


def _mindmap_body(lines, subs):
    for sub in sorted(subs):
        lines.append(f"      {mermaid_safe(sub)}")
        for kp in sorted(subs[sub]):
            cs = sorted(subs[sub][kp], key=lambda x: x.get("title") or "")
            lines.append(f"        {mermaid_safe(kp)}")
            for c in cs[:3]:
                lines.append(f"          ⭐{mermaid_safe(c.get('title') or '')} "
                             f"{mermaid_safe(c.get('upper') or '')}")
            if len(cs) > 3:
                lines.append(f"          等{len(cs)}个 见知识点索引")


def cmd_search(args, data_dir):
    q = (args.q or "").lower()
    if not q:
        fail("需要 --q 关键词")
    lib = os.path.join(data_dir, "library")
    hits = []
    if os.path.isdir(lib):
        for root, _d, names in os.walk(lib):
            for fn in sorted(names):
                if not fn.lower().endswith(".md"):
                    continue
                p = os.path.join(root, fn)
                try:
                    with open(p, encoding="utf-8") as fh:
                        lines = fh.readlines()
                except Exception:
                    continue
                title = fn[:-3]
                for ln in lines[:8]:
                    m = re.match(r"^#\s+(.+)$", ln)
                    if m:
                        title = m.group(1).strip()
                        break
                ctx = []
                for i, ln in enumerate(lines):
                    if q in ln.lower():
                        ctx.append({"line": i + 1, "text": ln.strip()[:160]})
                        if len(ctx) >= args.context:
                            break
                if ctx:
                    hits.append({"file": p, "title": title, "matches": ctx})
    if args.subtitles:
        # 检索字幕缓存: 尚未建档的视频也能按内容找到
        sub_dir = cache_path(data_dir, "subtitles")
        if os.path.isdir(sub_dir):
            for fn in sorted(os.listdir(sub_dir)):
                if not fn.lower().endswith(".txt"):
                    continue
                p = os.path.join(sub_dir, fn)
                try:
                    with open(p, encoding="utf-8") as fh:
                        lines = fh.readlines()
                except Exception:
                    continue
                ctx = []
                for i, ln in enumerate(lines):
                    if q in ln.lower():
                        ctx.append({"line": i + 1, "text": ln.strip()[:160]})
                        if len(ctx) >= args.context:
                            break
                if ctx:
                    hits.append({"file": p, "title": f"[字幕] {fn[:-4]}", "matches": ctx})
    hits = hits[:args.limit]
    emit({"ok": True, "query": args.q, "total": len(hits), "hits": hits})


def cmd_stats(args, data_dir):
    out = {"ok": True}
    lib = os.path.join(data_dir, "library")
    by_domain = {}
    total_md = 0
    statuses = {"未学": 0, "已学": 0, "复习中": 0, "未标记": 0}
    if os.path.isdir(lib):
        for root, _d, names in os.walk(lib):
            for fn in names:
                if not fn.lower().endswith(".md"):
                    continue
                total_md += 1
                p = os.path.join(root, fn)
                try:
                    with open(p, encoding="utf-8") as fh:
                        text = fh.read()
                except Exception:
                    continue
                m = re.search(r"^-\s*领域:\s*([^|]+)", text, re.M)
                if m:
                    dom = m.group(1).strip()
                    by_domain[dom] = by_domain.get(dom, 0) + 1
                m = re.search(r"^-\s*学习状态:\s*(\S+)", text, re.M)
                if m:
                    st = m.group(1).strip()
                    statuses[st] = statuses.get(st, 0) + 1
                else:
                    statuses["未标记"] += 1
    bo_dir = os.path.join(lib, "博主")
    out["library"] = {
        "md_files": total_md,
        "by_domain": by_domain,
        "learning_status": statuses,
        "uploader_archives": len(os.listdir(bo_dir)) if os.path.isdir(bo_dir) else 0,
    }
    fv = load_json(cache_path(data_dir, "fav_videos.json"))
    if fv:
        vids = fv.get("videos") or {}
        done = library_done_bvids(data_dir) | set((load_progress(data_dir).get("videos_done") or {}).keys())
        removed = {k for k, v in vids.items() if v.get("removed")}
        months = {}
        for v in vids.values():
            ft = v.get("fav_time")
            if ft:
                ym = datetime.fromtimestamp(ft).strftime("%Y-%m")
                months[ym] = months.get(ym, 0) + 1
        out["favorites"] = {
            "total": len(vids),
            "documented": len(set(vids) & done),
            "pending": len(set(vids) - done - removed),
            "removed": len(removed),
            "by_month": dict(sorted(months.items())[-12:]),
        }
    emit(out)


# ---------------- 写操作: 分类同步回 B站 ----------------

def require_write_cookie(cfg):
    if not cfg.get("sessdata") or not cfg.get("bili_jct"):
        fail("写操作需要 SESSDATA 与 bili_jct 两个 Cookie。"
             "请运行: setup --sessdata <值> --bili-jct <值>")
    return f"SESSDATA={cfg['sessdata']}; bili_jct={cfg['bili_jct']}"


def append_write_log(data_dir, entry):
    p = cache_path(data_dir, "write_log.json")
    log = load_json(p, []) or []
    if not isinstance(log, list):
        log = []
    entry["at"] = now_iso()
    log.append(entry)
    save_json(p, log)


def list_my_folders(cookie, uid):
    d = http_get(f"{API}/x/v3/fav/folder/created/list-all", {"up_mid": uid}, cookie=cookie)
    if d.get("code") != 0:
        fail(f"获取收藏夹列表失败: code={d.get('code')} msg={d.get('message')}")
    return (d.get("data") or {}).get("list") or []


def resolve_aid(bvid, data_dir, cookie, delay=0.4):
    cache = cache_path(data_dir, "aids.json")
    aids = load_json(cache, {}) or {}
    if bvid in aids:
        return aids[bvid]
    d = http_get(f"{API}/x/web-interface/view", {"bvid": bvid}, cookie=cookie)
    if d.get("code") != 0:
        return None
    aid = (d.get("data") or {}).get("aid")
    if aid:
        aids[bvid] = aid
        save_json(cache, aids)
        rate_sleep(delay)
    return aid


def resolve_aids(bvids, data_dir, cookie, batch=20, delay=0.4):
    """批量解析 bvid -> aid (view/multi 一次多条, 失败项回退单查)。"""
    cache = cache_path(data_dir, "aids.json")
    aids = load_json(cache, {}) or {}
    todo = [b for b in bvids if b not in aids]
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        qs = urllib.parse.urlencode([("bvid", b) for b in chunk], doseq=True)
        d = http_get(f"{API}/x/web-interface/view/multi?{qs}", cookie=cookie)
        if d.get("code") == 0:
            for v in (d.get("data") or {}).get("list") or []:
                if v.get("bvid") and v.get("aid"):
                    aids[v["bvid"]] = v["aid"]
        missing = [b for b in chunk if b not in aids]
        for b in missing:
            d1 = http_get(f"{API}/x/web-interface/view", {"bvid": b}, cookie=cookie)
            if d1.get("code") == 0 and (d1.get("data") or {}).get("aid"):
                aids[b] = d1["data"]["aid"]
        save_json(cache, aids)
        rate_sleep(delay)
    return aids


def cmd_create_folder(args, data_dir):
    """在 B 站创建收藏夹; 幂等: 同名收藏夹直接复用。"""
    cfg = load_config(data_dir)
    cookie = require_write_cookie(cfg)
    nav = http_get(f"{API}/x/web-interface/nav", cookie=cookie)
    if not (nav.get("data") or {}).get("isLogin"):
        fail("未登录或 Cookie 过期，请重新 setup")
    uid = nav["data"]["mid"]
    for f_ in list_my_folders(cookie, uid):
        if f_.get("title") == args.title:
            append_write_log(data_dir, {"action": "create_folder_reused",
                                        "title": args.title, "media_id": f_["id"]})
            emit({"ok": True, "media_id": f_["id"], "title": args.title,
                  "reused": True})
            return
    d = http_post(f"{API}/x/v3/fav/folder/add",
                  {"title": args.title, "intro": args.intro or "",
                   "privacy": args.privacy, "csrf": cfg["bili_jct"]}, cookie)
    if d.get("code") != 0:
        fail(f"创建收藏夹失败: code={d.get('code')} msg={d.get('message')}")
    fid = (d.get("data") or {}).get("id")
    append_write_log(data_dir, {"action": "create_folder", "title": args.title,
                                "media_id": fid, "privacy": args.privacy})
    emit({"ok": True, "media_id": fid, "title": args.title, "reused": False,
          "privacy": args.privacy})


def cmd_aid(args, data_dir):
    cfg = load_config(data_dir)
    aid = resolve_aid(args.bvid, data_dir, cookie_of(cfg))
    if not aid:
        fail(f"无法解析 {args.bvid} 的 aid")
    emit({"ok": True, "bvid": args.bvid, "aid": aid})


def cmd_plan_classify(args, data_dir):
    """按本地分类生成“同步回 B站”的移动计划(只读, 不执行任何写操作)。"""
    fj = load_json(cache_path(data_dir, "fav_folders.json"))
    fv = load_json(cache_path(data_dir, "fav_videos.json"))
    if not fj or not fv:
        fail("需要先 sync-favorites 生成收藏夹/视频缓存")
    folders = fj.get("folders") or []
    folder_by_title = {}
    for x in folders:
        folder_by_title.setdefault(x["title"], x["id"])
    vids = fv.get("videos") or {}
    base = os.path.join(data_dir, "library", "分类")
    plan = []
    untracked = {}
    new_folders = []
    if os.path.isdir(base):
        for cat in sorted(os.listdir(base)):
            cat_dir = os.path.join(base, cat)
            if not os.path.isdir(cat_dir):
                continue
            target = folder_by_title.get(cat)
            if target is None:
                new_folders.append(cat)
            for fn in sorted(os.listdir(cat_dir)):
                if not fn.lower().endswith(".md"):
                    continue
                try:
                    with open(os.path.join(cat_dir, fn), encoding="utf-8") as fh:
                        text = fh.read()
                except Exception:
                    continue
                for bv in re.findall(r"BV[0-9A-Za-z]{10}", text):
                    v = vids.get(bv)
                    if not v:
                        untracked.setdefault(bv, {"bvid": bv, "category": cat})
                        continue
                    if v.get("removed"):
                        continue
                    for src in v.get("folder_ids") or []:
                        if src == target:
                            continue
                        plan.append({"bvid": bv, "title": v.get("title"),
                                     "from": src, "to": target})
    # 去重(同一视频可能同时出现在知识卡片与列表行)
    seen, deduped = set(), []
    for item in plan:
        k = (item["bvid"], item["from"], item["to"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(item)
    plan = deduped
    moves_by_target = {}
    for item in plan:
        moves_by_target[item["to"]] = moves_by_target.get(item["to"], 0) + 1
    out_path = args.out or cache_path(data_dir, "classify_plan.json")
    save_json(out_path, plan)
    emit({"ok": True, "plan_file": out_path, "total_moves": len(plan),
          "new_folders_needed": sorted(set(new_folders)),
          "untracked_videos": len(untracked),
          "untracked_sample": list(untracked.values())[:10],
          "moves_by_target_folder_id": moves_by_target,
          "hint": "to 为 null 的项需要先用 create-folder 建好收藏夹并回填 media_id"})


def cmd_apply_plan(args, data_dir):
    """按计划把视频移动到分类收藏夹。默认只预览, 加 --apply 才真正执行。"""
    cfg = load_config(data_dir)
    cookie = require_write_cookie(cfg)
    plan = load_json(args.plan)
    if not isinstance(plan, list) or not plan:
        fail("计划文件不存在或为空。格式: JSON 数组, 每项 "
             '{"bvid": "...", "title": "...", "from": 原收藏夹media_id, '
             '"to": 目标收藏夹media_id}')
    valid = []
    for item in plan:
        if not item.get("bvid") or not item.get("from") or not item.get("to"):
            continue
        if item["from"] == item["to"]:
            continue
        valid.append(item)
    if not valid:
        fail("计划中没有有效的移动项")
    groups = {}
    for item in valid:
        groups.setdefault((item["from"], item["to"]), []).append(item)
    if not args.apply:
        emit({"ok": True, "dry_run": True, "total_moves": len(valid),
              "groups": [{"from": k[0], "to": k[1], "count": len(v),
                          "sample_titles": [x.get("title", "") for x in v[:5]]}
                         for k, v in groups.items()],
              "hint": "请用户确认后再加 --apply 执行"})
        return
    nav = http_get(f"{API}/x/web-interface/nav", cookie=cookie)
    if not (nav.get("data") or {}).get("isLogin"):
        fail("未登录或 Cookie 过期，请重新 setup")
    uid = nav["data"]["mid"]
    # 批量解析 aid
    aids = resolve_aids([x["bvid"] for x in valid], data_dir, cookie)
    errors = []
    done = 0
    for (src, tar), items in groups.items():
        usable = [x for x in items if x["bvid"] in aids]
        for i in range(0, len(usable), args.batch_size):
            chunk = usable[i:i + args.batch_size]
            resources = ",".join(f"{aids[x['bvid']]}:2" for x in chunk)
            d = http_post(f"{API}/x/v3/fav/resource/move",
                          {"resources": resources, "src_media_id": src,
                           "tar_media_id": tar, "mid": uid,
                           "csrf": cfg["bili_jct"], "platform": "web"}, cookie)
            okk = d.get("code") == 0
            append_write_log(data_dir, {
                "action": "move", "src": src, "tar": tar,
                "bvids": [x["bvid"] for x in chunk],
                "code": d.get("code"), "message": d.get("message")})
            if okk:
                done += len(chunk)
                sys.stderr.write(f"[ok] {src} -> {tar}: {len(chunk)} 条 "
                                 f"(累计 {done}/{len(valid)})\n")
            else:
                errors.append({"src": src, "tar": tar,
                               "bvids": [x["bvid"] for x in chunk],
                               "code": d.get("code"),
                               "message": d.get("message")})
                if not args.continue_on_error:
                    emit({"ok": False, "error": "移动失败, 已停止", "done": done,
                          "failed": errors[-1]})
                    sys.exit(1)
            rate_sleep(args.delay)
    usable_total = sum(len([x for x in items if x["bvid"] in aids])
                       for items in groups.values())
    emit({"ok": len(errors) == 0, "applied": True, "done": done,
          "total_planned": len(valid),
          "unresolved_aids": len(valid) - usable_total,
          "errors": errors,
          "log": cache_path(data_dir, "write_log.json")})


def safe_name_text(t):
    t = unicodedata.normalize("NFKC", t or "")
    t = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    if len(t) > 80:
        t = t[:80].strip(" .")
    return t or "untitled"


def cmd_safe_name(args, data_dir):
    print(safe_name_text(args.text))


HANDLERS = {
    "setup": cmd_setup, "status": cmd_status,
    "sync-favorites": cmd_sync_favorites, "sync-followings": cmd_sync_followings,
    "sync-uploader-videos": cmd_sync_uploader_videos,
    "subtitles": cmd_subtitles, "report": cmd_report, "pending": cmd_pending,
    "mark-done": cmd_mark_done, "focus": cmd_focus,
    "create-folder": cmd_create_folder, "aid": cmd_aid,
    "plan-classify": cmd_plan_classify,
    "apply-plan": cmd_apply_plan, "safe-name": cmd_safe_name,
    "review": cmd_review, "export-anki": cmd_export_anki,
    "build-index": cmd_build_index, "render-mindmap": cmd_render_mindmap,
    "analyze": cmd_analyze,
    "search": cmd_search, "stats": cmd_stats,
}


def main():
    ap = argparse.ArgumentParser(description="B站数据同步/知识库工具")
    ap.add_argument("--data-dir", default=os.environ.get("BILI_DATA_DIR")
                    or os.path.join(os.getcwd(), "bili-data"),
                    help="数据目录(默认环境变量 BILI_DATA_DIR 或 ./bili-data)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("setup", help="保存登录 Cookie 并验证")
    p.add_argument("--sessdata", required=True)
    p.add_argument("--bili-jct", default=None, help="写操作需要的 CSRF Cookie")

    sub.add_parser("status", help="登录态/缓存/知识库统计")

    p = sub.add_parser("sync-favorites", help="同步全部收藏夹视频")
    p.add_argument("--folder", type=int, help="只同步指定 media_id")
    p.add_argument("--no-prune", action="store_true",
                   help="不标记已取消收藏的视频(默认会标记 removed)")
    p.add_argument("--delay", type=float, default=DELAY_DEFAULT)

    p = sub.add_parser("sync-followings", help="同步关注列表")
    p.add_argument("--continue-on-error", action="store_true",
                   help="单页失败时保留已抓取的部分数据继续")
    p.add_argument("--delay", type=float, default=DELAY_DEFAULT)

    p = sub.add_parser("sync-uploader-videos", help="同步 UP 主投稿列表")
    p.add_argument("--mid", type=int, help="指定 UP 主 mid(否则同步全部关注者)")
    p.add_argument("--uname", help="配合 --mid 使用")
    p.add_argument("--limit", type=int, help="只同步前 N 个关注者")
    p.add_argument("--max-videos", type=int, default=50, help="每个 UP 主最多取几条(默认50)")
    p.add_argument("--delay", type=float, default=DELAY_DEFAULT)

    p = sub.add_parser("subtitles", help="抓取单个视频字幕")
    p.add_argument("--bvid", required=True)
    p.add_argument("--cid", type=int)
    p.add_argument("--lang", default="zh")
    p.add_argument("--preview-chars", type=int, default=800)

    p = sub.add_parser("pending", help="分批列出未建档项")
    p.add_argument("--kind", choices=["videos", "uploaders"], default="videos")
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--offset", type=int, default=0)

    p = sub.add_parser("mark-done", help="标记视频/UP主已完成(断点进度)")
    p.add_argument("--kind", choices=["video", "uploader"], required=True)
    p.add_argument("--bvid")
    p.add_argument("--title")
    p.add_argument("--mid", type=int)
    p.add_argument("--uname")

    p = sub.add_parser("focus", help="查看/重置重点知识领域(存于 settings.json)")
    p.add_argument("--reset", action="store_true")

    p = sub.add_parser("review", help="查看到期(未学/到期)的知识卡片, 或更新学习状态")
    p.add_argument("--all", action="store_true", help="列出全部卡片(默认只列到期/未学)")
    p.add_argument("--set", action="store_true", help="更新学习状态(需 --bvid)")
    p.add_argument("--bvid", help="配合 --set")
    p.add_argument("--status", choices=["未学", "已学", "复习中"], help="学习状态")
    p.add_argument("--next", help="下次复习日期, 格式 YYYY-MM-DD")

    p = sub.add_parser("export-anki", help="把知识卡片的核心知识点导出为 Anki CSV")
    p.add_argument("--out", help="输出路径(默认 library/anki_export.csv)")
    p.add_argument("--domain", help="只导出指定领域(按路径过滤)")

    sub.add_parser("build-index", help="扫描知识卡片, 重建知识地图与知识点索引")

    p = sub.add_parser("render-mindmap", help="把知识库绘制成图片/交互式HTML思维导图")
    p.add_argument("--out", help="输出 PNG 路径(默认 library/思维导图/知识地图.png)")
    p.add_argument("--dpi", type=int, default=130)
    p.add_argument("--max-videos", type=int, default=3,
                   help="每个知识点/分类最多显示几个视频叶节点(0=只显示结构)")
    p.add_argument("--exclude", action="append", default=[],
                   help="排除的领域/分类名(可多次, 如 --exclude 失效视频)")
    p.add_argument("--engineering", action="store_true",
                   help="只绘制工科教学类: 按话题分类器过滤, 结构为 分类→话题→视频")
    p.add_argument("--html", action="store_true",
                   help="额外生成交互式 HTML(单文件: 可折叠/搜索/点视频跳B站)")

    p = sub.add_parser("analyze", help="分析知识库: 工科教学类视频的话题/分类/UP主分布")
    p.add_argument("--top", type=int, default=0,
                   help="只输出前 N 条示例(0=输出全部示例)")

    p = sub.add_parser("search", help="在知识库全文检索")
    p.add_argument("--q", required=True, help="关键词")
    p.add_argument("--context", type=int, default=3, help="每个文件最多返回几行上下文")
    p.add_argument("--limit", type=int, default=30, help="最多返回几个文件")
    p.add_argument("--subtitles", action="store_true",
                   help="同时检索 cache/subtitles 下的字幕文本")

    sub.add_parser("stats", help="知识库统计")

    p = sub.add_parser("create-folder", help="在B站创建收藏夹(幂等,同名复用)")
    p.add_argument("--title", required=True)
    p.add_argument("--intro", default="")
    p.add_argument("--privacy", type=int, choices=[0, 1], default=1,
                   help="0=公开 1=私密(默认)")

    p = sub.add_parser("aid", help="解析 bvid 对应的 aid")
    p.add_argument("--bvid", required=True)

    p = sub.add_parser("plan-classify", help="按本地分类生成同步回B站的移动计划(只读)")
    p.add_argument("--out", help="计划输出路径(默认 cache/classify_plan.json)")

    p = sub.add_parser("apply-plan", help="按计划在B站移动视频到分类收藏夹")
    p.add_argument("--plan", required=True, help="计划JSON文件路径")
    p.add_argument("--apply", action="store_true", help="真正执行(默认仅预览)")
    p.add_argument("--batch-size", type=int, default=10)
    p.add_argument("--delay", type=float, default=DELAY_DEFAULT)
    p.add_argument("--continue-on-error", action="store_true")

    sub.add_parser("report", help="输出同步进度摘要(供规划用)")

    p = sub.add_parser("safe-name", help="清洗文件名")
    p.add_argument("--text", required=True)

    args = ap.parse_args()
    data_dir = os.path.abspath(args.data_dir)
    os.makedirs(data_dir, exist_ok=True)
    try:
        HANDLERS[args.cmd](args, data_dir)
    except SystemExit:
        raise
    except Exception as e:
        fail(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
