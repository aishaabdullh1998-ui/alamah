# -*- coding: utf-8 -*-
"""علامة — تطبيق الإملاء التشخيصي التكيّفي (WSGI).

يعمل في مكانين بالكود نفسه:
  • محليًا:  python3 server.py
  • سحابيًا: أي استضافة WSGI (PythonAnywhere مثلاً) تستورد `application` من هنا.

مفاتيح اختيارية تُقرأ من بيئة الخادم فقط (لا تصل للمتصفح أبدًا):
  ANTHROPIC_API_KEY    يفعّل تصحيح الفقرات المصوَّرة بنموذج رؤية.
  ELEVENLABS_API_KEY   يفعّل نطقًا عصبيًا بدل صوت المتصفح.
  ELEVENLABS_VOICE_ID  معرّف الصوت (اختياري).
"""

import base64
import collections
import http.cookies
import json
import mimetypes
import os
import random
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import content
import engine
import store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
COOKIE_NAME = "imla_session"
MAX_BODY = 12 * 1024 * 1024

# بايثون لا يعرف أنواع الخطوط الحديثة افتراضيًا
FONT_TYPES = {".woff2": "font/woff2", ".woff": "font/woff",
              ".ttf": "font/ttf", ".otf": "font/otf"}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID",
                                     "21m00Tcm4TlvDq8ikWAM").strip()

# الرابط السحابي عام ورقم الدخول ٤ خانات، فنبطّئ التخمين الآلي.
# ملاحظة مهمة: طلاب المدرسة كلهم خلف عنوان IP واحد (NAT)، لذا لا نقفل
# العنوان — قفله يعني حرمان الصف كله بسبب أخطاء طالب واحد. بدلاً من ذلك
# نؤخّر الرد تدريجيًا مع كل محاولة فاشلة: لا يشعر به طالب أخطأ مرة أو
# مرتين، بينما يجعل تجربة آلاف الأرقام آليًا غير عملية.
LOGIN_WINDOW = 300          # خمس دقائق
LOGIN_MAX_FAILS = 100       # صمّام أمان بعيد جدًا عن الاستعمال الطبيعي
LOGIN_DELAY_STEP = 0.4      # ثانية لكل محاولة فاشلة حديثة
LOGIN_DELAY_MAX = 3.0
_fails = collections.defaultdict(list)
_fails_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _recent_fails(ip):
    now = time.time()
    with _fails_lock:
        hits = [t for t in _fails[ip] if now - t < LOGIN_WINDOW]
        if hits:
            _fails[ip] = hits
        else:
            _fails.pop(ip, None)
        return len(hits)


def _too_many_logins(ip):
    return _recent_fails(ip) >= LOGIN_MAX_FAILS


def _throttle(ip):
    """يؤخّر الرد بقدر المحاولات الفاشلة الحديثة، قبل فحص الرقم."""
    n = _recent_fails(ip)
    if n:
        time.sleep(min(n * LOGIN_DELAY_STEP, LOGIN_DELAY_MAX))


def _note_failed_login(ip):
    with _fails_lock:
        _fails[ip].append(time.time())


def _clear_logins(ip):
    with _fails_lock:
        _fails.pop(ip, None)


# ------------------------------------------------------------ الردود

def public_categories():
    return [{"key": c["key"], "label": c["label"], "color": c["color"],
             "rule": c["rule"]} for c in content.CATEGORIES]


def student_state(conn, student_id):
    student = store.get_student(conn, student_id)
    if not student:
        raise ApiError(404, "لم يُعثر على الطالب.")
    teacher = store.get_teacher(conn, student["teacherId"])
    done = store.count_practice(conn, student_id)
    return {"role": "student",
            "student": {"id": student["id"], "name": student["name"],
                        "diagnosticDone": student["diagnosticDone"],
                        "teacherName": teacher["name"] if teacher else None},
            "mastery": engine.mastery_map(store.get_stats(conn, student_id)),
            "trophies": engine.trophy_state(done),
            "weekly": engine.weekly_state(
                store.count_practice_this_week(conn, student_id)),
            "trophyCatalog": engine.TROPHIES}


