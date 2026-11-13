# ruff: noqa: E501, RUF001
"""Deterministic safety layer for resident conversations (ADR-071). No model, no database, no clock: pure functions.

Three jobs, each independent of the model so a model failure can never become a clinical failure:

1. `classify` reads the resident's text (English, Hindi, Arabic keyword lists) BEFORE any model call and decides whether it
   is an emergency, a medical question, an eligibility dispute, an opt out or a prompt injection attempt. Those messages
   get an approved fixed reply (`approved`) and never reach the model; emergencies and medical questions raise an
   escalation. The lists are deliberately broad: a false escalation costs a clinician a minute, a missed one is the failure
   this feature must not have.
2. `check_reply` inspects a model reply AFTER generation: no clinical language, no claim that anything was booked or
   changed, every number grounded in the case facts, the right language, no contact details, within the approved length.
3. `approved` holds every fixed reply (emergency, clinician hand off, record review, opt out, scope, unavailable) in the
   three languages. Model replies carry no clinical content by construction of the checks above; these never come from a model.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from anticipa.engagement.templates import (
    FORBIDDEN_PHRASES,
    MAX_BODY_CHARS,
    SIMULATION_MARKERS,
    activity_label,
)

LANGS = ("en", "hi", "ar")
MAX_REPLY_CHARS = 480  # the approved total is 700 including the language marker


# --------------------------------------------------------------------------------------------- normalisation
_ARABIC_MARKS = re.compile("[ً-ٰٟـ]")
_MAP = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي",
    "’": "'", "‘": "'", "ँ": "ं", "़": "",
})  # fmt: skip
_DIGITS = str.maketrans({**{chr(0x0660 + i): str(i) for i in range(10)}, **{chr(0x06F0 + i): str(i) for i in range(10)}, **{chr(0x0966 + i): str(i) for i in range(10)}})
_DASH = re.compile(r"\s*[–—]\s*|\s+-\s+")
_WORD_HYPHEN = re.compile(r"(?<=[A-Za-z])-(?=[A-Za-z])")


def norm(text: str) -> str:
    """Lower case, NFKC, Arabic marks and letter variants folded, Hindi chandrabindu and nukta folded, digits made Western."""
    t = unicodedata.normalize("NFKC", text).lower()
    t = _ARABIC_MARKS.sub("", t).translate(_MAP).translate(_DIGITS)
    return re.sub(r"\s+", " ", t).strip()


def sanitise(text: str) -> str:
    """House rule for UI text: no em or en dashes and no spaced or word hyphens (a comma or a space instead)."""
    return _WORD_HYPHEN.sub(" ", _DASH.sub(", ", text)).replace(" ,", ",").strip()


def detect_language(text: str, hint: str | None = None, fallback: str = "en") -> str:
    """Script wins (Devanagari is Hindi, Arabic script is Arabic); Latin text is English; a very short Latin token
    ("ok", "yes") takes the coordinator's hint."""
    dev = sum(1 for c in text if "ऀ" <= c <= "ॿ")
    ara = sum(1 for c in text if "؀" <= c <= "ۿ" or "ݐ" <= c <= "ݿ")
    lat = sum(1 for c in text if c.isascii() and c.isalpha())
    if dev >= 2 and dev >= ara and dev * 2 >= lat:
        return "hi"
    if ara >= 2 and ara > dev and ara * 2 >= lat:
        return "ar"
    if lat >= 4:
        return "en"
    return hint if hint in LANGS else fallback


# --------------------------------------------------------------------------------------------- keyword lists
def _en(*fragments: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{f})" for f in fragments), re.IGNORECASE)


_AR_LETTERS = "ء-ي"


def _plain(terms: list[str], *, boundary_len: int = 3, arabic: bool = False) -> re.Pattern[str]:
    """Substring matcher for Hindi and Arabic terms (normalised the same way as the text). Short terms must not sit inside
    a longer word: Hindi terms of `boundary_len` letters or fewer must start a word, and short Arabic terms (five letters or
    fewer) must start a word (common attached prefixes allowed) and end one (pronoun suffixes allowed), so that the word for
    pain does not fire on the words for appointment or hospital, which begin with the same letters."""
    parts = []
    for raw in terms:
        n = norm(raw)
        t = re.escape(n)
        if arabic:
            tail = rf"(?:ي|ك|ه|ها|هم|نا|ات|ان)?(?![{_AR_LETTERS}])" if len(n.replace(" ", "")) <= 5 else ""
            parts.append(rf"(?<![{_AR_LETTERS}])(?:ال|بال|وال|لل|ب|و|ف|ل|ك)?{t}{tail}")
        elif len(n) <= boundary_len:
            parts.append(rf"(?<![ऀ-ॿ]){t}")
        else:
            parts.append(t)
    return re.compile("|".join(parts))


