<p align="center"><img src="logo.jpg" width="420" alt="Higgins"></p>

# Higgins

Write pages and posts in Markdown, locally. One script publishes them to WordPress
over SSH + WP-CLI. A second script generates a cover image for every post with a
free image model and uploads it as the featured image.

No plugin to install on the site, no WordPress REST API key, no exposed database:
just the SSH key you probably already have.

## Why

The usual flow — wp-admin, block editor, uploading images by hand — doesn't lend
itself to automation or to being versioned. This repo treats content as code: one
file per page/post, a history in git, a repeatable publishing step with no
surprises (`--dry-run` before every real upload).

It's meant to be used together with [Claude Code](https://claude.com/claude-code)
and a project `CLAUDE.md` describing the site's tone, the editorial rules and what
must never be invented — this repo stays the publishing layer, `CLAUDE.md` stays
the judgement layer. It isn't a requirement: the scripts work just as well if you
write the `.md` files by hand.

## What you need

- An SSH key that gets you onto the server as a user allowed to run `wp` (that one
  command alone is enough, via sudoers — see "Security" below)
- [WP-CLI](https://wp-cli.org/#installing) installed **on the server**
- Python 3.10+ locally, with:
  ```
  pip install markdown pyyaml Pillow
  ```

Windows, macOS, Linux: it works anywhere there's an SSH client (on Windows,
OpenSSH has shipped with the system for years) and Python.

## Setting up SSH access

`config.yml` has an `ssh:` field that can be a plain `user@host`, or — better,
because it doesn't tie the project to a hand-written user or IP — an **alias**
defined in your SSH client's configuration file. If you've never touched it,
here's how:

**1. A key, if you don't already have one.** On any system:
```bash
ssh-keygen -t ed25519 -C "your-name"
```
Pressing Enter at every question is fine (default path, no passphrase if you'd
rather not type one every time). It creates two files in `~/.ssh/`: `id_ed25519`
(private, it never leaves) and `id_ed25519.pub` (public, the one that goes on the
server).

