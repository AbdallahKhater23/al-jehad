/**
 * The second sign-in credential, and the request body the three of them produce.
 *
 * WHY THIS IS SEPARATE FROM THE SCREEN
 * -----------------------------------
 * The rules are pure - strings in, a message or nothing out - and keeping them free of DOM and
 * transport imports is what lets a Node harness bundle them and check the edge cases directly
 * (``backend/tests/test_mobile_login_identity.py``, in the same shape as the punch-flow and
 * signing suites). Reached through ``login.ts`` they would arrive behind ``core/http.ts`` and
 * its Capacitor plugins, which do not load outside a browser, so the rule could only ever be
 * checked through a device.
 *
 * THE SECOND CREDENTIAL IS NOT AN EMAIL FIELD
 * -------------------------------------------
 * ``POST /auth/login`` matches the account with::
 *
 *     WHERE id = ? AND (email = ? OR phone = ?)
 *
 * so what the worker types is compared against *either* column, and an account may hold a phone
 * number and no email at all. A worker registered by phone signs in with that phone:
 * ``backend/tests/test_walk_up_registration.py`` hands over ``+965 555 0199`` and then signs in
 * with the number itself as the second credential. An email-only rule would refuse a value the
 * server accepts - at the keyboard, before any request is sent, which is the worst place to be
 * wrong and the one place no server-side test can see.
 *
 * IT IS ALSO ALLOWED TO BE EMPTY
 * ------------------------------
 * ``users.email`` and ``users.phone`` both default to ``''``, and the console's create-account
 * form offers both as optional fields, so an account holding neither is one ordinary action
 * away. For that account the only second credential that can match is the empty string, which
 * means a blank field has to be *submittable*. The console's sign-in form carries the same rule
 * for the same reason; ``backend/tests/test_frontend_login_credentials.py`` holds it there, and
 * this module is the Android client's half of that contract.
 */

/** What the server can match: an address, or something that reads as a phone number. */
const EMAIL_SHAPE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const PHONE_SHAPE = /^[+\d][\d\s()-]{5,}$/;

/**
 * ``null``, ``undefined``, ``''`` and ``'   '`` are all *absent*, not invalid.
 *
 * Whitespace is folded in one place - here - rather than at each caller, because a field holding
 * a single space is empty for the purpose of "is anything here", and normalizing on the way in
 * is what stops the validator and the request body from answering that differently.
 */
export function normalizeIdentity(value: string | null | undefined): string {
  return value === null || value === undefined ? '' : String(value).trim();
}

/**
 * The complaint to show under the field, or ``''`` when there is nothing to say.
 *
 * Two silences, and both are load-bearing:
 *
 * * **empty** is not a failure. The request it produces carries ``''``, which is exactly what an
 *   account with no email and no phone matches. Complaining here is what used to strand such a
 *   worker on a screen telling them to fill in a box they have nothing to put in it.
 * * **recognized** is not a failure either. An address or a number passes, and the server stays
 *   the authority on whether it belongs to an account: the client only refuses text that could
 *   not match any row, so a credential the server would accept is never blocked here.
 *
 * Only a filled field that reads as neither shape earns a message, and the value stays on screen
 * underneath it to be corrected.
 */
export function identityError(value: string | null | undefined): string {
  const text = normalizeIdentity(value);
  if (!text) return '';
  if (EMAIL_SHAPE.test(text) || PHONE_SHAPE.test(text)) return '';
  return 'That is not an email address or a phone number.';
}

/** Local validation, so an obviously wrong entry does not cost a bcrypt round trip. */
export function validateCredentials(input: {
  userId: string;
  emailOrPhone: string | null | undefined;
  password: string;
}): Record<string, string> {
  const errors: Record<string, string> = {};
  const userId = String(input.userId ?? '').trim();
  if (!userId) errors.user_id = 'Enter your worker ID.';
  else if (userId.length > 64) errors.user_id = 'That ID is too long.';

  // Optional: blank is a valid answer, so the field stays silent until something unrecognizable
  // is in it. See ``identityError``.
  const identity = identityError(input.emailOrPhone);
  if (identity) errors.email_or_phone = identity;

  if (!input.password) errors.password = 'Enter your password.';
  return errors;
}

/**
 * The body ``POST /auth/login`` expects.
 *
 * ``email_or_phone`` is always present, and ``''`` when the field is blank. Neither alternative
 * works, and both are pinned by the console-side suite of the same name:
 *
 * * **omitting the key** is a 422 - ``LoginRequest.email_or_phone`` is a required ``str``; and
 * * **sending ``null``** is the same 422, for the same reason.
 *
 * The empty string is also the only value that can match an account whose ``email`` and
 * ``phone`` are both ``''``.
 *
 * The value travels otherwise **unchanged** after trimming. The server compares it to the stored
 * column with a plain ``=``, and ``users.email`` is a plain ``TEXT`` with no ``COLLATE NOCASE``,
 * so folding the case here would turn a correctly typed address into a 401 for every account
 * whose stored address carries a capital letter.
 */
export function loginRequestBody(values: {
  userId: string;
  emailOrPhone: string | null | undefined;
  password: string;
}): { user_id: string; email_or_phone: string; password: string } {
  return {
    user_id: String(values.userId ?? '').trim(),
    email_or_phone: normalizeIdentity(values.emailOrPhone),
    password: values.password,
  };
}
