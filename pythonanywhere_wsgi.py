# ═══════════════════════════════════════════════════════════════
#  انسخي محتوى هذا الملف كاملاً، والصقيه في ملف WSGI على
#  PythonAnywhere بعد مسح كل ما فيه. لا تحتاجين تعديل أي شيء —
#  المسار يُستنتج تلقائيًا من حسابك.
# ═══════════════════════════════════════════════════════════════

import os
import sys

# مجلد التطبيق داخل حسابك: /home/<اسم المستخدم>/imla
PROJECT = os.path.expanduser("~/imla")

if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from app import application          # noqa: E402,F401