# a programme or screening named in the message is not a health question about the resident
_STRIP_EN = re.compile(r"\b(?:\w+\s+){0,2}cancer\s+(?:screening|check|test|checkup)s?\b|\bgenetic\s+(?:risk\s+)?(?:assessment|counsel\w+|referral)\b|\bgenetic\s+counsel\w+\b", re.IGNORECASE)
_STRIP_HI = re.compile(norm("कैंसर की जांच") + "|" + norm("कैंसर जांच") + "|" + norm("कैंसर स्क्रीनिंग") + "|" + norm("कैंसर की स्क्रीनिंग") + "|" + norm("आनुवंशिक जोखिम"))
_STRIP_AR = re.compile(r"(?:فحص|الكشف(?: المبكر)? عن|فحوصات|فحص ال)\s*(?:ال)?سرطان(?:\s+\w+){0,2}|تقييم المخاطر الوراثيه")


def strip_programme_words(text: str) -> str:
    """Remove programme names ("colorectal cancer screening") so the word cancer there does not look like a health question."""
    return _STRIP_AR.sub(" ", _STRIP_HI.sub(" ", _STRIP_EN.sub(" screening ", text)))


_EMERGENCY_EN = _en(
    r"\bchest (?:pain|tightness|pressure)", r"\bpain in (?:my |the )?chest", r"\bheart attack", r"\bcardiac arrest",
    r"\b(?:can'?t|cannot|can not|unable to|hard to|trouble|difficulty|struggling to|not able to) breath\w*", r"\bshort(?:ness)? of breath",
    r"\bgasping", r"\b(?:unconscious|unresponsive|passed out|passing out|fainted|collapsed|not breathing)\b",
    r"\b(?:heavy|severe|uncontrolled|profuse|won'?t stop)\s+bleeding", r"\bbleeding (?:heavily|badly|a lot|profusely|won'?t stop|will not stop|and (?:dizzy|faint))",
    r"\b(?:coughing|vomiting|throwing up|spitting)(?: up)? blood", r"\bsevere (?:pain|headache|abdominal|stomach|chest)", r"\bunbearable pain", r"\bworst pain", r"\bnot responding\b", r"\bunresponsive\b", r"\bcan\x27t wake\b", r"\bwon\x27t wake\b",
    r"\bstroke\b", r"\bseizure", r"\bconvuls", r"\bparaly[sz]", r"\bcan'?t (?:move|feel) (?:my )?(?:arm|leg|face|side)", r"\bface (?:is )?droop",
    r"\bsuicid\w*", r"\b(?:kill|hurt|harm) (?:myself|me)\b", r"\bend (?:my life|it all)", r"\bwant to die", r"\bdon'?t want to (?:live|be alive)", r"\bself[- ]?harm",
    r"\boverdos\w*", r"\bpoison\w*", r"\bemergenc\w*", r"\bambulance", r"\bam (?:dying|going to die)", r"\bgoing to die", r"\bdying\b",
    r"\bneed (?:a |an )?doctor (?:now|right now|immediately)",
    r"\b(?:took|taken|swallowed|ate|drank|ingested)\b[^.?!]{0,30}\b(?:too (?:many|much)|a lot of|whole|entire|bottle|handful|extra|double)\b[^.?!]{0,30}\b(?:tablets?|pills?|medicine|medication|capsules?|drugs?)\b",
    r"\btoo many (?:tablets?|pills?|capsules?)\b", r"\b(?:tablets?|pills?) (?:by mistake|accidentally)\b", r"\b(?:swallowed|drank|ate|ingested)\b[^.?!]{0,30}\b(?:bleach|poison|cleaner|chemical|petrol|kerosene|insecticide|pesticide)\b",
    r"\b(?:blue|purple) (?:lips|face|fingers)\b", r"\blips? (?:look|looks|are|is|turned?|turning) blue\b", r"\b(?:not|won'?t|can'?t|cannot|isn'?t|doesn'?t|stopped) (?:waking|wake|breathing|responding|moving)\b",
    r"\b(?:blood everywhere|lot of blood|so much blood|lost a lot of blood)\b", r"\bcut (?:himself|herself|myself|themselves) badly\b", r"\b(?:trouble|difficulty) speaking\b", r"\bcan'?t speak\b",
)  # fmt: skip
_EMERGENCY_HI = _plain([
    "सीने में दर्द", "छाती में दर्द", "सीने में जकड़न", "सीने में दबाव", "दिल का दौरा", "हार्ट अटैक", "सांस नहीं", "साँस नहीं", "सांस लेने में तकलीफ", "सांस फूल", "दम घुट",
    "बेहोश", "होश नहीं", "होश खो", "बेसुध", "गिर पड़", "बहुत खून", "खून बह", "खून रुक नहीं", "खून की उल्टी", "खून की उलटी", "खांसी में खून", "तेज दर्द", "असहनीय दर्द", "भयानक दर्द",
    "लकवा", "दौरा पड़", "मिर्गी", "आत्महत्या", "जान दे", "मर जाना चाह", "मरना चाह", "खुद को नुकसान", "खुद को खत्म", "जहर", "ओवरडोज", "एम्बुलेंस", "एंबुलेंस",
    "आपातकाल", "इमरजेंसी", "मर रहा", "मर रही", "मरने वाला", "मरने वाली", "ज्यादा गोलियां", "ज्यादा गोलियाँ", "बहुत सारी गोलियां", "गोलियां खा ली", "दवा ज्यादा खा", "दवाई ज्यादा खा", "होंठ नीले", "नीले पड़", "उठ नहीं रहा", "उठ नहीं रही", "जाग नहीं रहा", "जाग नहीं रही", "जवाब नहीं दे रहा", "जवाब नहीं दे रही", "बोलने में दिक्कत", "बोलने में तकलीफ", "चेहरा टेढ़ा", "मुंह टेढ़ा", "खून ही खून",
])  # fmt: skip
_EMERGENCY_AR = _plain([
    "ألم في الصدر", "ألم بالصدر", "ألم صدري", "ضيق في الصدر", "ضيق بالصدر", "نوبة قلبية", "جلطة", "سكتة", "لا أستطيع التنفس", "لا استطيع التنفس", "لا اقدر اتنفس",
    "صعوبة في التنفس", "صعوبة بالتنفس", "ضيق في التنفس", "ضيق التنفس", "اختناق", "أختنق", "فقدت الوعي", "فقدان الوعي", "فقد الوعي", "فاقد الوعي", "فاقدة الوعي", "لا يستجيب", "لا تستجيب", "إغماء", "اغماء", "مغمى", "نزيف شديد",
    "نزيف حاد", "نزيف غزير", "ينزف بشدة", "لا يتوقف النزيف", "تقيأ دم", "تقيؤ دم", "أتقيأ دم", "سعال دم", "ألم شديد", "ألم لا يطاق", "تشنج", "نوبة صرع", "صرع", "شلل",
    "انتحار", "اقتل نفسي", "أنهي حياتي", "اريد ان اموت", "أريد أن أموت", "إيذاء نفسي", "تسمم", "جرعة زائدة", "اسعاف", "إسعاف", "طوارئ", "حالة طارئة", "سأموت", "سوف اموت", "ابتلعت حبوب", "ابتلع حبوب", "ابتلعت ادوية", "حبوب كثيرة", "حبوباً كثيرة", "ادوية كثيرة", "شفاه زرقاء", "شفتاه زرقاوان", "لون ازرق", "لا يستيقظ", "لا تستيقظ", "لا يتنفس", "لا تتنفس", "دم في كل مكان", "صعوبة في الكلام", "لا يستطيع الكلام", "التواء الوجه",
], arabic=True)  # fmt: skip

