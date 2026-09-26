/**
 * The two pages a link opens, in three languages, out of one file.
 *
 * WHY THIS EXISTS
 * ---------------
 * ``quick.js`` and ``enroll.js`` were written a month apart and each grew its own copy of
 * the same four things: the photo policy (``checkPhoto``, ``applyPolicy``), the camera
 * cycle (``startCamera`` / ``shoot`` / ``stopCamera`` / ``retake``), the location fix, and
 * every sentence that says what happened. The two copies had already drifted - the same
 * size limit refused with "Take the selfie again" on one page and "Choose a photo from the
 * gallery" on the other - and the drift was invisible, because nothing compared them.
 *
 * They are the two pages a worker with no session and possibly no app opens, at a gate,
 * on the worst connection the product is documented to assume. So they are also the two
 * pages that cannot afford to fetch the app's own table: ``i18n.js`` plus one language
 * chunk is 78 KB, eight times the whole page, for the thirty-odd sentences these screens
 * actually say. The table below is that subset, in all three languages, next to the
 * capture code that speaks it.
 *
 * WHAT IT OWNS
 * ------------
 *   - the strings, and the document's ``lang`` / ``dir`` (Arabic and Urdu read right to left);
 *   - the photo policy the server publishes: what the browser refuses before uploading;
 *   - the camera cycle, over the element ids both pages already use;
 *   - the location fix, including why there is none.
 *
 * WHAT IT DOES NOT OWN
 * --------------------
 * Each page keeps its own flow: what a tap does, what a submit sends, what a dead link
 * means. ``Capture.create`` hands the page a photo; the page decides what that photo is
 * for. A classic script rather than a module, because neither page has a build step and
 * ``frontendjavascript.js`` is reachable from both without one.
 */
