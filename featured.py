#!/usr/bin/env python3
"""
featured.py - genera l'immagine di copertina (featured image) di un post.

Il prompt e' costruito dal contenuto (titolo, excerpt, tag); lo stile e' una costante
uguale per tutte le copertine, ed e' quello che le rende una famiglia.

Provider, in ordine di preferenza: si usa il primo che risulta configurato.

    huggingface   HF_TOKEN                        FLUX.1-schnell. Token gratuito, senza
                                                  carta di credito, da huggingface.co/settings/tokens
    cloudflare    CF_ACCOUNT_ID + CF_API_TOKEN    FLUX.1-schnell su Workers AI. Free tier
                                                  giornaliero, senza carta.
    gemini        GEMINI_API_KEY                  gemini-2.5-flash-image. ATTENZIONE: la
                                                  generazione di immagini NON e' nel free
                                                  tier, serve fatturazione attiva sul progetto.
    pollinations  (nessuna chiave)                ultima spiaggia: nessuna registrazione, ma
                                                  serve il modello SANA e la resa e' scadente.
                                                  Va chiesto a mano con --provider pollinations.

Le chiavi si mettono in un .env (nel progetto, in .claude/, o in ~/.claude/) oppure
nell'ambiente. Qui non si scrivono e non si stampano mai.

Le immagini finiscono in immagini/<slug>.png e restano nel repo: non si rigenerano a ogni
caricamento, cosi' due upload dello stesso post danno la stessa copertina.

Uso:
    python featured.py contenuti/pagina.md                    # genera se manca
    python featured.py contenuti/pagina.md --force            # rigenera
    python featured.py contenuti/pagina.md --prompt-only      # stampa il prompt, nessuna chiamata
    python featured.py contenuti/pagina.md --provider cloudflare
    python featured.py --providers                            # cosa risulta configurato
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


def carica_env():
    """Legge le chiavi da un .env, se c'e'. L'ambiente vince sempre sul file,
    e i valori non vengono mai stampati ne' copiati altrove."""
    qui = Path(__file__).parent
    for f in (qui / ".env",
              qui / ".claude" / ".env",
              Path.home() / ".claude" / ".env",
              Path.home() / ".claude" / "skills" / ".env"):
        if not f.exists():
            continue
        for riga in f.read_text(encoding="utf-8").splitlines():
            riga = riga.strip()
            if not riga or riga.startswith("#") or "=" not in riga:
                continue
            k, v = riga.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


carica_env()

IMG_DIR = Path(__file__).parent / "immagini"
LARGHEZZA, ALTEZZA = 1216, 640          # 16:9 circa, multipli di 64: FLUX li gradisce
TIMEOUT = 180

# Stile comune a tutte le copertine: tenerlo fisso e' quello che le fa sembrare una
# famiglia invece di sedici immagini scollegate. Si cambia qui, per tutte.
# Scritto tutto in positivo, di proposito: i modelli diffusion non gestiscono le negazioni,
# e un elenco di "no brains, no faces" rende quelle cose PIU' probabili, non meno.
# Si descrive quindi cosa si vuole vedere, e l'inventario dei soggetti ammessi fa il resto.
STILE = (
    "Flat vector editorial illustration, wide banner. "
    "Dark slate blue background. Off-white shapes. One small amber accent. "
    "Three or four large elements only, lots of empty space, centered, crisp edges. "
    "Minimal, sober, wordless."
)


# ---------------------------------------------------------------- prompt

def costruisci_prompt(meta):
    """Soggetto dal contenuto del post, stile dalla costante."""
    if meta.get("image_prompt"):                  # il front-matter ha la precedenza
        soggetto = meta["image_prompt"]
    else:
        pezzi = []
        if meta.get("excerpt"):
            pezzi.append(meta["excerpt"])
        if meta.get("tags"):
            pezzi.append("Temi: " + ", ".join(meta["tags"]) + ".")
        soggetto = (
            'Immagine di copertina per un articolo intitolato "{}". {} '
            "Rappresenta il concetto in modo concreto e metaforico, non letterale."
        ).format(meta["title"], " ".join(pezzi))
    return f"{soggetto}\n\n{STILE}"


def percorso(slug):
    return IMG_DIR / f"{slug}.png"