_MEDICAL_EN = _en(
    r"\bsymptoms?\b", r"\b(?:pain|pains|painful|ache|aches|aching|hurts?|hurting|sore|soreness)\b",
    r"\b(?:lumps?|swelling|swollen|bump|nodules?|tumou?rs?)\b", r"\bbleed\w*", r"\bblood in\b", r"\bbloody\b", r"\bdischarge\b", r"\bspotting\b",
    r"\b(?:fever|cough\w*|nausea|nauseous|vomit\w*|dizz\w*|fatigue\w*|weight loss|losing weight|rash|itch\w*|diarrh\w*|constipat\w*|headache|migraine)\b",
    r"\bdiagnos\w*", r"\bprognos\w*", r"\bbiops\w*", r"\bresults?\b",
    r"\b(?:medicine|medicines|medication\w*|tablets?|pills?|drugs?|dose|doses|dosage|mg|prescri\w*|antibiotic\w*|painkiller\w*|insulin|chemo\w*|radiation|radiotherapy|treatment|treat\w*|therapy|surgery|vaccin\w*|injection)\b",
    r"\bside[- ]?effects?\b", r"\b(?:cancer\w*|malignan\w*|benign|carcinoma|polyps?|leukemi\w*)\b",
    r"\b(?:should|can|could|may|must) i (?:take|stop|skip|eat|drink|avoid|exercise|worry|continue|fast|smoke|lift)\b",
    r"\b(?:do|did|will|would|could|might|may|am|have) i (?:have|get|got|catch|develop|had)\s+(?:a |an |the |any )?(?:cancer|disease|infection|problem|condition|illness|tumou?r|lump|something|it)\b",
    r"\b(?:am i|is it|is this|is that|are these|am i going to be) (?:sick|ill|unwell|serious|dangerous|normal|safe|bad|contagious|infectious|harmful|a sign)\b", r"\bat risk\b",
    r"\b(?:pregnan\w*|breast ?feed\w*|diabet\w*|blood pressure|hypertension|cholesterol|asthma|infection|infected|disease|illness|sick|unwell)\b",
    r"\b(?:worr\w*|scared|afraid|anxious|anxiety|nervous|frightened|panic\w*)\b", r"\b(?:risk|likely|likelihood|probab\w+)\b",
    r"\b(?:my|the) chances?\b", r"\bchances? (?:of|that|i)\b", r"\bwhat are (?:my )?odds\b", r"\b(?:stool|urine|faeces|feces)\b", r"\bbowel (?:movement|habit)s?\b",
    r"\bfamily history\b", r"\bmy (?:mother|father|mom|dad|sister|brother|grandmother|grandfather|aunt|uncle) (?:had|has|died|passed|got)\b",
    r"\b(?:what does it mean|is it normal|is that normal)\b", r"\b(?:my|the) (?:health|condition)\b", r"\bdoctor said\b", r"\bsecond opinion\b",
)  # fmt: skip
_MEDICAL_HI = _plain([
    "दर्द", "पीड़ा", "गांठ", "गाँठ", "सूजन", "खून", "रक्त", "बुखार", "खांसी", "उल्टी", "चक्कर", "थकान", "कमजोरी", "वजन घट", "घाव", "दाने", "लक्षण", "निदान", "बीमारी",
    "बीमार", "रोग", "कैंसर", "जोखिम", "खतरा", "संक्रमण", "दवा", "दवाई", "गोली", "खुराक", "इलाज", "उपचार", "सर्जरी", "ऑपरेशन", "टीका", "इंजेक्शन", "रिपोर्ट में",
    "रिपोर्ट के", "रिपोर्ट का", "परिणाम", "नतीजा", "नतीजे", "रिजल्ट", "गंभीर", "घबरा", "चिंता", "गर्भवती", "गर्भावस्था", "मधुमेह", "शुगर", "ब्लड प्रेशर", "रक्तचाप",
    "कोलेस्ट्रॉल", "अस्थमा", "बायोप्सी", "ट्यूमर", "सिरदर्द", "सांस", "साँस", "दिल की", "डॉक्टर ने कहा", "माँ को", "पिता को", "बहन को", "भाई को",
])  # fmt: skip
_MEDICAL_HI_SHORT = _plain(["डर"])
_MEDICAL_AR = _plain([
    "ألم", "آلام", "وجع", "أوجاع", "ورم", "أورام", "كتلة", "تورم", "انتفاخ", "نزيف", "ينزف", "دم في", "حمى", "سعال", "كحة", "غثيان", "قيء", "تقيؤ", "دوخة", "دوار", "إرهاق",
    "فقدان الوزن", "نقص الوزن", "طفح", "حكة", "إسهال", "إمساك", "صداع", "أعراض", "تشخيص", "نتيجة", "نتائج", "مرض", "مريض", "أمراض", "عدوى", "التهاب", "دواء", "أدوية",
    "عقار", "حبوب", "جرعة", "علاج", "جراحة", "خزعة", "لقاح", "حقنة", "خطر", "خطير", "خطورة", "قلق", "خائف", "خايف", "أخاف", "خوف", "حامل", "سكري", "ضغط الدم", "كوليسترول",
    "ربو", "أقلق", "تقلق", "يقلق", "هل هذا طبيعي", "هل هو آمن", "هل يجب ان اتناول", "هل لدي مرض", "هل عندي مرض", "احتمال", "نصيحه طبيه", "رأي الطبيب", "قال الطبيب", "والدتي", "والدي", "اختي", "اخي",
], arabic=True)  # fmt: skip
_MEDICAL_AR_CANCER = re.compile(rf"(?:هل|ان|اني|انا|مصاب|مصابه|اصابه|خطر|احتمال)\s*(?:[{_AR_LETTERS}]+\s+){{0,2}}(?:ب)?(?:ال)?سرطان")
_MEDICAL_HI_CANCER = _plain(["कैंसर है", "कैंसर तो", "कैंसर का खतरा", "कैंसर हो", "कैंसर हुआ", "कैंसर के लक्षण", "कैंसर नहीं", "कैंसर का डर", "कैंसर की गांठ", "कैंसर की दवा", "कैंसर का इलाज"])

