"""Errors the API answers with: a stable code, an HTTP status and a Hebrew message.

The workspace frontend switches on ``code``; the message is what the employee sees and, for
voice commands, hears. Messages never contain customer details, tokens or provider text.
"""

from collections.abc import Mapping

MESSAGES: dict[str, tuple[int, str]] = {
    # Sign-in.
    "invalid_session": (401, "צריך להתחבר מחדש למערכת."),
    "forbidden": (403, "אין לך הרשאה לפעולה הזו."),
    "admin_required": (403, "רק מנהל יכול לבצע את הפעולה הזו."),
    "workspace_auth_unavailable": (503, "שירות ההתחברות לא זמין כרגע. נסו שוב בעוד רגע."),
    # Packing units.
    "fulfillment_not_found": (404, "ההזמנה לא נמצאה."),
    "fulfillment_locked": (409, "עובד אחר אורז את ההזמנה הזו כרגע."),
    "not_claimed": (409, "ההזמנה לא פתוחה אצלך. פתחו אותה מחדש."),
    "invalid_state": (409, "אי אפשר לבצע את הפעולה במצב הנוכחי של ההזמנה."),
    "order_finished": (409, "ההזמנה כבר הושלמה. אפשר רק לצפות בה או להדפיס מדבקה שוב."),
    "line_not_found": (404, "המוצר לא נמצא בהזמנה הזו."),
    "note_not_found": (404, "ההערה לא נמצאה בהזמנה הזו."),
    "quantity_invalid": (422, "הכמות לא תקינה."),
    "quantity_exceeds_required": (422, "הכמות גדולה ממה שנדרש בהזמנה."),
    "nothing_to_undo": (409, "אין אישור לבטל."),
    "line_already_confirmed": (409, "המוצר הזה כבר אושר במלואו."),
    "line_removed": (409, "המוצר הוסר מההזמנה בקונימבו. הוציאו אותו מהקופסה."),
    "note_required": (422, "יש כמה הערות פתוחות. אמרו איזו מהן בוצעה."),
    "no_open_notes": (409, "אין הוראות אריזה פתוחות."),
    "already_packed": (409, "ההזמנה כבר נארזה. אפשר רק להדפיס מדבקה."),
    "reopen_blocked": (409, "ההזמנה כבר בתהליך סיום מול קונימבו ואי אפשר לפתוח אותה מחדש."),
    "line_missing": (409, "המוצר סומן כחסר במלאי. בטלו את הדיווח לפני שמאשרים אותו."),
    "incomplete_order": (409, "יש עוד מוצרים או הוראות אריזה שלא אושרו."),
    "missing_reason_required": (422, "צריך לכתוב סיבה."),
    "skip_reason_required": (422, "צריך לכתוב למה מדלגים על ההזמנה."),
    "no_next_order": (404, "אין עוד הזמנות בתור."),
    "no_previous_order": (404, "אין הזמנה קודמת."),
    "not_awaiting_inventory": (409, "ההזמנה לא ממתינה למלאי."),
    "action_unknown": (422, "הפעולה לא מוכרת."),
    "idempotency_conflict": (409, "הבקשה כבר נשלחה עם תוכן אחר."),
    "batch_empty": (422, "לא נבחרו הזמנות לאצווה."),
    # Duplicate review.
    "review_not_found": (404, "הבדיקה לא נמצאה."),
    "review_decided": (409, "כבר הוחלט על הקבוצה הזו."),
    "review_orders_changed": (409, "ההזמנות בקבוצה השתנו. רעננו ונסו שוב."),
    "consolidation_blocked": (
        409,
        "אי אפשר לאחד הזמנות שכבר נארזו, נשלחו או נפתחו אצל עובד אחר.",
    ),
    # Shipments.
    "shipment_not_required": (409, "להזמנה הזו לא נדרש משלוח עם Cargo."),
    "shipments_by_konimbo": (
        409,
        "המשלוחים נוצרים ב-Cargo דרך קונימבו. קשרו את מספר המשלוח אם הוא לא נמצא לבד.",
    ),
    "shipment_exists": (409, "כבר יש משלוח להזמנה הזו."),
    "shipment_pending": (409, "המשלוח עדיין נוצר ב-Cargo. רגע אחד."),
    "shipment_uncertain": (
        409,
        "לא ברור אם המשלוח נוצר ב-Cargo. בדקו באתר Cargo וקשרו את מספר המשלוח, "
        "או אשרו שלא נוצר כדי לנסות שוב.",
    ),
    "shipment_missing": (409, "עוד אין משלוח להזמנה הזו."),
    "shipment_not_found": (404, "המשלוח לא נמצא ב-Cargo."),
    "shipment_number_invalid": (422, "מספר המשלוח לא תקין."),
    "shipment_cancelled": (409, "המשלוח בוטל ב-Cargo."),
    "cargo_not_configured": (503, "החיבור ל-Cargo לא הוגדר."),
    "cargo_writes_disabled": (
        409,
        "יצירת משלוחים ב-Cargo כבויה במצב בדיקה. קשרו מספר משלוח קיים.",
    ),
    "cargo_customer_code_missing": (503, "חסר קוד לקוח של Cargo לסוג המשלוח הזה."),
    "cargo_sender_incomplete": (503, "חסרים פרטי השולח (כתובת וטלפון) בהגדרות Cargo."),
    "cargo_rejected": (422, "Cargo דחתה את המשלוח. בדקו את פרטי ההזמנה."),
    "cargo_unavailable": (503, "Cargo לא זמינה כרגע. ננסה שוב."),
    "cargo_unexpected_response": (502, "Cargo החזירה תשובה לא צפויה."),
    "address_incomplete": (422, "כתובת המשלוח חסרה או לא מלאה."),
    "phone_missing": (422, "חסר טלפון ללקוח, ו-Cargo דורשת אותו."),
    "shipment_number_ambiguous": (
        409,
        "בהיסטוריה של ההזמנה בקונימבו יש כמה מספרי משלוח. קשרו את המספר הנכון ידנית.",
    ),
    "shipment_not_in_store": (
        409,
        "מספר המשלוח עוד לא הופיע בקונימבו. בדקו ב-Cargo וקשרו אותו ידנית.",
    ),
    "transmit_not_configured": (
        409,
        "לא הוגדר מצב ההזמנה ששולח אותה ל-Cargo. הגדירו אותו במסך ההגדרות או קשרו מספר משלוח.",
    ),
    "pickup_point_unknown": (
        422,
        "לא ידוע לאיזו נקודת איסוף לשלוח. צרו את המשלוח ב-Cargo וקשרו את מספרו.",
    ),
    # Labels.
    "printer_not_configured": (503, "מדפסת המדבקות לא הוגדרה."),
    "label_printing_unavailable": (
        503,
        "הדפסת מדבקות Cargo עוד לא מחוברת. הדפיסו את המדבקה כמו היום.",
    ),
    "label_not_ready": (409, "המדבקה עדיין לא מוכנה. המשלוח נוצר ב-Cargo."),
    "label_mismatch": (409, "המדבקה ש-Cargo החזירה לא שייכת למשלוח הזה. לא הודפס דבר."),
    "label_unavailable": (502, "לא הצלחנו לקבל את המדבקה מ-Cargo."),
    "print_failed": (502, "ההדפסה נכשלה. בדקו את המדפסת ונסו שוב."),
    "print_outcome_unknown": (
        502,
        "לא ברור אם המדבקה הודפסה. בדקו את המדפסת ואת תור ההדפסה לפני שמדפיסים שוב.",
    ),
    "printer_unavailable": (503, "שירות ההדפסה לא זמין כרגע."),
    "already_printed": (409, "המדבקה כבר הודפסה. כדי להדפיס שוב בקשו הדפסה חוזרת."),
    # Konimbo.
    "konimbo_not_configured": (503, "החיבור לקונימבו לא הוגדר."),
    "konimbo_unavailable": (503, "קונימבו לא זמינה כרגע. ננסה שוב."),
    "konimbo_rate_limited": (503, "הגענו למכסת הבקשות של קונימבו. ננסה שוב בעוד כמה דקות."),
    "konimbo_unauthorized": (503, "קונימבו דחתה את מפתח ה-API."),
    "konimbo_rejected": (422, "קונימבו דחתה את העדכון."),
    "konimbo_unexpected_response": (502, "קונימבו החזירה תשובה לא צפויה."),
    "konimbo_order_not_found": (404, "ההזמנה לא נמצאה בקונימבו."),
    "konimbo_unverified": (502, "קונימבו לא אישרה שהעדכון נשמר."),
    "konimbo_writes_disabled": (409, "עדכונים לקונימבו כבויים במצב בדיקה."),
    "konimbo_status_unknown": (
        409,
        "קונימבו רשמה את העדכון אבל מצב ההזמנה לא השתנה. בדקו ששם המצב בהגדרות זהה לשם בקונימבו.",
    ),
    "order_left_scope": (
        409,
        "ההזמנה עברה בקונימבו למצב שהמחסן לא מטפל בו. בדקו מול המשרד לפני שממשיכים.",
    ),
    # Settings.
    "settings_invalid": (422, "ההגדרות לא נשמרו."),
    # Jobs.
    "job_not_found": (404, "המשימה לא נמצאה."),
    "job_not_retryable": (409, "אי אפשר לנסות שוב את המשימה הזו."),
    # Voice.
    "stt_not_configured": (503, "זיהוי הדיבור המקומי לא הוגדר."),
    "stt_unavailable": (503, "זיהוי הדיבור לא זמין כרגע."),
    "audio_invalid": (422, "ההקלטה לא תקינה."),
    "audio_too_large": (413, "ההקלטה ארוכה מדי."),
    "transcript_invalid": (422, "הטקסט שזוהה לא תקין."),
    "ai_unavailable": (503, "העוזר החכם לא זמין כרגע."),
    "ai_refused": (422, "העוזר החכם לא ענה על הבקשה הזו."),
    "ai_unexpected_response": (502, "העוזר החכם החזיר תשובה לא צפויה."),
    # Aliases.
    "alias_invalid": (422, "הכינוי לא תקין."),
    "alias_target_required": (422, "צריך לבחור מוצר לכינוי."),
    "alias_not_found": (404, "הכינוי לא נמצא."),
    # General.
    "request_invalid": (422, "הבקשה לא תקינה."),
    "not_found": (404, "הדף לא נמצא."),
    "internal_error": (500, "אירעה שגיאה פנימית."),
    "service_unavailable": (503, "השירות לא זמין כרגע."),
}


class ServiceError(Exception):
    """A failure the caller should see. ``code`` is stable; ``message`` is safe to show."""

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        message: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        default_status, default_message = MESSAGES.get(code, MESSAGES["internal_error"])
        self.code = code
        self.status = status or default_status
        self.message = message or default_message
        self.headers = dict(headers or {})
        super().__init__(code)


def message_for(code: str) -> str:
    return MESSAGES.get(code, MESSAGES["internal_error"])[1]


class ProviderError(Exception):
    """A provider call failed. ``code`` is one of the codes above.

    ``retryable``: trying again later may work. ``sent``: the request may have reached the
    provider, so a write may have happened even though no answer came back.
    """

    def __init__(self, code: str, *, retryable: bool = False, sent: bool = True) -> None:
        self.code = code
        self.retryable = retryable
        self.sent = sent
        super().__init__(code)