(function () {
    "use strict";

    //: The vendor credited at the foot of every page the app serves. A wordmark, so it is
    //: the same string in all four languages and only the words around it move.
    var BRAND = "دوامك اسهل";

    var LANGUAGES = [
        { code: "en", label: "EN", name: "English" },
        { code: "ar", label: "ع", name: "العربية" },
        { code: "hi", label: "हि", name: "हिन्दी" },
        { code: "ur", label: "UR", name: "اردو" }
    ];

    //: The languages that read right to left. Arabic and Urdu both do, so the page mirrors
    //: for either - a list, so the next one is a line here rather than a call-site branch.
    var RTL = ["ar", "ur"];

    /**
     * The subset of the app's vocabulary these two pages use, in every language the app
     * offers.
     *
     * Written here and not in ``i18n.<lang>.js`` on purpose: those files are the app's
     * four-figure table, fetched by a signed-in session, and neither of these pages has a
     * session to speak of. Where a sentence already exists in the app's table the wording
     * is that table's, so the same act is not named two ways - "You are on shift" on the
     * handset and something else on the link that clocked the shift in.
     *
     * ``{braces}`` are substituted by ``t()``; a translation keeps every one it is given.
     */
    var STRINGS = {
        en: {
            "credit": "Powered by {brand}",
            "language": "Language",

            "photo.policy": "Photos only: {types}, up to {mb} MB.",
            "photo.none.selfie": "Take or choose a selfie first.",
            "photo.none.photo": "Take or choose a photo first.",
            "photo.empty.selfie": "That file is empty. Take the selfie again.",
            "photo.empty.photo": "That file is empty. Choose a photo from the gallery.",
            "photo.big": "That photo is {mb} MB and the limit is {max} MB. Retake it at a lower resolution.",
            "photo.type": "That file is not a photo. Only {types} are accepted - no documents, PDFs or videos.",
            "photo.problem": "That photo cannot be used.",

            "camera.blocked.punch": "Camera blocked. Use the button below to open your phone's camera app instead.",
            "camera.blocked.enroll": "Camera blocked. Use the button below to open your phone's gallery.",
            "camera.open": "Open camera",
            "camera.shoot.selfie": "Take the selfie",
            "camera.shoot.photo": "Take photo",
            "camera.retake": "Retake",
            "camera.sendSelfie": "Send this selfie",
            "camera.preview.selfie": "Your selfie preview",
            "camera.preview.photo": "Your photo preview",

            "location.checking": "Checking your location…",
            "location.found": "Location found (±{m} m).",
            "location.unsupported": "This browser cannot report a location, so the punch cannot be recorded.",
            "location.blocked": "Location is blocked. Allow location for this page and try again.",
            "location.unknown": "Your location could not be determined. Step outside and try again.",

            "link.invalid": "This link is not valid.",
            "refuse.revoked": "This link was revoked by an administrator. Ask for a new one.",
            "refuse.expired": "This link has expired. Ask your administrator for a new one.",
            "refuse.used_up": "This link has already been used. Ask your administrator for a new one.",
            "offline": "Could not reach the server. Check your connection.",

            "quick.title": "Clock in or out",
            "quick.headTitle": "Clock in or out — Site Attendance",
            "quick.sub": "Checking this link…",
            "quick.state.in": "You are clocked in",
            "quick.state.out": "You are clocked out",
            "quick.state.since": "Since {time}{site}.",
            "quick.state.site": " at {site}",
            "quick.state.closed": "Your last shift is closed.",
            "quick.state.remaining": " This link has {n} tap(s) left.",
            "quick.for": "For {name} (id {id})",
            "quick.expires": "expires {date}",
            "quick.btn.in": "Clock in",
            "quick.btn.out": "Clock out",
            "quick.fallback": "Camera not available? Tap here to use the phone's camera app instead",
            "quick.note": "Your administrator issued this link for you. It clocks you in and out without a password, and every tap is recorded with the selfie it was taken with.",
            "quick.recording": "Recording your tap…",
            "quick.noFix": "No location fix, so the punch was not sent. Allow location and try again.",
            "quick.clockedInAt": "Clocked in at {site}.",
            "quick.clockedOut": "Clocked out of {site}. {hours}h paid",
            "quick.break": " after a {min}-minute break",
            "quick.notRecorded": "The tap was not recorded.",
            "quick.notReached": "The tap did not reach the server. Check your connection and try again.",
            "quick.earlyTitle": "Clock out early?",
            "quick.earlyBody": "You have worked {paid}h of the {regular}h paid day. If you clock out now, this time it will be recorded as {paid}h \u2014 not the full {regular}h day.",
            "quick.earlyConfirm": "Confirm and clock out",
            "quick.earlyCancel": "Cancel",

            "enroll.title": "Register your face",
            "enroll.registerTitle": "Create your account",
            "enroll.headTitle": "Register your face — Site Attendance",
            "enroll.sub": "Checking your link…",
            "enroll.verb.enroll": "Enrolling",
            "enroll.verb.register": "Creating an account for",
            "enroll.intro": "Stand in good light, hold the phone at eye level, and look straight at the camera. Remove hats or sunglasses. This photo is compared with the selfie you take when you clock in, so take it where you normally clock in.",
            "enroll.password.hint": "Choose a password of at least {n} characters. You will sign in with your ID ({id}) and this password, so keep it safe.",
            "enroll.password.placeholder": "Choose a password",
            "enroll.password2.placeholder": "Type it again",
            "enroll.phone.placeholder": "Phone (optional)",
            "enroll.email.placeholder": "Email (optional)",
            "enroll.submit": "Submit my photo",
            "enroll.create": "Create my account",
            "enroll.uploading": "Uploading…",
            "enroll.liveness": "Liveness: {verdict}",
            "enroll.done.register": "Done. Your account is ready - sign in with your ID and the password you chose.",
            "enroll.done.enroll": "Done. Your reference photo has been registered.",
            "enroll.failed": "Enrollment failed.",
            "enroll.unusable": "This link can no longer be used. Ask your administrator for a new one.",
            "enroll.passwordShort": "Your password needs at least {n} characters.",
            "enroll.passwordMismatch": "The two passwords are not the same.",
            "enroll.uploadFailed": "Upload failed. Check your connection and try again.",
            "enroll.fallback": "Camera not available? Tap here to use the phone's gallery instead"
        },

        ar: {
            "credit": "مدعوم من {brand}",
            "language": "اللغة",

            "photo.policy": "صور فقط: {types}، بحد أقصى {mb} ميغابايت.",
            "photo.none.selfie": "التقط سيلفي أو اختر واحدة أولاً.",
            "photo.none.photo": "التقط صورة أو اختر صورة أولاً.",
            "photo.empty.selfie": "هذا الملف فارغ. التقط السيلفي مرة أخرى.",
            "photo.empty.photo": "هذا الملف فارغ. اختر صورة من المعرض.",
            "photo.big": "حجم الصورة {mb} ميغابايت والحد الأقصى {max} ميغابايت. أعد التصوير بجودة أقل.",
            "photo.type": "هذا الملف ليس صورة. المقبول هو {types} فقط - لا مستندات ولا PDF ولا فيديو.",
            "photo.problem": "لا يمكن استخدام هذه الصورة.",

            "camera.blocked.punch": "الكاميرا محجوبة. استخدم الزر أدناه لفتح تطبيق الكاميرا في هاتفك بدلاً منها.",
            "camera.blocked.enroll": "الكاميرا محجوبة. استخدم الزر أدناه لفتح معرض الصور في هاتفك.",
            "camera.open": "افتح الكاميرا",
            "camera.shoot.selfie": "التقط السيلفي",
            "camera.shoot.photo": "التقط الصورة",
            "camera.retake": "أعد التصوير",
            "camera.sendSelfie": "أرسل هذه الصورة",
            "camera.preview.selfie": "معاينة السيلفي",
            "camera.preview.photo": "معاينة الصورة",

            "location.checking": "جارٍ تحديد موقعك…",
            "location.found": "تم تحديد الموقع (±{m} م).",
            "location.unsupported": "هذا المتصفح لا يستطيع تحديد الموقع، لذا لا يمكن تسجيل الحضور.",
            "location.blocked": "الموقع محجوب. اسمح بالوصول إلى الموقع لهذه الصفحة ثم حاول مرة أخرى.",
            "location.unknown": "تعذّر تحديد موقعك. اخرج إلى الخارج وحاول مرة أخرى.",

            "link.invalid": "هذا الرابط غير صالح.",
            "refuse.revoked": "ألغى المدير هذا الرابط. اطلب منه رابطاً جديداً.",
            "refuse.expired": "انتهت صلاحية هذا الرابط. اطلب من مديرك رابطاً جديداً.",
            "refuse.used_up": "هذا الرابط مستخدم من قبل. اطلب من مديرك رابطاً جديداً.",
            "offline": "تعذّر الوصول إلى الخادم. تحقّق من اتصالك.",

            "quick.title": "تسجيل الحضور أو الانصراف",
            "quick.headTitle": "تسجيل الحضور أو الانصراف — نظام حضور الموقع",
            "quick.sub": "جارٍ التحقق من الرابط…",
            "quick.state.in": "أنت على رأس العمل",
            "quick.state.out": "أنت حالياً غير مسجل",
            "quick.state.since": "منذ {time}{site}.",
            "quick.state.site": " في {site}",
            "quick.state.closed": "تم إغلاق آخر وردية لك.",
            "quick.state.remaining": " بقي {n} تسجيل لهذا الرابط.",
            "quick.for": "لـ {name} (رقم {id})",
            "quick.expires": "ينتهي {date}",
            "quick.btn.in": "تسجيل الحضور",
            "quick.btn.out": "تسجيل الانصراف",
            "quick.fallback": "الكاميرا غير متاحة؟ اضغط هنا لاستخدام تطبيق الكاميرا في هاتفك بدلاً منها",
            "quick.note": "أصدر المدير هذا الرابط لك. يسجّل حضورك وانصرافك بدون كلمة مرور، وكل تسجيل يُحفظ مع السيلفي الذي التُقط معه.",
            "quick.recording": "جارٍ تسجيل حضورك…",
            "quick.noFix": "لم يتم تحديد الموقع، لذا لم يُرسل التسجيل. اسمح بالوصول إلى الموقع ثم حاول مرة أخرى.",
            "quick.clockedInAt": "تم تسجيل الحضور في {site}.",
            "quick.clockedOut": "تم تسجيل الانصراف من {site}. {hours} ساعة مدفوعة",
            "quick.break": " بعد استراحة {min} دقيقة",
            "quick.notRecorded": "لم يتم تسجيل الحضور.",
            "quick.notReached": "لم يصل التسجيل إلى الخادم. تحقّق من اتصالك وحاول مرة أخرى.",
            "quick.earlyTitle": "تسجيل الخروج مبكراً؟",
            "quick.earlyBody": "لقد عملت {paid} ساعة فقط من أصل {regular} ساعة مدفوعة. إذا سجّلت الخروج الآن فسيُحتسب الوقت هذه المرة كما هو: {paid} ساعة، وليس يوماً كاملاً ({regular} ساعة).",
            "quick.earlyConfirm": "تأكيد وتسجيل الخروج",
            "quick.earlyCancel": "إلغاء",

            "enroll.title": "تسجيل صورتك",
            "enroll.registerTitle": "أنشئ حسابك",
            "enroll.headTitle": "تسجيل صورتك — نظام حضور الموقع",
            "enroll.sub": "جارٍ التحقق من الرابط…",
            "enroll.verb.enroll": "تسجيل صورة لـ",
            "enroll.verb.register": "إنشاء حساب لـ",
            "enroll.intro": "قف في إضاءة جيدة، وارفع الهاتف إلى مستوى عينيك، وانظر مباشرة إلى الكاميرا. اخلع القبعة أو النظارة الشمسية. تُقارن هذه الصورة بالسيلفي الذي تلتقطه عند تسجيل الحضور، لذا التقطها في المكان الذي تسجّل فيه عادة.",
            "enroll.password.hint": "اختر كلمة مرور من {n} أحرف على الأقل. ستسجّل الدخول برقمك ({id}) وهذه الكلمة، فاحفظها جيداً.",
            "enroll.password.placeholder": "اختر كلمة مرور",
            "enroll.password2.placeholder": "أعد كتابتها",
            "enroll.phone.placeholder": "الهاتف (اختياري)",
            "enroll.email.placeholder": "البريد الإلكتروني (اختياري)",
            "enroll.submit": "أرسل صورتي",
            "enroll.create": "أنشئ حسابي",
            "enroll.uploading": "جارٍ الإرسال…",
            "enroll.liveness": "فحص الحياة: {verdict}",
            "enroll.done.register": "تم. حسابك جاهز - سجّل الدخول برقمك وكلمة المرور التي اخترتها.",
            "enroll.done.enroll": "تم. سُجّلت صورة المرجع الخاصة بك.",
            "enroll.failed": "فشل التسجيل.",
            "enroll.unusable": "لم يعد هذا الرابط صالحًا. اطلب من مديرك رابطاً جديداً.",
            "enroll.passwordShort": "كلمة المرور تحتاج {n} أحرف على الأقل.",
            "enroll.passwordMismatch": "كلمتا المرور غير متطابقتين.",
            "enroll.uploadFailed": "فشل الإرسال. تحقّق من اتصالك وحاول مرة أخرى.",
            "enroll.fallback": "الكاميرا غير متاحة؟ اضغط هنا لاستخدام معرض الصور في هاتفك بدلاً منها"
        },

        hi: {
            "credit": "{brand} द्वारा संचालित",
            "language": "भाषा",

            "photo.policy": "केवल फ़ोटो: {types}, अधिकतम {mb} MB।",
            "photo.none.selfie": "पहले सेल्फ़ी लें या चुनें।",
            "photo.none.photo": "पहले फ़ोटो लें या चुनें।",
            "photo.empty.selfie": "यह फ़ाइल खाली है। सेल्फ़ी दोबारा लें।",
            "photo.empty.photo": "यह फ़ाइल खाली है। गैलरी से फ़ोटो चुनें।",
            "photo.big": "फ़ोटो {mb} MB की है और सीमा {max} MB है। कम रेज़ोल्यूशन पर दोबारा लें।",
            "photo.type": "यह फ़ाइल फ़ोटो नहीं है। केवल {types} मान्य हैं - कोई दस्तावेज़, PDF या वीडियो नहीं।",
            "photo.problem": "यह फ़ोटो इस्तेमाल नहीं की जा सकती।",

            "camera.blocked.punch": "कैमरा ब्लॉक है। नीचे के बटन से अपने फ़ोन का कैमरा ऐप खोलें।",
            "camera.blocked.enroll": "कैमरा ब्लॉक है। नीचे के बटन से अपने फ़ोन की गैलरी खोलें।",
            "camera.open": "कैमरा खोलें",
            "camera.shoot.selfie": "सेल्फ़ी लें",
            "camera.shoot.photo": "फ़ोटो लें",
            "camera.retake": "दोबारा लें",
            "camera.sendSelfie": "यह सेल्फ़ी भेजें",
            "camera.preview.selfie": "आपकी सेल्फ़ी का पूर्वावलोकन",
            "camera.preview.photo": "आपकी फ़ोटो का पूर्वावलोकन",

            "location.checking": "आपकी लोकेशन देखी जा रही है…",
            "location.found": "लोकेशन मिल गई (±{m} मी)।",
            "location.unsupported": "यह ब्राउज़र लोकेशन नहीं बता सकता, इसलिए पंच दर्ज नहीं हो सकता।",
            "location.blocked": "लोकेशन ब्लॉक है। इस पेज के लिए लोकेशन की अनुमति दें और दोबारा कोशिश करें।",
            "location.unknown": "आपकी लोकेशन तय नहीं हो सकी। बाहर निकलें और दोबारा कोशिश करें।",

            "link.invalid": "यह लिंक मान्य नहीं है।",
            "refuse.revoked": "यह लिंक एडमिन ने रद्द कर दिया है। नया लिंक मांगें।",
            "refuse.expired": "इस लिंक की अवधि खत्म हो गई है। अपने एडमिन से नया लिंक मांगें।",
            "refuse.used_up": "यह लिंक पहले ही इस्तेमाल हो चुका है। अपने एडमिन से नया लिंक मांगें।",
            "offline": "सर्वर तक नहीं पहुँच सके। अपना इंटरनेट कनेक्शन देखें।",

            "quick.title": "क्लॉक इन या आउट",
            "quick.headTitle": "क्लॉक इन या आउट — साइट उपस्थिति",
            "quick.sub": "इस लिंक की जाँच हो रही है…",
            "quick.state.in": "आप शिफ्ट पर हैं",
            "quick.state.out": "आप अभी क्लॉक आउट हैं",
            "quick.state.since": "{time} से{site}।",
            "quick.state.site": " {site} पर",
            "quick.state.closed": "आपकी पिछली शिफ्ट बंद है।",
            "quick.state.remaining": " इस लिंक पर {n} पंच बाकी हैं।",
            "quick.for": "{name} के लिए (आईडी {id})",
            "quick.expires": "{date} तक मान्य",
            "quick.btn.in": "क्लॉक इन",
            "quick.btn.out": "क्लॉक आउट",
            "quick.fallback": "कैमरा नहीं चल रहा? यहाँ दबाकर अपने फ़ोन का कैमरा ऐप इस्तेमाल करें",
            "quick.note": "यह लिंक आपके एडमिन ने आपके लिए बनाया है। यह बिना पासवर्ड आपका क्लॉक इन और आउट करता है, और हर पंच अपनी सेल्फ़ी के साथ दर्ज होता है।",
            "quick.recording": "आपका पंच दर्ज हो रहा है…",
            "quick.noFix": "लोकेशन नहीं मिली, इसलिए पंच नहीं भेजा गया। लोकेशन की अनुमति दें और दोबारा कोशिश करें।",
            "quick.clockedInAt": "{site} पर क्लॉक इन हुआ।",
            "quick.clockedOut": "{site} से क्लॉक आउट हुआ। {hours} घंटे का भुगतान",
            "quick.break": " {min} मिनट के ब्रेक के बाद",
            "quick.notRecorded": "पंच दर्ज नहीं हुआ।",
            "quick.notReached": "पंच सर्वर तक नहीं पहुँचा। कनेक्शन देखें और दोबारा कोशिश करें।",
            "quick.earlyTitle": "जल्दी चेक आउट करें?",
            "quick.earlyBody": "आपने {regular} घंटे के वैतनिक दिन में से केवल {paid} घंटे काम किया है। अभी चेक आउट करने पर इस बार यही दर्ज होगा \u2014 {paid} घंटे, पूरा {regular} घंटे का दिन नहीं।",
            "quick.earlyConfirm": "पुष्टि करें और चेक आउट",
            "quick.earlyCancel": "रद्द करें",

            "enroll.title": "अपना चेहरा रजिस्टर करें",
            "enroll.registerTitle": "अपना खाता बनाएँ",
            "enroll.headTitle": "अपना चेहरा रजिस्टर करें — साइट उपस्थिति",
            "enroll.sub": "आपके लिंक की जाँच हो रही है…",
            "enroll.verb.enroll": "रजिस्टर हो रहा है",
            "enroll.verb.register": "खाता बन रहा है",
            "enroll.intro": "अच्छी रोशनी में खड़े हों, फ़ोन को आँखों के स्तर पर रखें, और सीधे कैमरे की ओर देखें। टोपी या धूप का चश्मा हटा दें। इस फ़ोटो की तुलना उस सेल्फ़ी से होती है जो आप क्लॉक इन करते समय लेते हैं, इसलिए इसे वहीं लें जहाँ आप आमतौर पर क्लॉक इन करते हैं।",
            "enroll.password.hint": "कम से कम {n} अक्षरों का पासवर्ड चुनें। आप अपनी आईडी ({id}) और इसी पासवर्ड से साइन इन करेंगे, इसलिए इसे संभाल कर रखें।",
            "enroll.password.placeholder": "पासवर्ड चुनें",
            "enroll.password2.placeholder": "दोबारा लिखें",
            "enroll.phone.placeholder": "फ़ोन (वैकल्पिक)",
            "enroll.email.placeholder": "ईमेल (वैकल्पिक)",
            "enroll.submit": "मेरी फ़ोटो भेजें",
            "enroll.create": "मेरा खाता बनाएँ",
            "enroll.uploading": "भेजा जा रहा है…",
            "enroll.liveness": "लाइवनेस: {verdict}",
            "enroll.done.register": "हो गया। आपका खाता तैयार है - अपनी आईडी और चुने हुए पासवर्ड से साइन इन करें।",
            "enroll.done.enroll": "हो गया। आपकी संदर्भ फ़ोटो रजिस्टर हो गई है।",
            "enroll.failed": "रजिस्ट्रेशन नहीं हुआ।",
            "enroll.unusable": "यह लिंक अब काम नहीं करता। अपने एडमिन से नया लिंक मांगें।",
            "enroll.passwordShort": "पासवर्ड में कम से कम {n} अक्षर चाहिए।",
            "enroll.passwordMismatch": "दोनों पासवर्ड एक जैसे नहीं हैं।",
            "enroll.uploadFailed": "भेजना नहीं हुआ। कनेक्शन देखें और दोबारा कोशिश करें।",
            "enroll.fallback": "कैमरा नहीं चल रहा? यहाँ दबाकर अपने फ़ोन की गैलरी इस्तेमाल करें"
        },
        ur: {
            "credit": "{brand} کے تعاون سے",
            "language": "زبان",

            "photo.policy": "صرف تصاویر: {types}، {mb} MB تک۔",
            "photo.none.selfie": "پہلے سیلفی لیں یا منتخب کریں۔",
            "photo.none.photo": "پہلے تصویر لیں یا منتخب کریں۔",
            "photo.empty.selfie": "یہ فائل خالی ہے۔ سیلفی دوبارہ لیں۔",
            "photo.empty.photo": "یہ فائل خالی ہے۔ گیلری سے تصویر منتخب کریں۔",
            "photo.big": "یہ تصویر {mb} MB ہے اور حد {max} MB ہے۔ کم ریزولوشن پر دوبارہ لیں۔",
            "photo.type": "یہ فائل تصویر نہیں ہے۔ صرف {types} قبول ہوتے ہیں - کوئی دستاویز، PDF یا ویڈیو نہیں۔",
            "photo.problem": "یہ تصویر استعمال نہیں ہو سکتی۔",

            "camera.blocked.punch": "کیمرہ بند ہے۔ نیچے دیے بٹن سے اپنے فون کا کیمرہ ایپ کھولیں۔",
            "camera.blocked.enroll": "کیمرہ بند ہے۔ نیچے دیے بٹن سے اپنے فون کی گیلری کھولیں۔",
            "camera.open": "کیمرہ کھولیں",
            "camera.shoot.selfie": "سیلفی لیں",
            "camera.shoot.photo": "تصویر لیں",
            "camera.retake": "دوبارہ لیں",
            "camera.sendSelfie": "یہ سیلفی بھیجیں",
            "camera.preview.selfie": "آپ کی سیلفی کا پیش منظر",
            "camera.preview.photo": "آپ کی تصویر کا پیش منظر",

            "location.checking": "آپ کی لوکیشن دیکھی جا رہی ہے…",
            "location.found": "لوکیشن مل گئی (±{m} میٹر)۔",
            "location.unsupported": "یہ براؤزر لوکیشن نہیں بتا سکتا، اس لیے پنچ درج نہیں ہو سکتا۔",
            "location.blocked": "لوکیشن بند ہے۔ اس صفحے کے لیے لوکیشن کی اجازت دیں اور دوبارہ کوشش کریں۔",
            "location.unknown": "آپ کی لوکیشن معلوم نہیں ہو سکی۔ باہر جا کر دوبارہ کوشش کریں۔",

            "link.invalid": "یہ لنک درست نہیں۔",
            "refuse.revoked": "یہ لنک ایڈمنسٹریٹر نے منسوخ کر دیا ہے۔ نیا لنک مانگیں۔",
            "refuse.expired": "اس لنک کی مدت ختم ہو گئی ہے۔ اپنے ایڈمنسٹریٹر سے نیا مانگیں۔",
            "refuse.used_up": "یہ لنک پہلے استعمال ہو چکا ہے۔ اپنے ایڈمنسٹریٹر سے نیا مانگیں۔",
            "offline": "سرور تک رسائی نہیں۔ اپنا کنکشن دیکھیں۔",

            "quick.title": "چیک اِن یا آؤٹ",
            "quick.headTitle": "چیک اِن یا آؤٹ - حاضری الموقع",
            "quick.sub": "یہ لنک دیکھا جا رہا ہے…",
            "quick.state.in": "آپ چیک اِن ہیں",
            "quick.state.out": "آپ چیک آؤٹ ہیں",
            "quick.state.since": "{time}{site} سے۔",
            "quick.state.site": " {site} پر",
            "quick.state.closed": "آپ کی آخری شفٹ بند ہو چکی ہے۔",
            "quick.state.remaining": " اس لنک کے {n} ٹیپ باقی ہیں۔",
            "quick.for": "برائے {name} (آئی ڈی {id})",
            "quick.expires": "میعاد {date}",
            "quick.btn.in": "چیک اِن",
            "quick.btn.out": "چیک آؤٹ",
            "quick.fallback": "کیمرہ نہیں چل رہا؟ یہاں دبا کر اپنے فون کا کیمرہ ایپ استعمال کریں",
            "quick.note": "یہ لنک آپ کے ایڈمنسٹریٹر نے آپ کے لیے بنایا ہے۔ یہ بغیر پاس ورڈ آپ کو چیک اِن اور آؤٹ کرتا ہے، اور ہر ٹیپ اپنی لی گئی سیلفی کے ساتھ درج ہوتا ہے۔",
            "quick.recording": "آپ کا ٹیپ درج ہو رہا ہے…",
            "quick.noFix": "لوکیشن نہیں ملی، اس لیے پنچ نہیں بھیجا گیا۔ لوکیشن کی اجازت دیں اور دوبارہ کوشش کریں۔",
            "quick.clockedInAt": "{site} پر چیک اِن ہوا۔",
            "quick.clockedOut": "{site} سے چیک آؤٹ ہوا۔ {hours} گھنٹے ادا شدہ",
            "quick.break": " {min} منٹ کے وقفے کے بعد",
            "quick.notRecorded": "ٹیپ درج نہیں ہوا۔",
            "quick.notReached": "ٹیپ سرور تک نہیں پہنچا۔ کنکشن دیکھیں اور دوبارہ کوشش کریں۔",
            "quick.earlyTitle": "جلدی چیک آؤٹ کریں؟",
            "quick.earlyBody": "آپ نے {regular} گھنٹے کے ادا شدہ دن میں سے صرف {paid} گھنٹے کام کیا ہے۔ ابھی چیک آؤٹ کرنے پر اس بار یہی درج ہوگا \u2014 {paid} گھنٹے، پورا {regular} گھنٹے کا دن نہیں۔",
            "quick.earlyConfirm": "تصدیق کریں اور چیک آؤٹ",
            "quick.earlyCancel": "منسوخ کریں",

            "enroll.title": "اپنا چہرہ رجسٹر کریں",
            "enroll.registerTitle": "اپنا اکاؤنٹ بنائیں",
            "enroll.headTitle": "اپنا چہرہ رجسٹر کریں - حاضری الموقع",
            "enroll.sub": "آپ کا لنک دیکھا جا رہا ہے…",
            "enroll.verb.enroll": "رجسٹر ہو رہا ہے",
            "enroll.verb.register": "اکاؤنٹ بنایا جا رہا ہے برائے",
            "enroll.intro": "اچھی روشنی میں کھڑے ہوں، فون آنکھوں کی سطح پر رکھیں، اور سیدھا کیمرے کی طرف دیکھیں۔ ٹوپی یا دھوپ کے چشمے اتار دیں۔ اس تصویر کا موازنہ آپ کی چیک اِن والی سیلفی سے ہوتا ہے، اس لیے یہ وہیں لیں جہاں آپ عام طور پر چیک اِن کرتے ہیں۔",
            "enroll.password.hint": "کم از کم {n} حروف کا پاس ورڈ چنیں۔ آپ اپنی آئی ڈی ({id}) اور اسی پاس ورڈ سے لاگ ان کریں گے، اس لیے اسے سنبھال کر رکھیں۔",
            "enroll.password.placeholder": "پاس ورڈ چنیں",
            "enroll.password2.placeholder": "دوبارہ لکھیں",
            "enroll.phone.placeholder": "فون (اختیاری)",
            "enroll.email.placeholder": "ای میل (اختیاری)",
            "enroll.submit": "میری تصویر بھیجیں",
            "enroll.create": "میرا اکاؤنٹ بنائیں",
            "enroll.uploading": "اپ لوڈ ہو رہا ہے…",
            "enroll.liveness": "لائیو نیس: {verdict}",
            "enroll.done.register": "مکمل۔ آپ کا اکاؤنٹ تیار ہے - اپنی آئی ڈی اور چنے ہوئے پاس ورڈ سے لاگ ان کریں۔",
            "enroll.done.enroll": "مکمل۔ آپ کی حوالہ تصویر رجسٹر ہو گئی ہے۔",
            "enroll.failed": "رجسٹریشن ناکام ہو گئی۔",
            "enroll.unusable": "یہ لنک اب کام نہیں کرتا۔ اپنے ایڈمنسٹریٹر سے نیا مانگیں۔",
            "enroll.passwordShort": "آپ کے پاس ورڈ میں کم از کم {n} حروف ہونے چاہئیں۔",
            "enroll.passwordMismatch": "دونوں پاس ورڈ ایک جیسے نہیں ہیں۔",
            "enroll.uploadFailed": "اپ لوڈ ناکام ہو گیا۔ کنکشن دیکھیں اور دوبارہ کوشش کریں۔",
            "enroll.fallback": "کیمرہ نہیں چل رہا؟ یہاں دبا کر اپنے فون کی گیلری استعمال کریں"
        }
    };

    //: The language this page reads in, and the only place the choice is stored. The same
    //: key the app writes, because these pages are served from the same origin: a worker
    //: who chose Arabic in the app has already said so for the link they were sent.
    function storedLanguage() {
        var stored = "";
        try { stored = window.localStorage.getItem("lang") || ""; } catch (e) { stored = ""; }
        if (STRINGS[stored]) return stored;
        var asked = String(navigator.language || navigator.userLanguage || "en").slice(0, 2).toLowerCase();
        return STRINGS[asked] ? asked : "en";
    }

    var current = storedLanguage();

    function t(key, vars) {
        var table = STRINGS[current] || STRINGS.en;
        var text = table[key] || STRINGS.en[key] || key;
        if (!vars) return text;
        return text.replace(/\{(\w+)\}/g, function (whole, name) {
            return Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : whole;
        });
    }

    /** Whether the chosen language, or the English every table is written from, has a key. */
    function has(key) {
        return !!((STRINGS[current] || {})[key] || STRINGS.en[key]);
    }

    //: The reasons a link can be refused, as *tokens* rather than sentences. The server
    //: says the same three words two ways - as the ``error_code`` of a punch
    //: (``link_expired``) and as the ``status`` of a peek at an invite (``expired``) - so
    //: one list here serves both pages and neither has to invent a mapping of its own.
    var REFUSALS = ["revoked", "expired", "used_up"];

    /** The reason inside a refusal: ``link_expired`` and ``expired`` mean the same thing. */
    function refusalToken(detail) {
        if (!detail || typeof detail !== "object") return "";
        return String(detail.error_code || detail.status || "").toLowerCase().replace(/^(link|invite)_/, "");
    }

    /**
     * A refusal the server sent, in the reader's language.
     *
     * The server's ``message`` is English, written for an administrator reading a log or
     * an API client; the phone rendering it belongs to a worker who may read Arabic,
     * Hindi or Urdu. So the reason - the token - is treated as the contract, and the
     * English sentence is demoted to the fallback for a reason this page has never heard
     * of. The code stays in brackets: "tell your administrator: link_expired" is the one
     * part of a dead link a worker can usefully pass on.
     */
    function serverMessage(detail, fallbackKey) {
        var token = refusalToken(detail);
        var key = REFUSALS.indexOf(token) >= 0 ? "refuse." + token
            : (token === "unknown" ? "link.invalid" : "");
        var code = detail && typeof detail === "object" ? detail.error_code || "" : "";
        if (key && has(key)) return code ? t(key) + " (" + code + ")" : t(key);
        if (detail && typeof detail === "object" && detail.message) return detail.message;
        if (typeof detail === "string" && detail) return detail;
        return t(fallbackKey);
    }

    /** Escapes a server-supplied value for the places a sentence has to be assembled. */
    function esc(value) {
        return String(value === null || value === undefined ? "" : value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    /**
     * Reads the page's own English into the chosen language.
     *
     * The markup ships English and is re-read here, rather than being built in JavaScript:
     * a page whose script never arrives then reads as English instead of as a page full of
     * key names, which is the failure the link pages already had once.
     *
     * ``data-t``          - replaces the element's text
     * ``data-t-ph``       - replaces its ``placeholder``
     * ``data-t-alt``      - replaces its ``alt``
     * ``data-t-aria``     - replaces its ``aria-label``
     * ``data-t-title``    - replaces its ``title``
     */
    function translate(root) {
        var scope = root || document;
        var nodes = scope.querySelectorAll("[data-t], [data-t-ph], [data-t-alt], [data-t-aria], [data-t-title]");
        for (var i = 0; i < nodes.length; i += 1) {
            var node = nodes[i];
            var key = node.getAttribute("data-t");
            if (key) node.textContent = t(key);
            key = node.getAttribute("data-t-ph");
            if (key) node.setAttribute("placeholder", t(key));
            key = node.getAttribute("data-t-alt");
            if (key) node.setAttribute("alt", t(key));
            key = node.getAttribute("data-t-aria");
            if (key) node.setAttribute("aria-label", t(key));
            key = node.getAttribute("data-t-title");
            if (key) node.setAttribute("title", t(key));
        }
        // The selector is a row of three buttons; the one being read says so.
        var buttons = scope.querySelectorAll("[data-lang]");
        for (var j = 0; j < buttons.length; j += 1) {
            var mine = buttons[j].getAttribute("data-lang") === current;
            buttons[j].setAttribute("aria-pressed", mine ? "true" : "false");
        }
    }

    function applyDirection() {
        document.documentElement.setAttribute("lang", current);
        document.documentElement.setAttribute("dir", RTL.indexOf(current) >= 0 ? "rtl" : "ltr");
    }

    /**
     * Switches language without a reload, and remembers it.
     *
     * No fetch and no failure path: the whole table is in this file, so there is nothing
     * that can fail to arrive. That is the one thing the app's own language switch cannot
     * promise, and the reason it has a sentence for when a language does not come back.
     */
    function setLang(code, onChange) {
        if (!STRINGS[code] || code === current) return current;
        current = code;
        try { window.localStorage.setItem("lang", code); } catch (e) { /* private mode */ }
        applyDirection();
        translate(document);
        if (onChange) onChange(current);
        return current;
    }

    function wireLanguagePicker(onChange) {
        var buttons = document.querySelectorAll("[data-lang]");
        for (var i = 0; i < buttons.length; i += 1) {
            buttons[i].addEventListener("click", function (event) {
                event.preventDefault();
                setLang(event.currentTarget.getAttribute("data-lang"), onChange);
            });
        }
        var host = document.getElementById("langs");
        if (host) host.setAttribute("aria-label", t("language"));
    }

    // -----------------------------------------------------------------------
    // The photo policy
    // -----------------------------------------------------------------------
    //: What the server will accept, until its own answer says otherwise. The numbers match
    //: the server's, so a link whose response has not landed yet still refuses a 40 MB
    //: video rather than uploading it over a phone tether.
    //:
    //: ``face_frame_max_pixels`` / ``face_frame_max_edge_px`` are the server's *ingestion
    //: boundary* (``uploads.policy()``, mirrored in ``backend/config.py``). They are not a
    //: refusal the browser has to phrase - they are the size a photo is *resized to* before
    //: it is held, because a phone's own camera app produces 12 MP and the server refuses
    //: anything above this with a 422. A file picked from the gallery or taken by the native
    //: camera is the normal way a worker enrolls, so without this the boundary would refuse
    //: the ordinary case; with it, the browser sends what the deployment can use.
    var policy = {
        max_bytes: 5 * 1024 * 1024,
        accepted: ["image/jpeg", "image/png", "image/webp"],
        face_frame_max_pixels: 4000000,
        face_frame_max_edge_px: 2048
    };

    function setPolicy(next) {
        if (!next) return policy;
        if (next.max_bytes) policy.max_bytes = next.max_bytes;
        if (next.accepted && next.accepted.length) policy.accepted = next.accepted;
        if (next.face_frame_max_pixels) policy.face_frame_max_pixels = Number(next.face_frame_max_pixels);
        if (next.face_frame_max_edge_px) policy.face_frame_max_edge_px = Number(next.face_frame_max_edge_px);
        return policy;
    }

    /**
     * The size to send, for a photo of a given size. Pure, and the arithmetic both halves use.
     *
     * Both rules apply - the area ceiling and the long edge - and the smaller scale wins, so
     * "this photo is above the boundary" and "the browser resized it" are one question rather
     * than two. Never enlarges: ``scale`` is capped at 1, because upscaling adds no detail and
     * would change what the detector sees for every phone in the field.
     *
     * A function rather than an inline calculation in the resize below, because it is the part
     * worth testing: the suite calls it with numbers instead of needing a camera and a canvas.
     */
    function fitToLimits(width, height, limits) {
        var box = limits || policy;
        var w = Number(width || 0);
        var h = Number(height || 0);
        if (!(w > 0) || !(h > 0)) return { width: w, height: h, scale: 1 };
        var scale = 1;
        var maxEdge = Number(box.face_frame_max_edge_px || 0);
        if (maxEdge > 0) scale = Math.min(scale, maxEdge / Math.max(w, h));
        var maxPixels = Number(box.face_frame_max_pixels || 0);
        if (maxPixels > 0) scale = Math.min(scale, Math.sqrt(maxPixels / (w * h)));
        if (!(scale < 1)) return { width: w, height: h, scale: 1 };
        return {
            width: Math.max(1, Math.floor(w * scale)),
            height: Math.max(1, Math.floor(h * scale)),
            scale: scale
        };
    }

    /** The resized blob as a named JPEG. */
    function renameToJpeg(blob, file) {
        var name = String((file && file.name) || "photo").replace(/\.[a-z0-9]+$/i, "") + ".jpg";
        try {
            return new File([blob], name, { type: "image/jpeg" });
        } catch (e) {
            // A browser without ``File``: a Blob cannot carry a name, and the server reads the
            // *bytes*, so the upload is still correct - only the filename a log shows is not.
            try { blob.name = name; } catch (e2) {}
            return blob;
        }
    }

    /**
     * ``file`` reduced to the policy's boundary, or ``file`` itself when it already fits.
     *
     * ``done(small, resized)`` is called once, always, and a failure comes back as the original
     * file rather than as nothing: the server is the authority on what it will accept, and its
     * refusal names the problem in the worker's own language. The browser's job here is only to
     * not spend a phone tether sending something that will be refused.
     *
     * The re-encode is at the *maximum* quality (``1.0``). The boundary this brings a photo
     * under is about how many pixels the detector is shown, so the size is bought with
     * resolution and nothing else - lowering the JPEG quality instead would soften the very
     * crop the embedding is measured from, which is the half of the boundary that must not
     * move. Only the pixel count was over budget.
     */
    function shrinkPhoto(file, done, limits) {
        var finish = done || function () {};
        var box = limits || policy;
        // No decoder, no resize. Feature-detected rather than assumed, which also keeps this a
        // pass-through under a test harness that models a DOM but not an image decoder.
        if (!file || typeof Image === "undefined" || typeof document === "undefined") {
            return finish(file, false);
        }
        var url = null;
        var settled = false;
        function settle(small, resized) {
            if (settled) return;
            settled = true;
            if (url && typeof URL !== "undefined" && URL.revokeObjectURL) {
                try { URL.revokeObjectURL(url); } catch (e) {}
            }
            finish(small, resized);
        }
        try {
            url = URL.createObjectURL(file);
        } catch (e) {
            return settle(file, false);
        }
        var image = new Image();
        image.onload = function () {
            var fit = fitToLimits(
                image.naturalWidth || image.width,
                image.naturalHeight || image.height,
                box
            );
            if (!(fit.scale < 1)) return settle(file, false);
            try {
                var canvas = document.createElement("canvas");
                canvas.width = fit.width;
                canvas.height = fit.height;
                canvas.getContext("2d").drawImage(
                    image, 0, 0, image.naturalWidth || image.width, image.naturalHeight || image.height,
                    0, 0, fit.width, fit.height
                );
                canvas.toBlob(function (blob) {
                    if (!blob) return settle(file, false);
                    settle(renameToJpeg(blob, file), true);
                }, "image/jpeg", 1.0);
            } catch (e) {
                settle(file, false);
            }
        };
        image.onerror = function () { settle(file, false); };
        image.src = url;
        return undefined;
    }

    function mb(bytes) {
        return (Number(bytes || 0) / (1024 * 1024)).toFixed(1);
    }

    function acceptedList() {
        return policy.accepted.map(function (type) { return type.replace("image/", "").toUpperCase(); }).join(", ");
    }

    /**
     * Why this file cannot be used, or null when it can.
     *
     * The real check is the server's, from the bytes: the file name and the browser's idea
     * of the type are both supplied by whoever sends the request. This only saves somebody
     * an upload over a phone tether. ``family`` picks the wording - a worker on the punch
     * page "takes a selfie", a worker registering a face "chooses a photo", and neither
     * sentence belongs on the other page.
     */
    function photoProblem(file, family) {
        var kind = family === "photo" ? "photo" : "selfie";
        if (!file) return t("photo.none." + kind);
        var size = Number(file.size || 0);
        if (size <= 0) return t("photo.empty." + kind);
        if (size > Number(policy.max_bytes || 0)) {
            return t("photo.big", { mb: mb(size), max: mb(policy.max_bytes) });
        }
        var type = String(file.type || "").toLowerCase();
        if (policy.accepted.indexOf(type) < 0) {
            var name = String(file.name || "").toLowerCase();
            if (!(type === "" && /\.(jpe?g|png|webp)$/.test(name))) {
                return t("photo.type", { types: acceptedList() });
            }
        }
        return null;
    }

    /** The policy line under the camera, and the file input's own filter. */
    function applyPolicy() {
        var line = document.getElementById("photo-policy");
        if (line) line.textContent = t("photo.policy", { types: acceptedList(), mb: mb(policy.max_bytes) });
        var input = document.getElementById("file");
        if (input) input.setAttribute("accept", policy.accepted.join(","));
    }

    // -----------------------------------------------------------------------
    // The camera cycle
    // -----------------------------------------------------------------------
    /**
     * A selfie, from the camera or from the gallery, over the ids both pages share.
     *
     * The cycle is: ``start()`` opens the stream, ``shoot()`` draws the frame to the canvas
     * and stops the camera, ``retake()`` throws it away and opens the camera again. The
     * background-file path (the fallback label, for a browser that will not give a page a
     * stream) lands in the same place, so everything downstream sees one kind of value.
     *
     * ``options``:
     *   primary  - the id of the button that stays disabled until there is a photo; the
     *              page's own submit button, which is the thing the photo is for.
     *   allow    - asked before that button is re-enabled: a page whose link turned out to
     *              be dead must not be re-armed by a photo arriving.
     *   family   - "selfie" (punch page) or "photo" (enrollment page), for the wording.
     *   readyKey - what the primary says once there is a photo, if the page renames it.
     *   onPhoto  - called with the file, in place of any page-side bookkeeping.
     */
    function createCamera(options) {
        var opts = options || {};
        var stream = null;
        var photo = null;
        var canvas = document.getElementById("canvas");
        var video = document.getElementById("video");
        var preview = document.getElementById("preview");

        function el(id) { return document.getElementById(id); }

        function hide(id, hidden) {
            var node = el(id);
            if (node) node.classList[hidden ? "add" : "remove"]("hidden");
        }

        function allowed() {
            return !opts.allow || opts.allow() !== false;
        }

        function arm(enabled) {
            var button = opts.primary ? el(opts.primary) : null;
            if (button) button.disabled = !enabled;
        }

        function showProblem(text) {
            var box = el("photo-problem");
            if (!box) return;
            box.className = text ? "note warn" : "note warn hidden";
            box.textContent = text || "";
        }

        /** Takes a file the browser handed us - a capture, or one from the gallery. */
        function accept(file, ready) {
            var found = photoProblem(file, opts.family);
            if (found) {
                showProblem(found);
                photo = null;
                arm(false);
                return null;
            }
            showProblem("");
            // Held only once it fits the server's boundary, and the primary stays disabled
            // until then: a tap in that window would submit the full-size original, which is
            // exactly the failure this removes. With no decoder available (an old browser, or
            // a test harness) this is the synchronous flow it always was.
            arm(false);
            shrinkPhoto(file, function (small) {
                photo = small;
                if (preview) {
                    preview.src = URL.createObjectURL(small);
                    preview.alt = t("camera.preview." + (opts.family === "photo" ? "photo" : "selfie"));
                }
                hide("preview", false);
                hide("video", true);
                arm(allowed());
                if (opts.readyKey && ready) {
                    var button = opts.primary ? el(opts.primary) : null;
                    if (button) button.textContent = t(opts.readyKey);
                }
                if (opts.onPhoto) opts.onPhoto(small);
            });
            return file;
        }

        function start() {
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                showProblem("");
                if (opts.onUnsupported) opts.onUnsupported();
                return;
            }
            navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 720 } }, audio: false })
                .then(function (s) {
                    stream = s;
                    if (video) video.srcObject = s;
                    hide("video", false);
                    hide("preview", true);
                    hide("btn-shoot", false);
                    hide("btn-retake", true);
                    hide("btn-start", true);
                    hide("fallback-label", true);
                })
                .catch(function () {
                    // The stream was refused (no camera, a policy, or an unencrypted
                    // origin). The label underneath is the way out, so it stays.
                    if (opts.onBlocked) opts.onBlocked();
                });
        }

        function shoot() {
            if (!video || !canvas) return;
            // The frame is drawn at the size the boundary allows, not blindly at the stream's:
            // a browser that hands the page a 4K track would otherwise put an 8 MP frame on the
            // wire, and the server would refuse the worker's own selfie with a sentence about
            // their photo being too big. The aspect ratio is the stream's, unchanged.
            var fit = fitToLimits(video.videoWidth || 720, video.videoHeight || 960);
            canvas.width = fit.width;
            canvas.height = fit.height;
            canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
            canvas.toBlob(function (blob) {
                accept(blob, true);
                hide("btn-shoot", true);
                hide("btn-retake", false);
                stop();
            }, "image/jpeg", 0.92);
        }

        function stop() {
            if (!stream) return;
            stream.getTracks().forEach(function (track) { track.stop(); });
            stream = null;
        }

        function retake() {
            photo = null;
            arm(false);
            showProblem("");
            hide("btn-shoot", false);
            hide("btn-retake", true);
            start();
        }

        /** A file out of the picker, which reports nothing useful when it is refused. */
        function chooseFromInput(input) {
            var file = input && input.files && input.files[0];
            if (file) accept(file, false);
        }

        return {
            start: start,
            shoot: shoot,
            stop: stop,
            retake: retake,
            accept: accept,
            chooseFromInput: chooseFromInput,
            problem: function () { return photoProblem(photo, opts.family); },
            showProblem: showProblem,
            photo: function () { return photo; },
            hasPhoto: function () { return !!photo; },
            clear: function () { photo = null; arm(false); hide("preview", true); },
            arm: arm
        };
    }

    // -----------------------------------------------------------------------
    // The location fix
    // -----------------------------------------------------------------------
    /**
     * The location fix, or an explanation of why there is none.
     *
     * The site's geofence still applies to a link - that is what keeps it from being a way
     * to clock in from home - so the coordinates go with the punch and a refusal names the
     * reason. A high-accuracy fix is requested because a phone's coarse network fix can be
     * kilometres out, which would fail a geofence the worker is standing inside.
     *
     * Resolves ``null`` rather than rejecting: "no fix" is an answer this page acts on, and
     * a rejection here would arrive as an unhandled one on a page with no handler.
     */
    //: The last thing the location line said, as a key and its values rather than as a
    //: sentence - so a language switch can say it again in the other language instead of
    //: falling back to "checking your location", which would be a lie about a fix that has
    //: already happened.
    var locationLine = { key: "location.checking", vars: null };

    function repaintLocation() {
        var line = document.getElementById("location-line");
        if (line) line.textContent = t(locationLine.key, locationLine.vars);
    }

    function locate(options) {
        var line = document.getElementById((options && options.statusId) || "location-line");
        var write = function (key, vars) {
            locationLine = { key: key, vars: vars || null };
            if (line) line.textContent = t(key, vars);
        };
        return new Promise(function (resolve) {
            write("location.checking");
            if (!navigator.geolocation) {
                write("location.unsupported");
                resolve(null);
                return;
            }
            navigator.geolocation.getCurrentPosition(
                function (position) {
                    var fix = {
                        lat: position.coords.latitude,
                        lon: position.coords.longitude,
                        accuracy: position.coords.accuracy
                    };
                    write("location.found", { m: Math.round(fix.accuracy || 0) });
                    resolve(fix);
                },
                function (error) {
                    write(error && error.code === 1 ? "location.blocked" : "location.unknown");
                    resolve(null);
                },
                { enableHighAccuracy: true, timeout: 15000, maximumAge: 30000 }
            );
        });
    }

    /** The foot of every page: the vendor's credit, in the reader's language. */
    function credit() {
        return t("credit", { brand: BRAND });
    }

    window.Capture = {
        BRAND: BRAND,
        LANGUAGES: LANGUAGES,
        t: t,
        esc: esc,
        get lang() { return current; },
        setLang: setLang,
        wireLanguagePicker: wireLanguagePicker,
        translate: translate,
        applyDirection: applyDirection,
        credit: credit,
        serverMessage: serverMessage,
        languages: function () { return LANGUAGES.slice(); },
        setPolicy: setPolicy,
        policy: function () { return policy; },
        mb: mb,
        photoProblem: photoProblem,
        fitToLimits: fitToLimits,
        shrinkPhoto: shrinkPhoto,
        applyPolicy: applyPolicy,
        createCamera: createCamera,
        locate: locate,
        repaintLocation: repaintLocation
    };
})();