_ELIGIBILITY_EN = _en(
    r"\bwhy (?:am|was|were|have|did|do|should|are|is) (?:i|you|they|we)\b.*\b(?:selected|chosen|picked|invited|eligible|contacted|need\w*|receiv\w*|get|getting|messag\w*|text\w*|screen\w*|tested|test|due|asked)\b",
    r"\bwhy me\b", r"\b(?:not|n'?t) eligible\b", r"\b(?:i|we) (?:do not|don'?t|dont|never) (?:need|qualify)\b", r"\bno need\b", r"\bnot (?:the )?(?:right|correct) (?:person|patient|resident|age)\b",
    r"\bwrong (?:person|patient|number|age|record|name|details|information|info|date of birth|dob)\b", r"\b(?:too young|too old|not old enough)\b",
    r"\b(?:i|we) (?:already|just|recently|have already) (?:did|had|have had|completed|took|got|done)\b", r"\b(?:i|we) (?:did|had|completed|took|got) (?:it|this|that|the (?:test|screening|scan|mammogram|colonoscopy|check))\b",
    r"\balready (?:done|completed|screened|tested|had it|did it|did)\b", r"\b(?:last year|last month|few months ago|years ago|recently) (?:i|we) (?:did|had|got|took)\b",
    r"\b(?:incorrect|mistake|error|inaccurate|outdated)\b", r"\b(?:mandatory|compulsory|optional|forced)\b", r"\bam i (?:due|eligible|required|supposed|meant)\b",
    r"\bwho decided\b", r"\bon what basis\b", r"\bhow did you (?:know|get|find)\b", r"\b(?:complain\w*|lawyer|legal action|sue)\b", r"\b(?:update|correct|change) my (?:record|records|details|information|age|data)\b",
)  # fmt: skip
_ELIGIBILITY_HI = _plain([
    "मुझे क्यों", "मेरा नाम क्यों", "क्यों चुना", "क्यों भेजा", "क्यों बुलाया", "पहले ही करवा", "पहले करवा चुक", "पिछले साल करवा", "पहले कर चुक", "करवा चुका", "करवा चुकी",
    "जरूरत नहीं", "आवश्यकता नहीं", "पात्र नहीं", "योग्य नहीं", "गलत व्यक्ति", "गलत नंबर", "गलत जानकारी", "गलत रिकॉर्ड", "गलत उम्र", "रिकॉर्ड गलत", "उम्र गलत", "अनिवार्य",
    "शिकायत", "वकील", "नंबर कहां से", "नंबर कहाँ से", "किसने तय",
])  # fmt: skip
_ELIGIBILITY_AR = _plain([
    "لماذا انا", "لماذا أنا", "لماذا اخترتموني", "لماذا تم اختياري", "لماذا ارسلتم", "لست مؤهل", "غير مؤهل", "لا احتاج", "لا حاجة لي", "سبق ان اجريت", "سبق أن أجريت",
    "اجريته من قبل", "أجريته من قبل", "اجريت الفحص", "قمت بالفحص", "فعلته سابقا", "رقم خاطئ", "شخص خاطئ", "معلومات خاطئة", "سجلي خاطئ", "اجباري", "إلزامي", "شكوى", "محامي",
    "من اين حصلتم", "من أين حصلتم", "من قرر",
], arabic=True)  # fmt: skip

