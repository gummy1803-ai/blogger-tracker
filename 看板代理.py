"""
博主看板代理服务
- 转发 Anthropic API（解决 GitHub Pages CORS 限制）
- 转发 飞书 API（解决 CORS 限制）
双击运行，保持窗口开启，然后正常使用看板
"""
import json, urllib.request, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

ANTHROPIC_BASE = "https://api.anthropic.com"
FEISHU_BASE    = "https://open.feishu.cn"

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        path = self.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        if path.startswith("/anthropic/"):
            target = ANTHROPIC_BASE + path[len("/anthropic"):]
            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.headers.get("x-api-key", ""),
                "anthropic-version": self.headers.get("anthropic-version", "2023-06-01"),
            }
        elif path.startswith("/feishu/"):
            target = FEISHU_BASE + path[len("/feishu"):]
            headers = {
                "Content-Type": "application/json",
                "Authorization": self.headers.get("Authorization", ""),
            }
        else:
            self.send_response(404); self._cors(); self.end_headers()
            return

        self._proxy(target, headers, body, method="POST")

    def do_GET(self):
        path = self.path
        if path.startswith("/feishu/"):
            target = FEISHU_BASE + path[len("/feishu"):]
            headers = {"Authorization": self.headers.get("Authorization", "")}
            self._proxy(target, headers, None, method="GET")
        else:
            self.send_response(200); self._cors()
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("看板代理运行中 ✓".encode("utf-8"))

    def _proxy(self, url, headers, body, method):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_response(500)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
            "Content-Type, Authorization, x-api-key, anthropic-version")

if __name__ == "__main__":
    port = 8765
    server = HTTPServer(("localhost", port), Handler)
    print("=" * 48)
    print("  博主看板代理已启动 ✓")
    print(f"  地址：http://localhost:{port}")
    print("  保持此窗口开启，正常使用看板即可")
    print("  按 Ctrl+C 停止")
    print("=" * 48)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
