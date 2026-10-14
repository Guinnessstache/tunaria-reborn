from __future__ import annotations

import socketserver
import gzip
import binascii
from pathlib import Path
from dataclasses import dataclass
from email.utils import formatdate

from .config import ServerConfig


MOTD_PATH = "/patch/web/eqoa/motd/news.html"
UPDATE_MANIFEST_PATHS = {
    "/patch/eqoa/eqoa-frontierslive-update.xml.gz",
    "/patch/eqoa/eqoa-live-update.xml.gz",
}
PATCH_PREFIX = "/patch/eqoa/"
MOTD_BODY_SIZE = 400
MOTD_RESPONSE_SIZE = 707


def fixed_motd_body(config: ServerConfig) -> bytes:
    text = (
        f"Welcome to {config.shard_name}!\n"
        f"Server: {config.public_host}\n\n"
        "This is a local EQOA community server scaffold.\n"
        "Account creation and login are available. Character/world support is experimental.\n\n"
        "Next milestone: server select, character select, and a minimal empty-world login.\n"
    )
    body = text.encode("ascii", "ignore")[:MOTD_BODY_SIZE]
    return body.ljust(MOTD_BODY_SIZE, b" ")


def build_motd_response(config: ServerConfig) -> bytes:
    body = fixed_motd_body(config)
    headers = [
        "HTTP/1.1 200 OK",
        f"Date: {formatdate(usegmt=True)}",
        "Server: EQOACommunity/0.1",
        "Last-Modified: Tue, 24 Feb 2026 15:24:56 GMT",
        'ETag: "190-local-eqoa"',
        "Accept-Ranges: bytes",
        f"Content-Length: {len(body)}",
        "Vary: Accept-Encoding",
        "Cache-Control: max-age=3600, private, must-revalidate",
        "Content-Type: text/html",
    ]
    response_head = "\r\n".join(headers).encode("ascii") + b"\r\n"
    pad_len = MOTD_RESPONSE_SIZE - len(response_head) - 2 - len(body)
    if pad_len >= len("X-Pad:\r\n"):
        response_head += b"X-Pad:" + (b"." * (pad_len - len("X-Pad:\r\n"))) + b"\r\n"
    return response_head + b"\r\n" + body


def build_not_found_response() -> bytes:
    body = b"Not found\n"
    return (
        b"HTTP/1.1 404 Not Found\r\n"
        b"Server: EQOACommunity/0.1\r\n"
        + f"Content-Length: {len(body)}\r\n".encode("ascii")
        + b"Content-Type: text/plain\r\n"
        + b"Connection: close\r\n\r\n"
        + body
    )


def build_http_response(
    body: bytes,
    *,
    status: str = "200 OK",
    content_type: str = "application/octet-stream",
    extra_headers: list[str] | None = None,
) -> bytes:
    headers = [
        f"HTTP/1.1 {status}",
        f"Date: {formatdate(usegmt=True)}",
        "Server: EQOACommunity/0.1",
        f"Content-Length: {len(body)}",
        f"Content-Type: {content_type}",
        "Connection: close",
    ]
    if extra_headers:
        headers[3:3] = extra_headers
    return "\r\n".join(headers).encode("ascii") + b"\r\n\r\n" + body


def patch_file_bytes(config: ServerConfig, name: str, data: bytes) -> bytes:
    if name == "eqahosts.txt.gz":
        return gzip.compress(f"{config.public_host}\n\n".encode("ascii"), mtime=0)
    if name == "eqoa-frontierslive-update.xml.gz":
        return build_verant_manifest(config)
    return data


def load_patch_file(config: ServerConfig, request_path: str) -> bytes | None:
    if config.patch_source_dir is None:
        return None
    clean = request_path.split("?", 1)[0].lstrip("/")
    if clean.lower().startswith("patch/eqoa/"):
        name = clean[len("patch/eqoa/") :]
    else:
        name = Path(clean).name
    if not name:
        return None
    path = config.patch_source_dir / name
    if not path.exists() or not path.is_file():
        return None
    return patch_file_bytes(config, name, path.read_bytes())


def gz_info(config: ServerConfig, name: str) -> dict[str, int]:
    data = load_patch_file(config, f"/patch/eqoa/{name}")
    if data is None:
        raise FileNotFoundError(name)
    raw = gzip.decompress(data)
    return {
        "crc": binascii.crc32(raw) & 0xFFFFFFFF,
        "total": len(raw),
        "download": len(data),
    }


