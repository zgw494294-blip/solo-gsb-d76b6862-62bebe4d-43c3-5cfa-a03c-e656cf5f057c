"""站内页面可达性分析（POST /v1/site-reachability）核心。

给定 Base64 WARC、首页 HTTP(S) URL 与 RFC3339 时点，从首页出发沿 ``a[href]``
链接广度优先遍历站内页面，计算归档中哪些站内页面可以从首页点击到达：

- WARC 解码、2 MiB 上限、结构/revisit 引用链校验与
  ``allow_missing_target_uri`` 容忍完全复用 :mod:`app.replay`，与
  /v1/navigate 一致；
- 每个 URL 的时点选取（截至时点最新、同刻取归档后者）与重定向跟随
  （301/302/303/307/308、相对 Location、8 跳上限、成环检测）完全复用
  :func:`app.navigate._follow_redirects`；
- **节点**为终点 URL 与首页终点**同源**、记录自身 HTTP 状态为 **2xx**、且
  HTTP ``Content-Type`` 的媒体类型为 ``text/html`` 的页面；链接只从节点的
  **还原正文**（revisit 已还原为链终点 response 的正文）中提取；
- 提取 ``a[href]``，相对地址按**首个有效** ``<base href>`` 解析（base 规则
  与 /v1/page-assets 相同：可相对页面解析、结果必须是 HTTP(S) 绝对地址），
  去掉片段，忽略跨源与非 HTTP(S) 链接；
- 逐层广度优先，每层页面按终点 URL 字典序处理，已遇到的 URL 去重防环；
  子链接的任何失败（无抓取、重定向异常、终点跨源/非 2xx/非 HTML）都只
  跳过该链接，不影响其余遍历；
- 首页（根）无合格抓取 → **404**；根重定向异常、终点非 2xx 或媒体类型非
  ``text/html`` → **422**。

响应 ``reachable`` 按遍历顺序给出每个节点的终点 URL、记录 ID、自首页起的
**最短点击次数**（首页为 0，重定向不计点击）与**前驱页面 URL**（发现该
页面的链接所在节点的终点 URL，首页为 null）；``unreachable`` 给出归档中
同源、时点前有可见抓取、但遍历从未遇到的 URL 及其时点选取记录的 ID，按
URL 字典序。
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlsplit

from .navigate import (
    _build_index,
    _follow_redirects,
    _http_url,
    _parse_rfc3339,
    _select,
)
from .page_assets import _resolve_http_url
from .replay import ReplayError, ResolvedRecord, load_records


def _origin(url: str) -> tuple[str, str, int]:
    """URL 的 Web 源 ``(scheme, hostname, port)``；缺省端口按 80/443 补。

    scheme/hostname 统一小写；非法端口（如 ``http://a:bad/``）兜底为缺省
    端口，避免遍历中抛出 ValueError。
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, parts.hostname or "", port


def _media_type(content_type: Optional[str]) -> Optional[str]:
    """HTTP Content-Type 的媒体类型：去参数、去空白、小写；缺失为 None。"""
    if not content_type:
        return None
    media = content_type.split(";", 1)[0].strip().lower()
    return media or None


def _is_html_ok(rec: ResolvedRecord) -> bool:
    """终点记录自身状态为 2xx 且媒体类型为 text/html（同源由调用方判）。"""
    status = rec.http_status
    if status is None or not 200 <= status < 300:
        return False
    return _media_type(rec.http_content_type) == "text/html"


