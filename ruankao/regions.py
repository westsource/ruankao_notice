"""机构（报考地区）字典。

官方报名平台把全国报名单位拆成 35 个互相独立的机构，其中：
- 大连、宁波是计划单列市，独立于辽宁、浙江单独组织报名
- 新疆兵团独立于新疆
- 中国香港独立

字典里的两个名字用途不同，不要混用：
- ``official``：官方页面上的写法，仅用于和抓取结果做匹配
- ``name``：本站对外展示的写法

抓取到的机构名如果不在字典里，会被记录下来并触发管理员告警，
而不是静默丢弃——官方一旦新增机构，我们要第一时间知道。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    code: str
    name: str
    official: str
    group: str
    order: int

    @property
    def label(self) -> str:
        return self.name


REGIONS: tuple[Region, ...] = (
    # 华北
    Region("beijing", "北京", "北京", "华北", 1010),
    Region("tianjin", "天津", "天津", "华北", 1020),
    Region("hebei", "河北", "河北", "华北", 1030),
    Region("shanxi", "山西", "山西", "华北", 1040),
    Region("neimenggu", "内蒙古", "内蒙古", "华北", 1050),
    # 东北
    Region("liaoning", "辽宁", "辽宁", "东北", 2010),
    Region("jilin", "吉林", "吉林", "东北", 2020),
    Region("heilongjiang", "黑龙江", "黑龙江", "东北", 2030),
    Region("dalian", "大连", "大连", "东北", 2040),
    # 华东
    Region("shanghai", "上海", "上海", "华东", 3010),
    Region("jiangsu", "江苏", "江苏", "华东", 3020),
    Region("zhejiang", "浙江", "浙江", "华东", 3030),
    Region("ningbo", "宁波", "宁波", "华东", 3040),
    Region("anhui", "安徽", "安徽", "华东", 3050),
    Region("fujian", "福建", "福建", "华东", 3060),
    Region("jiangxi", "江西", "江西", "华东", 3070),
    Region("shandong", "山东", "山东", "华东", 3080),
    # 华中
    Region("henan", "河南", "河南", "华中", 4010),
    Region("hubei", "湖北", "湖北", "华中", 4020),
    Region("hunan", "湖南", "湖南", "华中", 4030),
    # 华南
    Region("guangdong", "广东", "广东", "华南", 5010),
    Region("guangxi", "广西", "广西", "华南", 5020),
    Region("hainan", "海南", "海南", "华南", 5030),
    # 西南
    Region("chongqing", "重庆", "重庆", "西南", 6010),
    Region("sichuan", "四川", "四川", "西南", 6020),
    Region("guizhou", "贵州", "贵州", "西南", 6030),
    Region("yunnan", "云南", "云南", "西南", 6040),
    Region("xizang", "西藏", "西藏", "西南", 6050),
    # 西北
    Region("shaanxi", "陕西", "陕西", "西北", 7010),
    Region("gansu", "甘肃", "甘肃", "西北", 7020),
    Region("qinghai", "青海", "青海", "西北", 7030),
    Region("ningxia", "宁夏", "宁夏", "西北", 7040),
    Region("xinjiang", "新疆", "新疆", "西北", 7050),
    Region("xinjiangbt", "新疆兵团", "新疆兵团", "西北", 7060),
    # 港澳
    Region("hongkong", "中国香港", "香港", "港澳", 8010),
)

GROUP_ORDER: tuple[str, ...] = ("华北", "东北", "华东", "华中", "华南", "西南", "西北", "港澳")

BY_CODE: dict[str, Region] = {r.code: r for r in REGIONS}
# 官方写法 -> Region。注意"新疆"与"新疆兵团"前缀相同，必须先长后短匹配。
BY_OFFICIAL: dict[str, Region] = {r.official: r for r in REGIONS}


def match_official(name: str) -> Region | None:
    """把官方页面上的机构名匹配到 Region。

    精确匹配优先；失败时按名称长度倒序做前缀匹配，避免
    「新疆兵团」被误判成「新疆」。
    """
    name = (name or "").strip()
    if not name:
        return None
    if name in BY_OFFICIAL:
        return BY_OFFICIAL[name]
    for region in sorted(REGIONS, key=lambda r: -len(r.official)):
        if name.startswith(region.official) or region.official.startswith(name):
            return region
    return None


def grouped() -> list[tuple[str, list[Region]]]:
    """按大区分组，供页面渲染选择器。"""
    buckets: dict[str, list[Region]] = {g: [] for g in GROUP_ORDER}
    for region in sorted(REGIONS, key=lambda r: r.order):
        buckets[region.group].append(region)
    return [(g, buckets[g]) for g in GROUP_ORDER if buckets[g]]
