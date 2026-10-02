"""端到端流程测试。

    python -m unittest discover -s tests -v

刻意不依赖网络：解析器用内联的 HTML 片段测，提醒判定用临时数据库测。
抓取部分只在 ``cli.py doctor`` 和 ``cli.py scrape`` 里真正联网，
免得测试因为对方机房抖动就变红。
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

# 必须在 import ruankao 之前把环境变量摆好——config 在导入时就会读取它们
_TMP = tempfile.mkdtemp(prefix="ruankao-test-")
os.environ.update(
    DB_PATH=str(pathlib.Path(_TMP) / "test.db"),
    DRY_RUN="true",
    MAIL_ENABLED="false",
    SCHEDULER_ENABLED="false",
    ADMIN_TOKEN="test-admin-token",
    SITE_URL="http://test.local",
    SECRET_KEY="test-secret-key",
    MAIL_FROM="test@example.com",
    MAIL_HOST="smtp.example.com",
    MAIL_USER="test@example.com",
    MAIL_PASS="x",
    ALERT_EMAIL="",
)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ruankao import create_app, scraper, scheduler, services  # noqa: E402
from ruankao.db import get_db  # noqa: E402
from ruankao.security import reset_rate_limits  # noqa: E402
from ruankao.util import iso, new_token, now  # noqa: E402

FIXTURE_HTML = """
<html><body>
<select name="time">
  <option selected="selected" value="2607ac">2026年下半年软考</option>
  <option value="2603ac">2026年上半年软考</option>
</select>
<div class="contentShow"><div class="contentShowIn">
  <div class="layui-row listCont selectorg">
    <div class="layui-col-md3 col1">北京</div>
    <div class="layui-col-md4 col2 timeW">2026-09-11 00:00 ~ 2026-09-17 23:59</div>
    <div class="layui-col-md4 col2 timeW">2026-09-11 00:00 ~ 2026-09-17 23:59</div>
    <div class="layui-col-md1 col2"><a class="aLink" task-id="T1" or-id="O1">进入</a></div>
  </div>
  <div class="layui-row listCont selectorg">
    <div class="layui-col-md3 col1">大连</div>
    <div class="layui-col-md4 col2 timeW">2026-08-14 09:00 ~ 2026-09-18 17:00</div>
    <div class="layui-col-md4 col2 timeW">2026-08-14 09:00 ~ 2026-09-22 17:00</div>
    <div class="layui-col-md1 col2"><a class="aLink" task-id="T1" or-id="O2">进入</a></div>
  </div>
  <div class="layui-row listCont selectorg">
    <div class="layui-col-md3 col1">香港</div>
    <div class="layui-col-md4 col2 timeW">2026-09-01 00:00 ~ 2026-09-18 00:00</div>
    <div class="layui-col-md4 col2 timeW"> ~ </div>
    <div class="layui-col-md1 col2"><a class="aLink" task-id="T1" or-id="O3">进入</a></div>
  </div>
