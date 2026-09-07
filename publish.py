#!/usr/bin/env python3
"""
publish.py - carica contenuti markdown su WordPress via SSH + WP-CLI.

Nessuna credenziale: usa la tua chiave ssh e WP-CLI installato sul server.
Idempotente sullo slug: aggiorna se esiste, crea se no. Default: ONLINE.
Un contenuto gia' pubblicato viene aggiornato sul posto, senza copie di sicurezza.

Config in config.yml accanto allo script (nessun segreto):
    ssh:   host ssh, es. root@vps
    path:  path dell'installazione WordPress sul server
    cli:   comando wp sul server (default: "wp --allow-root")

Uso:
    python3 publish.py --dry-run                       # cosa farebbe
    python3 publish.py contenuti/pagina.md             # online
    python3 publish.py contenuti/pagina.md --draft     # come bozza
    python3 publish.py contenuti/pagina.md --solo-copertina   # solo l'immagine
"""

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

import markdown
import yaml

import featured

CONTENT_DIR = Path(__file__).parent / "contenuti"
STATI = ("publish", "draft", "pending", "private", "future")


def config():
    cfg_file = Path(__file__).parent / "config.yml"
    if not cfg_file.exists():
        sys.exit("Manca config.yml accanto allo script (vedi README.md)")
    cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
    if not cfg.get("ssh") or not cfg.get("path"):
        sys.exit("config.yml: servono 'ssh' e 'path'")
    return cfg["ssh"], cfg["path"], cfg.get("cli", "wp --allow-root"), cfg.get("author")


