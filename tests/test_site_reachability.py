"""/v1/site-reachability 端到端测试：BFS 遍历、同源/类型判定、最短点击、
前驱页面、不可达集合与根/子链接错误场景。"""

from __future__ import annotations

import base64
import gzip
import io

from fastapi.testclient import TestClient
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import BufferWARCWriter

from app.main import app
from app.replay import MAX_DECODED_BYTES

client = TestClient(app)


# ---------- WARC 构造辅助 ----------


def _make_response(w, rec_id, uri, date, status_code=200, reason="OK",
                   body=b"", location=None, content_type=None,
                   with_http=True, rec_type="response", refers_to=None):
    headers = []
    if with_http:
        if location is not None:
            headers.append(("Location", location))
        if content_type is not None:
            headers.append(("Content-Type", content_type))
        headers.append(("Content-Length", str(len(body))))
        http = StatusAndHeaders(f"{status_code} {reason}", headers,
                                protocol="HTTP/1.1")
    else:
        http = None
    wh = {"WARC-Date": date, "WARC-Record-ID": rec_id}
    if refers_to is not None:
        wh["WARC-Refers-To"] = refers_to
    return w.create_warc_record(
        uri, rec_type, http_headers=http, payload=io.BytesIO(body),
        warc_headers_dict=wh,
    )


def build_warc(fn, gzip_=False):
    w = BufferWARCWriter(gzip=gzip_)
    fn(w)
    return w.get_contents()


def raw_record(rec_id="<urn:uuid:r>", rec_type="response", block=b"",
               extra_headers=b"", date="2020-01-01T00:00:00Z",
               uri="http://a/"):
    uri_line = f"WARC-Target-URI: {uri}\r\n".encode() if uri is not None else b""
    return (
        f"WARC/1.1\r\nWARC-Type: {rec_type}\r\n"
        f"WARC-Record-ID: {rec_id}\r\nWARC-Date: {date}\r\n".encode()
        + uri_line
        + f"Content-Length: {len(block)}\r\n".encode()
        + extra_headers + b"\r\n" + block + b"\r\n\r\n"
    )


def http_block(status_line="HTTP/1.1 200 OK", headers=None, body=b""):
    h = status_line.encode() + b"\r\n"
    for k, v in (headers or []):
        h += f"{k}: {v}\r\n".encode()
    return h + b"\r\n" + body


def html_page(*links, base=None, extra=b""):
    """构造含 a[href] 链接的 HTML 正文；links 为 href 字面值列表。"""
    parts = [b"<html><head>"]
    if base is not None:
        parts.append(f'<base href="{base}">'.encode())
    parts.append(b"</head><body>")
    for href in links:
        parts.append(f'<a href="{href}">x</a>'.encode())
    parts.append(extra)
    parts.append(b"</body></html>")
    return b"".join(parts)


def site_warc(pages, root_url="http://a/", root_id="<urn:uuid:root>",
              root_date="2021-01-01T00:00:00Z", root_body=None, root_kw=None):
    """根页面 + 其余记录的完整 WARC；pages 为 (rec_id, url, kw) 列表。"""
    def fill(w):
        kw = {"date": root_date, "content_type": "text/html"}
        kw.update(root_kw or {})
        w.write_record(_make_response(w, root_id, root_url,
                                      body=root_body or b"<html></html>",
                                      **kw))
        for rec_id, url, kw2 in pages:
            w.write_record(_make_response(w, rec_id, url, **kw2))
    return build_warc(fill)


def sr(warc_bytes, url, at, **kw):
    return client.post(
        "/v1/site-reachability",
        json={"warc": base64.b64encode(warc_bytes).decode(), "url": url,
              "at": at},
        **kw,
    )


def sr_b64(warc_b64, url, at, **kw):
    return client.post("/v1/site-reachability",
                       json={"warc": warc_b64, "url": url, "at": at}, **kw)


AT = "2021-06-01T00:00:00Z"
D = "2021-01-01T00:00:00Z"
HTML = {"date": D, "content_type": "text/html"}


def reachable_by_url(d):
    return {p["url"]: p for p in d["reachable"]}


# ---------- 正常路径：BFS、最短点击、前驱、不可达 ----------