def ritaglia(dati: bytes) -> bytes:
    """Porta l'immagine al formato panoramico delle copertine.

    Serve perche' i modelli restituiscono quadrati: flux-1-schnell su Cloudflare non
    accetta nemmeno width/height. Si ritaglia al centro, dove questi modelli mettono
    il soggetto. Se Pillow non c'e', si tiene l'immagine com'e': meglio quadrata che niente.
    """
    try:
        import io
        from PIL import Image
    except ImportError:
        return dati

    img = Image.open(io.BytesIO(dati)).convert("RGB")
    voluto = LARGHEZZA / ALTEZZA
    l, h = img.size
    if abs(l / h - voluto) > 0.01:
        if l / h > voluto:                        # troppo larga: taglia ai lati
            nuova = int(h * voluto)
            img = img.crop(((l - nuova) // 2, 0, (l - nuova) // 2 + nuova, h))
        else:                                     # troppo alta: taglia sopra e sotto
            nuova = int(l / voluto)
            img = img.crop((0, (h - nuova) // 2, l, (h - nuova) // 2 + nuova))
    img = img.resize((LARGHEZZA, ALTEZZA), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------- provider

def _post(url, body, headers, timeout=TIMEOUT):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "")


def da_huggingface(prompt):
    token = os.environ.get("HF_TOKEN")
    if not token:
        return None
    corpo = json.dumps({
        "inputs": prompt,
        "parameters": {"width": LARGHEZZA, "height": ALTEZZA},
    }).encode()
    for url in ("https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell",
                "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell"):
        try:
            dati, tipo = _post(url, corpo, {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "image/png",
            })
        except urllib.error.HTTPError as e:
            if e.code == 404:                     # endpoint vecchio: prova l'altro
                continue
            raise RuntimeError(f"Hugging Face {e.code}: {e.read()[:200].decode(errors='replace')}")
        if tipo.startswith("image/"):
            return dati
        raise RuntimeError(f"Hugging Face: risposta non-immagine ({tipo}): {dati[:200]!r}")
    raise RuntimeError("Hugging Face: nessun endpoint valido per FLUX.1-schnell")


def da_cloudflare(prompt):
    account, token = os.environ.get("CF_ACCOUNT_ID"), os.environ.get("CF_API_TOKEN")
    if not (account and token):
        return None
    url = (f"https://api.cloudflare.com/client/v4/accounts/{account}"
           "/ai/run/@cf/black-forest-labs/flux-1-schnell")
    # FLUX.1-schnell e' distillato per pochi passi: 4 bastano, costano meta' ed e' piu' veloce.
    # width/height NON si passano: questo modello li rifiuta ("properties not allowed") e
    # restituisce sempre un quadrato 1024x1024. Al 16:9 ci pensa ritaglia() dopo.
    corpo = json.dumps({"prompt": prompt, "steps": 4}).encode()
    try:
        dati, tipo = _post(url, corpo, {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
    except urllib.error.HTTPError as e:
        testo = e.read()[:300].decode(errors="replace")
        if "NSFW" in testo:                       # capita anche su prompt innocui
            raise RuntimeError("Cloudflare ha bocciato il prompt come NSFW (falso positivo "
                               "frequente): riprova con --force o cambia image_prompt")
        raise RuntimeError(f"Cloudflare {e.code}: {testo}")
    if tipo.startswith("image/"):
        return dati
    risposta = json.loads(dati)
    if not risposta.get("success", True):
        raise RuntimeError(f"Cloudflare: {risposta.get('errors')}")
    b64 = (risposta.get("result") or {}).get("image")
    if not b64:
        raise RuntimeError(f"Cloudflare: nessuna immagine nella risposta: {str(risposta)[:200]}")
    return base64.b64decode(b64)


def da_gemini(prompt):
    chiave = os.environ.get("GEMINI_API_KEY")
    if not chiave:
        return None
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise RuntimeError("manca il pacchetto google-genai: pip install google-genai")

    # Il client va tenuto in una variabile: creandolo inline viene chiuso dal garbage
    # collector mentre la richiesta e' ancora in volo ("client has been closed").
    client = genai.Client(api_key=chiave)
    risposta = client.models.generate_content(
        model="gemini-2.5-flash-image",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
            image_config=types.ImageConfig(aspect_ratio="16:9"),
        ),
    )
    for parte in risposta.candidates[0].content.parts:
        inline = getattr(parte, "inline_data", None)
        if inline and inline.mime_type.startswith("image/"):
            return inline.data
    raise RuntimeError("Gemini non ha restituito un'immagine")


def da_pollinations(prompt):
    """Senza registrazione, ma il tier anonimo serve SANA e la resa e' scadente:
    si usa solo se richiesto esplicitamente."""
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt[:1500])
           + f"?width={LARGHEZZA}&height={ALTEZZA}&nologo=true")
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        if not r.headers.get("Content-Type", "").startswith("image/"):
            raise RuntimeError("Pollinations: risposta non-immagine")
        return r.read()


PROVIDER = {                                      # ordine = preferenza
    "huggingface": (da_huggingface, "HF_TOKEN"),
    "cloudflare": (da_cloudflare, "CF_ACCOUNT_ID + CF_API_TOKEN"),
    "gemini": (da_gemini, "GEMINI_API_KEY"),
    "pollinations": (da_pollinations, "nessuna chiave (solo su richiesta)"),
}
AUTOMATICI = ("huggingface", "cloudflare", "gemini")


def configurati():
    return [n for n in AUTOMATICI
            if all(os.environ.get(v) for v in {
                "huggingface": ["HF_TOKEN"],
                "cloudflare": ["CF_ACCOUNT_ID", "CF_API_TOKEN"],
                "gemini": ["GEMINI_API_KEY"],
            }[n])]


# ---------------------------------------------------------------- generazione

def genera(meta, force=False, verbose=True, provider=None):
    """Genera la copertina e ne restituisce il path, o None se non e' stato possibile.

    None non e' un errore fatale: chi chiama prosegue senza copertina.
    """
    out = percorso(meta["slug"])
    if out.exists() and not force:
        if verbose:
            print(f"            copertina gia' presente: {out.name}")
        return out

    if meta.get("image") is False:
        if verbose:
            print("            copertina disattivata dal front-matter (image: false)")
        return None

    da_provare = [provider] if provider else configurati()
    if not da_provare:
        if verbose:
            print("            nessun provider di immagini configurato: copertina saltata.")
            print("            Il piu' semplice: HF_TOKEN da huggingface.co/settings/tokens")
        return None

    prompt = costruisci_prompt(meta)
    for nome in da_provare:
        funzione = PROVIDER[nome][0]
        if verbose:
            print(f"            genero la copertina con {nome}...")
        try:
            dati = funzione(prompt)
        except Exception as e:                    # rete, quota, token revocato
            if verbose:
                print(f"            {nome} non ha funzionato: {str(e)[:160]}")
            continue
        if not dati:
            continue
        dati = ritaglia(dati)
        IMG_DIR.mkdir(exist_ok=True)
        out.write_bytes(dati)
        if verbose:
            print(f"            copertina salvata: {out.name} ({len(dati) // 1024} KB, {nome})")
        return out

    if verbose:
        print("            nessun provider ha prodotto un'immagine: copertina saltata.")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?")
    ap.add_argument("--force", action="store_true", help="rigenera anche se esiste gia'")
    ap.add_argument("--prompt-only", action="store_true", help="stampa il prompt e basta")
    ap.add_argument("--provider", choices=list(PROVIDER), help="forza un provider")
    ap.add_argument("--providers", action="store_true", help="elenca cosa risulta configurato")
    a = ap.parse_args()

    if a.providers:
        pronti = configurati()
        for nome, (_, serve) in PROVIDER.items():
            if nome == "pollinations":
                stato = "disponibile, ma solo con --provider pollinations (resa scadente)"
            else:
                stato = "CONFIGURATO" if nome in pronti else f"manca {serve}"
            print(f"  {nome:14} {stato}")
        return

    if not a.file:
        sys.exit("Serve il file del contenuto (oppure --providers).")

    import publish                                # riusa il parser del front-matter
    meta = publish.parse_file(Path(a.file))

    if meta["type"] != "post":
        sys.exit("Le copertine si generano solo per i post, non per le pagine.")

    if a.prompt_only:
        print(costruisci_prompt(meta))
        return

    if genera(meta, force=a.force, provider=a.provider) is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
