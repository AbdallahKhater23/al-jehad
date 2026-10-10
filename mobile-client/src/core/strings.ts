/**
 * The mobile client's own strings, in the four languages the app ships.
 *
 * The web console already has a reviewed four-table vocabulary (``frontend/i18n.js``
 * and its ``.ar`` / ``.hi`` / ``.ur`` siblings), and every key below that names the
 * same concept uses that file's wording verbatim -- ``nav.*`` is the worker tab bar,
 * ``auth.userId`` / ``auth.emailOrPhone`` / ``auth.password`` are the console login
 * form's own labels, ``app.name`` is its ``title``. Where the handset says something
 * the console does not (its header controls), the sentence is written here.
 *
 * WHAT IS COVERED, AND WHAT IS NOT. This table carries the shell's chrome (the tab
 * bar, the header, the theme and language controls) and the whole sign-in screen --
 * the screens that carry these controls and therefore have to change when the
 * worker changes language. The other screens' bodies are still English literals in
 * their own files; moving them here is a per-screen job, and a half-translated
 * screen is worse than an untranslated one.
 *
 * English is the fallback: a key missing from a table (a compile error, since every
 * table is typed ``Record<StringKey, string>``) would have to be filled in before
 * the build passes, and a lookup that somehow misses falls back to English rather
 * than rendering the key.
 */

import { getLocale, type LocaleCode } from './locale.js';

const EN = {
  'app.name': 'Site attendance',
  'header.roleSignedOut': 'Sign in',
  'nav.sections': 'Sections',
  'nav.clock': 'Clock',
  'nav.history': 'History',
  'nav.alerts': 'Alerts',
  'nav.notes': 'Notes',
  'nav.profile': 'Profile',
  'theme.label': 'Theme',
  'theme.switchToDark': 'Switch to dark screen',
  'theme.switchToLight': 'Switch to light screen',
  'language.label': 'Language',
  'auth.subtitle': 'Sign in with the ID and details your administrator gave you.',
  'auth.userId': 'Worker ID',
  'auth.emailOrPhone': 'Email or phone',
  'auth.password': 'Password',
  'auth.signIn': 'Sign in',
  'auth.signingIn': 'Signing in…',
  'auth.welcome': 'Welcome, {name}',
  'auth.pendingApproval':
    'Signed in. Your account is waiting for approval before you can record attendance.',
  'auth.offline': 'No connection to the server. Check your signal and try again.',
  'auth.rejected': 'That ID, email or password was not accepted.',
  'auth.forbidden': 'This account cannot sign in.',
  'auth.tooMany': 'Too many attempts. Wait a minute and try again.',
  'auth.failed': 'Sign-in failed.',
} as const;

export type StringKey = keyof typeof EN;

const AR: Record<StringKey, string> = {
  'app.name': 'نظام حضور الموقع',
  'header.roleSignedOut': 'تسجيل الدخول',
  'nav.sections': 'الأقسام',
  'nav.clock': 'الحضور',
  'nav.history': 'السجل',
  'nav.alerts': 'التنبيهات',
  'nav.notes': 'الملاحظات',
  'nav.profile': 'الحساب',
  'theme.label': 'السمة',
  'theme.switchToDark': 'التبديل إلى الشاشة الداكنة',
  'theme.switchToLight': 'التبديل إلى الشاشة الفاتحة',
  'language.label': 'اللغة',
  'auth.subtitle': 'سجّل الدخول برقم المستخدم والبيانات التي أعطاك إياها المدير.',
  'auth.userId': 'رقم المستخدم',
  'auth.emailOrPhone': 'البريد الإلكتروني أو الهاتف',
  'auth.password': 'كلمة المرور',
  'auth.signIn': 'تسجيل الدخول',
  'auth.signingIn': 'جارٍ تسجيل الدخول…',
  'auth.welcome': 'مرحباً، {name}',
  'auth.pendingApproval': 'تم تسجيل الدخول. حسابك بانتظار الموافقة قبل تسجيل الحضور.',
  'auth.offline': 'لا اتصال بالخادم. تحقق من الشبكة وحاول مرة أخرى.',
  'auth.rejected': 'لم يُقبل رقم المستخدم أو البريد الإلكتروني أو كلمة المرور.',
  'auth.forbidden': 'هذا الحساب لا يمكنه تسجيل الدخول.',
  'auth.tooMany': 'محاولات كثيرة. انتظر دقيقة ثم حاول مرة أخرى.',
  'auth.failed': 'تعذّر تسجيل الدخول.',
};

