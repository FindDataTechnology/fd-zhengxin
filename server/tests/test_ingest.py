"""文档来源：链接 / 上传文件 → 正文（change zhengxin-document-ingest）。

口径线（本文件守的是「别把服务器变成用户的代理」）：
- SSRF 守卫：内网 / 回环 / 链路本地 / 保留地址一律拒（用户贴个 URL 就能让
  服务器替他发请求，169.254.169.254 是云元数据接口）；
- 只接受 http/https；
- 取不到正文时**结构化报错**，不静默返回空（也绝不冒充「无影响」）；
- PDF 无文本层（扫描件）如实说需 OCR，不假装抽出了东西。
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from fd_zhengxin_api import ingest
from fd_zhengxin_api.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer dev-tok-alice"}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FDZX__DB", str(tmp_path / "personal.db"))
    monkeypatch.setenv("FDZX__AUTH_DEV_TOKENS", "dev-tok-alice=alice")


# ---------------------------------------------------------------- SSRF 守卫（最重要）

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/api/v1/me",
    "http://localhost/x",
    "http://169.254.169.254/latest/meta-data/",   # 云元数据
    "http://10.0.0.1/",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
    "http://[::1]/",
    "http://svc.fd-prod.svc.cluster.internal/mcp",
    "http://printer.local/",
])
def test_private_hosts_rejected(url: str) -> None:
    with pytest.raises(ingest.IngestError) as ei:
        ingest.from_url(url)
    assert ei.value.reason in ("private_host", "bad_url")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/y", "gopher://x"])
def test_non_http_schemes_rejected(url: str) -> None:
    with pytest.raises(ingest.IngestError) as ei:
        ingest.from_url(url)
    assert ei.value.reason == "bad_scheme"


def test_public_host_passes_guard(monkeypatch) -> None:
    """公网地址过守卫（不实际发请求：把 httpx.Client 换掉）。"""
    class FakeResp:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        content = "<html><body><p>发文机关：财政部</p></body></html>".encode()

    class FakeClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "Client", FakeClient)
    doc = ingest.from_url("https://www.mof.gov.cn/example.htm")
    assert "财政部" in doc.text
    assert doc.source == "url"


# ---------------------------------------------------------------- 文本 / HTML

def test_html_strips_tags_and_scripts() -> None:
    raw = "<html><script>var a=1</script><p>发文机关：财政部</p><p>文号：财预〔2026〕1号</p></html>"
    doc = ingest.from_bytes(raw.encode(), content_type="text/html")
    assert "var a=1" not in doc.text
    assert "财政部" in doc.text
    assert "财预〔2026〕1号" in doc.text


def test_plain_text_passthrough() -> None:
    doc = ingest.from_bytes("文号：财预〔2026〕1号".encode(), content_type="text/plain")
    assert "财预" in doc.text


def test_gb18030_decoded_via_meta() -> None:
    """中文政府站点常见 GB18030：靠 meta 声明兜底，不靠猜。"""
    html = '<html><head><meta charset="gb18030"></head><body><p>自然资源部</p></body></html>'
    doc = ingest.from_bytes(html.encode("gb18030"), content_type="text/html")
    assert "自然资源部" in doc.text


def test_empty_file_rejected() -> None:
    with pytest.raises(ingest.IngestError) as ei:
        ingest.from_bytes(b"", content_type="text/plain")
    assert ei.value.reason == "empty"


def test_oversize_rejected(monkeypatch) -> None:
    monkeypatch.setattr(ingest, "MAX_BYTES", 10)
    with pytest.raises(ingest.IngestError) as ei:
        ingest.from_bytes(b"x" * 100, content_type="text/plain")
    assert ei.value.reason == "too_large"


# ---------------------------------------------------------------- PDF

def test_pdf_with_no_text_layer_says_ocr_needed() -> None:
    """扫描件 PDF（零文本层）→ 如实说需 OCR，不假装抽出内容。"""
    from pypdf import PdfWriter

    buf = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.write(buf)
    with pytest.raises(ingest.IngestError) as ei:
        ingest.from_bytes(buf.getvalue(), content_type="application/pdf")
    assert ei.value.reason == "no_text"


# ---------------------------------------------------------------- 端点

def test_impact_file_endpoint_accepts_html() -> None:
    body = ("<html><body><p>发文机关：财政部</p><p>文 号：财预〔2026〕1号</p>"
            "<p>自2026年3月1日起施行。</p><p>各省、自治区人民政府：</p></body></html>")
    r = client.post(
        "/api/v1/loops/impact/file",
        files={"file": ("公告.html", body.encode(), "text/html")},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["target"]["kind"] == "file"
    assert j["claims"][0]["body_available"] is True
    els = {e["element"]: e for e in j["claims"][0]["elements"]}
    assert els.get("发文机关", {}).get("value") == "财政部"
    assert "judgment" in j


def test_impact_file_endpoint_reports_bad_input_without_crashing() -> None:
    r = client.post(
        "/api/v1/loops/impact/file",
        files={"file": ("empty.txt", b"", "text/plain")},
        headers=AUTH,
    )
    assert r.status_code == 200
    j = r.json()
    assert j["gaps"][0]["reason"] == "empty"
    assert j["claims"] == []


def test_impact_file_requires_auth() -> None:
    r = client.post("/api/v1/loops/impact/file",
                    files={"file": ("a.txt", b"x", "text/plain")})
    assert r.status_code == 401


def test_impact_url_kind_returns_structured_failure_on_unreachable() -> None:
    """url 模式抓不到时：结构化 gap，不是 500。"""
    r = client.post("/api/v1/loops/impact",
                    json={"target_kind": "url",
                          "target_ref": "http://127.0.0.1:1/x"}, headers=AUTH)
    assert r.status_code == 200
    j = r.json()
    assert j["gaps"][0]["stage"] == "document"
    assert j["gaps"][0]["reason"] in ("private_host", "fetch_failed")