def parse_file(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        sys.exit(f"{path.name}: manca il front-matter YAML")
    _, front, body = raw.split("---", 2)
    meta = yaml.safe_load(front) or {}
    for f in ("title", "slug"):
        if not meta.get(f):
            sys.exit(f"{path.name}: campo obbligatorio '{f}' mancante")
    meta.setdefault("type", "post")
    # jetpack-portfolio: i 13 progetti del portfolio (custom post type di Jetpack).
    # Aggiunto il 2026-09-05 per poterne correggere i refusi con lo stesso flusso sicuro
    # di pagine e articoli, senza nuovi comandi ad-hoc via SSH.
    if meta["type"] not in ("post", "page", "jetpack-portfolio"):
        sys.exit(f"{path.name}: type deve essere post, page o jetpack-portfolio")
    meta["html"] = markdown.markdown(body.strip(), extensions=["extra", "sane_lists"])
    meta["source_file"] = path.name
    return meta


def wp(host, path, cli, args, stdin=None, check=True) -> str:
    remote = f"{cli} {' '.join(shlex.quote(a) for a in args)} --path={shlex.quote(path)}"
    r = subprocess.run(["ssh", host, remote], input=stdin if stdin is not None else "", encoding="utf-8", capture_output=True)
    if r.returncode != 0:
        # check=False per i comandi il cui fallimento e' una risposta legittima:
        # "wp post meta get" esce 1 quando la meta non esiste, che e' un caso normale.
        if check:
            sys.exit(f"    ERRORE wp-cli: {r.stderr.strip()[:400]}")
        return ""
    return r.stdout.strip()


def ha_copertina(host, path, cli, pid) -> bool:
    """True se il post ha gia' una featured image. Le copertine non si sovrascrivono:
    se ne esiste una, e' stata scelta da una persona e vince."""
    out = wp(host, path, cli, ["post", "meta", "get", pid, "_thumbnail_id", "--format=json"],
             check=False)          # esce 1 se la meta non c'e': e' un "no", non un errore
    return bool(out and out.strip('"').strip() not in ("", "0", "null"))


def carica_copertina(host, path, cli, pid, png: Path, titolo: str) -> str | None:
    """Copia il PNG sul server, lo importa nella media library e lo imposta come
    featured image del post. Ritorna l'ID dell'allegato, o None se salta."""
    remoto = f"/tmp/{png.name}"
    scp = subprocess.run(["scp", "-q", str(png), f"{host}:{remoto}"], capture_output=True, encoding="utf-8")
    if scp.returncode != 0:
        print(f"            copertina non caricata (scp): {scp.stderr.strip()[:200]}")
        return None
    try:
        aid = wp(host, path, cli, [
            "media", "import", remoto, f"--post_id={pid}", "--featured_image",
            f"--title={titolo}", "--porcelain",
        ])
    finally:
        # Il temp non e' roba nostra da lasciare in giro sul server.
        subprocess.run(["ssh", host, f"rm -f {shlex.quote(remoto)}"], capture_output=True)
    return aid


def find(host, path, cli, post_type, slug):
    """Ritorna (id, status) del contenuto con quello slug, o (None, None)."""
    out = wp(host, path, cli, [
        "post", "list", f"--post_type={post_type}", f"--name={slug}",
        # NON "any": con --name impostato WP_Query esclude gli stati con
        # exclude_from_search (draft compreso) e le bozze non si troverebbero mai.
        f"--post_status={','.join(STATI)}", "--fields=ID,post_status", "--format=csv",
    ])
    lines = out.splitlines()
    if len(lines) < 2:
        return None, None
    pid, status = lines[1].split(",")[:2]
    return pid.strip(), status.strip()


def solo_copertina(host, path, cli, meta, dry_run):
    """Imposta la sola featured image di un contenuto gia' online, senza toccarne il testo.

    Serve perche' un post gia' pubblicato non si puo' aggiornare senza --publish (finirebbe
    su una copia [BOZZA]): qui il contenuto non si tocca proprio, cambia solo l'immagine.
    """
    slug, ptype = meta["slug"], meta["type"]
    if ptype != "post":
        print(f"  SALTO     {slug}: le copertine sono solo per i post")
        return
    pid, status = find(host, path, cli, ptype, slug)
    if not pid:
        print(f"  SALTO     {slug}: non esiste sul sito")
        return
    if ha_copertina(host, path, cli, pid):
        print(f"  SALTO     {slug} (id={pid}): ha gia' una copertina, non si sovrascrive")
        return
    png = featured.percorso(slug)
    print(f"  COPERTINA {ptype:5} /{slug:45} [{status}] id={pid}  <- {png.name}")
    if dry_run:
        if not png.exists():
            print("            (da generare)")
        return
    png = featured.genera(meta)
    if not png:
        return
    aid = carica_copertina(host, path, cli, pid, png, meta["title"])
    if aid:
        print(f"            copertina impostata: allegato id={aid}")


def sync_one(host, path, cli, meta, bozza, dry_run, immagini=True, autore=None):
    slug, title, ptype = meta["slug"], meta["title"], meta["type"]
    autore = meta.get("author", autore)      # il front-matter vince sul config.yml
    pid = status = None
    if not dry_run:
        pid, status = find(host, path, cli, ptype, slug)

    # Default: online. Un contenuto gia' pubblicato viene aggiornato sul posto, senza
    # copie [BOZZA]. Con --draft si carica come bozza.
    note = ""
    new_status = "draft" if bozza else "publish"
    print(f"  {'AGGIORNA' if pid else 'CREA':9} {ptype:5} /{slug:45} [{new_status}]  <- {meta['source_file']}{note}")
    if dry_run:
        print(f"            autore: {autore if autore else 'NESSUNO (post_author=0)'}")
        if immagini and ptype == "post":
            png = featured.percorso(slug)
            stato = "gia' pronta" if png.exists() else "da generare"
            print(f"            copertina: {png.name} ({stato}), se il post non ne ha gia' una")
        return

    # Un post nuovo che deve finire online si crea in bozza, gli si mette la copertina e
    # solo alla fine si pubblica. Cosi' transition_post_status - l'hook a cui si aggancia
    # l'auto-poster social - scatta quando og:image c'e' gia', e l'anteprima su Facebook e
    # LinkedIn non parte senza immagine (il primo scrape se lo tengono in cache).
    # Su un contenuto GIA' online non si fa: vorrebbe dire toglierlo dal sito per qualche
    # secondo e rifar scattare l'hook, cioe' un secondo post sui social per un aggiornamento.
    copertina_prima = immagini and ptype == "post" and not pid and new_status == "publish"

    common = [f"--post_title={title}", f"--post_status={'draft' if copertina_prima else new_status}"]
    if autore:
        common.append(f"--post_author={autore}")
    for key in ("excerpt", "parent", "menu_order"):
        if meta.get(key) is not None:
            common.append(f"--post_{key}={meta[key]}")
    if meta.get("categories"):            # ID numerici: wp term list category --fields=term_id,name
        common.append("--post_category=" + ",".join(str(c) for c in meta["categories"]))
    if meta.get("tags"):                  # nomi, vengono creati se non esistono
        common.append("--tags_input=" + ",".join(meta["tags"]))

    if pid:
        wp(host, path, cli, ["post", "update", pid, "-", *common], stdin=meta["html"])
    else:
        pid = wp(host, path, cli, [
            "post", "create", "-", f"--post_type={ptype}", f"--post_name={slug}",
            "--porcelain", *common,
        ], stdin=meta["html"])
    print(f"            id={pid}")

    # Copertina: solo per i post, solo se non ne hanno gia' una.
    if immagini and ptype == "post" and not ha_copertina(host, path, cli, pid):
        png = featured.genera(meta)
        if png:
            aid = carica_copertina(host, path, cli, pid, png, meta["title"])
            if aid:
                print(f"            copertina impostata: allegato id={aid}")

    if copertina_prima:
        wp(host, path, cli, ["post", "update", pid, "--post_status=publish"])
        print("            pubblicato (la copertina c'era gia')")

    # Ripulisce una vecchia copia [BOZZA] rimasta dalla versione precedente dello script.
    bid, _ = find(host, path, cli, ptype, f"{meta['slug']}-bozza")
    if bid:
        wp(host, path, cli, ["post", "delete", bid, "--force"])
        print(f"            rimossa la vecchia copia [BOZZA] id={bid}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--draft", action="store_true",
                    help="carica come bozza invece che online")
    ap.add_argument("--no-image", action="store_true",
                    help="non generare/caricare la copertina")
    ap.add_argument("--solo-copertina", action="store_true",
                    help="imposta solo la featured image, senza toccare il testo")
    a = ap.parse_args()

    paths = [Path(f) for f in a.files] or sorted(CONTENT_DIR.glob("*.md"))
    if not paths:
        sys.exit("Nessun contenuto.")
    contents = [parse_file(p) for p in paths]

    # config.yml si legge sempre: e' un file locale, e il dry-run deve poter dire con che
    # autore finirebbe il post - senza, l'auto-poster social non parte (post_author=0).
    host, path, cli, autore = config()
    if a.dry_run and not a.solo_copertina:
        print(f"\nDRY RUN - {len(contents)} contenuti, nessuna modifica:\n")
    else:
        # --solo-copertina interroga il sito anche in dry-run: per dire cosa farebbe deve
        # sapere quali post hanno gia' una copertina, e sono tutte letture.
        modo = "DRY RUN copertine" if a.dry_run else (
            "COPERTINE" if a.solo_copertina else ("BOZZE" if a.draft else "PUBBLICAZIONE"))
        print(f"\n{modo} su {host}:{path} - {len(contents)} contenuti:\n")

    for m in contents:
        if a.solo_copertina:
            solo_copertina(host, path, cli, m, a.dry_run)
        else:
            sync_one(host, path, cli, m, a.draft, a.dry_run,
                     immagini=not a.no_image, autore=autore)
    print()


if __name__ == "__main__":
    main()
