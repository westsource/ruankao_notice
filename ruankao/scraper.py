"""抓取并解析官方报名平台的机构时间表。

数据源
------
https://bm.ruankao.org.cn/sign/welcome

该页面是**服务端渲染的静态 HTML**（约 25KB），无需登录、无需浏览器、
无接口加密，一个纯 GET 就能拿到全量数据。页面结构（2026-10 实测）::

    <select>
      <option selected="selected" value="2607...">2026年下半年软考</option>
      ...
    </select>
    ...
    <div class="layui-row listCont selectorg">
      <div class="layui-col-md3 col1">北京</div>                            机构名
      <div class="layui-col-md4 col2 timeW">2026-09-11 00:00 ~ 2026-09-17 23:59</div>  报名
      <div class="layui-col-md4 col2 timeW">2026-09-11 00:00 ~ 2026-09-17 23:59</div>  缴费
      <div class="layui-col-md1 col2">
        <a class="aLink" task-id="..." or-id="00171128112006000002">进入</a>
      </div>
    </div>

设计取舍
--------
报名与缴费两个单元格的 class **完全相同**，只能靠行内顺序区分。所以这里
不按列下标取值，而是收集全部 ``.timeW`` 后按顺序解释；一旦某天官方把缴费
列去掉，行内 ``.timeW`` 个数会从 2 变成 1，我们据此记 warning 而不是
错位写库——错位写库会让用户收到完全错误的报名时间，这是最坏的失败模式。

入口链接
--------
页面上 ``<a href="javascript:;">`` 不是真实地址，真实跳转由脚本完成::

    POST /sign/setselectorg {orid, taskid}  ->  window.location = /sign/in

因此我们生成 ``/sign/changeOrg?orid=..&taskid=..`` 作为「本省直达」链接，
同时始终把官方首页 ``/sign/welcome`` 作为更稳妥的备选入口一并存下。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import requests
from bs4 import BeautifulSoup

from . import regions as regions_mod
from .util import iso, parse_dt

SOURCE_URL = "https://bm.ruankao.org.cn/sign/welcome"
OFFICIAL_HOME = "https://bm.ruankao.org.cn/sign/welcome"
DEEP_LINK_TEMPLATE = "https://bm.ruankao.org.cn/sign/changeOrg?orid={orid}&taskid={taskid}"

EXAM_TYPE = "ruankao"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 ruankao-notice/0.1"
)

_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# 官方在报名时间未公布时会填「未定」「待定」等占位词
_PLACEHOLDER = {"未定", "待定", "暂无", "无", "-", "—", "/"}


class ScrapeError(RuntimeError):
    """抓取或解析失败。抛出它的目的是让调度器发管理员告警，而不是静默跳过。"""


@dataclass
class ScrapedWindow:
    official_name: str
    region_code: str
    region_name: str
    sign_start: str | None
    sign_end: str | None
    pay_start: str | None
    pay_end: str | None
    entry_url: str | None
    official_home: str = OFFICIAL_HOME

    def fingerprint(self) -> str:
        payload = "|".join(
            [
                self.region_code,
                self.sign_start or "",
                self.sign_end or "",
                self.pay_start or "",
                self.pay_end or "",
                self.entry_url or "",
            ]
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:40]

    @property
    def has_sign_window(self) -> bool:
        return bool(self.sign_start or self.sign_end)


@dataclass
class ScrapeResult:
    batch: str
    windows: list[ScrapedWindow] = field(default_factory=list)
    unknown_names: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_url: str = SOURCE_URL

    @property
    def ok(self) -> bool:
        return bool(self.windows)


def fetch(url: str = SOURCE_URL, timeout: int = 25) -> str:
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
        )
        resp.raise_for_status()
    except requests.RequestException as exc:  # 网络层错误统一转成 ScrapeError
        raise ScrapeError(f"请求官方页面失败：{exc}") from exc

    raw = resp.content
    head = raw[:2048].lower()
    if b"charset=gb" in head:
        return raw.decode("gb18030", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value or value in _PLACEHOLDER:
        return None
    return value


def _split_range(text: str | None) -> tuple[str | None, str | None]:
    """把「2026-09-11 00:00 ~ 2026-09-17 23:59」拆成起止两段。

    官方在缴费时间为空时只写一个孤零零的 ``~``，所以要允许任一端缺失。
    """
    if not text:
        return None, None
    parts = re.split(r"[~～]", text, maxsplit=1)
    if len(parts) == 2:
        return _clean(parts[0]), _clean(parts[1])
    return _clean(parts[0]), None


def _normalize_batch(label: str) -> str:
    label = (label or "").strip()
    label = re.sub(r"\s*软考\s*$", "", label).strip()
    return label


def _derive_batch(windows: list[ScrapedWindow]) -> str:
    """批次标签解析失败时，从报名起始日期反推。"""
    years: dict[int, int] = {}
    months: dict[int, int] = {}
    for w in windows:
        dt = parse_dt(w.sign_start)
        if dt:
            years[dt.year] = years.get(dt.year, 0) + 1
            months[dt.month] = months.get(dt.month, 0) + 1
    if not years:
        return "未知批次"
    year = max(years, key=lambda k: years[k])
    month = max(months, key=lambda k: months[k]) if months else 1
    half = "下半年" if month >= 7 else "上半年"
    return f"{year}年{half}"


def _parse_batch(soup: BeautifulSoup) -> str | None:
    """当前批次由 <option selected> 标记。"""
    for select in soup.find_all("select"):
        options = select.find_all("option")
        if not options:
            continue
        if not any("软考" in o.get_text() for o in options):
            continue
        # 不依赖 BS4 对 selected 属性的归一化，直接看原始属性表
        chosen = next((o for o in options if "selected" in o.attrs), None) or options[0]
        return _normalize_batch(chosen.get_text(strip=True))
    return None


def parse(html: str) -> ScrapeResult:
    soup = BeautifulSoup(html, "html.parser")

    rows = soup.select("div.listCont.selectorg")
    if not rows:
        # 兜底：万一官方改了外层 class，用容器内的行再试一次
        container = soup.select_one(".contentShowIn")
        if container:
            rows = [d for d in container.find_all("div", recursive=False) if d.select(".timeW")]
    if not rows:
        raise ScrapeError(
            "页面上找不到任何机构行（div.listCont.selectorg）。"
            "官方很可能改版了，需要更新解析规则。"
        )

    windows: list[ScrapedWindow] = []
    unknown: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()

    for row in rows:
        name_cell = row.select_one(".col1")
        if not name_cell:
            warnings.append("有一行缺少机构名称单元格，已跳过")
            continue
        official_name = name_cell.get_text(strip=True)

        region = regions_mod.match_official(official_name)
        if region is None:
            unknown.append(official_name)
            continue
        if region.code in seen:
            warnings.append(f"机构「{official_name}」重复出现，只保留首次")
            continue
        seen.add(region.code)

        cells = row.select(".timeW")
        if len(cells) != 2:
            warnings.append(
                f"机构「{official_name}」本应有 2 个时间单元格，实际 {len(cells)} 个，"
                "报名/缴费可能存在错位风险"
            )
        sign_text = cells[0].get_text(" ", strip=True) if len(cells) > 0 else ""
        pay_text = cells[1].get_text(" ", strip=True) if len(cells) > 1 else ""

        sign_start, sign_end = _split_range(sign_text)
        pay_start, pay_end = _split_range(pay_text)

        for label, value in (
            ("报名开始", sign_start),
            ("报名截止", sign_end),
            ("缴费开始", pay_start),
            ("缴费截止", pay_end),
        ):
            if value and not _TIME_RE.search(value):
                warnings.append(f"机构「{official_name}」的{label}无法解析成日期：{value!r}")

        link = row.select_one("a.aLink")
        entry_url = None
        if link is not None:
            orid = (link.get("or-id") or "").strip()
            taskid = (link.get("task-id") or "").strip()
            if orid and taskid:
                entry_url = DEEP_LINK_TEMPLATE.format(orid=orid, taskid=taskid)

        windows.append(
            ScrapedWindow(
                official_name=official_name,
                region_code=region.code,
                region_name=region.name,
                sign_start=sign_start,
                sign_end=sign_end,
                pay_start=pay_start,
                pay_end=pay_end,
                entry_url=entry_url,
            )
        )

    batch = _parse_batch(soup) or _derive_batch(windows)

    if len(windows) < 30:
        warnings.append(
            f"仅解析出 {len(windows)} 个机构，明显少于预期的 35 个，请人工核对官方页面"
        )

    return ScrapeResult(batch=batch, windows=windows, unknown_names=unknown, warnings=warnings)


def scrape(url: str = SOURCE_URL) -> ScrapeResult:
    return parse(fetch(url))


# ---------------------------------------------------------------------------
# 落库与变化检测
# ---------------------------------------------------------------------------

@dataclass
class StoredChange:
    window_id: int
    region_code: str
    region_name: str
    kind: str  # 'new' | 'updated'
    old: dict[str, Any] | None
    new: dict[str, Any]


def persist(conn: sqlite3.Connection, result: ScrapeResult, scraped_at: str | None = None) -> list[StoredChange]:
    """把抓取结果 upsert 进 exam_windows，并返回本次真正发生变化的窗口。

    变化检测是这套系统的生存底线：没有它，用户每天都会收到同一封邮件，
    第三天就全退订了。
    """
    stamp = scraped_at or iso()
    changes: list[StoredChange] = []

    for w in result.windows:
        fingerprint = w.fingerprint()
        existing = conn.execute(
            """SELECT id, sign_start, sign_end, pay_start, pay_end, entry_url, raw_hash
               FROM exam_windows
               WHERE exam_type = ? AND batch = ? AND region_code = ?""",
            (EXAM_TYPE, result.batch, w.region_code),
        ).fetchone()

        if existing is None:
            cur = conn.execute(
                """INSERT INTO exam_windows
                   (exam_type, batch, region_code, official_name, sign_start, sign_end,
                    pay_start, pay_end, entry_url, source_url, scraped_at, raw_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    EXAM_TYPE, result.batch, w.region_code, w.official_name,
                    w.sign_start, w.sign_end, w.pay_start, w.pay_end,
                    w.entry_url, result.source_url, stamp, fingerprint,
                ),
            )
            changes.append(
                StoredChange(cur.lastrowid, w.region_code, w.region_name, "new", None, _as_dict(w))
            )
        elif existing["raw_hash"] != fingerprint:
            conn.execute(
                """UPDATE exam_windows SET
                       official_name = ?, sign_start = ?, sign_end = ?,
                       pay_start = ?, pay_end = ?, entry_url = ?,
                       source_url = ?, scraped_at = ?, raw_hash = ?
                   WHERE id = ?""",
                (
                    w.official_name, w.sign_start, w.sign_end, w.pay_start, w.pay_end,
                    w.entry_url, result.source_url, stamp, fingerprint,
                    existing["id"],
                ),
            )
            changes.append(
                StoredChange(
                    existing["id"], w.region_code, w.region_name, "updated",
                    dict(existing), _as_dict(w),
                )
            )
        else:
            # 内容没变，只刷新抓取时间
            conn.execute(
                "UPDATE exam_windows SET scraped_at = ? WHERE id = ?", (stamp, existing["id"])
            )

    conn.commit()
    return changes


def _as_dict(w: ScrapedWindow) -> dict[str, Any]:
    return {
        "region_code": w.region_code,
        "region_name": w.region_name,
        "sign_start": w.sign_start,
        "sign_end": w.sign_end,
        "pay_start": w.pay_start,
        "pay_end": w.pay_end,
        "entry_url": w.entry_url,
    }


def record_run(
    conn: sqlite3.Connection,
    *,
    started_at: str,
    ok: bool,
    batch: str | None = None,
    rows: int = 0,
    changed: int = 0,
    error: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO scrape_runs (started_at, finished_at, ok, batch, rows_parsed, changed, error)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (started_at, iso(), 1 if ok else 0, batch, rows, changed, error),
    )
    conn.commit()
    return cur.lastrowid


def latest_batch(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT batch FROM exam_windows WHERE exam_type = ? ORDER BY scraped_at DESC LIMIT 1",
        (EXAM_TYPE,),
    ).fetchone()
    return row["batch"] if row else None
