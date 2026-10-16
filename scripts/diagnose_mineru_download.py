"""诊断 MinerU 结果下载的网络问题(只用标准库,不依赖 deephoto)。

用法(在运行 deephoto 服务的同一台机器、同一个 Python 环境里):
    python scripts/diagnose_mineru_download.py
    python scripts/diagnose_mineru_download.py <域名或完整URL>

不传参数时检查 MinerU 的结果 CDN(cdn-mineru.openxlab.org.cn)和 API(mineru.net)。
报错日志里 "直连 xxx" 中的 xxx 就是实际下载用的域名,可以作为参数传进来。
"""
from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import subprocess
import ssl
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, getproxies

DEFAULT_TARGETS = ["https://cdn-mineru.openxlab.org.cn/", "https://mineru.net/"]
# Clash / Surge 等代理 fake-ip 模式使用的保留网段
FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _bad(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def check(url: str) -> None:
    if "://" not in url:
        url = f"https://{url}/"
    parsed = urlparse(url)
    host, port = parsed.hostname, parsed.port or 443
    print(f"\n=== {host}:{port}")

    # 1. DNS:落在 198.18.0.0/15 说明系统 DNS 被代理软件接管(fake-ip / TUN 模式)
    try:
        ips = sorted({info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except OSError as exc:
        _bad(f"DNS 解析失败: {exc}")
        return
    fake = [ip for ip in ips if ipaddress.ip_address(ip).version == 4
            and ipaddress.ip_address(ip) in FAKE_IP_NET]
    print(f"  DNS -> {', '.join(ips)}")
    if fake:
        _bad("解析到 198.18.x.x 假 IP:代理软件开了 TUN/fake-ip 模式,"
             "所有连接都会被系统层面接管,代码里设置直连无效")

    # 2. 裸 TCP + TLS 握手:分别用默认配置、仅 TLS1.2、仅 TLS1.3 尝试。
    #    默认失败而 TLS1.2 成功 → 握手包(如 OpenSSL 3.5 的抗量子密钥分组)被 CDN/中间设备丢弃
    for label, lo, hi in (("默认", None, None),
                          ("仅TLS1.2", ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_2),
                          ("仅TLS1.3", ssl.TLSVersion.TLSv1_3, ssl.TLSVersion.TLSv1_3)):
        ctx = ssl.create_default_context()
        if lo:
            ctx.minimum_version, ctx.maximum_version = lo, hi
        start = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=15) as sock:
                peer = sock.getpeername()[0]
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    _ok(f"TLS[{label}] 握手成功 -> {peer} {tls.version()} {tls.cipher()[0]} "
                        f"({int((time.monotonic() - start) * 1000)} ms)")
        except ssl.SSLError as exc:
            _bad(f"TLS[{label}] 握手失败: {exc}")
        except OSError as exc:
            _bad(f"TLS[{label}] TCP 连接失败: {exc}")

    # 2b. 对照:系统 curl(不同的 TLS 实现)能否连通
    curl = shutil.which("curl")
    if curl:
        try:
            out = subprocess.run(
                [curl, "-sS", "--noproxy", "*", "-o", os.devnull, "-m", "30",
                 "-w", "%{http_code}", url],
                capture_output=True, text=True, timeout=40)
            if out.returncode == 0:
                _ok(f"curl 直连: HTTP {out.stdout.strip()}")
            else:
                _bad(f"curl 直连失败: {(out.stderr or out.stdout).strip()}")
        except Exception as exc:  # noqa: BLE001
            _bad(f"curl 运行失败: {exc}")
    else:
        print("  (未找到 curl,跳过对照)")

    # 3. 与 deephoto 一致的直连 HTTP 请求(忽略代理环境变量);4xx 也说明链路是通的
    opener = build_opener(ProxyHandler({}))
    try:
        resp = opener.open(Request(url, method="GET"), timeout=30)
        _ok(f"直连 HTTP {resp.status}")
    except HTTPError as exc:
        _ok(f"直连 HTTP {exc.code}(有响应即说明网络和 TLS 正常)")
    except Exception as exc:  # noqa: BLE001 - 诊断脚本,原样打印
        _bad(f"直连请求失败: {exc}")


def main() -> None:
    print(f"Python  {sys.version.split()[0]}  ({sys.executable})")
    print(f"OpenSSL {ssl.OPENSSL_VERSION}")
    proxies = {k: v for k, v in getproxies().items() if k in ("http", "https", "all", "no")}
    print(f"环境/系统代理: {proxies or '无'}")
    for target in sys.argv[1:] or DEFAULT_TARGETS:
        check(target)


if __name__ == "__main__":
    main()
