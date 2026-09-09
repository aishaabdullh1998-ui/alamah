#!/bin/bash
# يجمع كل ملفات المستودع في ملف واحد: alamah-github.zip
# يبنيها من قائمة git نفسها، فلا تدخلها قاعدة البيانات ولا أي ملف مستبعد.
cd "$(dirname "$0")" || exit 1
rm -f alamah-github.zip
git ls-files -z | xargs -0 zip -q alamah-github.zip
echo "✔ alamah-github.zip — $(unzip -l alamah-github.zip | tail -1 | awk '{print $2}') ملفًا"
