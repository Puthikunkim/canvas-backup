#!/usr/bin/env python3
"""Back up your Canvas coursework (student account). See README.md for setup."""
import argparse
import csv
import getpass
import html
import json
import mimetypes
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests")

BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
FILE_LINK = re.compile(r"/files/(\d+)")
CAPPED = {"Course materials", "Discussions"}  # --max-file-mb never skips your submissions or feedback


def safe(name, limit=80):
    name = BAD_CHARS.sub("_", str(name or "untitled")).strip().rstrip(". ")
    name = name[:limit].rstrip(". ")
    return name or "untitled"


def lp(p):
    """Long-path-safe string (Windows MAX_PATH workaround)."""
    s = str(Path(p).resolve())
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        s = "\\\\?\\" + s
    return s


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def md(text):
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


class Restricted(Exception):
    """401/403/404: resource hidden, locked or not available to students."""


class AuthExpired(Exception):
    """Token or browser session no longer valid."""


def parse_json(r):
    text = r.text
    if text.startswith("while(1);"):  # Canvas prefix on cookie-authenticated API responses
        text = text[len("while(1);"):]
    return json.loads(text)


class Client:
    def __init__(self, base, token=None, cookie=None):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Accept"] = "application/json"
        # some CDN bot checks tie the session cookie to a browser user agent
        self.s.headers["User-Agent"] = os.environ.get("CANVAS_UA") or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
        if cookie:
            host = urlparse(self.base).hostname
            for part in cookie.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    # scoped to the Canvas host only, so it is not sent on file-storage redirects
                    self.s.cookies.set(k.strip(), v.strip(), domain=host)
        else:
            self.s.headers["Authorization"] = f"Bearer {token}"
        self.calls = 0

    def _req(self, url, params=None, stream=False):
        if not url.startswith("http"):
            url = self.base + url
        last = None
        for attempt in range(6):
            try:
                r = self.s.get(url, params=params, stream=stream, timeout=120)
            except requests.RequestException as e:
                last = e
                time.sleep(2 ** attempt)
                continue
            self.calls += 1
            if r.status_code == 403 and "rate limit" in r.text.lower():
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 401 and "unauthenticated" in r.text.lower():
                raise AuthExpired()
            if r.status_code in (403, 503) and (r.headers.get("cf-mitigated") or "just a moment" in r.text.lower()):
                raise AuthExpired()  # bot-check page instead of Canvas: needs a fresh browser cookie
            if r.status_code in (401, 403, 404):
                raise Restricted(f"HTTP {r.status_code}")
            r.raise_for_status()
            remaining = r.headers.get("X-Rate-Limit-Remaining")
            try:
                if remaining is not None and float(remaining) < 100:
                    time.sleep(1)
            except ValueError:
                pass
            return r
        raise RuntimeError(f"gave up after retries ({last}): {url}")

    def get(self, path, params=None):
        return parse_json(self._req(path, params))

    def paginate(self, path, params=None):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        url = path
        while url:
            r = self._req(url, params)
            data = parse_json(r)
            if isinstance(data, dict):  # e.g. {"quiz_submissions": [...]}
                data = next((v for v in data.values() if isinstance(v, list)), [])
            yield from data
            url = r.links.get("next", {}).get("url")
            params = None  # the next link already carries the query string

    def download(self, url, dest):
        r = self._req(url, stream=True)
        os.makedirs(lp(dest.parent), exist_ok=True)
        tmp = Path(str(dest) + ".part")
        with open(lp(tmp), "wb") as f:
            for chunk in r.iter_content(1 << 16):
                if chunk:
                    f.write(chunk)
        os.replace(lp(tmp), lp(dest))