def test_basic_bfs_clicks_predecessor_and_unreachable():
    # 根链接 /b、/a（乱序）；/a 再链接 /c；/orphan 在归档中但无人链接。
    root = html_page("/b", "/a")
    warc = site_warc([
        ("<urn:uuid:a>", "http://a/a", {**HTML, "body": html_page("/c")}),
        ("<urn:uuid:b>", "http://a/b", {**HTML}),
        ("<urn:uuid:c>", "http://a/c", {**HTML}),
        ("<urn:uuid:orphan>", "http://a/orphan", {**HTML}),
    ], root_body=root)
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["final_url"] == "http://a/"
    # 遍历顺序：根（0 点击），第 1 层按 URL 字典序 /a、/b，第 2 层 /c
    assert [p["url"] for p in d["reachable"]] == [
        "http://a/", "http://a/a", "http://a/b", "http://a/c"]
    pages = reachable_by_url(d)
    assert pages["http://a/"]["record_id"] == "<urn:uuid:root>"
    assert pages["http://a/"]["clicks"] == 0
    assert pages["http://a/"]["from"] is None
    assert pages["http://a/a"]["clicks"] == 1
    assert pages["http://a/a"]["from"] == "http://a/"
    assert pages["http://a/b"]["clicks"] == 1
    assert pages["http://a/c"]["clicks"] == 2
    assert pages["http://a/c"]["from"] == "http://a/a"
    assert pages["http://a/c"]["record_id"] == "<urn:uuid:c>"
    # 不可达：归档中同源但遍历未遇到的 URL，按 URL 字典序
    assert d["unreachable"] == [
        {"url": "http://a/orphan", "record_id": "<urn:uuid:orphan>"}]


def test_shortest_clicks_and_first_sorted_predecessor():
    # 根链接 /b 与 /a；/a、/b 都链接 /c：/c 点击数为 2，
    # 前驱为同层按 URL 排序先处理到的 /a。
    root = html_page("/b", "/a")
    warc = site_warc([
        ("<urn:uuid:a>", "http://a/a", {**HTML, "body": html_page("/c")}),
        ("<urn:uuid:b>", "http://a/b", {**HTML, "body": html_page("/c")}),
        ("<urn:uuid:c>", "http://a/c", {**HTML}),
    ], root_body=root)
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    pages = reachable_by_url(r.json())
    assert pages["http://a/c"]["clicks"] == 2
    assert pages["http://a/c"]["from"] == "http://a/a"


def test_redirected_link_node_is_terminal_hop_not_unreachable():
    # 根链接 /old → 301 → /new（HTML）：/new 以 1 次点击可达；
    # 重定向跳 /old 已遇到，不算不可达。
    root = html_page("/old")
    warc = site_warc([
        ("<urn:uuid:old>", "http://a/old",
         {"date": D, "status_code": 301, "reason": "Moved",
          "location": "/new"}),
        ("<urn:uuid:new>", "http://a/new", {**HTML}),
    ], root_body=root)
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    pages = reachable_by_url(d)
    assert pages["http://a/new"]["record_id"] == "<urn:uuid:new>"
    assert pages["http://a/new"]["clicks"] == 1
    assert pages["http://a/new"]["from"] == "http://a/"
    assert d["unreachable"] == []


def test_unlinked_redirect_capture_is_unreachable():
    # 归档里另有一条未被链接的 301：遍历从未遇到 → 不可达，记录 ID 为
    # 该 URL 时点选取的抓取（不额外跟随重定向）。
    root = html_page("/new")
    warc = site_warc([
        ("<urn:uuid:new>", "http://a/new", {**HTML}),
        ("<urn:uuid:old301>", "http://a/old",
         {"date": D, "status_code": 301, "reason": "Moved",
          "location": "/new"}),
    ], root_body=root)
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert [p["url"] for p in d["reachable"]] == [
        "http://a/", "http://a/new"]
    assert d["unreachable"] == [
        {"url": "http://a/old", "record_id": "<urn:uuid:old301>"}]


def test_cross_origin_ignored_everywhere():
    # 跨源链接被忽略；归档中的跨源抓取也不算不可达。
    root = html_page("http://other/x", "https://a/secure")
    warc = site_warc([
        ("<urn:uuid:ox>", "http://other/x", {**HTML}),
        ("<urn:uuid:oy>", "http://other/y", {**HTML}),
        ("<urn:uuid:sec>", "https://a/secure", {**HTML}),
    ], root_body=root)
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert [p["url"] for p in d["reachable"]] == ["http://a/"]
    assert d["unreachable"] == []


