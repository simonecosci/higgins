#!/usr/bin/env python3
"""
featured.py - generates the cover image (featured image) of a post.

The prompt is built from the content (title, excerpt, tags); the style is a constant
shared by every cover, and it's what makes them a family.

Providers, in order of preference: the first one found configured is used.

    huggingface   HF_TOKEN                        FLUX.1-schnell. Free token, no credit
                                                  card, from huggingface.co/settings/tokens
    cloudflare    CF_ACCOUNT_ID + CF_API_TOKEN    FLUX.1-schnell on Workers AI. Daily free
                                                  tier, no credit card.
    gemini        GEMINI_API_KEY                  gemini-2.5-flash-image. WARNING: image
                                                  generation is NOT in the free tier, it
                                                  needs billing enabled on the project.
    pollinations  (no key)                        last resort: no sign-up, but it serves the
                                                  SANA model and the output is poor.
                                                  Must be asked for with --provider pollinations.

The keys go in a .env (in the project, in .claude/, or in ~/.claude/) or in the
environment. They are never written down or printed here.

The images end up in images/<slug>.png and stay in the repo: they are not regenerated on
every upload, so two uploads of the same post give the same cover.

Usage:
    python featured.py content/page.md                  # generate if missing
    python featured.py content/page.md --force          # regenerate
    python featured.py content/page.md --prompt-only    # print the prompt, no API call
    python featured.py content/page.md --provider cloudflare
    python featured.py --providers                      # what is configured
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def load_env():
    """Reads the keys from a .env, if there is one. The environment always wins over the
    file, and the values are never printed nor copied anywhere else."""
    here = Path(__file__).parent
    for f in (here / ".env",
              here / ".claude" / ".env",
              Path.home() / ".claude" / ".env",
              Path.home() / ".claude" / "skills" / ".env"):
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


load_env()

IMG_DIR = Path(__file__).parent / "images"
WIDTH, HEIGHT = 1216, 640               # roughly 16:9, multiples of 64: FLUX likes those
TIMEOUT = 180

# Style shared by every cover: keeping it fixed is what makes them look like a family
# instead of sixteen unrelated images. Change it here, for all of them.
# Written entirely in the positive, on purpose: diffusion models don't handle negations,
# and a list of "no brains, no faces" makes those things MORE likely, not less.
# So it describes what you want to see, and the inventory of allowed subjects does the rest.
STYLE = (
    "Flat vector editorial illustration, wide banner. "
    "Dark slate blue background. Off-white shapes. One small amber accent. "
    "Three or four large elements only, lots of empty space, centered, crisp edges. "
    "Minimal, sober, wordless."
)


# ---------------------------------------------------------------- prompt

def build_prompt(meta):
    """Subject from the post's content, style from the constant."""
    if meta.get("image_prompt"):                  # the front-matter takes precedence
        subject = meta["image_prompt"]
    else:
        parts = []
        if meta.get("excerpt"):
            parts.append(meta["excerpt"])
        if meta.get("tags"):
            parts.append("Themes: " + ", ".join(meta["tags"]) + ".")
        subject = (
            'Cover image for an article titled "{}". {} '
            "Represent the concept concretely and metaphorically, not literally."
        ).format(meta["title"], " ".join(parts))
    return f"{subject}\n\n{STYLE}"


def image_path(slug):
    return IMG_DIR / f"{slug}.png"


