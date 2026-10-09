#!/usr/bin/env python3
"""Rebuild README.md from Jobbie's public internships feed.

Standard library only. Designed to be run by a scheduled GitHub Action:

* fetches https://api.jobbie.bot/public/internships.json with a descriptive
  User-Agent and, when a previous ETag is on disk, an If-None-Match header;
* on 304 Not Modified, a network error, a non-200 status, a malformed body or
  an empty list, it prints why and exits 0 WITHOUT touching README.md, so an
  upstream hiccup never fails the workflow or blanks the list;
* otherwise it renders the table and writes README.md only when the rendered
  text actually differs from what is on disk, and stores the new ETag.

Run it against a local fixture to check the rendering without the network:

    python3 -I scripts/update_readme.py --from fixtures/sample.json --readme /tmp/README.md
"""

from __future__ import annotations

import argparse
import re
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://api.jobbie.bot/public/internships.json"
REPO_URL = "https://github.com/jobbie-bot/Summer-2027-Tech-Internships"
# Images in table cells are addressed absolutely: GitHub renders the README
# in light and dark themes, and a black mark disappears on dark, so each image
# is a <picture> with a variant per theme.
ASSET_URL = "https://raw.githubusercontent.com/jobbie-bot/Summer-2027-Tech-Internships/main/assets/"
USER_AGENT = f"jobbie-internships-readme/1.0 (+{REPO_URL})"
JOBBIE_URL_PREFIX = "https://jobbie.bot/"
# The Apply button: sign up with the job remembered, so it is waiting in the
# new account's saved jobs (the app saves it once the account exists).
APPLY_URL = "https://jobbie.bot/register?job={id}&utm_source=github&utm_campaign=internships_list"
PUBLIC_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TIMEOUT_SECONDS = 30
# Bump whenever the filter or the rendered layout changes, so the next run
# re-renders even if the feed's ETag has not moved.
RENDER_VERSION = "6"

# The feed lists every US internship Jobbie can apply to; this README is the
# tech slice of it. A title that names a technical discipline outright
# (STRONG_TECH) is tech whatever else it says; one that only hints at it
# (TECH_HINT) is tech unless it also names a plainly non-technical field
# ("Social Media Analytics Intern" is a marketing intern). --all lists the
# whole feed instead.
STRONG_TECH = re.compile(
    r"software|engineer|developer|programm|data scien|data engineer|data analy|"
    r"machine learning|\bml\b|\bai\b|artificial intelligence|\bnlp\b|computer vision|"
    r"security|cyber|hardware|firmware|embedded|fpga|asic|silicon|\bsoc\b|\bcpu\b|\bgpu\b|"
    r"risc|chip|semiconductor|quant|devops|\bsre\b|site reliability|cloud|infrastructure|"
    r"backend|back-end|frontend|front-end|full[- ]?stack|\bios\b|android|web dev|"
    r"information technology|information systems|network|robotic|autonom|computer|"
    r"electrical|electronic|mechatronic|controls|signal|\brf\b|wireless|database|sql|python|java|"
    r"test engineer|validation engineer|verification|sales engineer|technical program|\btpm\b|"
    r"game dev|research scientist|statistic|simulation|\bgis\b|compiler|perception|desktop support|"
    r"web design|tableau|product manag|product design",
    re.I,
)
TECH_HINT = re.compile(
    r"\bdata\b|analytics|\bbi\b|business intelligence|platform|mobile|\bux\b|\bui\b|"
    r"product design|\bit\b|systems|technology|technical|\btech\b|modeling|\bqa\b|solutions|game",
    re.I,
)
NON_TECH = re.compile(
    r"marketing|communications|\bpr\b|public relations|brand|social media|content creat|"
    r"clinical|nurs|pharm|medical|patient|therap|counsel|social work|behavior|"
    r"accounting|audit|\btax\b|finance|financial|underwrit|actuar|claims?\b|"
    r"\bhr\b|human resources|recruit|talent|people ops|payroll|"
    r"legal|paralegal|compliance|contracts?\b|procurement|purchasing|"
    r"sales|account manage|customer success|customer service|retail|merchandis|"
    r"leasing|real estate|property|construction|facilities|"
    r"supply chain|logistics|warehouse|operations|project management|program management|"
    r"event|hospitality|culinary|food|tour|sustainability|environmental|"
    r"teach|education|curriculum|admissions|student affairs|"
    r"graphic design|video|photograph|journalis|editorial|copywrit|"
    r"strategy|consult|business development|business analyst|business operations|"
    r"skillbridge|insurance|banking|wealth|investment|equity research|investor|"
    r"chemistry|biology|material science|\blab\b|laboratory|protein|cell line|quality technician|metrology|"
    r"formulation|drug|genomic|hair care",
    re.I,
)