def test_non_html_and_non_2xx_children_skipped_but_seen():
    # 被链接的图片（200 image/png）与 404 页面：不是节点，跳过；
    # 因已被遍历遇到，不列入不可达。未被链接的图片则列入不可达。
    root = html_page("/img.png", "/gone")
    warc = site_warc([
        ("<urn:uuid:img>", "http://a/img.png",
         {"date": D, "content_type": "image/png", "body": b"PNG"}),
        ("<urn:uuid:gone>", "http://a/gone",
         {"date": D, "status_code": 404, "reason": "Not Found",
          "content_type": "text/html", "body": b"nf"}),
        ("<urn:uuid:img2>", "http://a/img2.png",
         {"date": D, "content_type": "image/png", "body": b"PNG2"}),
    ], root_body=root)
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert [p["url"] for p in d["reachable"]] == ["http://a/"]
    assert d["unreachable"] == [
        {"url": "http://a/img2.png", "record_id": "<urn:uuid:img2>"}]


def test_missing_content_type_child_skipped():
    root = html_page("/noct")
    warc = site_warc([
        ("<urn:uuid:noct>", "http://a/noct", {"date": D, "body": b"x"}),
    ], root_body=root)
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert [p["url"] for p in d["reachable"]] == ["http://a/"]
    assert d["unreachable"] == []  # 已遇到，不算不可达


def test_content_type_with_parameters_and_case():
    root = html_page("/p")
    warc = site_warc([
        ("<urn:uuid:p>", "http://a/p",
         {"date": D, "content_type": "Text/HTML; charset=utf-8",
          "body": html_page()}),
    ], root_body=root)
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    assert [p["url"] for p in r.json()["reachable"]] == [
        "http://a/", "http://a/p"]


def test_base_href_first_valid_and_relative_links():
    # 首个 base 无效（非 HTTP(S)），用第二个相对 base（相对页面解析）。
    root = (b'<html><head><base href="mailto:x@y">'
            b'<base href="/dir/"><base href="/other/"></head>'
            b'<body><a href="page.html">x</a></body></html>')
    warc = site_warc([
        ("<urn:uuid:p>", "http://a/dir/page.html", {**HTML}),
    ], root_body=root)
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    assert [p["url"] for p in r.json()["reachable"]] == [
        "http://a/", "http://a/dir/page.html"]


def test_fragment_stripped_query_kept_self_link_dedup():
    root = html_page("/p#frag", "/p?q=1#x", "#top", "/p")
    warc = site_warc([
        ("<urn:uuid:p>", "http://a/p", {**HTML}),
        ("<urn:uuid:pq>", "http://a/p?q=1", {**HTML}),
    ], root_body=root)
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert [p["url"] for p in d["reachable"]] == [
        "http://a/", "http://a/p", "http://a/p?q=1"]
    assert d["unreachable"] == []


def test_non_http_links_ignored():
    root = html_page("mailto:a@b.c", "javascript:void(0)",
                     "data:text/html,<p>x</p>", "ftp://a/f", "", "  ")
    warc = site_warc([], root_body=root)
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert [p["url"] for p in d["reachable"]] == ["http://a/"]
    assert d["unreachable"] == []


def test_link_cycle_terminates():
    warc = site_warc([
        ("<urn:uuid:a>", "http://a/a",
         {**HTML, "body": html_page("/b")}),
        ("<urn:uuid:b>", "http://a/b",
         {**HTML, "body": html_page("/a", "/")}),
    ], root_body=html_page("/a"))
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    assert [p["url"] for p in r.json()["reachable"]] == [
        "http://a/", "http://a/a", "http://a/b"]


def test_child_redirect_cycle_skipped():
    root = html_page("/loop1", "/ok")
    warc = site_warc([
        ("<urn:uuid:l1>", "http://a/loop1",
         {"date": D, "status_code": 301, "location": "/loop2"}),
        ("<urn:uuid:l2>", "http://a/loop2",
         {"date": D, "status_code": 302, "location": "/loop1"}),
        ("<urn:uuid:ok>", "http://a/ok", {**HTML}),
    ], root_body=root)
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert [p["url"] for p in d["reachable"]] == ["http://a/", "http://a/ok"]
    # 成环的两个 URL 已被遍历遇到，不列入不可达
    assert d["unreachable"] == []


def test_child_without_capture_skipped():
    root = html_page("/ghost", "/ok")
    warc = site_warc([
        ("<urn:uuid:ok>", "http://a/ok", {**HTML}),
    ], root_body=root)
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert [p["url"] for p in d["reachable"]] == ["http://a/", "http://a/ok"]
    assert d["unreachable"] == []


