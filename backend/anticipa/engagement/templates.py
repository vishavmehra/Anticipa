"""Approved outreach message templates (en / hi / ar) and deterministic rendering.

Constraints every message must satisfy (`check_constraints`, run on every render and on any future AI draft before it
may be used): it names only the guideline care activity, makes no diagnosis or clinical claim, points to a clinician for
questions, carries the localized "simulated message" marker, and has no unfilled placeholder. A wording change is a NEW
template version (rows are immutable once stored); old events keep pointing at the version they used.

AI drafting (router task `engagement.draft`) is intentionally not wired: the templates below are the default and the
only source of message text in the MVP.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from anticipa.core.errors import ValidationFailed
from anticipa.engagement.models import LANGUAGES, CommunicationTemplate

DEFAULT_LANGUAGE = "en"
DIRECTION = {"en": "ltr", "hi": "ltr", "ar": "rtl"}
MAX_BODY_CHARS = 700
FORBIDDEN_PHRASES = ("you have cancer", "you are at high risk", "your result", "urgent")  # no clinical claims

SIMULATION_MARKERS = {
    "en": "[SIMULATED MESSAGE - this is a simulation; it was not sent to a real person]",
    "hi": "[सिम्युलेटेड संदेश - यह एक सिमुलेशन है; इसे किसी वास्तविक व्यक्ति को नहीं भेजा गया है]",
    "ar": "[رسالة محاكاة - هذه محاكاة ولم يتم إرسالها إلى شخص حقيقي]",
}

VERSIONS = {"preventive_care_reminder": 1, "preparation_info": 1}
TEMPLATES: dict[str, dict[str, str]] = {
    # Generic arrival information only (what to bring, arrive early). No test specific preparation: a question about that
    # is a clinical question and goes to a clinician. The resident conversation (ADR-071) quotes this text and nothing else.
    "preparation_info": {
        "en": (
            "Please bring a photo ID and any earlier reports about this screening, and arrive a little before your appointment time. "
            "Your clinic will tell you if this test needs any special preparation."
        ),
        "hi": (
            "कृपया अपना फोटो पहचान पत्र और इस जाँच से जुड़ी पिछली रिपोर्ट साथ लाएँ, और अपनी अपॉइंटमेंट के समय से थोड़ा पहले पहुँचें। "
            "यदि इस जाँच के लिए किसी विशेष तैयारी की ज़रूरत होगी, तो आपका क्लिनिक आपको बताएगा।"
        ),
        "ar": (
            "يرجى إحضار هوية بصورة وأي تقارير سابقة تتعلق بهذا الفحص، والوصول قبل موعدك بقليل. "
            "وستخبرك عيادتك إذا كان هذا الفحص يحتاج إلى تحضير خاص."
        ),
    },
    "preventive_care_reminder": {
        "en": (
            "Hello. Your care team would like to remind you that the following preventive care is due: {activity}. "
            "You can arrange it at your local clinic. This is a reminder only, not a diagnosis or medical advice. "
            "If you have any questions about your health, please talk to your doctor or nurse."
        ),
        "hi": (
            "नमस्कार। आपकी देखभाल टीम आपको याद दिलाना चाहती है कि आपके लिए निम्नलिखित निवारक स्वास्थ्य सेवा देय है: {activity}। "
            "आप इसे अपने स्थानीय क्लिनिक में करवा सकते हैं। यह केवल एक अनुस्मारक है, कोई निदान या चिकित्सीय सलाह नहीं। "
            "अपने स्वास्थ्य के बारे में कोई भी प्रश्न हो तो कृपया अपने डॉक्टर या नर्स से बात करें।"
        ),
        "ar": (
            "مرحباً. يودّ فريق الرعاية الخاص بك تذكيرك بأن الرعاية الوقائية التالية مستحقة: {activity}. "
            "يمكنك ترتيبها في عيادتك المحلية. هذا مجرد تذكير وليس تشخيصاً أو نصيحة طبية. "
            "إذا كانت لديك أي أسئلة حول صحتك، يرجى التحدث إلى طبيبك أو ممرضتك."
        ),
    },
}
DEFAULT_TEMPLATE_KEY = "preventive_care_reminder"

# keyword (in the snake_case care_activity) -> label per language; unknown activities fall back to the English key.
_ACTIVITY_LABELS: tuple[tuple[tuple[str, ...], dict[str, str]], ...] = (
    (("brca", "genetic"), {"en": "genetic risk assessment (referral)", "hi": "आनुवंशिक जोखिम आकलन (रेफ़रल)",
                           "ar": "تقييم المخاطر الوراثية (إحالة)"}),  # fmt: skip
    (("colorectal", "colon", "crc"), {"en": "colorectal cancer screening",
                                      "hi": "कोलोरेक्टल (बृहदांत्र एवं मलाशय) कैंसर की जाँच",
                                      "ar": "الفحص للكشف عن سرطان القولون والمستقيم"}),  # fmt: skip
    (("breast", "mammo"), {"en": "breast cancer screening", "hi": "स्तन कैंसर की जाँच",
                           "ar": "الفحص للكشف عن سرطان الثدي"}),  # fmt: skip
    (("cervical", "cervix"), {"en": "cervical cancer screening", "hi": "गर्भाशय-ग्रीवा (सर्वाइकल) कैंसर की जाँच",
                              "ar": "الفحص للكشف عن سرطان عنق الرحم"}),  # fmt: skip
)


def activity_label(care_activity: str, language: str) -> str:
    key = care_activity.lower()
    for keywords, labels in _ACTIVITY_LABELS:
        if any(k in key for k in keywords):
            return labels[language]
    return care_activity.replace("_", " ").strip()


def select_language(requested: str | None, preferred: str | None) -> tuple[str, str]:
    """(language, source). An explicit request must be supported; an unsupported resident preference falls back to
    English (recorded as `default`) rather than failing the outreach."""
    if requested is not None:
        if requested not in LANGUAGES:
            raise ValidationFailed("Unsupported language.", [{"field": "language", "reason": f"one of {list(LANGUAGES)}"}])
        return requested, "requested"
    if preferred in LANGUAGES:
        return preferred, "resident_preference"
    return DEFAULT_LANGUAGE, "default"


def check_constraints(text: str, language: str) -> None:
    """Deterministic gate for any message text (template render or, later, an AI draft)."""
    problems = []
    if SIMULATION_MARKERS[language] not in text:
        problems.append("missing the simulated-message marker")
    if len(text) > MAX_BODY_CHARS:
        problems.append(f"longer than {MAX_BODY_CHARS} characters")
    if "{" in text or "}" in text:
        problems.append("unfilled placeholder")
    lowered = text.lower()
    problems += [f"forbidden phrase: {p!r}" for p in FORBIDDEN_PHRASES if p in lowered]
    if problems:
        raise ValidationFailed("The message violates the approved communication constraints.",
                               [{"field": "body", "reason": p} for p in problems])  # fmt: skip


def render(template_key: str, language: str, care_activity: str) -> str:
    if template_key not in TEMPLATES:
        raise ValidationFailed("Unknown template.", [{"field": "template_key", "reason": f"one of {sorted(TEMPLATES)}"}])
    body = TEMPLATES[template_key][language].replace("{activity}", activity_label(care_activity, language))
    text = f"{SIMULATION_MARKERS[language]}\n{body}"
    check_constraints(text, language)
    return text


def ensure_templates(session: Session) -> dict[tuple[str, str], CommunicationTemplate]:
    """Idempotently store the approved templates (one row per key/language/version) and return the current versions."""
    rows = [
        {"template_key": key, "language": lang, "version": VERSIONS[key], "body": body, "approved": True}
        for key, by_lang in TEMPLATES.items()
        for lang, body in by_lang.items()
    ]
    session.execute(
        pg_insert(CommunicationTemplate).values(rows).on_conflict_do_nothing(
            index_elements=["template_key", "language", "version"]
        )
    )
    found = session.execute(select(CommunicationTemplate)).scalars().all()
    return {(t.template_key, t.language): t for t in found if t.version == VERSIONS.get(t.template_key)}
