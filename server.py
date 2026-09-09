# -*- coding: utf-8 -*-
"""تشغيل محلي للتجربة على هذا الجهاز:  python3 server.py

النسخة السحابية تستورد `application` من app.py مباشرة ولا تمرّ بهذا الملف.
"""

import os
import socket
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server
from socketserver import ThreadingMixIn

from app import application

PORT = int(os.environ.get("PORT", "8000"))


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """يخدم عدة طلاب في وقت واحد بدل طابور واحد."""
    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, fmt, *args):
        pass                       # يبقى المخرَج نظيفًا للمعلّمة


def local_ips():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return [ip]
    except OSError:
        return []


def main():
    try:
        httpd = make_server("0.0.0.0", PORT, application,
                            server_class=ThreadingWSGIServer,
                            handler_class=QuietHandler)
    except OSError as e:
        if e.errno in (48, 98):
            print("\n  ⚠️  المنفذ {0} مستخدم بالفعل — غالبًا التطبيق يعمل في"
                  " نافذة أخرى.\n      افتح http://localhost:{0} مباشرة،"
                  " أو شغّله بمنفذ آخر:  PORT=8001 python3 server.py\n"
                  .format(PORT), flush=True)
        else:
            print("\n  ⚠️  تعذّر التشغيل: {}\n".format(e), flush=True)
        raise SystemExit(1)

    lines = ["", "  ✿  عَلَامَةٌ — التطبيق يعمل الآن",
             "  ─────────────────────────────────────────────",
             "  على هذا الجهاز:    http://localhost:{}".format(PORT)]
    for ip in local_ips():
        lines.append("  من أجهزة الطلاب:  http://{}:{}".format(ip, PORT))
    lines += ["  ─────────────────────────────────────────────",
              "  للإيقاف: اضغط Ctrl+C", ""]
    print("\n".join(lines), flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  إلى اللقاء ✿\n", flush=True)
        httpd.server_close()


if __name__ == "__main__":
    main()
