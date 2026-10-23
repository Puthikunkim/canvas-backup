# canvas-backup

Back up your coursework from Canvas LMS before you lose access. You run one Python script with your own student login. You don't need admin access or an API key from your school.

## What it saves

For every course you can see, both current and concluded:

- Your submissions, including every attempt (files, text entries, URLs)
- Grades, rubric scores, instructor comments and feedback files
- Assignment briefs and the files linked from them
- Module files, course files and pages
- Discussions (full threads, with your posts highlighted) and announcements
- Classic quiz scores and attempts
- Your personal Canvas files

At the top of the output folder it also writes `grades.csv`, `courses.csv` and `SUMMARY.txt`.

## Usage

Requires Python 3.8+.

```
pip install -r requirements.txt
python canvas_backup.py --estimate   # size check only, downloads nothing
python canvas_backup.py              # full backup into ./canvas_backup
```

The script asks for your Canvas URL, for example `https://canvas.example.edu`. It then asks for **one** of the following:

- **Browser cookie.** This works even when your school has disabled access tokens.
  1. Log in to Canvas in your browser.
  2. Press F12 to open developer tools, then open the **Network** tab and reload the page.
  3. Click the first request. Under **Request Headers**, copy the whole **Cookie** value.
- **Access token**, if your school allows tokens. In Canvas, go to **Account > Settings > Approved Integrations > + New Access Token**.

To skip the prompts, set the environment variables `CANVAS_URL` and `CANVAS_TOKEN` (or `CANVAS_COOKIE`).

Your cookie or token works like your password. Don't share it with anyone. When the backup is done, log out of Canvas so the cookie stops working.

## Options

| Option | Description |
|---|---|
| `--base-url URL` | Your Canvas URL |
| `--out DIR` | Output folder (default: `canvas_backup`) |
| `--estimate` | Show the backup size without downloading anything |
| `--max-file-mb N` | Skip course-material files larger than N MB. Your own work is always saved. |
| `--skip-course "A,B"` | Skip courses whose name contains any of these words |
| `--no-course-materials` | Skip modules, course files and pages |

## Resuming

It is safe to run the script again. It skips files that are already downloaded. If your session expires during a run, log in to Canvas again, copy a new cookie and run the same command. The backup continues where it stopped.

Some assignments use external tools such as Turnitin, so the script can't download them. It lists these in `MANUAL_DOWNLOADS.txt` for you to save by hand.

## License

[MIT](LICENSE)
