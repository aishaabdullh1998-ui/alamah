# -*- coding: utf-8 -*-
"""محرّك التصحيح والتشخيص التكيّفي.

التصحيح: تطبيع (إزالة التشكيل + توحيد المسافات) ثم مطابقة حرفية تامة.
التكيّف: اختيار عشوائي مرجّح بأوزان مشتقّة من آخر ٥ محاولات في كل موضوع.
"""

import random
import re

from content import CATEGORY_KEYS, practice_pool

# الحركات والشدّة والسكون والمدّ والألف الخنجرية والتطويل.
_DIACRITICS = re.compile(r"[ً-ْٰـ]")
_WHITESPACE = re.compile(r"\s+")

# طول النافذة المتدحرجة لنتائج كل موضوع.
RECENT_WINDOW = 5
# وزن الموضوع الذي لا توجد عنه بيانات كافية بعد — فرصة عادلة أولاً.
DEFAULT_WEIGHT = 70
# وزن الموضوع المُتقن — منخفض جدًا لا معدوم، لتحقيق مراجعة متباعدة خفيفة.
MASTERED_WEIGHT = 8
# عدد كلمات جلسة التدريب الافتراضية.
DEFAULT_SESSION_SIZE = 6


def normalize(text):
    """يُهيّئ النص للمقارنة: بلا تشكيل، بمسافة مفردة، بلا فراغ طرفي."""
    if not text:
        return ""
    stripped = _DIACRITICS.sub("", text)
    return _WHITESPACE.sub(" ", stripped).strip()


# علامات الترقيم تلتصق بآخر الكلمة بعد التطبيع («جميلٍ.» ← «جميل.») —
# نجرّدها عند مطابقة الكلمة بتصنيفها الإملائي، لا عند تصحيح الكتابة نفسها.
_PUNCT = "،.؟!:؛\"'«»()"


def word_key(word):
    """مفتاح الكلمة للتصنيف: مطبَّعة وبلا علامات ترقيم طرفية."""
    return normalize(word).strip(_PUNCT)


def is_correct(expected, written):
    """مطابقة حرفية تامة بعد التطبيع — أي اختلاف في حرف واحد يُعدّ خطأ."""
    return normalize(expected) == normalize(written)


def is_mastered(recent):
    """الموضوع مُتقن إذا كانت آخر ٥ محاولات كلها صحيحة."""
    return len(recent) >= RECENT_WINDOW and all(recent[-RECENT_WINDOW:])


def recent_rate(recent):
    """نسبة النجاح في آخر ٥ محاولات، أو None إن لم توجد بيانات."""
    window = recent[-RECENT_WINDOW:]
    if not window:
        return None
    return round(100 * sum(1 for r in window if r) / len(window))


def overall_rate(stats_row):
    """نسبة النجاح التراكمية لموضوع، أو None إن لم توجد محاولات."""
    if not stats_row or not stats_row.get("attempts"):
        return None
    return round(100 * stats_row["correct"] / stats_row["attempts"])


def category_weight(stats_row):
    """وزن الموضوع في القرعة المرجّحة: كلما ضعُف الطالب فيه زاد وزنه."""
    recent = (stats_row or {}).get("recent") or []
    if is_mastered(recent):
        return MASTERED_WEIGHT
    rate = recent_rate(recent)
    if rate is None:
        return DEFAULT_WEIGHT
    # 100% نجاح -> وزن 0، لذا نضمن حدًا أدنى يبقيه في القرعة.
    return max(MASTERED_WEIGHT, 100 - rate)


def weighted_categories(stats, count, rng=None):
    """يسحب `count` موضوعًا بعشوائية مرجّحة (مع الإرجاع) حسب أوزان الضعف."""
    rng = rng or random
    weights = [category_weight(stats.get(key)) for key in CATEGORY_KEYS]
    if sum(weights) <= 0:
        weights = [1] * len(CATEGORY_KEYS)
    return rng.choices(CATEGORY_KEYS, weights=weights, k=count)


def build_adaptive_session(stats, size=DEFAULT_SESSION_SIZE, rng=None):
    """يبني جلسة تدريب: مواضيع مرجّحة بالضعف، وكلمات غير مكرّرة داخل الجلسة."""
    rng = rng or random
    picks = weighted_categories(stats, size, rng=rng)
    used = set()
    items = []
    for category in picks:
        pool = [w for w in practice_pool(category) if w not in used]
        if not pool:                       # نفد المسبح غير المكرّر لهذا الموضوع
            pool = practice_pool(category)
        word = rng.choice(pool)
        used.add(word)
        items.append({"word": word, "category": category})
    rng.shuffle(items)
    return items


def mastery_map(stats):
    """خريطة الإتقان المعروضة للطالب وللمعلّم، مرتّبة من الأضعف إلى الأقوى."""
    rows = []
    for key in CATEGORY_KEYS:
        row = stats.get(key) or {"attempts": 0, "correct": 0, "recent": []}
        recent = row.get("recent") or []
        rows.append({
            "category": key,
            "attempts": row.get("attempts", 0),
            "correct": row.get("correct", 0),
            "rate": overall_rate(row),
            "recentRate": recent_rate(recent),
            "mastered": is_mastered(recent),
        })
    # الأضعف أولاً؛ والمواضيع بلا بيانات توضع في الوسط (تُعامل كـ ٥٠٪).
    rows.sort(key=lambda r: r["rate"] if r["rate"] is not None else 50)
    return rows