def build_verant_manifest(config: ServerConfig) -> bytes:
    assert config.patch_source_dir is not None
    version = 26050317

    def file_entry(local: str, remote: str, info: dict[str, int]) -> str:
        return (
            f'<File LocalName="{local}" RemotePath="" Server="{config.public_host}"'
            f' Name="{remote}" CRC="{info["crc"]}"'
            f' TotalSize="{info["total"]}" DownloadSize="{info["download"]}"></File>'
        )

    entries = [
        file_entry("eqahosts.txt", "eqahosts.txt.gz", gz_info(config, "eqahosts.txt.gz")),
    ]
    for local, remote in [
        ("SS", "ss-patch-6.dat.gz"),
        ("station.pak", "station4.pak.gz"),
        ("addart.csf", "addart-2.csf.gz"),
    ]:
        try:
            entries.append(file_entry(local, remote, gz_info(config, remote)))
        except FileNotFoundError:
            pass

    try:
        entries.append('<File LocalName="words.txt" Delete="True"></File>')
        entries.append(file_entry("WORDS.TXT", "words-11.txt.gz", gz_info(config, "words-11.txt.gz")))
    except FileNotFoundError:
        pass

    changes = b"No changes.\n"
    changes_path = config.patch_source_dir / "Changes.txt"
    if changes_path.exists():
        changes = changes_path.read_bytes()
    changes_crc = binascii.crc32(changes) & 0xFFFFFFFF

    xml = (
        '<VerantPatcher Version="1">\r\n'
        f'<Product Version="{version}">\r\n'
        '<Distribution Name="Frontiers" >'
        + "".join(entries)
        + "\r\n</Distribution>\r\n"
        f'<Changes Server="{config.public_host}" Port="{config.motd_port}" Version="0"'
        f' RemotePath="patch/eqoa/Changes.txt" CRC="{changes_crc}"></Changes>\r\n'
        "</Product>\r\n"
        "</VerantPatcher>\n"
    )
    return gzip.compress(xml.encode("utf-8"), mtime=0)


def build_update_manifest_response(config: ServerConfig) -> bytes:
    if config.patch_source_dir:
        manifest = load_patch_file(config, "/patch/eqoa/eqoa-frontierslive-update.xml.gz")
        if manifest:
            return build_http_response(
                manifest,
                content_type="application/x-gzip",
                extra_headers=[
                    "Last-Modified: Tue, 24 Feb 2026 15:24:56 GMT",
                    'ETag: "local-verant-manifest"',
                    "Accept-Ranges: bytes",
                    "Cache-Control: no-cache",
                ],
            )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<VerantPatcher Version="1">\r\n'
        '<Product Version="0">\r\n'
        '<Distribution Name="Frontiers" >\r\n'
        "</Distribution>\r\n"
        '<Changes Server="127.0.0.1" Port="7000" Version="0" RemotePath="patch/eqoa/Changes.txt" CRC="0"></Changes>\r\n'
        "</Product>\r\n"
        "</VerantPatcher>\n"
    ).encode("utf-8")
    body = gzip.compress(xml, mtime=0)
    return build_http_response(
        body,
        content_type="application/x-gzip",
        extra_headers=[
            "Last-Modified: Tue, 24 Feb 2026 15:24:56 GMT",
            'ETag: "local-empty-update"',
            "Accept-Ranges: bytes",
            "Cache-Control: no-cache",
        ],
    )

@dataclass(slots=True)
class PatchRequest:
    method: str
    path: str
    version: str
    host: str = ""


def parse_patch_request(data: bytes) -> PatchRequest:
    text = data.decode("iso-8859-1", "replace")
    lines = text.split("\r\n")
    request_line = lines[0].split()
    if len(request_line) != 3:
        raise ValueError("invalid HTTP request line")
    host = ""
    for line in lines[1:]:
        if line.lower().startswith("host:"):
            host = line.split(":", 1)[1].strip()
            break
    return PatchRequest(request_line[0], request_line[1], request_line[2], host)


class PatchTCPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: PatchTCPServer = self.server  # type: ignore[assignment]
        data = self.request.recv(4096)
        if not data:
            return
        try:
            parsed = parse_patch_request(data)
            if server.log_requests:
                print(
                    f"PATCH {self.client_address[0]}:{self.client_address[1]} "
                    f"{parsed.method} {ascii(parsed.path)} host={parsed.host!r} len={len(data)}"
                )
            if parsed.method == "GET" and parsed.path == MOTD_PATH:
                self.request.sendall(build_motd_response(server.config))
            elif parsed.method == "GET" and (
                parsed.path in UPDATE_MANIFEST_PATHS
                or parsed.path.startswith("/patch/eqoa/")
                and parsed.path.endswith("-update.xml.gz")
            ):
                self.request.sendall(build_update_manifest_response(server.config))
            elif parsed.method == "GET" and (
                parsed.path.startswith(PATCH_PREFIX)
                or "/" not in parsed.path.lstrip("/")
            ):
                patch_data = load_patch_file(server.config, parsed.path)
                if patch_data is not None:
                    content_type = "application/x-gzip" if parsed.path.endswith(".gz") else "application/octet-stream"
                    self.request.sendall(build_http_response(patch_data, content_type=content_type))
                else:
                    self.request.sendall(build_not_found_response())
            else:
                self.request.sendall(build_not_found_response())
        except Exception as exc:
            if server.log_requests:
                print(f"PATCH request failed from {self.client_address}: {exc}")
            self.request.sendall(build_not_found_response())


class PatchTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        config: ServerConfig,
        *,
        log_requests: bool = True,
    ):
        super().__init__(address, PatchTCPHandler)
        self.config = config
        self.log_requests = log_requests