**2. The public key on the server.** From Linux/macOS:
```bash
ssh-copy-id -p 22 user@yourserver.com
```
From Windows (PowerShell, if you don't have `ssh-copy-id`):
```powershell
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh user@yourserver.com "cat >> ~/.ssh/authorized_keys"
```
It will ask for the user's password **one last time** — after that, access is
key-only.

**3. The alias, in the `config` file (no extension) inside `~/.ssh/`.** On Windows
that's `C:\Users\<you>\.ssh\config`; if the folder or the file don't exist, create
them (any text editor will do, as long as it doesn't append `.txt`). One block per
server:

```
Host my-vps
    HostName yourserver.com
    User user
    Port 22
    IdentityFile ~/.ssh/id_ed25519
```

`Host` is a name you make up — it's what you then put in `config.yml` under
`ssh:`. `Port` is only needed if the server doesn't use the default 22; otherwise
leave it out.

**4. Check it.** `ssh my-vps` must connect you **without asking for a password**.
If that works, `config.yml` can simply say `ssh: my-vps`, and every command the
script runs will automatically use that key, that user, that port — with nothing
to repeat in `config.yml` or anywhere else.

One alias per server is also handy because the same `~/.ssh/config` file can hold
several `Host` blocks, one for each site you manage with a separate copy of this
project.

## Quick start

```bash
git clone https://github.com/simonecosci/higgins.git
cd higgins
pip install markdown pyyaml Pillow

cp config.example.yml config.yml
# edit config.yml: ssh alias, WordPress path on the server, author

ssh <your-alias> wp --info --path=<wordpress-path>   # check that WP-CLI answers

python publish.py --dry-run          # touches nothing, only shows what it would do
```

If the dry-run finds the sample `.md` files, all is well: it shows what would
happen, and nothing is written to the site until you run the same command without
`--dry-run`.

## Layout

```
higgins/
├── content/               # one .md per page/post, file name = slug
├── images/                # generated covers, <slug>.png (created automatically)
├── publish.py             # publishes: reads content/, writes to WordPress over SSH
├── featured.py            # generates a post's cover, called by publish.py
├── config.yml             # server, path, author — must NOT go in git (real data)
├── config.example.yml     # the template to copy
├── .env                   # image provider keys — must NOT go in git
├── .env.example           # the template to copy
├── editorial-plan.md      # what to write, what has gone out — not in git (see its .example)
├── topics.md              # the queue the automation empties — not in git (see its .example)
└── CONTENT-FORMAT.md      # reference for the content format
```

`config.yml`, `.env` and the logs stay out of git on purpose (see `.gitignore`):
the first points at a real server, the second holds credentials. Copy the two
`.example` files and fill them in with your own values.

## The content format

A file in `content/<slug>.md`, YAML front-matter plus a Markdown body:

```markdown
---
title: Business process automation
slug: business-process-automation
type: page                   # post | page | jetpack-portfolio
excerpt: A one-line summary — the meta description if you don't use an SEO plugin,
         or if the SEO plugin doesn't already have one set for this content.
categories: [12]              # numeric IDs: wp term list category --fields=term_id,name
tags: [automation, php]       # free-form names, created if they don't exist
parent: 12                    # optional, ID of the parent page
author: 1                     # optional, WP user ID — otherwise the one in config.yml
image_prompt: a sheet of paper on the left, three lines to a circle on the right
---

## Section heading

Plain Markdown text. It becomes Gutenberg block HTML, compatible with the native
WordPress editor.
```

Full reference, with a minimal example ready to copy: `CONTENT-FORMAT.md`.

Required fields: `title`, `slug`. Everything else is optional. `type` defaults to
`post`. `jetpack-portfolio` is only needed if the site uses Jetpack's projects
custom post type — ignore it otherwise.

## Publishing

```bash
python publish.py --dry-run                     # what it would do, no changes
python publish.py content/page.md               # publishes that file online
python publish.py content/page.md --draft       # uploads it as a draft
python publish.py content/page.md --no-image    # without generating the cover
python publish.py --cover-only content/x.md     # only the featured image, the text stays as it is
python publish.py                               # every .md in content/, with no arguments
```

Idempotent on the slug: if content with that slug already exists it is updated in
place; otherwise it's created. Running the same command twice duplicates nothing.

**Careful**: publishing updates existing content *in full* — it replaces the text
online with the one in the file. There is no automatic backup: if the file is
incomplete, whatever is online today is lost. Always use `--dry-run` first.

A new post headed for `publish` is created as a draft first, then given its cover,
and only made public at the end — that way a social auto-sharing plugin (Blog2Social
and friends typically hook into the `transition_post_status` event) already finds
the image when it builds the preview. Content that is already online, on the other
hand, is updated in place without that step: putting it back into draft for a
moment would take it off the site and could fire a second share for a mere update.

## Cover images

Every post (`type: post`) created from here gets its own cover image, generated by
`featured.py` and uploaded automatically by `publish.py`. The prompt is built from
the title, excerpt and tags — or you write it yourself with `image_prompt:` in the
front-matter. With `image: false` in the front-matter, no cover for that content.

```bash
python featured.py --providers                  # which providers are configured
python featured.py content/x.md --prompt-only   # show the prompt, without calling the API
python featured.py content/x.md --force         # regenerate the cover from scratch
```

Supported providers, in the order they're tried — the first one found configured in
`.env` or in the environment wins:

| Provider | Variables | Notes |
|---|---|---|
| Hugging Face | `HF_TOKEN` | FLUX.1-schnell, free, no credit card |
| Cloudflare Workers AI | `CF_ACCOUNT_ID` + `CF_API_TOKEN` | same model, 10,000 free neurons/day |
| Google Gemini | `GEMINI_API_KEY` | `gemini-2.5-flash-image`, requires billing enabled on the AI Studio project |
| Pollinations | no key | only with `--provider pollinations`: the anonymous tier serves a different model and the output tends to be unusable |

**With no provider configured, no fallback is invented**: `featured.py` says so
plainly, and `publish.py` publishes the text anyway, without an image.

Covers are not regenerated on every publish (two uploads of the same file give the
same image — use `--force` to redo it), and **a cover already present on a piece of
content is never overwritten**: if one is there, a person put it there by hand.

The visual style is a constant at the top of `featured.py` (`STYLE`): change it
there for all future covers, so they stay a coherent family instead of a set of
unrelated images.

A couple of things learned the hard way, so you don't have to rediscover them:

- **Short prompts.** With 4 inference steps (FLUX-schnell) a long prompt produces
  crowded compositions; a two-line one gives clean results.
- **Never use negations in the prompt.** "no lightbulbs, no faces" tends to make
  those elements *more* likely, not less. Describe only what you want to see.
- `image_prompt` works best in English and concrete ("a sheet of paper on the
  left, three lines to a circle on the right") — a title alone almost always
  produces generic illustrations.

## The editorial plan (optional)

Deciding what to write is the part no script does for you, and it's the part that gets
lost between sessions. `editorial-plan.md.example` is a template for keeping it in
one file: copy it to `editorial-plan.md` (it's already in `.gitignore` — it's about
your site, not about this tool) and read it at the start of a session to decide what
goes out today.

It has three sections that carry the weight, plus two optional ones:

- **To write** — ideas, with the angle, the language and *what is still missing*. An
  idea that needs a number, a client name or a decision only you can make isn't ready:
  writing that down is what stops it from being written half-invented later.
- **Written** — date, WordPress ID, category and where the facts came from. It's what
  keeps the same topic from going out twice, and what makes a year-old post traceable.
- **Dropped** — the ideas that didn't hold up and why, so they don't come back around
  in six months.
- **Interactive session only** and **Open technical notes** — optional: rewrites that
  must not go out unattended, and problems already solved once.

Used together with Claude Code, this file is the difference between "write me a post"
and a blog with a memory. The daily automation reads a different, shorter file — see
below.

## Automation (optional)

`daily.cmd.example` and `weekly.cmd.example` show how to run Claude Code in
non-interactive mode (`claude -p`) to write and publish one piece of content per
day from a queue, with file logging and automatic cleanup.

**The cycle feeds itself.** The queue lives in its own Markdown file (`topics.md`,
template in `topics.md.example`, also out of git), separate from the editorial plan
above: that one is where you think, this one is what the automation consumes. It has
three sections: Queue, Published, Skipped. Every day `daily.cmd` takes the first
entry, writes it, publishes it and moves it to Published. **If the queue is empty, it
regenerates a new one on its own before writing that day's post** — new topics, never
a duplicate of what is already in Published, in Skipped or live on the site (the
script checks by querying WordPress). You never have to fill it by hand for the
cycle to keep running;
`weekly.cmd` is only an optional top-up that brings it back to N entries without
touching the ones already there.

The rule that holds it all together: if a topic would need a fact the script
doesn't have (a technical detail, a number, something verifiable), that entry goes
to Skipped with the reason, and it moves on to the next — **nothing is invented to
make up the numbers**. A shorter queue beats one full of weak topics.

Copy the two `.cmd.example` files without that extension, replace the `<...>`
placeholders and schedule them with your system's scheduler (`schtasks` on
Windows, `cron` elsewhere). Copy `topics.md.example` to `topics.md` too, and put a
couple of real topics in the Queue section: the first run takes the first one.

### Drafts instead of publishing directly

If you'd rather review every piece before it goes online, you can run the
automation in draft mode: just add `--draft` to the `python publish.py` command
inside the `daily.cmd` prompt (`publish.py` already supports it — see "Publishing"
above). The post is created as a draft, **with its cover already generated and
set**, ready to be read through and published by hand from wp-admin.

One detail to watch out for if you adopt it: the duplicate check in the prompt
queries `wp post list` without specifying a status, which by default shows only
published content — a draft wouldn't show up. When switching to `--draft` it's
worth adding `--post_status=publish,draft` to that check (in both `daily.cmd` and
`weekly.cmd`), otherwise the queue risks proposing again the very topic of a draft
that has been written but not yet published.

These two stay out of git as well (they end up containing your real domain and
paths), along with `config.yml` and `.env`. On the content of the prompt: the real
version I use is more detailed than these examples — it includes, for instance, the
rules for verifying every fact against a primary source before writing about a CVE,
with `WebSearch`/`WebFetch` among the `allowedTools`. That level of detail is worth
writing into the prompt itself, not just into the project `CLAUDE.md`: in `-p` mode,
with nobody at a terminal to confirm anything, the routine has to stand on its own.

## Security

- **No credentials in this repo.** Server access goes through an SSH key; the only
  keys living here are the (optional) image provider ones, in `.env`, excluded from
  git.
- **Limit what the SSH user can do on the server.** The script only uses `wp` read
  commands, plus a few well-defined writes (create/update content, upload an image
  as featured, set a single meta key). A sudoers rule that allows *only* `wp` for
  that user (not a general shell) drastically reduces what can happen if the key or
  the local machine is compromised.
- **`--dry-run` before every real publish.** It isn't just a recommendation: it's
  what keeps you from discovering a wrong front-matter after you've already
  overwritten a page online.
- If you customise the script to make different writes over SSH, keep them explicit
  and narrow (one precise command, not a general shell) — it's far easier to verify
  "this script can only do X" than "this script has access to everything".

## License

MIT — see `LICENSE`.