class Backup:
    def __init__(self, client, out, estimate=False, max_bytes=0, materials=True, skip=()):
        self.c = client
        self.out = Path(out)
        self.estimate = estimate
        self.max_bytes = max_bytes
        self.materials = materials
        self.skip = [x.strip().lower() for x in skip if x.strip()]
        self.me = None
        self.seen_files = set()
        self.written = set()
        self.dir_owner = {}
        self.sizes = defaultdict(int)
        self.counts = defaultdict(int)
        self.largest = []
        self.notes = []
        self.skipped = []
        self.manual = []
        self.grades = []
        self.course_rows = []

    def note(self, where, msg):
        self.notes.append(f"{where}: {msg}")

    def udir(self, parent, name, key, limit=60):
        p = parent / safe(name, limit)
        owner = self.dir_owner.setdefault(str(p), key)
        if owner != key:
            p = parent / safe(f"{name} ({key})", limit + 12)
            self.dir_owner.setdefault(str(p), key)
        return p

    def save_text(self, path, text):
        if self.estimate:
            return
        os.makedirs(lp(path.parent), exist_ok=True)
        with open(lp(path), "w", encoding="utf-8") as f:
            f.write(text)

    def save_json(self, path, obj):
        self.save_text(path, json.dumps(obj, indent=2, ensure_ascii=False))

    def page_html(self, title, body, meta=()):
        meta_html = "".join(f"<li>{html.escape(str(m))}</li>" for m in meta if m)
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<base href='{self.c.base}/'><title>{html.escape(str(title))}</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;line-height:1.5}"
            ".meta{color:#666;font-size:.85em}.entry{border-left:3px solid #ddd;padding:.4rem .8rem;margin:.6rem 0}"
            ".mine{border-left-color:#2a7ae2;background:#f2f7ff}</style></head><body>"
            f"<h1>{html.escape(str(title))}</h1><ul class='meta'>{meta_html}</ul>{body or ''}</body></html>"
        )

    def get_file(self, cid, fid):
        for path in (f"/api/v1/courses/{cid}/files/{fid}", f"/api/v1/files/{fid}"):
            try:
                return self.c.get(path)
            except Restricted:
                continue
            except AuthExpired:
                raise
            except Exception as e:
                self.note(f"file {fid}", e)
                return None
        return None

    def linked_files(self, cid, body, dest, category):
        for fid in sorted(set(FILE_LINK.findall(body or ""))):
            if int(fid) in self.seen_files:
                continue
            f = self.get_file(cid, fid)
            if f:
                self.save_file(f, dest, category)

    def save_file(self, f, dest_dir, category, name=None):
        fid = f.get("id")
        if fid is not None:
            if fid in self.seen_files:
                return
            self.seen_files.add(fid)
        size = f.get("size") or 0
        fname = safe(name or f.get("display_name") or f.get("filename"), 120)
        dest = dest_dir / fname
        if str(dest) in self.written:
            dest = dest_dir / safe(f"{fid}_{fname}", 140)
        self.written.add(str(dest))
        rel = str(dest.relative_to(self.out))
        url = f.get("url")
        if not url:
            self.skipped.append((size, rel, "no download URL (locked or hidden)"))
            return
        if self.max_bytes and size > self.max_bytes and category in CAPPED:
            self.skipped.append((size, rel, "larger than --max-file-mb"))
            return
        self.sizes[category] += size
        self.counts[category] += 1
        self.largest.append((size, rel))
        if self.estimate:
            return
        try:
            if os.path.exists(lp(dest)) and (not size or os.path.getsize(lp(dest)) == size):
                return
            self.c.download(url, dest)
        except AuthExpired:
            raise
        except Exception as e:
            self.note(f"download {rel}", e)

    def run(self):
        self.me = self.c.get("/api/v1/users/self")
        print(f"Signed in as {self.me.get('name')}")
        courses = self.get_courses()
        print(f"{len(courses)} courses found\n")
        for i, co in enumerate(courses, 1):
            self.do_course(co, i, len(courses))
        print("\nPersonal Canvas files ...")
        self.section("my files", self.my_files)
        self.report()

    def get_courses(self):
        found = {}
        for state in ("active", "completed", "invited_or_pending"):
            try:
                for co in self.c.paginate(
                    "/api/v1/courses",
                    {"enrollment_state": state, "include[]": ["term", "total_scores"]},
                ):
                    found.setdefault(co["id"], co)
            except Restricted as e:
                self.note("course list", f"{state}: {e}")
        return sorted(found.values(), key=lambda c: (str(c.get("start_at") or c.get("created_at") or ""), c["id"]))

    def section(self, label, fn, *args):
        try:
            fn(*args)
        except Restricted as e:
            self.note(label, f"not available to students ({e})")
        except AuthExpired:
            raise
        except Exception as e:
            self.note(label, f"error: {e}")

    def do_course(self, co, i, n):
        cid = co["id"]
        if co.get("access_restricted_by_date") or not co.get("name"):
            self.course_rows.append({"course_id": cid, "term": "", "code": "", "name": "(access restricted by date)",
                                     "final_score": "", "final_grade": ""})
            self.note(f"course {cid}", "access restricted by date; only submissions in personal files can be saved")
            print(f"[{i}/{n}] course {cid}: access restricted, skipped")
            return
        term = (co.get("term") or {}).get("name") or ""
        code = co.get("course_code") or co.get("name")
        label = f"{term} - {code}" if term and "default" not in term.lower() else code
        hay = f"{label} {co.get('name')}".lower()
        if any(x in hay for x in self.skip):
            print(f"[{i}/{n}] {label}: skipped (--skip-course)")
            return
        cdir = self.udir(self.out, label, cid)
        before = sum(self.sizes.values())
        print(f"[{i}/{n}] {label}")

        enr = next((e for e in co.get("enrollments") or [] if "Student" in str(e.get("type", "")).title()), {})
        self.course_rows.append({
            "course_id": cid, "term": term, "code": code, "name": co.get("name"),
            "final_score": enr.get("computed_final_score", ""), "final_grade": enr.get("computed_final_grade", ""),
        })

        self.section(f"{code} assignments", self.assignments, co, cdir, label)
        self.section(f"{code} quizzes", self.quizzes, co, cdir)
        self.section(f"{code} discussions", self.discussions, co, cdir)
        self.section(f"{code} announcements", self.announcements, co, cdir)
        if self.materials:
            self.section(f"{code} modules", self.modules, co, cdir)
            self.section(f"{code} pages", self.pages, co, cdir)
            self.section(f"{code} files", self.course_files, co, cdir)
        print(f"        {human(sum(self.sizes.values()) - before)}")

    def assignments(self, co, cdir, label):
        cid = co["id"]
        for a in self.c.paginate(f"/api/v1/courses/{cid}/assignments"):
            adir = self.udir(cdir / "assignments", a.get("name"), a["id"])
            desc = a.get("description") or ""
            self.save_text(adir / "brief.html", self.page_html(a.get("name"), desc, [
                f"Due: {a.get('due_at')}", f"Points possible: {a.get('points_possible')}",
                f"Submission types: {', '.join(a.get('submission_types') or [])}", f"Canvas link: {a.get('html_url')}",
            ]))
            self.linked_files(cid, desc, adir / "brief_files", "Assignment briefs")

            tool_url = str((a.get("external_tool_tag_attributes") or {}).get("url") or "")
            if "external_tool" in (a.get("submission_types") or []) and "quiz-lti" not in tool_url:
                self.manual.append((label, a.get("name"), tool_url, a.get("html_url")))

            try:
                sub = self.c.get(
                    f"/api/v1/courses/{cid}/assignments/{a['id']}/submissions/self",
                    {"include[]": ["submission_history", "submission_comments", "rubric_assessment"]},
                )
            except Restricted:
                sub = None
            self.save_json(adir / "metadata.json", {"assignment": a, "submission": sub})
            if not sub:
                continue
            self.submission_files(sub, adir)
            self.feedback(a, sub, adir)
            self.grades.append({
                "course": label, "assignment": a.get("name"), "due_at": a.get("due_at"),
                "submitted_at": sub.get("submitted_at"), "score": sub.get("score"),
                "points_possible": a.get("points_possible"), "grade": sub.get("grade"),
                "late": sub.get("late"), "missing": sub.get("missing"),
                "state": sub.get("workflow_state"), "link": a.get("html_url"),
            })

    def submission_files(self, sub, adir):
        history = sub.get("submission_history") or [sub]
        for h in history:
            if not (h.get("attachments") or h.get("body") or h.get("url")):
                continue
            sdir = adir / "submission" / f"attempt_{h.get('attempt') or 1}"
            for att in h.get("attachments") or []:
                self.save_file(att, sdir, "Submissions")
            if h.get("body"):
                self.save_text(sdir / "text_entry.html",
                               self.page_html("Text entry", h["body"], [f"Submitted: {h.get('submitted_at')}"]))
            if h.get("url"):
                self.save_text(sdir / "url.txt", h["url"] + "\n")

    def feedback(self, a, sub, adir):
        lines = [
            f"# {a.get('name')}", "",
            f"- Score: {sub.get('score')} / {a.get('points_possible')}",
            f"- Grade: {sub.get('grade')}",
            f"- Submitted: {sub.get('submitted_at')}  (late: {sub.get('late')}, missing: {sub.get('missing')})",
            f"- Graded: {sub.get('graded_at')}",
        ]
        rubric = a.get("rubric") or []
        ra = sub.get("rubric_assessment") or {}
        if rubric:
            lines += ["", "## Rubric", "", "| Criterion | Points | Max | Rating | Comment |", "|---|---|---|---|---|"]
            for crit in rubric:
                r = ra.get(crit.get("id"), {}) or {}
                rating = next((x.get("description") for x in crit.get("ratings") or []
                               if x.get("id") == r.get("rating_id")), "")
                lines.append(f"| {md(crit.get('description'))} | {md(r.get('points'))} | {md(crit.get('points'))} "
                             f"| {md(rating)} | {md(r.get('comments'))} |")
        comments = sub.get("submission_comments") or []
        if comments:
            lines += ["", "## Comments", ""]
            for cm in comments:
                lines += [f"**{cm.get('author_name')}** ({cm.get('created_at')})", "", cm.get("comment") or "", ""]
                for att in cm.get("attachments") or []:
                    self.save_file(att, adir / "feedback_files", "Feedback")
                media = cm.get("media_comment") or {}
                if media.get("url"):
                    ext = mimetypes.guess_extension(media.get("content-type") or "") or ".media"
                    self.save_file({"id": f"media_{media.get('media_id')}", "url": media["url"], "size": 0},
                                   adir / "feedback_files", "Feedback",
                                   name=f"media_comment_{media.get('media_id')}{ext}")
        if sub.get("score") is not None or rubric or comments:
            self.save_text(adir / "feedback.md", "\n".join(lines) + "\n")

    def quizzes(self, co, cdir):
        cid = co["id"]
        for q in self.c.paginate(f"/api/v1/courses/{cid}/quizzes"):
            qdir = self.udir(cdir / "quizzes", q.get("title"), q["id"])
            try:
                subs = list(self.c.paginate(f"/api/v1/courses/{cid}/quizzes/{q['id']}/submissions"))
            except Restricted:
                subs = []
            meta = [f"Points possible: {q.get('points_possible')}", f"Due: {q.get('due_at')}"]
            for s in subs:
                meta.append(f"Attempt {s.get('attempt')}: score {s.get('score')} "
                            f"(kept {s.get('kept_score')}), finished {s.get('finished_at')}")
            self.save_text(qdir / "quiz.html", self.page_html(q.get("title"), q.get("description") or "", meta))
            self.save_json(qdir / "metadata.json", {"quiz": q, "submissions": subs})
            self.linked_files(cid, q.get("description"), qdir / "files", "Course materials")

    def discussions(self, co, cdir):
        cid = co["id"]
        for t in self.c.paginate(f"/api/v1/courses/{cid}/discussion_topics"):
            tdir = self.udir(cdir / "discussions", t.get("title"), t["id"])
            for att in t.get("attachments") or []:
                self.save_file(att, tdir / "files", "Discussions")
            try:
                view = self.c.get(f"/api/v1/courses/{cid}/discussion_topics/{t['id']}/view")
            except Restricted:
                view = {}
            names = {p.get("id"): p.get("display_name") for p in view.get("participants") or []}
            body = (t.get("message") or "") + "<hr>" + "".join(self.render_entries(view.get("view") or [], names, tdir))
            self.save_text(tdir / "thread.html", self.page_html(t.get("title"), body, [
                f"Posted: {t.get('posted_at')}", f"By: {t.get('user_name') or (t.get('author') or {}).get('display_name')}",
            ]))

    def render_entries(self, entries, names, tdir, depth=0):
        out = []
        for e in entries:
            if e.get("deleted"):
                continue
            uid = e.get("user_id")
            cls = "entry mine" if self.me and uid == self.me.get("id") else "entry"
            out.append(f"<div class='{cls}' style='margin-left:{depth * 24}px'><div class='meta'>"
                       f"{html.escape(str(names.get(uid, 'Unknown')))} · {e.get('created_at', '')}</div>"
                       f"{e.get('message') or ''}</div>")
            atts = e.get("attachments") or ([e["attachment"]] if e.get("attachment") else [])
            for att in atts:
                self.save_file(att, tdir / "files", "Discussions")
            out += self.render_entries(e.get("replies") or [], names, tdir, depth + 1)
        return out

    def announcements(self, co, cdir):
        cid = co["id"]
        items = list(self.c.paginate(f"/api/v1/courses/{cid}/discussion_topics", {"only_announcements": "true"}))
        if not items:
            return
        items.sort(key=lambda t: str(t.get("posted_at") or ""))
        body = "".join(
            f"<div class='entry'><h3>{html.escape(str(t.get('title')))}</h3>"
            f"<div class='meta'>{t.get('posted_at')} · {html.escape(str(t.get('user_name') or ''))}</div>"
            f"{t.get('message') or ''}</div>" for t in items)
        self.save_text(cdir / "announcements.html", self.page_html("Announcements", body))
        for t in items:
            for att in t.get("attachments") or []:
                self.save_file(att, cdir / "announcement_files", "Course materials")

    def modules(self, co, cdir):
        cid = co["id"]
        for m in self.c.paginate(f"/api/v1/courses/{cid}/modules", {"include[]": ["items"]}):
            mdir = self.udir(cdir / "modules", f"{m.get('position', 0):02d} {m.get('name')}", m["id"])
            items = m.get("items")
            if items is None and m.get("items_url"):
                items = list(self.c.paginate(m["items_url"]))
            index = [f"# {m.get('name')}", ""]
            for it in items or []:
                kind, title = it.get("type"), it.get("title")
                if kind == "File":
                    f = self.get_file(cid, it.get("content_id"))
                    if f:
                        self.save_file(f, mdir, "Course materials")
                    index.append(f"- File: {title}")
                elif kind in ("ExternalUrl", "ExternalTool"):
                    index.append(f"- Link: [{title}]({it.get('external_url') or it.get('html_url')})")
                elif kind == "SubHeader":
                    index.append(f"\n## {title}\n")
                else:
                    index.append(f"- {kind}: {title} ({it.get('html_url')})")
            self.save_text(mdir / "_index.md", "\n".join(index) + "\n")

    def pages(self, co, cdir):
        cid = co["id"]
        for p in self.c.paginate(f"/api/v1/courses/{cid}/pages"):
            try:
                full = self.c.get(f"/api/v1/courses/{cid}/pages/{p['url']}")
            except Restricted:
                continue
            body = full.get("body") or ""
            self.save_text(cdir / "pages" / f"{safe(p.get('title'), 80)}.html",
                           self.page_html(p.get("title"), body, [f"Updated: {p.get('updated_at')}"]))
            self.linked_files(cid, body, cdir / "pages" / "_files", "Course materials")

    def course_files(self, co, cdir):
        cid = co["id"]
        folders = {f["id"]: f.get("full_name", "") for f in self.c.paginate(f"/api/v1/courses/{cid}/folders")}
        for f in self.c.paginate(f"/api/v1/courses/{cid}/files"):
            d = cdir / "files"
            full = folders.get(f.get("folder_id"), "")
            for part in full.split("/")[1:]:
                d = d / safe(part, 60)
            self.save_file(f, d, "Course materials")

    def my_files(self):
        folders = {f["id"]: f.get("full_name", "") for f in self.c.paginate("/api/v1/users/self/folders")}
        for f in self.c.paginate("/api/v1/users/self/files"):
            d = self.out / "_my_canvas_files"
            for part in folders.get(f.get("folder_id"), "").split("/")[1:]:
                d = d / safe(part, 60)
            self.save_file(f, d, "Personal files")

    def report(self):
        total = sum(self.sizes.values())
        lines = ["Canvas backup " + ("ESTIMATE (nothing downloaded)" if self.estimate else "summary"), ""]
        for cat in sorted(self.sizes, key=lambda k: -self.sizes[k]):
            lines.append(f"  {cat:<20} {self.counts[cat]:>6} files  {human(self.sizes[cat]):>10}")
        lines += [f"  {'TOTAL':<20} {sum(self.counts.values()):>6} files  {human(total):>10}", ""]
        lines.append("Largest files:")
        for size, rel in sorted(self.largest, reverse=True)[:15]:
            lines.append(f"  {human(size):>10}  {rel}")
        lines += ["", f"Skipped files: {len(self.skipped)} (see skipped.csv)",
                  f"External-tool assignments to save by hand (e.g. Turnitin): {len(self.manual)} (see MANUAL_DOWNLOADS.txt)",
                  f"Notes/errors: {len(self.notes)} (see notes.txt)", f"API calls: {self.c.calls}"]
        text = "\n".join(lines)
        print("\n" + text)
        if self.estimate:
            for m in self.manual:
                print(f"  manual: {m[0]} / {m[1]}  [{urlparse(m[2]).hostname or 'unknown tool'}]")
            return
        self.save_text(self.out / "SUMMARY.txt", text + "\n")
        self.save_text(self.out / "notes.txt", "\n".join(self.notes) + "\n")
        self.save_text(self.out / "MANUAL_DOWNLOADS.txt", "".join(
            f"{c} | {a}\n  tool: {t}\n  open: {u}\n\n" for c, a, t, u in self.manual) or "None\n")
        for name, rows, cols in (
            ("grades.csv", self.grades, ["course", "assignment", "due_at", "submitted_at", "score", "points_possible",
                                         "grade", "late", "missing", "state", "link"]),
            ("courses.csv", self.course_rows, ["course_id", "term", "code", "name", "final_score", "final_grade"]),
        ):
            with open(lp(self.out / name), "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                w.writerows(rows)
        with open(lp(self.out / "skipped.csv"), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["size_bytes", "path", "reason"])
            w.writerows(self.skipped)
        print(f"\nSaved to {self.out.resolve()}")


def main():
    ap = argparse.ArgumentParser(description="Back up your Canvas coursework.")
    ap.add_argument("--base-url", default=os.environ.get("CANVAS_URL", ""),
                    help="your Canvas address, e.g. https://canvas.example.edu (asked for if not given)")
    ap.add_argument("--out", default="canvas_backup", help="output folder (default: canvas_backup)")
    ap.add_argument("--estimate", action="store_true", help="report sizes only, download nothing")
    ap.add_argument("--max-file-mb", type=float, default=0,
                    help="skip course-material files larger than this; your own work is never skipped")
    ap.add_argument("--no-course-materials", action="store_true",
                    help="skip modules, course files and pages")
    ap.add_argument("--skip-course", default="",
                    help='comma-separated text; courses whose name contains any of it are skipped, e.g. "MATH101,PHYS"')
    args = ap.parse_args()

    base = args.base_url.strip() or input("Canvas URL (e.g. https://canvas.example.edu): ").strip()
    if not base:
        sys.exit("No Canvas URL given.")
    if not base.startswith("http"):
        base = "https://" + base
    base = base.rstrip("/")

    token = os.environ.get("CANVAS_TOKEN", "").strip()
    cookie = os.environ.get("CANVAS_COOKIE", "").strip()
    if not token and not cookie:
        secret = getpass.getpass("Paste your Canvas Cookie header (or access token), then Enter: ").strip()
        if secret.lower().startswith("cookie:"):
            secret = secret[7:].strip()
        if "=" in secret:
            cookie = secret
        else:
            token = secret
    if not token and not cookie:
        sys.exit("Nothing pasted.")
    client = Client(base, token=token or None, cookie=cookie or None)
    backup = Backup(client, args.out, estimate=args.estimate,
                    max_bytes=int(args.max_file_mb * 1024 * 1024), materials=not args.no_course_materials, skip=args.skip_course.split(","))
    if not args.estimate:
        os.makedirs(lp(backup.out), exist_ok=True)
    try:
        backup.run()
    except (Restricted, AuthExpired):
        if backup.me and not args.estimate:
            backup.report()
        sys.exit("\nCanvas rejected the login (expired or incomplete). Log in to Canvas again, copy a fresh "
                 "Cookie value and re-run the same command; finished files are kept.")
    except KeyboardInterrupt:
        print("\nStopped. Re-run the same command to resume.")


if __name__ == "__main__":
    main()