_OPT_OUT_EN = _en(
    r"^\s*stop\W*$", r"\bstop (?:sending|messag\w*|contact\w*|texting|calling|calls?|emailing|whatsapp\w*|reminders?|bothering|disturbing|it)\b", r"\bunsubscribe\b", r"\bopt[- ]?out\b",
    r"\b(?:do not|don'?t|dont|please do not) (?:send|message|text|contact|call|email|whatsapp|disturb|bother)\b(?!(?:\s+me)?\s+(?:before|after|at|between|until|during|on|in the|early|late|except|only)\b)", r"\bno more (?:messages|texts|calls|reminders|emails)\b",
    r"\b(?:remove|take) me (?:from|off)\b(?: (?:your|the|this|that|all|any))* ?(?:mailing|messaging|contact|reminders?|sms|texts?|calls?|whatsapp|distribution|notification)", r"\bleave me alone\b", r"\bi (?:do not|don'?t|dont) want (?:any )?(?:more )?(?:messages|reminders|calls|to be contacted|texts)\b", r"\bwithdraw (?:my )?consent\b",
)  # fmt: skip
_OPT_OUT_HI = _plain([
    "मैसेज बंद", "संदेश बंद", "भेजना बंद", "भेजना रोक", "फोन करना बंद", "कॉल करना बंद", "मैसेज मत", "संदेश मत", "मत भेजो", "मत भेजिए", "मत भेजें", "संपर्क न करें", "संपर्क मत", "परेशान मत", "फोन मत", "फ़ोन मत", "कॉल मत", "सूचना बंद",
    "रोक दो", "रोक दीजिए", "रोक दें", "अनुस्मारक बंद", "रिमाइंडर बंद", "संदेश नहीं चाहिए", "मैसेज नहीं चाहिए",
])  # fmt: skip
_OPT_OUT_AR = _plain([
    "توقف عن", "توقفوا عن", "لا ترسل", "لا ترسلوا", "لا تراسل", "لا تتصل", "لا تتصلوا", "الغاء الاشتراك", "إلغاء الاشتراك", "ايقاف الرسائل", "إيقاف الرسائل", "لا اريد رسائل",
    "لا أريد رسائل", "لا اريد اي رسائل", "اوقف الرسائل", "أوقفوا الرسائل", "لا تزعجوني", "لا تزعجني",
], arabic=True)  # fmt: skip