def is_tech(title: str) -> bool:
    if STRONG_TECH.search(title):
        return True
    return bool(TECH_HINT.search(title)) and not NON_TECH.search(title)

HEADER = """<p align="center">
  <a href="https://jobbie.bot">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="assets/jobbie-lockup-on-dark.png">
      <img src="assets/jobbie-lockup.png" alt="Jobbie" width="170">
    </picture>
  </a>
</p>

<h1 align="center">Summer 2027 &amp; Fall 2026 Tech Internships</h1>

<p align="center">
  An automatically refreshed list of <b>US tech internships and co-ops</b>, newest first.<br>
  Every row links to the real posting on the employer's own careers site.
</p>

<p align="center">
  <a href="https://jobbie.bot"><b>Apply to all of them in one click with Jobbie →</b></a>
</p>

## What is Jobbie?

[Jobbie](https://jobbie.bot) is an AI job-search agent. You tell it the roles, locations
and pay you want; it finds matching postings across Greenhouse, Lever, Workday, Ashby,
Workable and more, tailors your resume to each one, answers the application questions
and submits on the employer's own careers site. Interview invitations and offers land in
one inbox, and Autopilot keeps applying while you sleep.

- **Every link below is live.** The list is rebuilt from Jobbie's public feed every 6 hours;
  nothing is hand-edited and nothing stale stays up.
- **Tech roles only:** software, data, ML/AI, security, hardware, IT, quant and the like.
- **Apply with Jobbie:** the button in the last column creates your account with that job
  already saved, and Jobbie applies for you.
- Spotted a problem with a row? Open an issue and include the link.

"""

FOOTER = """
---

Last refreshed: **{refreshed}** · {count} postings · Powered by [Jobbie](https://jobbie.bot)

Want this data for your own project? The feed is public: `{url}`
(JSON, cached for 10 minutes, supports `ETag` / `If-None-Match`, rate-limited to
30 requests per minute per IP, please send a descriptive `User-Agent`).
"""

