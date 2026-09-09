#!/bin/bash
# يبني حزمة الرفع السحابي من المصدر.
cd "$(dirname "$0")" || exit 1
rm -f imla-upload.zip
zip -r -q imla-upload.zip app.py content.py engine.py server.py store.py static \
    -x '*.DS_Store' -x 'static/fonts/*.woff2' -x 'static/fonts/*.woff' \
    -x 'static/fonts/*.otf' -x 'static/fonts/*.ttf'
echo "✔ imla-upload.zip جاهزة للرفع"