_INJECTION_EN = _en(
    r"\bignore (?:all |any |the |your |previous |prior |above |these |those |earlier |my )*(?:instructions?|rules?|prompts?|guidelines?|restrictions?|polic\w+)", r"\bdisregard\b", r"\bsystem prompt\b",
    r"\b(?:reveal|show|print|repeat|output|tell me|display) (?:me )?(?:your|the) (?:system |hidden |initial |original |secret )?(?:prompt|instructions?|rules|configuration)", r"\byou are (?:now|no longer)\b",
    r"\bact as\b", r"\bpretend\b", r"\bjailbreak", r"\bdeveloper mode\b", r"\bdan mode\b", r"\bnew instructions?\b", r"\boverride\b", r"\brole[- ]?play", r"\bas an? (?:doctor|physician|nurse|clinician|medical)\b",
    r"\bforget (?:everything|all|your)\b", r"\bbypass\b", r"\bunrestricted\b", r"\bnot bound by\b",
)  # fmt: skip
_INJECTION_HI = _plain([
    "निर्देश अनदेखा", "निर्देशों को अनदेखा", "निर्देश भूल", "सभी निर्देश", "पिछले निर्देश", "सिस्टम प्रॉम्प्ट", "सिस्टम प्रोम्प्ट", "अब तुम", "अब आप डॉक्टर", "डॉक्टर बन", "नियम भूल", "नियम तोड़",
])  # fmt: skip
_INJECTION_AR = _plain([
    "تجاهل التعليمات", "تجاهل كل التعليمات", "تجاهل جميع التعليمات", "تجاهل ما سبق", "تجاهل تعليماتك", "انت الان", "أنت الآن", "تصرف كطبيب", "تصرف ك", "اظهر التعليمات", "أظهر لي التعليمات",
    "موجه النظام", "برنامج النظام", "انس كل", "انسى كل", "انسَ كل",
], arabic=True)  # fmt: skip


@dataclass(frozen=True)
class Pre:
    """What the deterministic pre classifier found. `category` is the reply route; `opt_out` is independent of it."""

    category: str | None  # emergency | medical | eligibility | injection | opt_out | None
    opt_out: bool = False
    hits: tuple[str, ...] = field(default_factory=tuple)


def classify(text: str) -> Pre:
    """Route a resident message. Precedence: emergency, medical, eligibility, opt out, injection; None means a logistics
    question that may go to the model. An opt out is recorded even when a more urgent category wins."""
    t = norm(text)
    scan = strip_programme_words(t)
    opt = bool(_OPT_OUT_EN.search(t) or _OPT_OUT_HI.search(t) or _OPT_OUT_AR.search(t))
    if _EMERGENCY_EN.search(t) or _EMERGENCY_HI.search(t) or _EMERGENCY_AR.search(t):
        return Pre("emergency", opt, ("emergency",))
    if (
        _MEDICAL_EN.search(scan) or _MEDICAL_HI.search(scan) or _MEDICAL_HI_SHORT.search(scan) or _MEDICAL_HI_CANCER.search(t) or _MEDICAL_AR.search(scan) or _MEDICAL_AR_CANCER.search(t)
    ):
        return Pre("medical", opt, ("medical",))  # "stop the messages, the lump hurts" is a health message; the opt out is recorded too
    if _ELIGIBILITY_EN.search(scan) or _ELIGIBILITY_HI.search(scan) or _ELIGIBILITY_AR.search(scan):
        return Pre("eligibility", opt, ("eligibility",))
    if opt:
        return Pre("opt_out", True, ("opt_out",))
    if _INJECTION_EN.search(t) or _INJECTION_HI.search(t) or _INJECTION_AR.search(t):
        return Pre("injection", False, ("injection",))
    return Pre(None, False, ())