const HI: Record<StringKey, string> = {
  'app.name': 'साइट उपस्थिति',
  'header.roleSignedOut': 'लॉग इन करें',
  'nav.sections': 'अनुभाग',
  'nav.clock': 'अटेंडेंस',
  'nav.history': 'इतिहास',
  'nav.alerts': 'अलर्ट',
  'nav.notes': 'नोट्स',
  'nav.profile': 'प्रोफ़ाइल',
  'theme.label': 'थीम',
  'theme.switchToDark': 'डार्क स्क्रीन पर जाएँ',
  'theme.switchToLight': 'लाइट स्क्रीन पर जाएँ',
  'language.label': 'भाषा',
  'auth.subtitle': 'अपने प्रशासक द्वारा दिए गए आईडी और विवरण से साइन इन करें।',
  'auth.userId': 'यूज़र आईडी',
  'auth.emailOrPhone': 'ईमेल या फ़ोन',
  'auth.password': 'पासवर्ड',
  'auth.signIn': 'लॉग इन करें',
  'auth.signingIn': 'साइन इन हो रहा है…',
  'auth.welcome': 'स्वागत है, {name}',
  'auth.pendingApproval':
    'साइन इन हो गया। हाज़िरी दर्ज करने से पहले आपके खाते को मंज़ूरी का इंतज़ार है।',
  'auth.offline': 'सर्वर से कोई कनेक्शन नहीं। नेटवर्क जाँचें और फिर कोशिश करें।',
  'auth.rejected': 'यह आईडी, ईमेल या पासवर्ड स्वीकार नहीं हुआ।',
  'auth.forbidden': 'यह खाता साइन इन नहीं कर सकता।',
  'auth.tooMany': 'बहुत ज़्यादा कोशिशें। एक मिनट रुककर दोबारा कोशिश करें।',
  'auth.failed': 'साइन इन नहीं हो सका।',
};

const UR: Record<StringKey, string> = {
  'app.name': 'حاضری الموقع',
  'header.roleSignedOut': 'لاگ ان',
  'nav.sections': 'حصے',
  'nav.clock': 'کلاک',
  'nav.history': 'تاریخ',
  'nav.alerts': 'الرٹس',
  'nav.notes': 'نوٹس',
  'nav.profile': 'پروفائل',
  'theme.label': 'تھیم',
  'theme.switchToDark': 'گہری اسکرین پر جائیں',
  'theme.switchToLight': 'روشن اسکرین پر جائیں',
  'language.label': 'زبان',
  'auth.subtitle': 'اپنے ایڈمنسٹریٹر کے دیے گئے آئی ڈی اور تفصیلات سے لاگ ان کریں۔',
  'auth.userId': 'صارف آئی ڈی',
  'auth.emailOrPhone': 'ای میل یا فون',
  'auth.password': 'پاس ورڈ',
  'auth.signIn': 'لاگ ان',
  'auth.signingIn': 'لاگ ان ہو رہا ہے…',
  'auth.welcome': 'خوش آمدید، {name}',
  'auth.pendingApproval': 'لاگ ان ہو گیا۔ حاضری درج کرنے سے پہلے آپ کے اکاؤنٹ کو منظوری کا انتظار ہے۔',
  'auth.offline': 'سرور سے کوئی رابطہ نہیں۔ نیٹ ورک دیکھیں اور دوبارہ کوشش کریں۔',
  'auth.rejected': 'یہ آئی ڈی، ای میل یا پاس ورڈ قبول نہیں ہوا۔',
  'auth.forbidden': 'یہ اکاؤنٹ لاگ ان نہیں کر سکتا۔',
  'auth.tooMany': 'بہت زیادہ کوششیں۔ ایک منٹ رک کر دوبارہ کوشش کریں۔',
  'auth.failed': 'لاگ ان ناکام رہا۔',
};

const TABLES: Record<LocaleCode, Record<StringKey, string>> = { ar: AR, en: EN, hi: HI, ur: UR };

/**
 * One string, in the language the screen is drawn in, with ``{name}`` placeholders
 * filled from ``vars``. The value returned is plain text, never markup.
 */
export function t(key: StringKey, vars?: Record<string, string | number>): string {
  const table = TABLES[getLocale()] ?? EN;
  const template = table[key] ?? EN[key];
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in vars ? String(vars[name]) : match,
  );
}

export const __test__ = { TABLES };
