#!/bin/bash
# ملف تشغيل يُفتح بالنقر المزدوج من Finder.
cd "$(dirname "$0")" || exit 1
clear
python3 server.py
