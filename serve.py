"""Local preview server that mirrors GitHub Pages' clean URLs.

GitHub Pages serves /standings from standings.html automatically; Python's
built-in http.server does not. This adds that one behaviour so the site's
extensionless links work the same locally as they do in production.

    python3 serve.py        # then open http://localhost:8000/
"""
import http.server
import os
import socketserver

PORT = int(os.environ.get("PORT", 8000))


class Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        local = super().translate_path(path)  # query/fragment already stripped
        # extensionless request that isn't a real file/dir → try <path>.html
        if not os.path.splitext(local)[1] and not os.path.isdir(local):
            html = local + ".html"
            if os.path.isfile(html):
                return html
        return local


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"Serving SlimeBallBench at http://localhost:{PORT}/  (Ctrl-C to stop)")
        httpd.serve_forever()