class _LinkHTMLParser(HTMLParser):
    """收集 ``<base href>`` 候选与 ``<a href>`` 链接（均为原始字面值）。

    HTMLParser 会把标签名与属性名统一成小写；同一属性重复出现时只取首次
    值（与浏览器一致）。``<base>`` 的 href 按文档顺序全部保留，由调用方
    选出首个有效者；自闭合标签经默认 ``handle_startendtag`` 进入
    ``handle_starttag``，无需单独处理。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.base_hrefs: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str,
                        attrs: list[tuple[str, Optional[str]]]) -> None:
        values: dict[str, Optional[str]] = {}
        for name, value in attrs:
            if name not in values:
                values[name] = value

        if tag == "base":
            href = values.get("href")
            if href:
                self.base_hrefs.append(href)
        elif tag == "a":
            href = values.get("href")
            if href and href.strip():
                self.links.append(href)


def _page_links(body: bytes, page_url: str,
                origin: tuple[str, str, int]) -> list[str]:
    """从节点还原正文提取同源链接目标，按 URL 字典序去重。

    相对地址按首个有效 ``<base href>`` 解析（规则同 /v1/page-assets），
    解析结果去片段、必须为 HTTP(S) 绝对地址且与首页终点同源；其余
    （``mailto:``、``javascript:``、跨源等）一律忽略。
    """
    html = body.decode("utf-8", errors="replace")
    parser = _LinkHTMLParser()
    parser.feed(html)
    parser.close()

    base_url = page_url
    for raw_base in parser.base_hrefs:
        resolved_base = _resolve_http_url(raw_base, page_url)
        if resolved_base is not None:
            base_url = resolved_base
            break

    links: set[str] = set()
    for raw in parser.links:
        link = _resolve_http_url(raw, base_url)
        if link is not None and _origin(link) == origin:
            links.add(link)
    return sorted(links)


def _mark_path_seen(seen: set[str], path: list[dict]) -> None:
    """把一次重定向跟随经过的各跳 URL 记入 seen（含终点）。"""
    for hop in path:
        url = hop.get("url")
        if isinstance(url, str):
            seen.add(url)


def site_reachability(warc_b64: str, start_url: str, at: str) -> dict:
    """site-reachability 入口：返回 reachable/unreachable；错误抛 ReplayError。"""
    url = _http_url(start_url)
    point_in_time = _parse_rfc3339(at)

    records = load_records(warc_b64, allow_missing_target_uri=True)
    index = _build_index(records)

    # 根页面：与 /v1/navigate 完全相同的选取与重定向跟随；无抓取 404、
    # 重定向异常 422 原样透传。
    path, root_final, root_rec = _follow_redirects(index, url, point_in_time,
                                                   at)

    # 根终点必须 2xx 且媒体类型为 text/html，否则 422（不检查跨源：站点
    # 源就定义为首页终点的源，http→https 等跨源重定向后以终点为准）。
    status = root_rec.http_status
    if status is None or not 200 <= status < 300:
        raise ReplayError(
            "root_status_not_2xx",
            f"首页终点 {root_final} 的 HTTP 状态为 "
            f"{status if status is not None else '缺失'}，不是 2xx，"
            "无法作为站内遍历的根页面",
            url=root_final,
            record_id=root_rec.record_id_raw,
            http_status=status,
            path=path,
        )
    if _media_type(root_rec.http_content_type) != "text/html":
        raise ReplayError(
            "root_not_html",
            f"首页终点 {root_final} 的 Content-Type 媒体类型不是 text/html",
            url=root_final,
            record_id=root_rec.record_id_raw,
            content_type=root_rec.http_content_type,
            path=path,
        )

    origin = _origin(root_final)

    # 已遇到的 URL：种子、各次解析经过（含失败）的重定向跳、已入队链接。
    # 既用于去重防环，也用于判定归档 URL 是否“从未被遍历遇到”。
    seen: set[str] = {url}
    _mark_path_seen(seen, path)

    root_entry = {
        "url": root_final,
        "record_id": root_rec.record_id_raw,
        "clicks": 0,
        "from": None,
    }
    reachable: list[dict] = [root_entry]
    reachable_urls = {root_final}

    # 当前点击层的节点：(终点 URL, 终点记录)。首页为第 0 层。
    frontier: list[tuple[str, ResolvedRecord]] = [(root_final, root_rec)]
    clicks = 0
    while frontier:
        # 本层新发现的节点：(终点 URL, 终点记录, 前驱页面 URL)。
        new_nodes: list[tuple[str, ResolvedRecord, str]] = []
        level_finals: set[str] = set()

        # 每层节点按终点 URL 字典序处理（“按 URL 排序广度优先”）；
        # 因此多个页面链接同一目标时，URL 最小的前驱胜出。
        for page_url, page_rec in sorted(frontier, key=lambda item: item[0]):
            for link in _page_links(page_rec.body, page_url, origin):
                if link in seen:
                    continue
                seen.add(link)

                # 子链接的选取/重定向失败只跳过该链接（404/422 均吞掉）。
                try:
                    hop_path, final_url, rec = _follow_redirects(
                        index, link, point_in_time, at
                    )
                except ReplayError as exc:
                    _mark_path_seen(seen, exc.detail.get("path", []))
                    for key in ("url", "next_url"):
                        extra = exc.detail.get(key)
                        if isinstance(extra, str):
                            seen.add(extra)
                    continue
                _mark_path_seen(seen, hop_path)

                # 终点已在更浅层或本层更早发现 → 不重复计节点（BFS 保证
                # 首次发现即最短点击）。
                if final_url in reachable_urls or final_url in level_finals:
                    continue
                # 同源重定向到跨源、非 2xx、非 text/html：子链接失败跳过。
                if _origin(final_url) != origin or not _is_html_ok(rec):
                    continue

                level_finals.add(final_url)
                new_nodes.append((final_url, rec, page_url))

        # 本层节点按终点 URL 字典序输出并作为下一层的遍历顺序。
        new_nodes.sort(key=lambda item: item[0])
        next_frontier: list[tuple[str, ResolvedRecord]] = []
        for final_url, rec, parent in new_nodes:
            reachable_urls.add(final_url)
            reachable.append({
                "url": final_url,
                "record_id": rec.record_id_raw,
                "clicks": clicks + 1,
                "from": parent,
            })
            next_frontier.append((final_url, rec))

        frontier = next_frontier
        clicks += 1

    # 不可达：归档中同源、截至时点有可见抓取、但遍历从未遇到的 URL。
    # 记录 ID 即该 URL 在时点下选中的抓取（同刻取归档后者），不额外跟随
    # 重定向或判断其状态/类型。
    unreachable: list[dict] = []
    for candidate in sorted(index):
        if candidate in seen or candidate in reachable_urls:
            continue
        if _origin(candidate) != origin:
            continue
        rec = _select(index, candidate, point_in_time)
        if rec is None:
            continue
        unreachable.append({"url": candidate,
                            "record_id": rec.record_id_raw})

    return {
        "final_url": root_final,
        "reachable": reachable,
        "unreachable": unreachable,
    }