# --------------------------------------------------------------------------------------------- approved fixed replies
_APPROVED: dict[str, dict[str, str]] = {
    "clinician": {
        "en": "Thank you for your message. I cannot answer medical questions. I have passed your question to a clinician from your care team, who will follow up with you. If you feel unwell in the meantime, please contact your doctor or nurse.",
        "hi": "आपके संदेश के लिए धन्यवाद। चिकित्सा संबंधी प्रश्नों का उत्तर देना मेरे दायरे में नहीं है। मैंने आपका प्रश्न आपकी देखभाल टीम के एक चिकित्सक को भेज दिया है, जो आपसे संपर्क करेंगे। यदि इस बीच आप अस्वस्थ महसूस करें, तो कृपया अपने डॉक्टर या नर्स से संपर्क करें।",
        "ar": "شكراً لرسالتك. لا يمكنني الإجابة عن الأسئلة الطبية. لقد أحلت سؤالك إلى طبيب من فريق رعايتك، وسيتواصل معك. وإذا شعرت بتوعك في هذه الأثناء، فيرجى التواصل مع طبيبك أو ممرضتك.",
    },
    "emergency": {
        "en": "If you or someone with you may be in danger or seriously unwell, please go to the nearest hospital or call your local emergency number immediately. I cannot give medical advice. I have alerted a clinician from your care team.",
        "hi": "यदि आप या आपके साथ कोई व्यक्ति खतरे में या गंभीर रूप से अस्वस्थ है, तो कृपया तुरंत निकटतम अस्पताल जाएँ या अपने स्थानीय आपातकालीन नंबर पर कॉल करें। चिकित्सा सलाह देना मेरे दायरे में नहीं है। मैंने आपकी देखभाल टीम के एक चिकित्सक को सूचित कर दिया है।",
        "ar": "إذا كنت أنت أو أي شخص معك في خطر أو تشعر بتوعك شديد، فيرجى التوجه فوراً إلى أقرب مستشفى أو الاتصال برقم الطوارئ المحلي. لا يمكنني تقديم نصيحة طبية. لقد نبّهت طبيباً من فريق رعايتك.",
    },
    "review": {
        "en": "Thank you for your message. I cannot decide who needs a screening or change your records. I have passed your message to your care team, who will review it and get back to you.",
        "hi": "आपके संदेश के लिए धन्यवाद। किसे जाँच की आवश्यकता है यह तय करना या आपका रिकॉर्ड बदलना मेरे दायरे में नहीं है। मैंने आपका संदेश आपकी देखभाल टीम को भेज दिया है, जो इसकी समीक्षा करके आपसे संपर्क करेगी।",
        "ar": "شكراً لرسالتك. لا يمكنني تحديد من يحتاج إلى الفحص أو تغيير سجلاتك. لقد أحلت رسالتك إلى فريق رعايتك، وسيراجعها ويتواصل معك.",
    },
    "opt_out": {
        "en": "Understood. We will not send you further reminders about this. If you change your mind, please tell your clinic or care team.",
        "hi": "ठीक है। हम इस बारे में आपको आगे अनुस्मारक नहीं भेजेंगे। यदि आप अपना मन बदलें, तो कृपया अपने क्लिनिक या देखभाल टीम को बताएँ।",
        "ar": "حسناً. لن نرسل لك تذكيرات أخرى بهذا الشأن. وإذا غيّرت رأيك، فيرجى إبلاغ عيادتك أو فريق رعايتك.",
    },
    "scope": {
        "en": "I can only help with your preventive care appointment: the time, the place, how to prepare, or changing the booking. For anything else, please speak to your care team.",
        "hi": "इस बातचीत में आपकी निवारक स्वास्थ्य सेवा की अपॉइंटमेंट से जुड़ी जानकारी दी जा सकती है: समय, स्थान, तैयारी या बुकिंग में बदलाव। किसी और बात के लिए कृपया अपनी देखभाल टीम से बात करें।",
        "ar": "يمكنني المساعدة فقط في موعد رعايتك الوقائية: الوقت والمكان وكيفية التحضير أو تغيير الحجز. وبالنسبة لأي أمر آخر، يرجى التحدث إلى فريق رعايتك.",
    },
    "unavailable": {
        "en": "Thank you for your message. I could not prepare a reply just now. A member of your care team will get back to you.",
        "hi": "आपके संदेश के लिए धन्यवाद। अभी उत्तर तैयार नहीं हो सका। आपकी देखभाल टीम का कोई सदस्य आपसे संपर्क करेगा।",
        "ar": "شكراً لرسالتك. تعذّر إعداد رد في الوقت الحالي. سيتواصل معك أحد أعضاء فريق رعايتك.",
    },
}  # fmt: skip
ROUTE_KIND = {"emergency": "emergency", "medical": "clinician", "eligibility": "review", "opt_out": "opt_out", "injection": "scope"}
ESCALATION_REASON = {"emergency": "emergency", "medical": "medical_question", "eligibility": "eligibility_dispute"}


def approved(kind: str, lang: str) -> str:
    return _APPROVED[kind][lang if lang in LANGS else "en"]


def with_marker(body: str, lang: str) -> str:
    return f"{SIMULATION_MARKERS[lang]}\n{body}"


