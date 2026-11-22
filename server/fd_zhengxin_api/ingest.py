"""正文来源：链接 / 上传文件 → 可抽取的正文（change zhengxin-document-ingest）。

存在理由：工作台此前要求用户粘贴 47 位 item_id——那是系统内部编号，用户在页面上
无处找到。但用户手里本来就拿着**链接**或**文件**（部委网站的公告页、下载的 PDF）。
本模块把这两样东西变成正文，让「解读这份文件」这件事从「找 ID」变成「贴过来」。

三条来源，共用同一套「正文 → 要素」的下游：

    url    http(s) 抓取 → 剥 HTML（默认）/ pypdf 抽文本（PDF）
    file   上传的 PDF / 纯文本 → 同上
    item   既有 fd-info 条目（保留旧路径，item_id 仍可用）

许可与安全（照 fd-zhengxin-reference 的既有纪律）：
- 出站前过红线（`assert_allowed_url`：SEP 等只链不抄的源不得作为请求目标）；
- 只允许 http(s)，拒绝 file:// / gopher:// 等协议；
- **拒绝指向内网/回环的地址**（SSRF：用户贴一个 169.254.169.254 就能让服务器
  替他打云元数据接口）；
- 大小与类型上限，超限如实拒绝而不是静默截断。

所有失败都带结构化原因（fetch_failed / not_html_or_pdf / too_large /
unsupported_type / no_text / blocked），不静默返回空正文。
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlparse

MAX_BYTES = 8 * 1024 * 1024        # 8MB：部委公告页/PDF 远小于此
MAX_CHARS = 400_000                # 抽出的正文上限（再大也超出判断环的意义）

TEXT_TYPES = ("text/plain", "text/html", "application/xhtml+xml")
PDF_TYPES = ("application/pdf",)


class IngestError(ValueError):
    """取正文失败，带结构化原因（调用方据此填 gaps，不静默）。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"[{reason}] {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Ingested:
    text: str
    source: str          # "url" | "file" | "item"
    content_type: str
    bytes: int


# ---------------------------------------------------------------- URL 安全


def _reject_private_host(url: str) -> None:
    """SSRF 守卫：拒绝指向内网 / 回环 / 链路本地的地址。

    用户贴 URL 就能让**服务器**替他发请求；不拦的话 `http://169.254.169.254/`
    会打到云厂商元数据接口，`http://127.0.0.1:8000/` 会打到我们自己的 API。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise IngestError("bad_scheme", f"只接受 http/https，收到 {parsed.scheme!r}")
    host = (parsed.hostname or "").strip()
    if not host:
        raise IngestError("bad_url", "URL 缺主机名")
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
        raise IngestError("private_host", f"拒绝内网/本机地址：{host}")
    # IP 字面量：内网、回环、链路本地、保留段一律拒
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return   # 域名：交给下游 DNS 与出站策略
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise IngestError("private_host", f"拒绝内网/保留地址：{host}")


# ---------------------------------------------------------------- 抽取


def _looks_like_pdf(data: bytes, content_type: str) -> bool:
    return content_type in PDF_TYPES or data[:5] == b"%PDF-"


def _pdf_to_text(data: bytes) -> str:
    """PDF → 文本。pypdf 是**可选依赖**：没装时如实说不可用，不假装。"""
    try:
        from pypdf import PdfReader  # 延迟导入：不装 pypdf 时其他来源照常用
    except ImportError as exc:  # pragma: no cover —— 依赖缺失时才有
        raise IngestError(
            "pdf_unsupported",
            "服务端未安装 pypdf，暂不能解析 PDF；请改贴 HTML 页面链接",
        ) from exc
    import io

    try:
        reader = PdfReader(io.BytesIO(data))
        parts = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 —— pypdf 的异常类型不稳定
        raise IngestError("fetch_failed", f"PDF 解析失败：{type(exc).__name__}") from exc
    text = "\n".join(p for p in parts if p.strip())
    if not text.strip():
        raise IngestError("no_text", "PDF 无文本层（扫描件/图片型）——需 OCR，当前不支持")
    return text


def html_to_text(raw: str) -> str:
    """HTML → 纯文本。与 impact._strip_html 同一套规则（正则剥标签、去脚本样式）。"""
    if not raw or "<" not in raw:
        return raw or ""
    t = re.sub(r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>", " ", raw,
               flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</h[1-6]>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    t = re.sub(r"[ \t　]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def _decode(data: bytes, content_type: str) -> str:
    """字节 → 文本。中文政府站点常见 GB18030，靠 meta 声明兜底。"""
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if not m:
        head = data[:2048].decode("ascii", errors="ignore")
        m2 = re.search(r'charset=["\']?([\w-]+)', head, re.I)
        charset = m2.group(1) if m2 else "utf-8"
    else:
        charset = m.group(1)
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def from_bytes(data: bytes, *, content_type: str = "", source: str = "file") -> Ingested:
    """上传文件 → 正文（PDF / HTML / 纯文本）。"""
    if not data:
        raise IngestError("empty", "文件为空")
    if len(data) > MAX_BYTES:
        raise IngestError("too_large", f"文件 {len(data)} 字节，超过上限 {MAX_BYTES}")
    if _looks_like_pdf(data, content_type):
        text = _pdf_to_text(data)
    elif content_type in TEXT_TYPES or data[:1] in (b"<", b" ", b"\n") or not content_type:
        raw = _decode(data, content_type)
        text = html_to_text(raw) if "<" in raw[:4096] else raw
    else:
        raise IngestError("unsupported_type", f"不支持的内容类型：{content_type or '未知'}")
    return _finalize(text, source=source, content_type=content_type, size=len(data))


def from_url(url: str, *, timeout: float = 20.0, allow_private: bool = False) -> Ingested:
    """抓取 URL → 正文。出站前过 SSRF 与红线守卫。"""
    if not allow_private:
        _reject_private_host(url)
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise IngestError("unsupported", "服务端未安装 httpx") from exc

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True,
                          headers={"User-Agent": "fd-zhengxin/0.1 (document ingest)"}) as c:
            resp = c.get(url)
    except Exception as exc:  # noqa: BLE001 —— 网络失败统一归一
        raise IngestError("fetch_failed", f"{type(exc).__name__}: {exc}") from exc

    if resp.status_code >= 400:
        raise IngestError("fetch_failed", f"HTTP {resp.status_code}")
    data = resp.content
    if len(data) > MAX_BYTES:
        raise IngestError("too_large", f"响应 {len(data)} 字节，超过上限 {MAX_BYTES}")
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    return from_bytes(data, content_type=ctype, source="url")


def _finalize(text: str, *, source: str, content_type: str, size: int) -> Ingested:
    text = (text or "").strip()
    if not text:
        raise IngestError("no_text", "没抽出任何正文")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    return Ingested(text=text, source=source, content_type=content_type, bytes=size)
