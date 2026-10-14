from __future__ import annotations

import socketserver
import gzip
import io
import struct
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


MANIFEST_NAME = "eqoa-frontierslive-update.xml.gz"
DEFAULT_CHANGES = b"No changes.\n"
# Station login pak as served by Sandstorm (captures/from_sandstorm.pcapng, station4.pak.gz).
# It carries the login UI pages plus the auth server address the client connects to.
STATION_PAK_PATH = Path(__file__).parent / "patch_data" / "station4.pak"
STATION_HOSTS_ENTRY = b"stationdata/servers/stationLiveHosts.txt"
STATION_OPTIONS_ENTRY = b"stationdata/config/options.cfg"


def gzip_named(name: str, data: bytes) -> bytes:
    out = io.BytesIO()
    with gzip.GzipFile(filename=name, mode="wb", fileobj=out, mtime=0) as gz:
        gz.write(data)
    return out.getvalue()


def eqahosts_bytes(config: ServerConfig) -> bytes:
    return gzip_named("eqahosts.txt", f"{config.public_host}\n\n".encode("ascii"))


def parse_pak(data: bytes) -> list[tuple[bytes, bytes]]:
    """A pak is a run of (u32 BE name length, name, u32 BE data length, data) entries."""
    entries = []
    offset = 0
    while offset < len(data):
        (name_len,) = struct.unpack_from(">I", data, offset)
        name = data[offset + 4 : offset + 4 + name_len]
        offset += 4 + name_len
        (data_len,) = struct.unpack_from(">I", data, offset)
        entries.append((name, data[offset + 4 : offset + 4 + data_len]))
        offset += 4 + data_len
    if offset != len(data):
        raise ValueError("truncated pak")
    return entries


def build_pak(entries: list[tuple[bytes, bytes]]) -> bytes:
    return b"".join(
        struct.pack(">I", len(name)) + name + struct.pack(">I", len(data)) + data
        for name, data in entries
    )


def station_pak_bytes(config: ServerConfig) -> bytes:
    entries = []
    for name, data in parse_pak(STATION_PAK_PATH.read_bytes()):
        if name == STATION_HOSTS_ENTRY:
            data = f"{config.public_host}:{config.auth_port}\n".encode("ascii")
        elif name == STATION_OPTIONS_ENTRY:
            data = (
                f"NewsURL:http://{config.public_host}:{config.motd_port}{MOTD_PATH}\r\n"
            ).encode("ascii")
        entries.append((name, data))
    return gzip_named("pak.pak", build_pak(entries))


def changes_bytes(config: ServerConfig) -> bytes:
    if config.patch_source_dir is not None:
        changes_path = config.patch_source_dir / "Changes.txt"
        if changes_path.is_file():
            return changes_path.read_bytes()
    return DEFAULT_CHANGES


def generated_patch_file(config: ServerConfig, name: str) -> bytes | None:
    """Files the server synthesizes itself, so no patch directory is required."""
    lowered = name.lower()
    if lowered == "eqahosts.txt.gz":
        return eqahosts_bytes(config)
    if lowered == "changes.txt":
        return changes_bytes(config)
    if lowered == "station4.pak.gz":
        return station_pak_bytes(config)
    if lowered == MANIFEST_NAME:
        return build_verant_manifest(config)
    return None


def patch_file_name(request_path: str) -> str:
    clean = request_path.split("?", 1)[0].lstrip("/")
    if clean.lower().startswith("patch/eqoa/"):
        return clean[len("patch/eqoa/") :]
    return Path(clean).name


def load_patch_file(config: ServerConfig, request_path: str) -> bytes | None:
    name = patch_file_name(request_path)
    if not name:
        return None
    generated = generated_patch_file(config, name)
    if generated is not None:
        return generated
    if config.patch_source_dir is None:
        return None
    path = config.patch_source_dir / name
    if not path.exists() or not path.is_file():
        return None
    return path.read_bytes()


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
        words = file_entry("WORDS.TXT", "words-11.txt.gz", gz_info(config, "words-11.txt.gz"))
        entries.append('<File LocalName="words.txt" Delete="True"></File>')
        entries.append(words)
    except FileNotFoundError:
        pass

    changes_crc = binascii.crc32(changes_bytes(config)) & 0xFFFFFFFF

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
    return build_http_response(
        build_verant_manifest(config),
        content_type="application/x-gzip",
        extra_headers=[
            "Last-Modified: Tue, 24 Feb 2026 15:24:56 GMT",
            'ETag: "local-verant-manifest"',
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
                    if server.log_requests:
                        print(f"PATCH   -> 200 {len(patch_data)} bytes")
                    self.request.sendall(build_http_response(patch_data, content_type=content_type))
                else:
                    if server.log_requests:
                        print("PATCH   -> 404")
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
