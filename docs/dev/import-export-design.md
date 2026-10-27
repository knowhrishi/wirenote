# Design note: import and export

Status: **chosen 2026-10-27**. Notes came only from calls Wirenote recorded, and stayed in Wirenote and its Markdown files. This note adds a way in for audio recorded elsewhere, and two ways out for sharing a note.

## Import

1. **`wirenote import FILE`**, or **Import** in the meetings app (button, or drop a file on the window).
   - ffmpeg converts any audio or video file it can read to the format pw-record writes: 16 kHz mono 16-bit WAV. The converted file becomes the recording's **system track**, so every voice in it is told apart as Speaker 1, 2, … and none is taken for "Me".
   - The recording gets the id `YYYYmmdd-HHMMSS-import`, with `-2`, `-3`, … if one with that time exists. The time is `--date`, else the file's modification time (in the app: the time the browser reports for the file).
   - The manifest has `app: "import"`, the file's name as `name`, `end_reason: "imported"`, and `title_override` when `--title` is given. Without a title, the note is named like a manual recording: from the summary, else the file's name.
   - A job is queued and the daemon is told to start it now (control command `kick-jobs`) instead of at its next 30 s check. Without a running daemon the job waits, as any job does.
2. **The original file is never changed or kept.** The CLI reads it in place. The app streams the upload to a private temporary folder (`.import-*`, mode 700) inside the recordings folder, so a large file never sits in memory, and deletes it once ffmpeg is done.
3. **Limits.** Files up to 4 GB, and the first 4 h of audio, the same cap as a recording. ffmpeg gets 30 min to convert. A file without audio fails with "not an audio or video file", and nothing is left behind. Error messages never contain paths.
4. **Who can import.** The upload is a write: it needs the app's session cookie and token like every other change. Without them the body is not read.

## Export

1. **Word (.docx):** "Download as Word (.docx)" in the call page's menu, `GET /api/calls/ID/docx`, or `wirenote export ID [-o FILE_OR_FOLDER]`.
   - Built from the same data the call page shows (`Store.call`): title, date, length, app and room, who was there, the summary, decisions, action items with their ticks, open questions, your notes, and the transcript with names and times. Empty sections are left out.
   - Written with the standard library: a .docx is a zip of a few XML parts. No new dependency. Characters XML does not allow are dropped.
   - The file is named `YYYY-MM-DD HHMM Title.docx`, without characters that are not allowed in file names.
2. **PDF:** "Print or save as PDF" opens the browser's print dialog on a print view of the call: notes and transcript, without the sidebar, buttons or audio player. The browser's "Save as PDF" makes the file, so Wirenote needs no PDF library.

## What stays

- Nothing leaves the computer: import and export happen in the local app or on the command line, and the exported file goes where you save it.
- Recorded calls and their notes are unchanged.
- An imported call is processed like a recorded one: transcribed, speakers told apart, summarized, and written as a Markdown note.

## Defaults (docs/dev/PLAN.md › Defaults)

| Parameter | Value |
| --- | --- |
| Import size | up to 4 GB |
| Import length | the first 4 h, the recording cap |
| Import conversion | ffmpeg, at most 30 min |