# --------------------------------------------------------------------------------------------- reply checks
_CLINICAL_EN = _en(
    r"\bdiagnos\w*", r"\bsymptom\w*", r"\bdos(?:e|es|age)\b", r"\bmg\b", r"\btablets?\b", r"\bpills?\b", r"\bmedicin\w*", r"\bmedicat\w*", r"\bdrugs?\b", r"prescri\w*", r"\btreat\w*",
    r"\btherap\w*", r"\bchemo\w*", r"\bsurger\w*", r"\bbiops\w*", r"\bresults?\b", r"\bmalignan\w*", r"\bbenign\b", r"\btumou?rs?\b", r"\blumps?\b", r"\bcancer\w*", r"\bserious(?:ly)?\b",
    r"\bdangerous\b", r"\b(?:don'?t|do not|no need to|nothing to) worry", r"\bnot (?:a )?(?:problem|concern|worry)\b", r"\blikely\b", r"\bprobabl\w*", r"\brisks?\b", r"\bhealthy\b",
    r"\byou (?:have|may have|might have|probably have|could have)\s+(?:a |an |the )?(?:cancer|disease|infection|condition|problem|illness)", r"\bit (?:is|'s) (?:normal|safe|fine|common)\b",
    r"\b(?:should|must|need to|ought to) (?:take|stop|eat|drink|avoid|fast|rest|exercise)\b", r"\bi (?:recommend|advise|suggest) (?:that )?you (?:take|stop|eat|drink|avoid|see|get|rest)\b",
    r"\b(?:pain|ache|fever|bleed\w*|blood|nausea|vomit\w*|dizz\w*|infection|disease|illness|sick)\b",
)  # fmt: skip
_CLINICAL_HI = _plain([
    "निदान", "लक्षण", "खुराक", "दवा", "दवाई", "गोली", "इलाज", "उपचार", "सर्जरी", "ऑपरेशन", "बायोप्सी", "परिणाम", "नतीजा", "नतीजे", "रिजल्ट", "गंभीर", "खतरनाक", "चिंता न करें",
    "घबराएं नहीं", "घबराइए नहीं", "चिंता की बात नहीं", "सामान्य है", "सुरक्षित है", "जोखिम", "खतरा", "संभावना", "कैंसर", "बीमारी", "रोग", "संक्रमण", "ट्यूमर", "गांठ", "दर्द", "बुखार", "खून",
])  # fmt: skip
_CLINICAL_AR = _plain([
    "تشخيص", "أعراض", "جرعة", "دواء", "أدوية", "حبوب", "علاج", "جراحة", "خزعة", "نتيجة", "نتائج", "خطير", "خطورة", "لا تقلق", "لا داعي للقلق", "طبيعي", "خطر", "احتمال", "سرطان",
    "مرض", "عدوى", "ورم", "كتلة", "ألم", "حمى", "نزيف", "دم في",
], arabic=True)  # fmt: skip
_BOOKING_EN = _en(
    r"\b(?:i|we)(?:'ve| have)?\s+(?:booked|confirmed|scheduled|rescheduled|cancell?ed|reserved|arranged|changed|moved|updated|cancelled)\b",
    r"\b(?:has|have|had) been (?:booked|confirmed|scheduled|rescheduled|cancell?ed|reserved|arranged|changed|moved)\b",
    r"\b(?:i|we)(?: will|'ll| can| shall| am going to)\s+(?:book|confirm|schedule|reschedule|cancel|reserve|arrange|change|move)\b", r"\bis now (?:booked|confirmed|rescheduled|cancell?ed)\b",
    r"\b(?:booking|appointment|slot) (?:is|has been) (?:confirmed|secured|reserved|made|done)\b",
)  # fmt: skip
_BOOKING_HI = _plain(["बुक कर दिया", "बुक हो गया", "बुक कर लिया", "तय कर दिया", "कन्फर्म कर दिया", "पुष्टि कर दी", "रद्द कर दिया", "बदल दिया", "आरक्षित कर", "शेड्यूल कर दिया", "बुक कर दूंगा", "बुक कर दूँगी", "कन्फर्म हो गया"])
_BOOKING_AR = _plain(["تم الحجز", "تم تأكيد", "قمت بحجز", "حجزت", "أكدت", "اكدت", "تم الغاء", "تم إلغاء", "ألغيت", "تم تغيير", "غيرت الموعد", "تم تحديد موعد", "سأحجز", "سوف احجز"], arabic=True)
_CONTACT = re.compile(r"https?://|www\.|@\w|\+?\d[\d\s().]{6,}\d")
_NUMBER = re.compile(r"\d+")
_LETTER_RANGES = {"hi": ("ऀ", "ॿ"), "ar": ("؀", "ۿ")}


def numbers_in(text: str) -> set[int]:
    """Whole numbers written with digits (any script), as integers so 08 and 8 agree."""
    return {int(m) for m in _NUMBER.findall(norm(text))}


def script_ok(text: str, lang: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 4:
        return True
    if lang == "en":
        return sum(1 for c in letters if c.isascii()) / len(letters) >= 0.8
    lo, hi = _LETTER_RANGES[lang]
    extra = ("ݐ", "ݿ") if lang == "ar" else None
    n = sum(1 for c in letters if lo <= c <= hi or (extra and extra[0] <= c <= extra[1]))
    return n / len(letters) >= 0.45  # clinic and street names may stay in Latin script


def reply_problems(reply: str, lang: str, *, allowed_numbers: set[int], labels: list[str]) -> list[str]:
    """Reasons a model reply may not be shown (empty list = safe). `labels` are programme names that are allowed to
    contain words such as cancer. Problem codes: clinical, booking_claim, ungrounded_number, language, contact_info,
    length, constraint, empty."""
    problems: list[str] = []
    if not reply.strip():
        return ["empty"]
    t = norm(reply)
    for label in labels:  # the approved programme name is allowed to say "cancer"
        t = t.replace(norm(label), " screening ")
    t = strip_programme_words(t)
    if _CLINICAL_EN.search(t) or _CLINICAL_HI.search(t) or _CLINICAL_AR.search(t):
        problems.append("clinical")
    if _BOOKING_EN.search(t) or _BOOKING_HI.search(t) or _BOOKING_AR.search(t):
        problems.append("booking_claim")
    if _CONTACT.search(reply):
        problems.append("contact_info")
    extra = numbers_in(reply) - allowed_numbers
    if extra:
        problems.append("ungrounded_number")
    if not script_ok(reply, lang):
        problems.append("language")
    if len(with_marker(reply, lang)) > MAX_BODY_CHARS or "{" in reply or "}" in reply:
        problems.append("length")
    low = reply.lower()
    if SIMULATION_MARKERS[lang] in reply or any(p in low for p in FORBIDDEN_PHRASES):
        problems.append("constraint")
    return problems


def labels_for(care_activity: str) -> list[str]:
    return [activity_label(care_activity, lang) for lang in LANGS] + [care_activity.replace("_", " ")]
