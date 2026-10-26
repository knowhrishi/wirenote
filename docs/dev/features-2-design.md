# Design note: notes during the call, record button, vocabulary, action items, Ask across meetings, follow-up, speaker names

Status: **chosen 2026-10-26** (features 1, 2, 3, 4, 5, 7 from a comparison with other meeting-note tools). Built straight away; this note records the design.

## 1. Your notes during the call (Granola)

- While a call records, its page in the app shows a **Your notes** editor. It autosaves (debounced) to `my_notes.md` in the recording folder, and stays editable after the call.
- The summary prompt gets these notes as "what mattered to the person who recorded". The model keeps their points, fills in details from the transcript, and corrects them where the transcript disagrees.
- The Markdown note gets a **My notes** section. On the call page, "Update summary" re-runs the summary with the edited notes.

## 2. Record button (in-person meetings)

- A **Record a meeting** button in the app (and `wirenote record --now`) starts a recording with no call app on the mic: mic plus system audio, both following the defaults.
- In the tracker it is an app with key `manual`. It never auto-stops (there is no app to leave the mic); only Stop, the 4 h cap and sleep end it. It is refused while another recording runs.
- The title is "In-person meeting", or the calendar event in the window if there is one.

## 3. Custom vocabulary (Wispr's personal dictionary)

- `~/.config/wirenote/vocabulary.txt`, one name or term per line, editable on the app's Settings page.
- Whisper gets the terms through faster-whisper's `hotwords`, which goes into every 30 s window's prompt, unlike `initial_prompt`, which only reaches the first. The summary prompt gets them too, as correct spellings.

## 4. My action items

- An **Action items** page lists every action item across calls: yours (owner "Me") first, then others', then done items.
- Each shows its call and date, can be ticked (same API as the call page) and links to its call. The sidebar shows the count of your open items.

## 5. Ask across meetings (Wispr, Granola chat)

- An **Ask** page with a question box and its own history (`state/asks.json`).
- **Retrieval, no embeddings:** transcript paragraphs and summary points from all calls are scored by word overlap with the question. Words are matched on their first 5 letters, so German inflections still match. The best ones are kept with their neighbours within the model's budget (18,000 characters for the local model).
- Each excerpt is labelled `[C<n> HH:MM:SS]`. The answer cites those labels, and the page turns them into links to that call and moment.

## 7. Follow-up email draft (Granola, Otter)

- A **Draft follow-up** button on a call writes a recap email (subject and body, in the call's language) from the summary, action items and your notes, through the summary backend. It is saved in `followup.json`.
- The card has **Copy** and **Open in mail** (`mailto:` to the attendees' addresses from the calendar event, when known).

## Speakers: who said what (asked for during the build)

- **Speaker separation:** sherpa-onnx (CPU), using pyannote segmentation 3.0 plus the `nemo_en_titanet_small` embedding with clustering threshold 0.9.
  - Picked on a 42 s, 3-speaker English test with known truth (pyannote's 2-speaker sample plus a third voice).

    | Embedding model | Best threshold | Speakers found | Correct speech | Time |
    | --- | --- | --- | --- | --- |
    | titanet_small | 0.9 | 3 | 87% | 3.6 s |
    | eres2net | 0.7 | 4 | 85% | 9.2 s |
    | campplus | 0.3 | 6 | 81% | 3.9 s |
    | wespeaker resnet34 | 0.3 | 6 | 71% | 9.3 s |

- **Lines are sentences:** sentences are built from Whisper's word timestamps, ending at punctuation or at a pause over 1 s. A sentence goes to the speaker who covers most of it. Only long sentences (6 words or more, with no speaker at 60% or more) are split, and 1-word fragments rejoin a neighbour.
- **Names, in this order:**
  1. **A remembered voice:** `voices.json`, mode 600, filled when you name a speaker with "Recognize this voice". A cosine similarity of 0.55 or more counts as a match; the same voice measured 0.66–0.88, different voices 0.32 or less.
  2. **The transcript, read by the summary model:** a name is kept only if the person introduces themselves or is addressed by it. The first name must occur in the transcript, and a calendar attendee's full name replaces it.
  3. **Otherwise "Speaker N":** click it to name the person, or merge two speakers that are one person.
- **End-to-end on PipeWire (2026-10-26):**
  - **Setup:** a virtual mic and a virtual output, Kennedy as "Me", pyannote's Diane/Sheila call as the others, and an ICS event "Chicago catch-up" with Diane Miller and Sheila Brown.
  - **Result:** both were found and named from the transcript with their calendar full names. The summary and the action item from my notes were right, and Ask was answered correctly with citations.
  - **Voice memory:** after "Recognize this voice", a second recording (with the new Record button) named both speakers by voice.
  - **Remaining error:** one very short reply ("Neither did I.") was merged into the other speaker. Speaker separation cannot resolve such quick exchanges.
- **Whisper copies the prompt's style:** with any vocabulary prompt it returns one long segment, and with a bare comma list ("Diane Miller, Sheila Brown") it drops sentence punctuation. Each term as its own sentence ("Diane Miller. Sheila Brown.") kept all 11 sentence ends, so lines no longer rely on Whisper's segments.
- **Follow-up email:** at first the local model added a deadline nobody said ("by tomorrow") and signed off as "Me". Marking tasks without a due date as "(no deadline given)" stopped the invented deadline; a trailing "Me"/"I" signature is removed.
- **Bugs found by driving the app with Playwright (system Chrome, scratch data):**
  - Record and Stop left their `{}` body unread, which broke the next request on the same connection (501). The server now reads every body first, per request.
  - A stray "null" appeared on the Action items page.

## Changes outside the UI

- `Tracker.start_manual()`; control commands `record-now`; `wirenote record --now`.
- The worker reads `my_notes.md` and the vocabulary; `note.render` gets a My notes section.
- Calendar attendees keep their e-mail addresses too (`attendee_emails`) for the follow-up `mailto:`.