TABLE_HEAD = "| Company | Role | Location | Posted | Link | Apply with Jobbie |\n|---|---|---|---|---|:---:|\n"


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def fetch(url: str, etag: str | None) -> tuple[int, bytes, str | None]:
    """GET the feed. Returns (status, body, etag). Raises on transport errors."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.status, resp.read(), resp.headers.get("ETag")
    except urllib.error.HTTPError as err:
        # 304 is the happy "nothing changed" path; anything else is reported
        # by the caller as a skipped refresh.
        return err.code, b"", err.headers.get("ETag") if err.headers else None


def escape_cell(text: str) -> str:
    """Make arbitrary text safe inside a Markdown table cell."""
    text = " ".join(str(text or "").split())  # collapse whitespace/newlines
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")


def posted_date(value: str) -> str:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except (TypeError, ValueError):
        return ""


def clean_jobs(payload: dict, tech_only: bool = True) -> list[dict]:
    """Keep only well-formed rows that link to jobbie.bot (and, by default, are tech roles)."""
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("payload has no 'jobs' list")
    out = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        url = str(job.get("url", "")).strip()
        title = str(job.get("title", "")).strip()
        company = str(job.get("company", "")).strip()
        if not (url.startswith(JOBBIE_URL_PREFIX) and title and company):
            continue
        # The posting itself, on the employer's ATS. A feed built before it
        # carried apply_url falls back to the Jobbie page.
        apply_url = str(job.get("apply_url", "")).strip()
        job["_job_url"] = apply_url if apply_url.startswith("https://") else url
        # Where the Apply button goes: signup with the job remembered when the
        # feed names the job's public id, else the job's Jobbie page.
        job_id = str(job.get("id", "")).strip().lower()
        job["_apply_url"] = APPLY_URL.format(id=job_id) if PUBLIC_ID.match(job_id) else url
        if tech_only and not is_tech(title):
            continue
        out.append(job)
    return out


def render(payload: dict, jobs: list[dict], feed_url: str) -> str:
    rows = []
    for job in jobs:
        salary = str(job.get("salary", "")).strip()
        # Only a stated figure is worth a column inch; "see description" is not.
        if not any(ch.isdigit() for ch in salary):
            salary = ""
        role = escape_cell(job["title"])
        if salary:
            role += f" · {escape_cell(salary)}"
        apply_cell = (
            f'<a href="{job["_apply_url"]}"><picture>'
            f'<source media="(prefers-color-scheme: dark)" srcset="{ASSET_URL}jobbie-mark-on-dark.png">'
            f'<img src="{ASSET_URL}jobbie-mark.png" height="22" alt="Jobbie"></picture></a>'
            f'&nbsp;<a href="{job["_apply_url"]}"><picture>'
            f'<source media="(prefers-color-scheme: dark)" srcset="{ASSET_URL}apply-on-dark.svg">'
            f'<img src="{ASSET_URL}apply.svg" height="26" alt="Apply"></picture></a>'
        )
        rows.append(
            "| {company} | {role} | {location} | {posted} | [Job listing]({link}) | {apply} |".format(
                company=escape_cell(job["company"]),
                role=role,
                location=escape_cell(job.get("location", "")) or "—",
                posted=posted_date(job.get("posted_at", "")) or "—",
                link=job["_job_url"],
                apply=apply_cell,
            )
        )
    refreshed = payload.get("generated_at") or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return HEADER + TABLE_HEAD + "\n".join(rows) + "\n" + FOOTER.format(
        refreshed=refreshed, count=len(jobs), url=feed_url
    )


def read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL, help="feed URL (default: %(default)s)")
    ap.add_argument("--from", dest="fixture", help="read the payload from this JSON file instead of the network")
    ap.add_argument("--readme", default="README.md", help="file to (re)write (default: %(default)s)")
    ap.add_argument("--etag-file", default=".cache/etag", help="where the last ETag is stored (default: %(default)s)")
    ap.add_argument("--allow-empty", action="store_true", help="write the README even when the feed lists zero jobs")
    ap.add_argument("--all", action="store_true", help="list every internship in the feed, not only tech roles")
    args = ap.parse_args(argv)

    new_etag = None
    if args.fixture:
        with open(args.fixture, encoding="utf-8") as fh:
            body = fh.read().encode("utf-8")
    else:
        # The stored ETag is only worth sending when it came from this version
        # of the renderer: a filter or layout change must re-render even an
        # unchanged feed, so the file carries the version it was written by.
        stored = (read_text(args.etag_file) or "").strip().split(" ", 1)
        stored_etag = stored[1] if len(stored) == 2 and stored[0] == RENDER_VERSION else None
        try:
            status, body, new_etag = fetch(args.url, stored_etag)
        except (urllib.error.URLError, OSError, TimeoutError) as err:
            log(f"skip: could not reach {args.url}: {err}")
            return 0
        if status == 304:
            log("skip: feed unchanged (304 Not Modified)")
            return 0
        if status != 200:
            log(f"skip: feed answered HTTP {status}")
            return 0

    try:
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
        jobs = clean_jobs(payload, tech_only=not args.all)
    except (ValueError, UnicodeDecodeError) as err:
        log(f"skip: feed body is malformed: {err}")
        return 0

    if not jobs and not args.allow_empty:
        log("skip: feed listed no usable jobs; keeping the current README")
        return 0

    text = render(payload, jobs, args.url)
    if read_text(args.readme) == text:
        log(f"no change: {len(jobs)} postings, README already current")
    else:
        write_text(args.readme, text)
        log(f"wrote {args.readme}: {len(jobs)} postings")
    if new_etag:
        write_text(args.etag_file, RENDER_VERSION + " " + new_etag.strip() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