def test_point_in_time_selection_and_tie_break():
    # /p 在时点后有更新抓取但不可见；同刻两条取归档后者。
    root = html_page("/p")
    warc = site_warc([
        ("<urn:uuid:p-old>", "http://a/p",
         {"date": "2021-01-01T00:00:00Z", "content_type": "text/html"}),
        ("<urn:uuid:p-new>", "http://a/p",
         {"date": "2022-01-01T00:00:00Z", "content_type": "text/html"}),
        ("<urn:uuid:tie1>", "http://a/tie",
         {"date": D, "content_type": "text/html"}),
        ("<urn:uuid:tie2>", "http://a/tie",
         {"date": D, "content_type": "text/html"}),
    ], root_body=root)
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    pages = reachable_by_url(d)
    assert pages["http://a/p"]["record_id"] == "<urn:uuid:p-old>"
    # /tie 无人链接 → 不可达；同刻取归档后者
    assert d["unreachable"] == [
        {"url": "http://a/tie", "record_id": "<urn:uuid:tie2>"}]


def test_capture_only_after_point_in_time_not_visible():
    # 链接目标只有时点后的抓取：子链接失败跳过；未被链接的同理不入不可达。
    root = html_page("/future")
    warc = site_warc([
        ("<urn:uuid:f1>", "http://a/future",
         {"date": "2022-01-01T00:00:00Z", "content_type": "text/html"}),
        ("<urn:uuid:f2>", "http://a/future2",
         {"date": "2022-01-01T00:00:00Z", "content_type": "text/html"}),
    ], root_body=root)
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert [p["url"] for p in d["reachable"]] == ["http://a/"]
    assert d["unreachable"] == []