def plan_headline(plan):
    """عنوان الخطة العلاجية بتسميات المسائل العربية."""
    kind = plan.get("headlineKind")
    if kind == "focus":
        names = "، ".join(content.CATEGORY_BY_KEY[c]["label"]
                          for c in plan.get("focusCategories", []))
        return "التركيز الآن على: " + names
    if kind == "watch":
        return "لا يوجد ضعف حادّ — المطلوب تثبيت المسائل المتذبذبة."
    if kind == "maintain":
        return "أتقن المسائل الخمس — المطلوب مراجعة متباعدة فقط."
    return "لم يُكمل الاختبار التشخيصي بعد."


def class_overview(conn, teacher_id):
    students = store.list_students_for_teacher(conn, teacher_id)
    cards = []
    totals = {k: {"attempts": 0, "correct": 0} for k in content.CATEGORY_KEYS}
    for s in students:
        stats = store.get_stats(conn, s["id"])
        mastery = engine.mastery_map(stats)
        rated = [m["rate"] for m in mastery if m["rate"] is not None]
        for key in content.CATEGORY_KEYS:
            totals[key]["attempts"] += stats[key]["attempts"]
            totals[key]["correct"] += stats[key]["correct"]
        done = store.count_practice(conn, s["id"])
        cards.append({"id": s["id"], "name": s["name"],
                      "username": s["username"], "secret": s["secret"],
                      "practiceDone": done,
                      "trophies": engine.trophy_state(done)["earned"],
                      "weekly": engine.weekly_state(
                          store.count_practice_this_week(conn, s["id"])),
                      "diagnosticDone": bool(s["diagnostic_done"]),
                      "lastSeen": s["last_seen"],
                      "masteredCount": sum(1 for m in mastery if m["mastered"]),
                      "totalCategories": len(content.CATEGORY_KEYS),
                      "totalAttempts": sum(m["attempts"] for m in mastery),
                      "averageRate": round(sum(rated) / len(rated)) if rated else None})
    class_rows = []
    for key in content.CATEGORY_KEYS:
        t = totals[key]
        class_rows.append({"category": key, "attempts": t["attempts"],
                           "correct": t["correct"],
                           "rate": round(100 * t["correct"] / t["attempts"])
                                   if t["attempts"] else None})
    class_rows.sort(key=lambda r: r["rate"] if r["rate"] is not None else 101)
    return {"students": cards, "classAverages": class_rows}


# ------------------------------------------------ خدمات خارجية اختيارية

def neural_tts(text, rate):
    payload = json.dumps({
        "text": text, "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75,
                           "speed": 0.75 if rate == "slow" else 1.0}}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/text-to-speech/" + ELEVENLABS_VOICE_ID,
        data=payload,
        headers={"xi-api-key": ELEVENLABS_API_KEY,
                 "Content-Type": "application/json", "Accept": "audio/mpeg"})
    with urllib.request.urlopen(req, timeout=30,
                                context=ssl.create_default_context()) as r:
        return r.read(), "audio/mpeg"


VISION_PROMPT = """أنت مصحّح إملاء عربي. في الصورة ورقة كتب فيها طالب فقرة إملاء بخط اليد.

النص الصحيح المطلوب هو:
{expected}

اقرأ ما كتبه الطالب فعلاً في الصورة، وقارنه بالنص الصحيح كلمةً بكلمة.
تجاهل اختلافات التشكيل (الحركات) تمامًا — قارن الحروف فقط.
إن تعذّرت قراءة كلمة بوضوح فاذكر ذلك في الحقل note بدل تخمينها.

أعد ردًا بصيغة JSON فقط، بلا أي نص خارج الـ JSON، بهذا الشكل:
{{"items":[{{"correct_text":"","written_text":"","is_correct":true,"note":""}}],
"score":0,"total":0,"overall_feedback":""}}"""