def crop(data: bytes) -> bytes:
    """Brings the image to the covers' widescreen format.

    Needed because the models return squares: flux-1-schnell on Cloudflare doesn't even
    accept width/height. It crops at the centre, where these models put the subject.
    If Pillow isn't there, the image is kept as it is: square beats nothing.
    """
    try:
        import io
        from PIL import Image
    except ImportError:
        return data

    img = Image.open(io.BytesIO(data)).convert("RGB")
    wanted = WIDTH / HEIGHT
    w, h = img.size
    if abs(w / h - wanted) > 0.01:
        if w / h > wanted:                        # too wide: cut the sides
            new = int(h * wanted)
            img = img.crop(((w - new) // 2, 0, (w - new) // 2 + new, h))
        else:                                     # too tall: cut top and bottom
            new = int(w / wanted)
            img = img.crop((0, (h - new) // 2, w, (h - new) // 2 + new))
    img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------- providers

def _post(url, body, headers, timeout=TIMEOUT):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "")


def from_huggingface(prompt):
    token = os.environ.get("HF_TOKEN")
    if not token:
        return None
    body = json.dumps({
        "inputs": prompt,
        "parameters": {"width": WIDTH, "height": HEIGHT},
    }).encode()
    for url in ("https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell",
                "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell"):
        try:
            data, kind = _post(url, body, {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "image/png",
            })
        except urllib.error.HTTPError as e:
            if e.code == 404:                     # old endpoint: try the other one
                continue
            raise RuntimeError(f"Hugging Face {e.code}: {e.read()[:200].decode(errors='replace')}")
        if kind.startswith("image/"):
            return data
        raise RuntimeError(f"Hugging Face: non-image response ({kind}): {data[:200]!r}")
    raise RuntimeError("Hugging Face: no valid endpoint for FLUX.1-schnell")


def from_cloudflare(prompt):
    account, token = os.environ.get("CF_ACCOUNT_ID"), os.environ.get("CF_API_TOKEN")
    if not (account and token):
        return None
    url = (f"https://api.cloudflare.com/client/v4/accounts/{account}"
           "/ai/run/@cf/black-forest-labs/flux-1-schnell")
    # FLUX.1-schnell is distilled for few steps: 4 are enough, they cost half and it's faster.
    # width/height are NOT passed: this model rejects them ("properties not allowed") and
    # always returns a 1024x1024 square. The 16:9 is handled by crop() afterwards.
    body = json.dumps({"prompt": prompt, "steps": 4}).encode()
    try:
        data, kind = _post(url, body, {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
    except urllib.error.HTTPError as e:
        text = e.read()[:300].decode(errors="replace")
        if "NSFW" in text:                        # it happens on innocuous prompts too
            raise RuntimeError("Cloudflare rejected the prompt as NSFW (a frequent false "
                               "positive): try again with --force or change image_prompt")
        raise RuntimeError(f"Cloudflare {e.code}: {text}")
    if kind.startswith("image/"):
        return data
    response = json.loads(data)
    if not response.get("success", True):
        raise RuntimeError(f"Cloudflare: {response.get('errors')}")
    b64 = (response.get("result") or {}).get("image")
    if not b64:
        raise RuntimeError(f"Cloudflare: no image in the response: {str(response)[:200]}")
    return base64.b64decode(b64)


def from_gemini(prompt):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise RuntimeError("the google-genai package is missing: pip install google-genai")

    # The client has to be kept in a variable: created inline it gets closed by the garbage
    # collector while the request is still in flight ("client has been closed").
    client = genai.Client(api_key=key)
    response = client.models.generate_content(
        model="gemini-2.5-flash-image",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
            image_config=types.ImageConfig(aspect_ratio="16:9"),
        ),
    )
    for part in response.candidates[0].content.parts:
        inline = getattr(part, "inline_data", None)
        if inline and inline.mime_type.startswith("image/"):
            return inline.data
    raise RuntimeError("Gemini didn't return an image")


def from_pollinations(prompt):
    """No sign-up, but the anonymous tier serves SANA and the output is poor:
    used only when explicitly asked for."""
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt[:1500])
           + f"?width={WIDTH}&height={HEIGHT}&nologo=true")
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        if not r.headers.get("Content-Type", "").startswith("image/"):
            raise RuntimeError("Pollinations: non-image response")
        return r.read()


PROVIDER = {                                      # order = preference
    "huggingface": (from_huggingface, "HF_TOKEN"),
    "cloudflare": (from_cloudflare, "CF_ACCOUNT_ID + CF_API_TOKEN"),
    "gemini": (from_gemini, "GEMINI_API_KEY"),
    "pollinations": (from_pollinations, "no key (on request only)"),
}
AUTOMATIC = ("huggingface", "cloudflare", "gemini")


def configured():
    return [n for n in AUTOMATIC
            if all(os.environ.get(v) for v in {
                "huggingface": ["HF_TOKEN"],
                "cloudflare": ["CF_ACCOUNT_ID", "CF_API_TOKEN"],
                "gemini": ["GEMINI_API_KEY"],
            }[n])]


# ---------------------------------------------------------------- generation

def generate(meta, force=False, verbose=True, provider=None):
    """Generates the cover and returns its path, or None if it wasn't possible.

    None is not a fatal error: the caller carries on without a cover.
    """
    out = image_path(meta["slug"])
    if out.exists() and not force:
        if verbose:
            print(f"            cover already there: {out.name}")
        return out

    if meta.get("image") is False:
        if verbose:
            print("            cover disabled by the front-matter (image: false)")
        return None

    to_try = [provider] if provider else configured()
    if not to_try:
        if verbose:
            print("            no image provider configured: cover skipped.")
            print("            The simplest one: HF_TOKEN from huggingface.co/settings/tokens")
        return None

    prompt = build_prompt(meta)
    for name in to_try:
        function = PROVIDER[name][0]
        if verbose:
            print(f"            generating the cover with {name}...")
        try:
            data = function(prompt)
        except Exception as e:                    # network, quota, revoked token
            if verbose:
                print(f"            {name} didn't work: {str(e)[:160]}")
            continue
        if not data:
            continue
        data = crop(data)
        IMG_DIR.mkdir(exist_ok=True)
        out.write_bytes(data)
        if verbose:
            print(f"            cover saved: {out.name} ({len(data) // 1024} KB, {name})")
        return out

    if verbose:
        print("            no provider produced an image: cover skipped.")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?")
    ap.add_argument("--force", action="store_true", help="regenerate even if it already exists")
    ap.add_argument("--prompt-only", action="store_true", help="print the prompt and nothing else")
    ap.add_argument("--provider", choices=list(PROVIDER), help="force a provider")
    ap.add_argument("--providers", action="store_true", help="list what is configured")
    a = ap.parse_args()

    if a.providers:
        ready = configured()
        for name, (_, requires) in PROVIDER.items():
            if name == "pollinations":
                state = "available, but only with --provider pollinations (poor output)"
            else:
                state = "CONFIGURED" if name in ready else f"{requires} missing"
            print(f"  {name:14} {state}")
        return

    if not a.file:
        sys.exit("The content file is required (or --providers).")

    import publish                                # reuse the front-matter parser
    meta = publish.parse_file(Path(a.file))

    if meta["type"] != "post":
        sys.exit("Covers are only generated for posts, not for pages.")

    if a.prompt_only:
        print(build_prompt(meta))
        return

    if generate(meta, force=a.force, provider=a.provider) is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
