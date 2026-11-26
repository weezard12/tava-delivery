# Voice

The employee talks to the packing screen in Hebrew. Speech becomes text, text becomes one or
more of the same validated actions the buttons use, and the screen answers in Hebrew. Nothing
spoken changes an order directly.

```
microphone ─▶ /voice/audio ─▶ stt (faster-whisper, Hebrew) ─▶ text ─┐
browser speech recognition (fallback) ─▶ /voice/text ───────────────┴▶ Hebrew parser
                                                   ─▶ actions ─▶ packing service ─▶ answer
                                    └─ product not found ─▶ AI (optional) ─▶ checked ─┘
```

## Speech recognition

`stt/` is a small FastAPI service around faster-whisper with the ivrit.ai Hebrew model
(`ivrit-ai/whisper-large-v3-turbo-ct2`), voice activity detection and beam search. It runs
next to the service (`deploy/compose.stt.yml`), so audio never leaves the server. Audio is
decoded in memory and never stored; only the recognised sentence is kept, for
`voice_transcript_days` (default 30), so the parser can be improved from real use.

- `POST /v1/transcribe?language=he` takes the raw recording (webm, ogg, wav or mp4) and
  answers `{text, confidence, duration, language}`.
- `STT_DEVICE` (`cpu` or `cuda`), `STT_COMPUTE_TYPE` (`int8` on CPU) and `STT_TOKEN` (a shared
  secret, set as `DELIVERY_STT_TOKEN` on the service) configure it.
- Without `DELIVERY_STT_URL` the screen uses the browser's own speech recognition and sends the
  text to `/voice/text`.

A sentence recognised below `stt_min_confidence` (default 0.45) is asked again, not guessed.

## The Hebrew parser

`domain/hebrew/` is deterministic and has no I/O, so every rule is a unit test.

- **Text.** Niqqud and punctuation are removed, final letters normalised, and the prefixes
  ו, ה, ב, ל, מ, ש, כ are tried off each word. Plurals are reduced to their singular.
- **Numbers.** Hebrew number words in both genders (אחד, שתיים, שתי, שלושה…), digits, and
  "הכל" or "את כולם" for every remaining unit.
- **Products.** A spoken span is matched against each line's name and the nicknames the
  warehouse taught it, by word similarity (edit distance) and coverage. A span counts only if
  its first word matches a word of the product, so "סבון ומשחת שיניים" names two products.
  Two close candidates are an ambiguity: the screen asks which one, and the next sentence can
  answer with a name or "הראשון" / "השני".
- **Commands.**

| Say | Action |
| --- | --- |
| שמתי סרום שפע, שמתי שתי משחות, הכנסתי את כל הסבונים | `confirm_product` (with the quantity) |
| תבטל את המוצר האחרון, תוריד את הסבון | `undo_product_confirmation` |
| מה עוד חסר? | `get_remaining_products` |
| מה ההערות?, תקריא את ההערות | `read_order_notes` |
| עטפתי באריזת מתנה (words of the note), עשיתי את ההערה (the only open one) | `confirm_order_note` |
| חסר לנו סבון, אין במלאי שתי משחות | `report_missing_inventory` |
| סיימתי, סיים הזמנה | `complete_order` |
| תדפיס מדבקה, תדפיס שוב | `print_shipping_label` (again: reprint) |
| עבור להזמנה הבאה, תחזור להזמנה הקודמת | `next_order`, `previous_order` |
| תשהה, דלג כי … | `pause_order`, `skip_order` (with the reason) |
| עזרה | the examples |

One sentence may hold several actions ("שמתי סבון ומשחה"). Each one is checked and run by the
packing service, which refuses what the state does not allow (for example completing with
products still open), and the answer says what was done and what was not.

## Acting once

- Each recording or sentence carries an utterance ID; the same ID returns the first answer.
- The same words again within `voice_duplicate_seconds` (default 4) after they were acted on
  are taken as a repeated recognition and ignored.
- Every utterance is saved with its outcome (`executed`, `clarify`, `failed`, `ignored`,
  `duplicate`, `low_confidence`, `help`) and which interpreter understood it (`rules`, `ai`).

## The AI fallback

Optional (`DELIVERY_AI_PROVIDER=anthropic` and `DELIVERY_ANTHROPIC_API_KEY`). It is asked only
when the parser understood the kind of command but not which product or note was meant,
usually a nickname, a colour or a size instead of the catalogue name.

- **What it sees.** The sentence; the order's products (id, catalogue name, quantity required,
  quantity confirmed); its unconfirmed packing notes, with e-mails and long numbers masked.
  Never the customer's name, phone, address, prices or any other order.
- **What it can say.** Structured output against a fixed JSON schema: up to five of
  `confirm_product`, `report_missing_inventory` and `confirm_order_note` with an ID from the
  lists, a clarification question, or "not for the screen". The sentence is treated as data.
- **What happens to it.** Every proposal is checked: the action must be one of the three, the
  ID must be in this order, the quantity must fit. What passes goes through the packing
  service like a button press. A failure, a refusal or a timeout (15 seconds) leaves the
  parser's answer (usually a question).
- A phrase the warehouse uses for a product can be saved as its nickname (`/aliases`), so
  the parser knows it next time without the AI.

The model is `DELIVERY_AI_MODEL`; a smaller model answers faster and costs less.