def vision_grade(image_b64, media_type, expected):
    if not ANTHROPIC_API_KEY:
        raise ApiError(503, "تصحيح الصور غير مفعّل على هذا الخادم.")
    payload = json.dumps({
        "model": "claude-opus-5", "max_tokens": 2000,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": VISION_PROMPT.format(expected=expected)}]}]
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload,
        headers={"x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120,
                                    context=ssl.create_default_context()) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ApiError(502, "تعذّر الاتصال بنموذج الرؤية: {}".format(e.code))
    except urllib.error.URLError as e:
        raise ApiError(502, "تعذّر الاتصال بنموذج الرؤية: {}".format(e.reason))
    text = "".join(b.get("text", "") for b in body.get("content", []))
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ApiError(502, "رد نموذج الرؤية غير مفهوم.")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise ApiError(502, "رد نموذج الرؤية غير مفهوم.")


# ------------------------------------------------------------- الطلب

class Request:
    def __init__(self, environ):
        self.environ = environ
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        self.path = environ.get("PATH_INFO", "/") or "/"
        self.query = environ.get("QUERY_STRING", "")
        self.secure = self._is_secure()
        self.ip = self._client_ip()
        self._body = None

    def _is_secure(self):
        proto = self.environ.get("HTTP_X_FORWARDED_PROTO", "")
        return proto.split(",")[0].strip() == "https" or \
            self.environ.get("wsgi.url_scheme") == "https"

    def _client_ip(self):
        fwd = self.environ.get("HTTP_X_FORWARDED_FOR", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.environ.get("REMOTE_ADDR", "?")

    def json(self):
        if self._body is None:
            try:
                length = int(self.environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                self._body = {}
            elif length > MAX_BODY:
                raise ApiError(413, "حجم الطلب كبير جدًا.")
            else:
                raw = self.environ["wsgi.input"].read(length)
                try:
                    self._body = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise ApiError(400, "صيغة الطلب غير صحيحة.")
            if not isinstance(self._body, dict):
                self._body = {}
        return self._body

    def cookie(self):
        raw = self.environ.get("HTTP_COOKIE")
        if not raw:
            return None
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return None
        m = jar.get(COOKIE_NAME)
        return m.value if m else None


def set_cookie(req, token, max_age=store.SESSION_TTL):
    # HttpOnly: لا يقرؤه جافاسكربت. Secure: يُضاف تلقائيًا على https.
    parts = ["{}={}".format(COOKIE_NAME, token), "Path=/", "HttpOnly",
             "SameSite=Lax", "Max-Age={}".format(max_age)]
    if req.secure:
        parts.append("Secure")
    return ("Set-Cookie", "; ".join(parts))


def require_session(req, conn, role=None):
    session = store.resolve_session(conn, req.cookie())
    if not session:
        raise ApiError(401, "انتهت الجلسة — سجّل الدخول من جديد.")
    if role and session[0] != role:
        raise ApiError(403, "لا تملك صلاحية الوصول إلى هذا المورد.")
    return session


# ------------------------------------------------------------ التوجيه

def dispatch(req, conn):
    """يعيد (data, status, extra_headers)."""
    path = req.path.rstrip("/") or "/"
    method = req.method
    body = req.json() if method == "POST" else {}

    if path == "/api/bootstrap" and method == "GET":
        store.purge_expired_sessions(conn)
        data = {"categories": public_categories(),
                "paragraphs": content.PARAGRAPHS,
                "neuralTts": bool(ELEVENLABS_API_KEY),
                "visionGrading": bool(ANTHROPIC_API_KEY)}
        session = store.resolve_session(conn, req.cookie())
        if session and session[0] == "student":
            store.touch_student(conn, session[1])
            data["session"] = student_state(conn, session[1])
        elif session and session[0] == "teacher":
            teacher = store.get_teacher(conn, session[1])
            if teacher:
                data["session"] = {"role": "teacher", "teacher": teacher}
        return data, 200, None

    if path == "/api/logout" and method == "POST":
        token = req.cookie()
        if token:
            store.destroy_session(conn, token)
        return {"ok": True}, 200, [("Set-Cookie", "{}=; Path=/; HttpOnly;"
                                    " SameSite=Lax; Max-Age=0".format(COOKIE_NAME))]

    # ---------- دخول الطالب: رقم واحد فقط
    if path == "/api/student/login" and method == "POST":
        if _too_many_logins(req.ip):
            raise ApiError(429, "محاولات كثيرة جدًا. انتظر قليلاً ثم أعد المحاولة.")
        _throttle(req.ip)
        username = (body.get("username") or "").strip()
        secret = (body.get("secret") or "").strip()
        if not (username.isdigit() and len(username) == store.CODE_LENGTH):
            raise ApiError(400, "رقم المستخدم ٤ أرقام.")
        if not (secret.isdigit() and len(secret) == store.CODE_LENGTH):
            raise ApiError(400, "الرقم السري ٤ أرقام.")
        student_id = store.authenticate_student(conn, username, secret)
        if not student_id:
            _note_failed_login(req.ip)
            raise ApiError(401, "رقم المستخدم أو الرقم السري غير صحيح. "
                                "تأكّد منه مع معلّمتك.")
        _clear_logins(req.ip)
        token = store.create_session(conn, "student", student_id)
        return student_state(conn, student_id), 200, [set_cookie(req, token)]

    # ---------- حساب المعلّمة
    if path == "/api/teacher/register" and method == "POST":
        name = (body.get("name") or "").strip()
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        if len(name) < 2:
            raise ApiError(400, "اكتبي اسمك.")
        if "@" not in email or len(email) < 5:
            raise ApiError(400, "البريد الإلكتروني غير صحيح.")
        if len(password) < 8:
            raise ApiError(400, "كلمة المرور ٨ أحرف على الأقل.")
        teacher = store.create_teacher(conn, name, email, password)
        if not teacher:
            raise ApiError(409, "هذا البريد مسجّل — سجّلي الدخول بدل ذلك.")
        token = store.create_session(conn, "teacher", teacher["id"])
        return {"role": "teacher", "teacher": teacher}, 200, [set_cookie(req, token)]

    if path == "/api/teacher/login" and method == "POST":
        if _too_many_logins(req.ip):
            raise ApiError(429, "محاولات كثيرة جدًا. انتظري قليلاً ثم أعيدي المحاولة.")
        _throttle(req.ip)
        teacher = store.authenticate_teacher(conn, body.get("email") or "",
                                             body.get("password") or "")
        if not teacher:
            _note_failed_login(req.ip)
            raise ApiError(401, "البريد أو كلمة المرور غير صحيحة.")
        _clear_logins(req.ip)
        token = store.create_session(conn, "teacher", teacher["id"])
        return {"role": "teacher", "teacher": teacher}, 200, [set_cookie(req, token)]

    # ---------- الطالب
    if path == "/api/student/state" and method == "GET":
        _, sid = require_session(req, conn, "student")
        return student_state(conn, sid), 200, None

    if path == "/api/student/diagnostic" and method == "GET":
        require_session(req, conn, "student")
        items = content.diagnostic_items()[:]
        random.shuffle(items)
        return {"mode": "diagnostic", "items": items,
                "sentence": {"id": content.DIAGNOSTIC_SENTENCE["id"],
                             "text": content.DIAGNOSTIC_SENTENCE["text"]}}, 200, None

    if path == "/api/student/session" and method == "GET":
        _, sid = require_session(req, conn, "student")
        stats = store.get_stats(conn, sid)
        return {"mode": "adaptive",
                "items": engine.build_adaptive_session(
                    stats, engine.DEFAULT_SESSION_SIZE)}, 200, None

    if path == "/api/student/attempt" and method == "POST":
        _, sid = require_session(req, conn, "student")
        word = body.get("word") or ""
        category = body.get("category") or ""
        answer = body.get("answer") or ""
        mode = "diagnostic" if body.get("mode") == "diagnostic" else "adaptive"
        if category not in content.CATEGORY_BY_KEY:
            raise ApiError(400, "موضوع غير معروف.")
        if word not in content.WORD_BANK[category]:
            raise ApiError(400, "كلمة غير موجودة في بنك هذا الموضوع.")
        before = engine.is_mastered(store.get_stats(conn, sid)[category]["recent"])
        correct = engine.is_correct(word, answer)
        store.record_attempt(conn, sid, category, word, answer, correct, mode)
        after = engine.is_mastered(store.get_stats(conn, sid)[category]["recent"])
        return {"correct": correct, "expected": word, "written": answer,
                "category": category,
                "justMastered": category if (after and not before) else None,
                "rule": content.CATEGORY_BY_KEY[category]["rule"]}, 200, None

    if path == "/api/student/diagnostic/sentence" and method == "POST":
        _, sid = require_session(req, conn, "student")
        sen = content.DIAGNOSTIC_SENTENCE
        result, graded = engine.grade_diagnostic_sentence(sen, body.get("answer") or "")
        # كل كلمة مصنّفة تُسجَّل كمحاولة في مسألتها، فتدخل التشخيص كبقية الكلمات.
        for category, ok in graded:
            store.record_attempt(conn, sid, category, sen["text"][:60],
                                 "(جملة)" if ok else "(جملة — خطأ)", ok, "diagnostic")
        result["expected"] = sen["text"]
        result["source"] = "sentence"
        return result, 200, None

    if path == "/api/student/practice/complete" and method == "POST":
        _, sid = require_session(req, conn, "student")
        kind = "paragraph" if body.get("kind") == "paragraph" else "adaptive"
        try:
            score = max(0, int(body.get("score") or 0))
            total = max(0, int(body.get("total") or 0))
        except (TypeError, ValueError):
            raise ApiError(400, "نتيجة غير صحيحة.")
        before = engine.trophy_state(store.count_practice(conn, sid))["earned"]
        store.record_practice(conn, sid, kind, score, total)
        done = store.count_practice(conn, sid)
        after = engine.trophy_state(done)
        new = [k for k in after["earned"] if k not in before]
        return {"trophies": after,
                "weekly": engine.weekly_state(
                    store.count_practice_this_week(conn, sid)),
                "newTrophies": new}, 200, None

    if path == "/api/student/diagnostic/finish" and method == "POST":
        _, sid = require_session(req, conn, "student")
        store.set_diagnostic_done(conn, sid, True)
        return student_state(conn, sid), 200, None

    # ---------- الفقرات
    if path == "/api/paragraph/grade-text" and method == "POST":
        require_session(req, conn, "student")
        para = content.PARAGRAPH_BY_ID.get(body.get("paragraphId"))
        if not para:
            raise ApiError(400, "فقرة غير معروفة.")
        result = engine.grade_paragraph(para["text"], body.get("answer") or "")
        result["source"] = "text"
        result["expected"] = para["text"]
        return result, 200, None

    if path == "/api/paragraph/grade-image" and method == "POST":
        require_session(req, conn, "student")
        para = content.PARAGRAPH_BY_ID.get(body.get("paragraphId"))
        if not para:
            raise ApiError(400, "فقرة غير معروفة.")
        image = body.get("image") or ""
        media_type = body.get("mediaType") or "image/jpeg"
        if image.startswith("data:"):
            header, _, image = image.partition(",")
            media_type = header[5:].split(";")[0] or "image/jpeg"
        if not image:
            raise ApiError(400, "لم تُرفق صورة.")
        try:
            base64.b64decode(image, validate=True)
        except (ValueError, TypeError):
            raise ApiError(400, "الصورة غير صالحة.")
        result = vision_grade(image, media_type, para["text"])
        result["source"] = "image"
        result["expected"] = para["text"]
        return result, 200, None

    # ---------- لوحة المعلّمة
    if path == "/api/teacher/overview" and method == "GET":
        _, tid = require_session(req, conn, "teacher")
        data = class_overview(conn, tid)
        data["teacher"] = store.get_teacher(conn, tid)
        return data, 200, None

    if path == "/api/teacher/students" and method == "POST":
        _, tid = require_session(req, conn, "teacher")
        student, err = store.add_student(conn, tid, body.get("name"),
                                         body.get("username"), body.get("secret"))
        if err:
            raise ApiError(400, err)
        return {"student": student}, 200, None

    if path.startswith("/api/teacher/student/"):
        _, tid = require_session(req, conn, "teacher")
        rest = path[len("/api/teacher/student/"):]
        parts = rest.split("/")
        try:
            sid = int(parts[0])
        except (ValueError, IndexError):
            raise ApiError(400, "معرّف طالب غير صحيح.")
        # التقييد على الخادم: لا ترى معلّمة طلاب معلّمة أخرى.
        if not store.student_belongs_to_teacher(conn, sid, tid):
            raise ApiError(403, "هذا الطالب ليس في قائمتك.")
        action = parts[1] if len(parts) > 1 else ""

        if not action and method == "GET":
            student = store.get_student(conn, sid)
            mastery = engine.mastery_map(store.get_stats(conn, sid))
            done = store.count_practice(conn, sid)
            weekly = engine.weekly_state(store.count_practice_this_week(conn, sid))
            trophies = engine.trophy_state(done)
            plan = engine.remedial_plan(mastery, weekly, trophies)
            # الخطة تُعيد مفاتيح المسائل — نحوّلها لتسميات وقواعد للعرض.
            for step in plan["steps"]:
                cat = content.CATEGORY_BY_KEY.get(step["category"], {})
                step["label"] = cat.get("label", step["category"])
                step["rule"] = cat.get("rule", "")
            plan["headline"] = plan_headline(plan)
            return {"student": {"id": student["id"], "name": student["name"],
                                "username": student["username"],
                                "secret": student["secret"],
                                "diagnosticDone": student["diagnosticDone"],
                                "lastSeen": student["lastSeen"]},
                    "mastery": mastery, "plan": plan,
                    "trophies": trophies, "weekly": weekly}, 200, None

        if action == "rename" and method == "POST":
            err = store.rename_student(conn, sid, body.get("name"))
            if err:
                raise ApiError(400, err)
            return {"ok": True}, 200, None

        if action == "credentials" and method == "POST":
            err = store.set_student_credentials(conn, sid, body.get("username"),
                                                body.get("secret"))
            if err:
                raise ApiError(400, err)
            return {"ok": True}, 200, None

        if action == "reset" and method == "POST":
            store.reset_stats(conn, sid)
            return {"ok": True}, 200, None

        if action == "delete" and method == "POST":
            store.delete_student(conn, sid)
            return {"ok": True}, 200, None

    raise ApiError(404, "المسار غير موجود.")


# ------------------------------------------------------- الملفات الثابتة

def serve_static(path):
    rel = "index.html" if path == "/" else path.lstrip("/")
    full = os.path.normpath(os.path.join(STATIC_DIR, rel))
    if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
        return "404 Not Found", [("Content-Type", "text/plain; charset=utf-8")], b"404"
    ctype = mimetypes.guess_type(full)[0] or FONT_TYPES.get(
        os.path.splitext(full)[1].lower(), "application/octet-stream")
    if ctype.startswith("text/") or ctype in ("application/javascript",
                                              "text/javascript", "image/svg+xml"):
        ctype += "; charset=utf-8"
    with open(full, "rb") as f:
        return "200 OK", [("Content-Type", ctype),
                          ("Cache-Control", "no-cache")], f.read()


def serve_tts(req, conn):
    require_session(req, conn)
    params = urllib.parse.parse_qs(req.query)
    text = (params.get("text") or [""])[0].strip()
    rate = (params.get("rate") or ["normal"])[0]
    if not text:
        raise ApiError(400, "لا يوجد نص.")
    if len(text) > 600:
        raise ApiError(400, "النص طويل جدًا.")
    if not ELEVENLABS_API_KEY:
        raise ApiError(501, "النطق العصبي غير مفعّل.")
    try:
        audio, mime = neural_tts(text, rate)
    except (urllib.error.URLError, OSError) as e:
        raise ApiError(502, "تعذّر توليد الصوت: {}".format(e))
    return "200 OK", [("Content-Type", mime),
                      ("Cache-Control", "private, max-age=86400")], audio


STATUS_TEXT = {200: "200 OK", 400: "400 Bad Request", 401: "401 Unauthorized",
               403: "403 Forbidden", 404: "404 Not Found", 409: "409 Conflict",
               413: "413 Payload Too Large", 429: "429 Too Many Requests",
               500: "500 Internal Server Error", 501: "501 Not Implemented",
               502: "502 Bad Gateway", 503: "503 Service Unavailable"}

SECURITY_HEADERS = [("X-Content-Type-Options", "nosniff"),
                    ("Referrer-Policy", "no-referrer"),
                    ("X-Frame-Options", "SAMEORIGIN")]

_db_ready = False
_db_lock = threading.Lock()


def ensure_db():
    global _db_ready
    if _db_ready:
        return
    with _db_lock:
        if not _db_ready:
            store.init_db()
            _db_ready = True


def application(environ, start_response):
    ensure_db()
    req = Request(environ)

    if not req.path.startswith("/api"):
        status, headers, body = serve_static(req.path)
        headers = headers + SECURITY_HEADERS + [("Content-Length", str(len(body)))]
        start_response(status, headers)
        return [body]

    conn = store.connect()
    try:
        if req.path.rstrip("/") == "/api/tts":
            status, headers, body = serve_tts(req, conn)
        else:
            data, code, extra = dispatch(req, conn)
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            status = STATUS_TEXT.get(code, "200 OK")
            headers = [("Content-Type", "application/json; charset=utf-8")]
            headers += list(extra or [])
    except ApiError as e:
        body = json.dumps({"error": e.message}, ensure_ascii=False).encode("utf-8")
        status = STATUS_TEXT.get(e.status, "400 Bad Request")
        headers = [("Content-Type", "application/json; charset=utf-8")]
    except Exception:                                          # noqa: BLE001
        import traceback
        traceback.print_exc()
        body = json.dumps({"error": "حدث خطأ في الخادم."},
                          ensure_ascii=False).encode("utf-8")
        status = "500 Internal Server Error"
        headers = [("Content-Type", "application/json; charset=utf-8")]
    finally:
        conn.close()

    headers = headers + SECURITY_HEADERS + [("Content-Length", str(len(body)))]
    start_response(status, headers)
    return [body]
