# -*- coding: utf-8 -*-
"""bili.py 离线冒烟测试: 不访问网络, 通过 mock urllib 模拟 B 站接口。

运行: python -B -m unittest discover -s tests -v   (在插件根目录)
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_HERE, "..", "scripts", "bili.py")
_spec = importlib.util.spec_from_file_location("bili", _SCRIPT)
bili = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bili)


def run_cmd(name, data_dir, **kw):
    ns = argparse.Namespace(**kw)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bili.HANDLERS[name](ns, data_dir)
    return json.loads(buf.getvalue())


def readf(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class BiliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = self._tmp.name

    def cache(self, *parts):
        return os.path.join(self.data_dir, "cache", *parts)

    def library(self, *parts):
        return os.path.join(self.data_dir, "library", *parts)


class TestUtils(BiliTestCase):
    def test_safe_name_clean(self):
        got = bili.safe_name_text('a<b>c:d"e/f\\g|h?i*j')
        self.assertEqual(got, "a b c d e f g h i j")

    def test_safe_name_nfkc(self):
        got = bili.safe_name_text("ＡＢＣ１２３：全角")
        self.assertEqual(got, "ABC123 全角")

    def test_safe_name_empty(self):
        self.assertEqual(bili.safe_name_text("..."), "untitled")

    def test_focus_hit(self):
        self.assertTrue(bili.focus_hit("STM32点灯教程", ["stm32"]))
        self.assertFalse(bili.focus_hit("美食探店", ["stm32"]))

    def test_wbi_sign_shape(self):
        params = bili.wbi_sign({"bvid": "BV1xx", "cid": 123}, "a" * 32)
        self.assertIn("wts", params)
        self.assertIn("w_rid", params)
        self.assertEqual(len(params["w_rid"]), 32)

    def test_mixin_key_length(self):
        self.assertEqual(len(bili.mixin_key_of("a" * 32, "b" * 32)), 32)

    def test_http_get_retries_on_412(self):
        responses = [
            json.dumps({"code": -412, "message": "风控"}).encode(),
            json.dumps({"code": 0, "data": {}}).encode(),
        ]

        class FakeResp:
            def __init__(self, data):
                self._d = data

            def read(self):
                return self._d

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        calls = []

        def fake_urlopen(req, timeout=25):
            calls.append(req.full_url)
            return FakeResp(responses[len(calls) - 1])

        with mock.patch.object(bili.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(bili.time, "sleep"):
            d = bili.http_get("https://api.test/", params={"a": 1}, cookie="")
        self.assertEqual(d["code"], 0)
        self.assertEqual(len(calls), 2)

    def test_split_subdomains(self):
        self.assertEqual(bili.split_subdomains("如 STM32/模电、SolidWorks"),
                         ["STM32", "模电", "SolidWorks"])


class TestLibraryScan(BiliTestCase):
    def test_library_done_bvids_only_scans_category_dir(self):
        write(self.library("分类", "电子", "card.md"),
              "# 卡片\n- BV号: BV1111111111\n")
        write(self.library("博主", "某UP", "视频列表.md"),
              "- [视频](https://www.bilibili.com/video/BV2222222222)\n")
        write(self.library("思维导图", "知识点索引.md"),
              "- [x](https://www.bilibili.com/video/BV3333333333)\n")
        done = bili.library_done_bvids(self.data_dir)
        self.assertIn("BV1111111111", done)
        self.assertNotIn("BV2222222222", done)  # 博主视频列表不算已建档
        self.assertNotIn("BV3333333333", done)  # 索引链接不算已建档
        # 缓存命中
        done2 = bili.library_done_bvids(self.data_dir)
        self.assertEqual(done, done2)

    def test_card_files_excludes_list_md(self):
        write(self.library("分类", "电子", "card.md"), "# 卡片\n")
        write(self.library("分类", "电子", "列表.md"), "- [x](y) | u | 2026-01-01 | BV1\n")
        files = [os.path.basename(p) for p in bili.card_files(self.data_dir)]
        self.assertEqual(files, ["card.md"])


class TestPending(BiliTestCase):
    def setUp(self):
        super().setUp()
        write(self.cache("fav_videos.json"), json.dumps({
            "synced_at": "2026-01-01 00:00:00", "uid": 1, "count": 3,
            "videos": {
                "BV1done000001": {"bvid": "BV1done000001", "title": "已完成", "fav_time": 100},
                "BV1rem0000001": {"bvid": "BV1rem0000001", "title": "已移除",
                                  "fav_time": 200, "removed": True},
                "BV1new0000001": {"bvid": "BV1new0000001", "title": "待建档", "fav_time": 300},
            }}, ensure_ascii=False))
        write(self.cache("progress.json"), json.dumps(
            {"videos_done": {"BV1done000001": {"title": "已完成"}}}, ensure_ascii=False))

    def test_pending_skips_done_and_removed(self):
        out = run_cmd("pending", self.data_dir, kind="videos", limit=10,
                      offset=0)
        self.assertTrue(out["ok"])
        self.assertEqual([v["bvid"] for v in out["videos"]], ["BV1new0000001"])
        self.assertEqual(out["removed_total"], 1)
        self.assertEqual(out["pending_total"], 1)


class TestSyncFavorites(BiliTestCase):
    def setUp(self):
        super().setUp()
        write(os.path.join(self.data_dir, "config.json"),
              json.dumps({"sessdata": "test-sess"}, ensure_ascii=False))

    def test_prunes_unfavorited_videos(self):
        write(self.cache("fav_videos.json"), json.dumps({
            "synced_at": "2026-01-01 00:00:00", "uid": 1, "count": 2,
            "videos": {
                "BV1000000001": {"bvid": "BV1000000001", "type": 2,
                                 "title": "旧视频X", "folder_ids": [100]},
                "BV1000000002": {"bvid": "BV1000000002", "type": 2,
                                 "title": "视频Y", "folder_ids": [100]},
            }}, ensure_ascii=False))

        def fake(url, params=None, cookie="", referer=bili.REFERER, retries=3):
            if "nav" in url:
                return {"code": 0, "data": {"isLogin": True, "mid": 1}}
            if "list-all" in url:
                return {"code": 0, "data": {"list": [
                    {"id": 100, "title": "A", "media_count": 1},
                    {"id": 200, "title": "B", "media_count": 0}]}}
            if "resource/list" in url:
                if params["media_id"] == 100:
                    return {"code": 0, "data": {"has_more": False, "medias": [
                        {"id": "BV1000000002", "type": 2, "title": "视频Y",
                         "upper": {"name": "U", "mid": 9}, "duration": 10,
                         "fav_time": 1700000000, "pubtime": 1690000000,
                         "attr": 0, "intro": "", "cover": "",
                         "cnt_info": {"play": 1, "collect": 0}}]}}
                return {"code": 0, "data": {"has_more": False, "medias": []}}
            return {"code": -1, "message": "unexpected"}

        with mock.patch.object(bili, "http_get", side_effect=fake), \
             mock.patch.object(bili, "rate_sleep"):
            out = run_cmd("sync-favorites", self.data_dir, folder=None,
                          no_prune=False, delay=0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["removed_this_run"], 1)
        saved = json.loads(readf(self.cache("fav_videos.json")))
        self.assertTrue(saved["videos"]["BV1000000001"].get("removed"))
        self.assertNotIn("removed", saved["videos"]["BV1000000002"])

    def test_no_prune_flag_keeps_old_entries(self):
        write(self.cache("fav_videos.json"), json.dumps({
            "videos": {
                "BV1000000001": {"bvid": "BV1000000001", "type": 2,
                                 "title": "旧视频X", "folder_ids": [100]},
            }}, ensure_ascii=False))

        def fake(url, params=None, cookie="", referer=bili.REFERER, retries=3):
            if "nav" in url:
                return {"code": 0, "data": {"isLogin": True, "mid": 1}}
            if "list-all" in url:
                return {"code": 0, "data": {"list": [
                    {"id": 100, "title": "A", "media_count": 0}]}}
            if "resource/list" in url:
                return {"code": 0, "data": {"has_more": False, "medias": []}}
            return {"code": -1, "message": "unexpected"}

        with mock.patch.object(bili, "http_get", side_effect=fake), \
             mock.patch.object(bili, "rate_sleep"):
            out = run_cmd("sync-favorites", self.data_dir, folder=None,
                          no_prune=True, delay=0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["removed_this_run"], 0)


class TestReview(BiliTestCase):
    def setUp(self):
        super().setUp()
        write(self.library("分类", "电子", "card.md"),
              "# 测试视频\n- BV号: BV1xx0000001\n- 学习状态: 未学\n- 下次复习:\n")

    def test_review_lists_due(self):
        out = run_cmd("review", self.data_dir, all=False, set=False,
                      bvid=None, status=None, next=None)
        self.assertTrue(out["ok"])
        self.assertEqual(out["due_count"], 1)
        self.assertEqual(out["cards"][0]["bvid"], "BV1xx0000001")

    def test_review_set_status(self):
        out = run_cmd("review", self.data_dir, all=False, set=True,
                      bvid="BV1xx0000001", status="已学", next="2026-09-01")
        self.assertTrue(out["ok"])
        self.assertTrue(out["updated"])
        text = readf(self.library("分类", "电子", "card.md"))
        self.assertIn("- 学习状态: 已学", text)
        self.assertIn("- 下次复习: 2026-09-01", text)


class TestExportAnki(BiliTestCase):
    def test_export_anki_rows(self):
        write(self.library("分类", "电子", "card.md"),
              "# 测试视频\n- BV号: BV1xx0000002\n"
              "- 知识点标签: #GPIO #STM32\n\n"
              "## 核心知识点\n"
              "1. **GPIO配置**：设置方向与上下拉\n"
              "2. **复用功能**：映射到外设\n")
        out_path = os.path.join(self._tmp.name, "out.csv")
        out = run_cmd("export-anki", self.data_dir, out=out_path, domain=None)
        self.assertTrue(out["ok"])
        self.assertEqual(out["flashcards"], 2)
        with open(out_path, encoding="utf-8-sig") as fh:
            rows = list(csv_rows(fh))
        self.assertEqual(rows[0], ["Front", "Back", "Tags"])
        self.assertIn("GPIO配置", rows[1][0])
        self.assertIn("GPIO", rows[1][2])


def csv_rows(fh):
    import csv as _csv
    return _csv.reader(fh)


class TestBuildIndex(BiliTestCase):
    def test_build_index_generates_files(self):
        write(self.library("分类", "电子", "gpio.md"),
              "# GPIO点灯入门\n- BV号: BV1xx0000003\n"
              "- UP主: 江科嵌入式 | 时长: 12:00\n"
              "- 领域: 电子 | 子领域: STM32/单片机\n"
              "- 知识点标签: #GPIO #点灯\n\n"
              "## 内容概要\nx\n")
        out = run_cmd("build-index", self.data_dir)
        self.assertTrue(out["ok"])
        idx = readf(self.library("思维导图", "知识点索引.md"))
        self.assertIn("## GPIO", idx)
        self.assertIn("江科嵌入式", idx)
        self.assertIn("GPIO点灯入门", idx)  # 卡片标题
        self.assertIn("../分类/电子/gpio.md", idx)
        mm = readf(self.library("思维导图", "知识地图.md"))
        self.assertIn("mindmap", mm)
        self.assertIn("电子", mm)  # 领域节点
        self.assertIn("STM32", mm)  # 子领域节点
        self.assertIn("GPIO", mm)  # 知识点节点
        self.assertIn("GPIO点灯入门", mm)  # 叶节点标题


class TestSearchStats(BiliTestCase):
    def setUp(self):
        super().setUp()
        write(self.library("分类", "电子", "card.md"),
              "# 运放入门\n- BV号: BV1yy0000001\n"
              "- 领域: 电子 | 子领域: 模电\n"
              "- 学习状态: 已学\n- 下次复习: 2026-08-01\n\n## 内容概要\n运放虚短虚断\n")

    def test_search_finds_keyword(self):
        out = run_cmd("search", self.data_dir, q="运放", context=3, limit=30,
                      subtitles=False)
        self.assertTrue(out["ok"])
        self.assertGreaterEqual(out["total"], 1)
        first = out["hits"][0]
        self.assertTrue(any("运放" in m["text"] for m in first["matches"]),
                        f"命中内容应包含关键词, got={first['matches']}")

    def test_stats_counts_status(self):
        out = run_cmd("stats", self.data_dir)
        self.assertTrue(out["ok"])
        self.assertEqual(out["library"]["learning_status"]["已学"], 1)


class TestSettingsMigration(BiliTestCase):
    def test_focus_migrates_from_config_to_settings(self):
        write(os.path.join(self.data_dir, "config.json"), json.dumps(
            {"sessdata": "abc", "focus": {"categories": ["电子"]}}, ensure_ascii=False))
        focus = bili.get_focus(self.data_dir)
        self.assertEqual(focus["categories"], ["电子"])
        settings = json.loads(readf(os.path.join(self.data_dir, "settings.json")))
        self.assertEqual(settings["focus"]["categories"], ["电子"])
        cfg = json.loads(readf(os.path.join(self.data_dir, "config.json")))
        self.assertNotIn("focus", cfg)


class TestPartialSyncNoPrune(BiliTestCase):
    """--folder 部分同步时不得把其他收藏夹的视频误标为 removed。"""

    def test_folder_filter_does_not_prune(self):
        write(os.path.join(self.data_dir, "config.json"),
              json.dumps({"sessdata": "test-sess"}, ensure_ascii=False))
        write(self.cache("fav_videos.json"), json.dumps({
            "videos": {
                "BV1000000001": {"bvid": "BV1000000001", "type": 2,
                                 "title": "旧视频X", "folder_ids": [200]},
            }}, ensure_ascii=False))

        def fake(url, params=None, cookie="", referer=bili.REFERER, retries=3):
            if "nav" in url:
                return {"code": 0, "data": {"isLogin": True, "mid": 1}}
            if "list-all" in url:
                return {"code": 0, "data": {"list": [
                    {"id": 100, "title": "A", "media_count": 0},
                    {"id": 200, "title": "B", "media_count": 0}]}}
            if "resource/list" in url:
                return {"code": 0, "data": {"has_more": False, "medias": []}}
            return {"code": -1, "message": "unexpected"}

        with mock.patch.object(bili, "http_get", side_effect=fake), \
             mock.patch.object(bili, "rate_sleep"):
            out = run_cmd("sync-favorites", self.data_dir, folder=100,
                          no_prune=False, delay=0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["removed_this_run"], 0)
        saved = json.loads(readf(self.cache("fav_videos.json")))
        self.assertNotIn("removed", saved["videos"]["BV1000000001"])


class TestPlanClassify(BiliTestCase):
    def test_generates_plan(self):
        write(self.cache("fav_folders.json"), json.dumps(
            {"folders": [{"id": 100, "title": "电子"}]}, ensure_ascii=False))
        write(self.cache("fav_videos.json"), json.dumps({
            "videos": {
                "BV1000000001": {"bvid": "BV1000000001", "type": 2,
                                 "title": "旧视频X", "folder_ids": [200]},
                "BV1000000002": {"bvid": "BV1000000002", "type": 2,
                                 "title": "已移除", "folder_ids": [200],
                                 "removed": True},
            }}, ensure_ascii=False))
        write(self.library("分类", "电子", "card.md"),
              "# 视频X\n- BV号: BV1000000001\n")
        write(self.library("分类", "机械", "列表.md"),
              "- [视频Y](https://www.bilibili.com/video/BV1000000003) | U | "
              "2026-01-01 | BV1000000003\n")
        out = run_cmd("plan-classify", self.data_dir,
                      out=os.path.join(self._tmp.name, "plan.json"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["total_moves"], 1)  # X: 200 -> 100(电子)
        self.assertEqual(out["new_folders_needed"], ["机械"])  # 机械夹需要新建
        self.assertEqual(out["untracked_videos"], 1)  # 列表行里的 Y 无缓存
        plan = json.loads(readf(os.path.join(self._tmp.name, "plan.json")))
        self.assertEqual(plan[0]["bvid"], "BV1000000001")
        self.assertEqual(plan[0]["to"], 100)


class TestReviewRelativeDate(BiliTestCase):
    def test_next_relative_date(self):
        write(self.library("分类", "电子", "card.md"),
              "# 测试视频\n- BV号: BV1xx0000001\n- 学习状态: 未学\n- 下次复习:\n")
        out = run_cmd("review", self.data_dir, all=False, set=True,
                      bvid="BV1xx0000001", status="已学", next="+7d")
        self.assertTrue(out["ok"])
        self.assertIsNotNone(out["next"])
        text = readf(self.library("分类", "电子", "card.md"))
        self.assertRegex(text, r"- 下次复习: \d{4}-\d{2}-\d{2}")

    def test_next_invalid_rejected(self):
        write(self.library("分类", "电子", "card.md"),
              "# 测试视频\n- BV号: BV1xx0000002\n- 学习状态: 未学\n- 下次复习:\n")
        with self.assertRaises(SystemExit):
            run_cmd("review", self.data_dir, all=False, set=True,
                    bvid="BV1xx0000002", status="已学", next="下周二")


class TestExportAnkiDomain(BiliTestCase):
    def test_domain_filter_matches_field_not_path(self):
        write(self.library("分类", "电子", "e.md"),
              "# 电子卡\n- BV号: BV1xx0000010\n- 领域: 电子 | 子领域: 模电\n"
              "- 知识点标签: #运放\n\n## 核心知识点\n1. **虚短**：x\n")
        write(self.library("分类", "机械", "m.md"),
              "# 机械卡\n- BV号: BV1xx0000011\n- 领域: 机械 | 子领域: CAD\n"
              "- 知识点标签: #草图\n\n## 核心知识点\n1. **草图**：x\n")
        out = run_cmd("export-anki", self.data_dir,
                      out=os.path.join(self._tmp.name, "o.csv"), domain="电子")
        self.assertEqual(out["cards"], 1)
        self.assertEqual(out["flashcards"], 1)


class TestSearchTitle(BiliTestCase):
    def test_title_from_heading(self):
        write(self.library("分类", "电子", "card.md"),
              "# 运放入门\n- BV号: BV1zz0000001\n\n## 内容概要\n虚短虚断\n")
        out = run_cmd("search", self.data_dir, q="虚短", context=3, limit=30,
                      subtitles=False)
        self.assertEqual(out["hits"][0]["title"], "运放入门")


class TestRenderMindmap(BiliTestCase):
    def test_render_png_and_svg(self):
        try:
            import matplotlib  # noqa: F401
        except Exception:
            self.skipTest("matplotlib 未安装")
        write(self.library("分类", "电子", "e.md"),
              "# 运放虚短\n- BV号: BV1xx0000099\n- UP主: 硬芯研究所 | 时长: 1:00\n"
              "- 领域: 电子 | 子领域: 模电\n- 知识点标签: #运放\n\n## 内容概要\nx\n")
        out_png = os.path.join(self._tmp.name, "m.png")
        out = run_cmd("render-mindmap", self.data_dir, out=out_png,
                      dpi=80, max_videos=3, exclude=[], engineering=False,
                      html=False)
        self.assertTrue(out["ok"])
        self.assertTrue(os.path.exists(out_png))
        self.assertTrue(os.path.exists(os.path.splitext(out_png)[0] + ".svg"))

    def test_render_legacy_flat_cards(self):
        try:
            import matplotlib  # noqa: F401
        except Exception:
            self.skipTest("matplotlib 未安装")
        # 旧格式: 分类嵌在 UP主 行, 全角冒号, 无标签
        write(self.library("分类", "健身与运动", "a.md"),
              "# 深蹲教学\n- BV号：BV1xx0000088\n"
              "- UP主：健身教练 | 时长：5:00 | 分类：健身与运动\n"
              "- 收藏于：2026-01-01 | 原收藏夹：默认收藏夹\n")
        out_png = os.path.join(self._tmp.name, "flat.png")
        out = run_cmd("render-mindmap", self.data_dir, out=out_png,
                      dpi=80, max_videos=3, exclude=[], engineering=False,
                      html=False)
        self.assertTrue(out["ok"])
        self.assertEqual(out["domains"], ["健身与运动"])
        self.assertEqual(out["videos"], 1)
        self.assertTrue(os.path.exists(out_png))

    def test_render_engineering_html(self):
        try:
            import matplotlib  # noqa: F401
        except Exception:
            self.skipTest("matplotlib 未安装")
        write(self.library("分类", "编程与科技", "a.md"),
              "# STM32点灯教程\n- BV号：BV1xx0000077\n"
              "- UP主：AI电子工坊 | 时长：5:00 | 分类：编程与科技\n"
              "## 内容总结\n\n> GPIO 控制的入门讲解。\n")
        write(self.library("分类", "健身与运动", "b.md"),
              "# 深蹲教学\n- BV号：BV1xx0000066\n"
              "- UP主：健身教练 | 时长：5:00 | 分类：健身与运动\n")
        out_png = os.path.join(self._tmp.name, "eng.png")
        out = run_cmd("render-mindmap", self.data_dir, out=out_png,
                      dpi=80, max_videos=3, exclude=[], engineering=True,
                      html=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["videos"], 1)  # 只有工科卡片
        self.assertEqual(out["domains"], ["编程与科技"])
        self.assertTrue(out["html"])
        html_text = readf(out["html"])
        self.assertIn('"root": {"name"', html_text)  # 数据包了 root 层
        self.assertIn("STM32点灯教程", html_text)
        self.assertIn("BV1xx0000077", html_text)
        self.assertIn("GPIO 控制的入门讲解", html_text)  # 内容总结进入简介
        self.assertIn('"dur": "5:00"', html_text)  # 时长被解析


class TestAnalyze(BiliTestCase):
    def test_classify_engineering(self):
        self.assertEqual(bili.classify_engineering("STM32点灯教程"), "嵌入式/单片机")
        self.assertEqual(bili.classify_engineering("Python数据分析实战"), "编程语言")
        self.assertIsNone(bili.classify_engineering("深蹲教学"))

    def test_is_teaching(self):
        self.assertTrue(bili.is_teaching("STM32入门教程"))
        self.assertFalse(bili.is_teaching("每周科技速报"))

    def test_summary_strips_boilerplate(self):
        p = os.path.join(self.library("分类", "编程与科技", "c.md"))
        write(p, "# 测试\n- 领域: 编程与科技 | 子领域: 无\n"
                 "## 内容总结\n\n> 根据标题与简介推断，仅供参考。 这是真实总结内容。\n")
        meta = bili.parse_card_full(p)
        self.assertEqual(meta["summary"], "这是真实总结内容。")

    def test_analyze_counts(self):
        write(self.library("分类", "编程与科技", "a.md"),
              "# STM32点灯教程\n- BV号：BV1xx0000011\n"
              "- UP主：AI电子工坊 | 时长：5:00 | 分类：编程与科技\n")
        write(self.library("分类", "编程与科技", "b.md"),
              "# 周报\n- BV号：BV1xx0000012\n"
              "- UP主：某UP | 时长：5:00 | 分类：编程与科技\n")
        out = run_cmd("analyze", self.data_dir, top=0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["total_cards"], 2)
        self.assertEqual(out["engineering_cards"], 1)
        self.assertEqual(out["by_topic"]["嵌入式/单片机"], 1)


if __name__ == "__main__":
    unittest.main()