def test_revisit_root_uses_restored_body():
    # 根为 revisit：自带 200 + text/html 头，正文来自链终点 response。
    endpoint = raw_record(
        "<urn:uuid:orig>", uri="http://a/orig", date="2020-01-01T00:00:00Z",
        block=http_block(body=html_page("/p")))
    rev = raw_record(
        "<urn:uuid:rev>", rec_type="revisit", uri="http://a/", date=D,
        block=http_block(headers=[("Content-Type", "text/html")]),
        extra_headers=b"WARC-Refers-To: <urn:uuid:orig>\r\n")
    page = raw_record(
        "<urn:uuid:p>", uri="http://a/p", date=D,
        block=http_block(headers=[("Content-Type", "text/html")],
                         body=b"<html></html>"))
    r = sr(endpoint + rev + page, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    pages = reachable_by_url(d)
    assert pages["http://a/"]["record_id"] == "<urn:uuid:rev>"
    assert pages["http://a/p"]["record_id"] == "<urn:uuid:p>"


def test_root_redirect_to_other_origin_defines_site():
    # 首页 http://a/ → 301 → https://a/：站点源以终点为准；
    # https 下链接可达，http 链接反而跨源被忽略。
    def fill(w):
        w.write_record(_make_response(
            w, "<urn:uuid:root>", "http://a/", D,
            status_code=301, reason="Moved", location="https://a/"))
        w.write_record(_make_response(
            w, "<urn:uuid:home>", "https://a/", D,
            content_type="text/html",
            body=html_page("https://a/page", "http://a/insecure")))
        w.write_record(_make_response(
            w, "<urn:uuid:p>", "https://a/page", D,
            content_type="text/html", body=b"<html></html>"))
        w.write_record(_make_response(
            w, "<urn:uuid:ins>", "http://a/insecure", D,
            content_type="text/html", body=b"<html></html>"))
    r = sr(build_warc(fill), "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["final_url"] == "https://a/"
    assert [p["url"] for p in d["reachable"]] == [
        "https://a/", "https://a/page"]
    # http://a/insecure 跨源不入不可达；http://a/（种子）已遇到
    assert d["unreachable"] == []


def test_default_and_explicit_port_origin():
    # 显式 80 端口与缺省端口同源（抓取按精确 URL 匹配，故归档记录带 :80）；
    # 8080 跨源。
    root = html_page("http://a:80/p", "http://a:8080/q")
    warc = site_warc([
        ("<urn:uuid:p>", "http://a:80/p", {**HTML}),
        ("<urn:uuid:q>", "http://a:8080/q", {**HTML}),
    ], root_body=root)
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    d = r.json()
    assert [p["url"] for p in d["reachable"]] == [
        "http://a/", "http://a:80/p"]
    assert d["unreachable"] == []


def test_gzip_and_multimember_input():
    root = html_page("/p")
    page = raw_record(
        "<urn:uuid:p>", uri="http://a/p", date=D,
        block=http_block(headers=[("Content-Type", "text/html")],
                         body=b"<html></html>"))
    root_rec = raw_record(
        "<urn:uuid:root>", uri="http://a/", date=D,
        block=http_block(headers=[("Content-Type", "text/html")], body=root))
    r = sr(gzip.compress(root_rec) + gzip.compress(page), "http://a/", AT)
    assert r.status_code == 200, r.text
    assert [p["url"] for p in r.json()["reachable"]] == [
        "http://a/", "http://a/p"]


def test_from_field_serialized_as_from():
    warc = site_warc([], root_body=html_page())
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 200, r.text
    entry = r.json()["reachable"][0]
    assert "from" in entry
    assert entry["from"] is None


# ---------- 根页面错误：404 / 422 ----------


def test_root_no_capture_404():
    warc = site_warc([], root_body=html_page())
    r = sr(warc, "http://a/nothing", AT)
    assert r.status_code == 404
    d = r.json()
    assert d["error"] == "capture_not_found"
    assert d["url"] == "http://a/nothing"


def test_empty_warc_root_404():
    r = sr(b"", "http://a/", AT)
    assert r.status_code == 404
    assert r.json()["error"] == "capture_not_found"


def test_root_redirect_error_422():
    def fill(w):
        w.write_record(_make_response(
            w, "<urn:uuid:root>", "http://a/", D,
            status_code=301, reason="Moved"))  # 缺 Location
    r = sr(build_warc(fill), "http://a/", AT)
    assert r.status_code == 422
    assert r.json()["error"] == "missing_location"


def test_root_non_2xx_422():
    warc = site_warc([], root_kw={"status_code": 404, "reason": "Not Found"})
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "root_status_not_2xx"
    assert d["url"] == "http://a/"
    assert d["record_id"] == "<urn:uuid:root>"
    assert d["http_status"] == 404


def test_root_non_html_422():
    warc = site_warc([], root_kw={"content_type": "text/plain"},
                     root_body=b"plain")
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "root_not_html"
    assert d["url"] == "http://a/"
    assert d["content_type"] == "text/plain"


def test_root_missing_content_type_422():
    warc = site_warc([], root_kw={"content_type": None})
    r = sr(warc, "http://a/", AT)
    assert r.status_code == 422
    d = r.json()
    assert d["error"] == "root_not_html"
    assert d["content_type"] is None


# ---------- 入参与 WARC 校验 ----------


def test_invalid_start_url_422():
    warc_b64 = base64.b64encode(b"").decode()
    for bad in ("ftp://a/", "not a url", "http://", ""):
        r = sr_b64(warc_b64, bad, AT)
        assert r.status_code == 422, bad
        assert r.json()["error"] == "invalid_start_url"


def test_invalid_at_422():
    warc_b64 = base64.b64encode(b"").decode()
    r = sr_b64(warc_b64, "http://a/", "2021-01-01")
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_at"


def test_missing_json_fields_422():
    r = client.post("/v1/site-reachability", json={"warc": ""})
    assert r.status_code == 422


def test_invalid_base64_422():
    r = sr_b64("!!!not-base64!!!", "http://a/", AT)
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_base64"


def test_malformed_warc_422():
    r = sr(b"garbage", "http://a/", AT)
    assert r.status_code == 422
    assert r.json()["error"] == "malformed_warc"


def test_size_limit_enforced():
    blob = b"x" * (MAX_DECODED_BYTES + 1)
    r = sr_b64(base64.b64encode(blob).decode(), "http://a/", AT)
    assert r.status_code == 422
    assert r.json()["error"] == "payload_too_large"


def test_broken_revisit_chain_still_422():
    rev = raw_record(
        "<urn:uuid:rev>", rec_type="revisit", uri="http://a/",
        block=b"", extra_headers=b"WARC-Refers-To: <urn:uuid:ghost>\r\n")
    r = sr(rev, "http://a/", AT)
    assert r.status_code == 422
    assert r.json()["error"] == "target_not_found"


def test_health_still_ok():
    assert client.get("/health").status_code == 200
