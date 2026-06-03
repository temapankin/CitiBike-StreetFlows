#!/usr/bin/env python3
"""HTTP server with Range request support (required for PMTiles byte-serving)."""
import http.server
import io
import os
import re
import sys


class RangeHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    # HTTP/1.1 keep-alive so DuckDB-WASM's many small range reads reuse one
    # connection instead of reconnecting per request.
    protocol_version = "HTTP/1.1"

    def end_headers(self):
        # Advertise byte-range support on *every* response. DuckDB-WASM checks
        # this (via HEAD) before issuing range reads; without it, range-only mode
        # refuses to open the file ("Failed to open file").
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def send_head(self):
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return super().send_head()

        range_header = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d+)-(\d*)$", range_header)
        if not m:
            return super().send_head()

        f = open(path, "rb")  # noqa: SIM115
        fs = os.fstat(f.fileno())
        total = fs.st_size
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else total - 1
        end = min(end, total - 1)
        length = end - start + 1

        f.seek(start)
        # Return exactly the requested bytes. The base handler's copyfile() streams
        # to EOF, so hand it a buffer holding only [start, end] — otherwise a range
        # read of an early byte range would over-stream the rest of the file.
        chunk = f.read(length)
        f.close()
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        return io.BytesIO(chunk)

    def log_message(self, fmt, *args):
        pass  # quiet


port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
print(f"Serving http://localhost:{port}/  (Range-request enabled)")
with http.server.HTTPServer(("", port), RangeHTTPRequestHandler) as httpd:
    httpd.serve_forever()