</div></div>
</body></html>
"""


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.result = scraper.parse(FIXTURE_HTML)

    def test_batch_detected(self):
        self.assertEqual(self.result.batch, "2026年下半年")

    def test_rows_parsed(self):
        self.assertEqual(len(self.result.windows), 3)

    def test_unknown_names_is_empty(self):
        self.assertEqual(self.result.unknown_names, [])

    def test_plan_single_city_is_distinct(self):
        """大连是计划单列市，不能和辽宁混为一谈。"""
        codes = {w.region_code for w in self.result.windows}
        self.assertIn("dalian", codes)
        self.assertNotIn("liaoning", codes)

    def test_hongkong_display_name(self):
        hk = next(w for w in self.result.windows if w.region_code == "hongkong")
        self.assertEqual(hk.region_name, "中国香港")

    def test_empty_pay_window_does_not_crash(self):
        """官方在缴费时间为空时只写一个孤零零的 ~，必须容错。"""
        hk = next(w for w in self.result.windows if w.region_code == "hongkong")
        self.assertIsNone(hk.pay_start)
        self.assertIsNone(hk.pay_end)
        self.assertEqual(hk.sign_start, "2026-09-01 00:00")

    def test_deep_link_built(self):
        bj = next(w for w in self.result.windows if w.region_code == "beijing")
        self.assertIn("changeOrg", bj.entry_url)
        self.assertIn("orid=O1", bj.entry_url)

    def test_single_time_cell_triggers_warning(self):
        broken = FIXTURE_HTML.replace(
            '<div class="layui-col-md4 col2 timeW"> ~ </div>', ""
        )
        result = scraper.parse(broken)
        self.assertTrue(any("时间单元格" in w for w in result.warnings))

    def test_missing_rows_raises(self):
        with self.assertRaises(scraper.ScrapeError):
            scraper.parse("<html><body>页面改版了</body></html>")


class PipelineTests(unittest.TestCase):
    """订阅 → 确认 → 提醒判定 → 回执 → 退订 的完整链路。"""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.conn = get_db()
        for table in ("notify_log", "feedback", "subscriptions", "subscribers",
                      "exam_windows"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()
        reset_rate_limits()

    def tearDown(self):
        self.ctx.pop()

    # -- 辅助 ------------------------------------------------------------

    def _make_active(self, email="a@example.com", regions=("guangdong",), channel="email"):
        cur = self.conn.execute(
            """INSERT INTO subscribers
               (email, channel, plan, status, access_token, unsubscribe_token, created_at, verified_at)
               VALUES (?, ?, 'free', 'active', ?, ?, ?, ?)""",
            (email, channel, new_token(20), new_token(20), iso(), iso()),
        )
        sid = cur.lastrowid
        for code in regions:
            self.conn.execute(
                "INSERT INTO subscriptions (subscriber_id, region_code, created_at) VALUES (?, ?, ?)",
                (sid, code, iso()),
            )
        self.conn.commit()
        return sid

    def _add_window(self, *, batch="测试批次", region="guangdong", start=None, end=None,
                    pay_start=None, pay_end=None, fresh=True):
        stamp = iso(now() + timedelta(minutes=5)) if fresh else iso(now() - timedelta(days=30))
        self.conn.execute(
            """INSERT INTO exam_windows
               (exam_type, batch, region_code, official_name, sign_start, sign_end,
                pay_start, pay_end, entry_url, source_url, scraped_at, raw_hash)
               VALUES ('ruankao', ?, ?, '广东', ?, ?, ?, ?, NULL, ?, ?, ?)""",
            (batch, region, start, end, pay_start, pay_end,
             scraper.SOURCE_URL, stamp, new_token(8)),
        )
        self.conn.commit()

    def _iso(self, **delta):
        return (now() + timedelta(**delta)).strftime("%Y-%m-%d %H:%M:%S")

    # -- 订阅链路 --------------------------------------------------------

    def test_subscribe_requires_verification(self):
        info = services.subscribe("new@example.com", ["guangdong"], "127.0.0.1")
        self.assertTrue(info["is_new"])
        row = self.conn.execute(
            "SELECT * FROM subscribers WHERE email = 'new@example.com'"
        ).fetchone()
        # 关键：未确认前不能是 active，否则等于任何人都能用别人邮箱订阅
        self.assertEqual(row["status"], "pending")
        self.assertTrue(row["login_token"])
        self.assertEqual(services.regions_of(row["id"]), [])

    def test_magic_link_activates_and_applies_regions(self):
        services.subscribe("v@example.com", ["guangdong", "zhejiang"], "127.0.0.1")
        token = self.conn.execute(
            "SELECT login_token FROM subscribers WHERE email = 'v@example.com'"
        ).fetchone()["login_token"]

        info = services.consume_login_token(token)
        self.assertTrue(info["just_activated"])

        row = self.conn.execute(
            "SELECT * FROM subscribers WHERE email = 'v@example.com'"
        ).fetchone()
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["login_token"])
        self.assertEqual(sorted(services.regions_of(row["id"])), ["guangdong", "zhejiang"])

    def test_magic_link_is_single_use(self):
        services.subscribe("once@example.com", ["guangdong"], "127.0.0.1")
        token = self.conn.execute(
            "SELECT login_token FROM subscribers WHERE email = 'once@example.com'"
        ).fetchone()["login_token"]
        services.consume_login_token(token)
        with self.assertRaises(services.ServiceError):
            services.consume_login_token(token)

    def test_change_does_not_affect_existing_subscription_before_confirm(self):
        """别人拿你的邮箱改了地区，在你点确认之前，你的原订阅必须原封不动。"""
        services.subscribe("keep@example.com", ["guangdong"], "127.0.0.1")
        token = self.conn.execute(
            "SELECT login_token FROM subscribers WHERE email = 'keep@example.com'"
        ).fetchone()["login_token"]
        services.consume_login_token(token)
        sid = self.conn.execute(
            "SELECT id FROM subscribers WHERE email = 'keep@example.com'"
        ).fetchone()["id"]

        services.subscribe("keep@example.com", ["beijing"], "1.2.3.4")
        self.assertEqual(services.regions_of(sid), ["guangdong"])
        status = self.conn.execute(
            "SELECT status FROM subscribers WHERE id = ?", (sid,)
        ).fetchone()["status"]
        self.assertEqual(status, "active")

    def test_invalid_email_rejected(self):
        for bad in ("", "  ", "nope", "a@b", "@example.com"):
            with self.assertRaises(services.ServiceError):
                services.subscribe(bad, ["guangdong"], "127.0.0.1")

    def test_empty_regions_rejected(self):
        with self.assertRaises(services.ServiceError):
            services.subscribe("x@example.com", [], "127.0.0.1")

    # -- 提醒判定 --------------------------------------------------------

    def test_open_window_notifies(self):
        self._make_active()
        self._add_window(start=self._iso(hours=-2), end=self._iso(days=5),
                         pay_start=self._iso(hours=-2), pay_end=self._iso(days=6))
        kinds = {p.kind for p in scheduler.collect_pending(self.conn)}
        self.assertEqual(kinds, {"open"})

    def test_closing_window_notifies(self):
        self._make_active()
        self._add_window(start=self._iso(days=-10), end=self._iso(hours=20),
                         pay_end=self._iso(days=3))
        kinds = {p.kind for p in scheduler.collect_pending(self.conn)}
        self.assertEqual(kinds, {"closing"})

    def test_pay_closing_beats_closing(self):
        """两个节点同时命中时只发最紧急的那封，不能连发两封把人烦到退订。"""
        self._make_active()
        self._add_window(start=self._iso(days=-10), end=self._iso(hours=20),
                         pay_end=self._iso(hours=10))
        pending = scheduler.collect_pending(self.conn)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].kind, "pay_closing")

    def test_published_window_notifies(self):
        self._make_active()
        self._add_window(start=self._iso(days=5), end=self._iso(days=15))
        kinds = {p.kind for p in scheduler.collect_pending(self.conn)}
        self.assertEqual(kinds, {"published"})

    def test_far_future_window_is_silent(self):
        self._make_active()
        self._add_window(start=self._iso(days=120), end=self._iso(days=130))
        self.assertEqual(scheduler.collect_pending(self.conn), [])

    def test_expired_batch_is_silent(self):
        """历史批次绝不能触发通知，否则新用户一订阅就收到过期消息。"""
        self._make_active()
        self._add_window(start=self._iso(days=-60), end=self._iso(days=-50),
                         pay_end=self._iso(days=-49))
        self.assertEqual(scheduler.collect_pending(self.conn), [])

    def test_unrelated_region_is_silent(self):
        self._make_active(regions=("zhejiang",))
        self._add_window(region="guangdong", start=self._iso(hours=-2),
                         end=self._iso(days=5), pay_end=self._iso(days=6))
        self.assertEqual(scheduler.collect_pending(self.conn), [])

    def test_paused_subscriber_is_silent(self):
        sid = self._make_active()
        services.set_status(sid, "paused")
        self._add_window(start=self._iso(hours=-2), end=self._iso(days=5),
                         pay_end=self._iso(days=6))
        self.assertEqual(scheduler.collect_pending(self.conn), [])

    # -- 发送与去重 ------------------------------------------------------

    def test_send_records_and_does_not_repeat(self):
        self._make_active()
        self._add_window(start=self._iso(hours=-2), end=self._iso(days=5),
                         pay_end=self._iso(days=6))

        first = scheduler.run_notify_job()
        self.assertEqual(first["sent"], 1)

        # 再跑一次不能重复发送——这是避免「每天收到同一封邮件」的关键
        second = scheduler.run_notify_job()
        self.assertEqual(second["sent"], 0)

        logged = self.conn.execute("SELECT COUNT(*) AS c FROM notify_log").fetchone()["c"]
        self.assertEqual(logged, 1)

    def test_registered_suppresses_signup_but_not_payment(self):
        """报了名就别再催报名，但**缴费提醒必须留着**——这是最后一道保险。"""
        sid = self._make_active()
        self._add_window(start=self._iso(days=-10), end=self._iso(hours=20),
                         pay_end=self._iso(hours=10))
        window_id = self.conn.execute("SELECT id FROM exam_windows").fetchone()["id"]
        services.record_feedback(sid, window_id, "registered")

        pending = scheduler.collect_pending(self.conn)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].kind, "pay_closing")

    def test_registered_still_suppresses_closing_only_window(self):
        sid = self._make_active()
        self._add_window(start=self._iso(days=-10), end=self._iso(hours=20),
                         pay_end=self._iso(days=3))
        window_id = self.conn.execute("SELECT id FROM exam_windows").fetchone()["id"]
        services.record_feedback(sid, window_id, "registered")
        self.assertEqual(scheduler.collect_pending(self.conn), [])

    def test_paid_suppresses_payment_reminder(self):
        sid = self._make_active()
        self._add_window(start=self._iso(days=-10), end=self._iso(hours=20),
                         pay_end=self._iso(hours=10))
        window_id = self.conn.execute("SELECT id FROM exam_windows").fetchone()["id"]
        services.record_feedback(sid, window_id, "paid")
        self.assertEqual(scheduler.collect_pending(self.conn), [])

    def test_skip_silences_everything(self):
        sid = self._make_active()
        self._add_window(start=self._iso(hours=-2), end=self._iso(days=5),
                         pay_end=self._iso(days=6))
        window_id = self.conn.execute("SELECT id FROM exam_windows").fetchone()["id"]
        services.record_feedback(sid, window_id, "skip")
        self.assertEqual(scheduler.collect_pending(self.conn), [])

    def test_unsubscribe_stops_notifications_but_keeps_record(self):
        sid = self._make_active()
        services.unsubscribe(sid)
        self._add_window(start=self._iso(hours=-2), end=self._iso(days=5),
                         pay_end=self._iso(days=6))
        self.assertEqual(scheduler.collect_pending(self.conn), [])
        # 取消订阅只停发提醒：记录保留，状态标记为 unsubscribed
        row = self.conn.execute(
            "SELECT status FROM subscribers WHERE id = ?", (sid,)
        ).fetchone()
        self.assertEqual(row["status"], "unsubscribed")


class AdminPageTests(unittest.TestCase):
    """后台页面必须真的能渲染。

    模板里的 ``url_for`` 端点名写错（例如 ``admin.run_all`` vs
    ``admin.manual_run_all``）会让整个页面 500，而任何业务断言都不会碰到它——
    只有真请求一次才暴露。这个 500 在线上真实发生过。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.client = cls.app.test_client()
        resp = cls.client.post("/admin/login", data={"token": "test-admin-token"})
        assert resp.status_code == 302, resp.status_code

    def test_dashboard_renders(self):
        resp = self.client.get("/admin/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("手动操作", resp.get_data(as_text=True))

    def test_subscribers_page_renders(self):
        resp = self.client.get("/admin/subscribers")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("订阅数据", resp.get_data(as_text=True))

    def test_subscribers_region_filter_and_page_clamp(self):
        """地区过滤要真的生效；页码越界收敛到最后一页，而不是空页或 500。"""
        from ruankao.db import get_db
        from ruankao.util import iso, new_token

        with self.app.app_context():
            conn = get_db()
            ids = []
            for email, code in (("gd@example.com", "guangdong"), ("zj@example.com", "zhejiang")):
                cur = conn.execute(
                    """INSERT INTO subscribers
                       (email, channel, plan, status, access_token, unsubscribe_token,
                        created_at, verified_at)
                       VALUES (?, 'email', 'free', 'active', ?, ?, ?, ?)""",
                    (email, new_token(20), new_token(20), iso(), iso()),
                )
                ids.append(cur.lastrowid)
                conn.execute(
                    """INSERT INTO subscriptions (subscriber_id, region_code, created_at)
                       VALUES (?, ?, ?)""",
                    (cur.lastrowid, code, iso()),
                )
            conn.commit()
            try:
                body = self.client.get("/admin/subscribers?region=guangdong").get_data(as_text=True)
                self.assertIn("gd@example.com", body)
                self.assertNotIn("zj@example.com", body)

                resp = self.client.get("/admin/subscribers?page=999")
                self.assertEqual(resp.status_code, 200)
            finally:
                conn.execute("DELETE FROM subscriptions WHERE subscriber_id IN (?, ?)", ids)
                conn.execute("DELETE FROM subscribers WHERE id IN (?, ?)", ids)
                conn.commit()


if __name__ == "__main__":
    unittest.main(verbosity=2)