def grade_paragraph(expected, written):
    """يقارن فقرة كلمةً بكلمة بعد التطبيع، ويعيد تفصيلاً لكل كلمة."""
    exp_words = normalize(expected).split(" ")
    got_words = normalize(written).split(" ") if normalize(written) else []
    items = []
    for i, exp in enumerate(exp_words):
        got = got_words[i] if i < len(got_words) else ""
        items.append({
            "correct_text": exp,
            "written_text": got,
            "is_correct": exp == got,
        })
    extra = got_words[len(exp_words):]
    score = sum(1 for it in items if it["is_correct"])
    return {
        "items": items,
        "extra_words": extra,
        "score": score,
        "total": len(items),
    }


# ══════════════ الكؤوس والمهام الأسبوعية والخطة العلاجية ══════════════

# «التدريب» = جلسة تدريب مكتملة أو تمرين فقرة مكتمل.
TROPHIES = [
    {"key": "bronze", "label": "الكأس البرونزي", "needed": 10,
     "color": "#DD6B3A", "ring": "#F8B553"},
    {"key": "silver", "label": "الكأس الفضي", "needed": 20,
     "color": "#94B2ED", "ring": "#BFD1F4"},
    {"key": "gold", "label": "الكأس الذهبي", "needed": 30,
     "color": "#F4D77A", "ring": "#F8B553"},
]

WEEKLY_GOAL = 3          # عدد التدريبات المطلوبة في الأسبوع


def trophy_state(sessions_done):
    """الكؤوس المحققة، والكأس التالي وكم بقي عليه."""
    earned = [t for t in TROPHIES if sessions_done >= t["needed"]]
    nxt = next((t for t in TROPHIES if sessions_done < t["needed"]), None)
    return {
        "sessionsDone": sessions_done,
        "earned": [t["key"] for t in earned],
        "next": None if not nxt else {
            "key": nxt["key"], "label": nxt["label"], "needed": nxt["needed"],
            "remaining": nxt["needed"] - sessions_done,
            "progress": round(100 * sessions_done / nxt["needed"]),
        },
    }


def weekly_state(done_this_week, goal=WEEKLY_GOAL):
    """المهمة الأسبوعية: كم أنجز الطالب من هدف الأسبوع."""
    done = min(done_this_week, goal)
    return {
        "done": done_this_week,
        "goal": goal,
        "complete": done_this_week >= goal,
        "remaining": max(0, goal - done_this_week),
        "progress": round(100 * done / goal) if goal else 0,
    }


def grade_diagnostic_sentence(sentence, written):
    """يصحّح جملة التشخيص ويعيد لكل كلمة مصنّفة نتيجتها ومسألتها.

    يعيد (تفصيل الكلمات، نتائج مصنّفة كـ [(category, correct), ...]).
    الكلمات غير المصنّفة تُعرض للطالب لكنها لا تدخل في تشخيص المسائل.
    """
    exp_words = normalize(sentence["text"]).split(" ")
    got_words = normalize(written).split(" ") if normalize(written) else []
    tags = sentence["tags"]
    items, graded = [], []
    for i, exp in enumerate(exp_words):
        got = got_words[i] if i < len(got_words) else ""
        correct = exp == got
        key = exp.strip(_PUNCT)
        category = tags.get(key)
        items.append({"correct_text": exp, "written_text": got,
                      "is_correct": correct, "category": category})
        if category:
            graded.append((category, correct))
    return {"items": items,
            "score": sum(1 for it in items if it["is_correct"]),
            "total": len(items)}, graded


def remedial_plan(mastery, weekly, trophies):
    """خطة علاجية مشتقّة من أداء الطالب — تُعرض للمعلّمة نصًا لا رقمًا.

    نقاط الضعف تأخذ تدريبًا مركّزًا، ونقاط القوة تأخذ مراجعة خفيفة
    تمنع تراجعها. الترتيب من الأضعف إلى الأقوى كما في خريطة الإتقان.
    """
    focus, maintain, watch = [], [], []
    for m in mastery:
        rate = m["rate"]
        if m["mastered"]:
            maintain.append(m)
        elif rate is None:
            watch.append(m)
        elif rate < 50:
            focus.append(m)
        else:
            watch.append(m)

    steps = []
    for m in focus[:2]:                       # التركيز على مسألتين لا أكثر
        steps.append({
            "kind": "focus",
            "category": m["category"],
            "rate": m["rate"],
            "action": "تدريب مركّز: التطبيق يرجّح كلمات هذه المسألة تلقائيًا "
                      "في كل جلسة حتى تصح آخر خمس محاولات متتالية.",
            "target": "الهدف: إتقانها خلال {} تدريبات.".format(
                max(3, WEEKLY_GOAL)),
        })
    for m in watch[:2]:
        steps.append({
            "kind": "watch",
            "category": m["category"],
            "rate": m["rate"],
            "action": "متابعة: الأداء متذبذب — كلمات هذه المسألة تظهر بوزن "
                      "متوسط حتى يثبت الإتقان.",
            "target": None,
        })
    for m in maintain:
        steps.append({
            "kind": "maintain",
            "category": m["category"],
            "rate": m["rate"],
            "action": "مراجعة متباعدة: كلمة من هذه المسألة تظهر أحيانًا "
                      "للتأكد من عدم تراجعها.",
            "target": None,
        })

    # العنوان يُصاغ في طبقة العرض لأنه يحتاج تسميات المسائل العربية.
    if focus:
        headline_kind = "focus"
    elif watch:
        headline_kind = "watch"
    elif maintain:
        headline_kind = "maintain"
    else:
        headline_kind = "empty"

    return {
        "headlineKind": headline_kind,
        "focusCategories": [f["category"] for f in focus[:2]],
        "focusCount": len(focus),
        "masteredCount": len(maintain),
        "steps": steps,
        "weekly": weekly,
        "trophies": trophies,
    }
