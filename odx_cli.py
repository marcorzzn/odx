"""
odx_cli.py — Interfaccia a riga di comando per il formato ODX

Comandi disponibili:
  odx convert   — converte PDF → ODX o ODX → PDF
  odx validate  — valida un file .odx secondo la specifica
  odx info      — mostra metadati e struttura di un file .odx
  odx extract   — estrae il testo puro da un file .odx
  odx ocr       — esegue OCR su un'immagine e crea un file .odx
  odx diff      — mostra la cronologia delle versioni
  odx new       — crea un nuovo file .odx da testo

Uso senza installazione:
    python odx_cli.py [comando] [argomenti]

Esempi:
    python odx_cli.py info documento.odx
    python odx_cli.py convert documento.pdf
    python odx_cli.py convert documento.pdf output.odx
    python odx_cli.py convert documento.odx output.pdf
    python odx_cli.py validate documento.odx
    python odx_cli.py extract documento.odx
    python odx_cli.py new "Il mio documento" --lang it --text "Contenuto..."
    python odx_cli.py ocr immagine.png --lang it
"""

import sys
import json
import argparse
import time
from pathlib import Path

# Risolvi path per import locali
sys.path.insert(0, str(Path(__file__).parent))

from odxlib import ODXReader, ODXWriter, ODXValidator


# ─────────────────────────────────────────────────────────────
#  HELPERS OUTPUT (fallback senza rich)
# ─────────────────────────────────────────────────────────────

def print_header(title: str):
    width = 58
    print(f"\n╔{'═' * width}╗")
    print(f"║  {title:<{width-2}}║")
    print(f"╚{'═' * width}╝")

def print_box(title: str, items: list):
    """Stampa una box con titolo e lista di (label, valore)."""
    print(f"\n  ┌─ {title} {'─' * max(0, 46 - len(title))}┐")
    for label, value in items:
        val_str = str(value)
        if len(val_str) > 38:
            val_str = val_str[:35] + "..."
        print(f"  │  {label:<18} {val_str:<38}│")
    print(f"  └{'─' * 50}┘")

def success(msg: str): print(f"  ✅  {msg}")
def warning(msg: str): print(f"  ⚠️   {msg}")
def error(msg: str):   print(f"  ❌  {msg}", file=sys.stderr)
def info(msg: str):    print(f"  ℹ️   {msg}")
def step(n: int, msg: str): print(f"\n  [{n}] {msg}")


# ─────────────────────────────────────────────────────────────
#  COMANDI
# ─────────────────────────────────────────────────────────────

def cmd_info(args):
    """Mostra metadati e struttura interna di un file .odx."""
    print_header(f"ODX INFO — {Path(args.file).name}")

    try:
        reader = ODXReader(args.file)
        info_data = reader.get_info()
        meta = reader.get_meta()
    except Exception as e:
        error(f"Impossibile leggere il file: {e}")
        sys.exit(1)

    # Sezione metadati
    authors = [a.get('name', '?') for a in info_data.get('authors', [])]
    print_box("Documento", [
        ("Titolo",       info_data.get('title', '(nessuno)')),
        ("Lingua",       info_data.get('lang', '?')),
        ("Autori",       ', '.join(authors) if authors else '(nessuno)'),
        ("Creato",       (info_data.get('created_at') or '')[:19]),
        ("Tipo",         meta.get('document_type', 'other')),
        ("Sorgente",     meta.get('source_format', 'native')),
    ])

    print_box("File ODX", [
        ("UUID",         info_data.get('uuid', '?')),
        ("Versione ODX", info_data.get('odx_version', '?')),
        ("Dimensione",   f"{info_data.get('file_size_bytes', 0):,} byte"),
        ("Pagine",       meta.get('page_count', '?')),
        ("Parole",       meta.get('word_count', '?')),
    ])

    segs = info_data.get('segments_present', [])
    flags = []
    if info_data.get('has_ocr'):        flags.append("OCR nativo")
    if info_data.get('has_diff'):       flags.append("Versioning")
    if info_data.get('has_signatures'): flags.append("Firme digitali")

    print_box("Layer presenti", [
        (f"/{s}", "✅") for s in segs
    ] + ([("Funzionalità extra", ', '.join(flags))] if flags else []))

    # Integrità
    print(f"\n  Verifica integrità hash...")
    integrity = reader.verify_integrity()
    for seg_name, result in integrity["segments"].items():
        icon = "✅" if result["ok"] else "❌"
        print(f"    {icon} /{seg_name:<12} {result['status']}")

    if integrity["all_ok"]:
        success("Tutti gli hash corrispondono — documento integro")
    else:
        warning("Uno o più hash non corrispondono")


def cmd_validate(args):
    """Valida un file .odx secondo la specifica."""
    print_header(f"ODX VALIDATE — {Path(args.file).name}")

    v = ODXValidator(args.file)
    is_valid = v.print_report()

    sys.exit(0 if is_valid else 1)


def cmd_extract(args):
    """Estrae il testo puro da un file .odx."""
    try:
        reader = ODXReader(args.file)
        text = reader.get_text()
    except Exception as e:
        error(f"Impossibile leggere il file: {e}")
        sys.exit(1)

    if args.output:
        Path(args.output).write_text(text, encoding='utf-8')
        success(f"Testo salvato in: {args.output}")
        info(f"Caratteri: {len(text):,}  —  Parole: {len(text.split()):,}")
    else:
        # Stampa su stdout (utile per pipe: odx extract doc.odx | grep "parola")
        print(text)


def cmd_convert(args):
    """Converte tra formati: PDF↔ODX."""
    src = Path(args.input)
    if not src.exists():
        error(f"File non trovato: {args.input}")
        sys.exit(1)

    src_ext = src.suffix.lower()

    if src_ext == '.pdf':
        # PDF → ODX
        print_header(f"ODX CONVERT — PDF → ODX")
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from converters.pdf_to_odx import PDFtoODXConverter
            converter = PDFtoODXConverter(
                run_ocr=getattr(args, 'ocr', False),
                lang=getattr(args, 'lang', None)
            )
            output = args.output if args.output else str(src.with_suffix('.odx'))
            t0 = time.time()
            stats = converter.convert(str(src), output)
            elapsed = time.time() - t0

            print(f"\n  Tempo conversione: {elapsed:.2f}s")
            success(f"File ODX creato: {stats['odx_path']}")

            if stats['size_reduction_pct'] > 0:
                success(f"Riduzione dimensione: {stats['size_reduction_pct']}%")
            else:
                info(f"Overhead: {abs(stats['size_reduction_pct']):.1f}% "
                     "(normale per PDF già ottimizzati)")

        except Exception as e:
            error(f"Conversione fallita: {e}")
            import traceback; traceback.print_exc()
            sys.exit(1)

    elif src_ext == '.odx':
        # ODX → PDF
        print_header(f"ODX CONVERT — ODX → PDF")
        try:
            from converters.pdf_to_odx import ODXtoPDFConverter
            converter = ODXtoPDFConverter()
            output = args.output if args.output else str(src.with_suffix('.pdf'))
            t0 = time.time()
            stats = converter.convert(str(src), output)
            elapsed = time.time() - t0
            print(f"\n  Tempo conversione: {elapsed:.2f}s")
            success(f"PDF creato: {stats['pdf_path']}")
        except Exception as e:
            error(f"Conversione fallita: {e}")
            import traceback; traceback.print_exc()
            sys.exit(1)
    else:
        error(f"Formato non supportato: {src_ext}")
        info("Formati supportati: .pdf, .odx")
        sys.exit(1)


def cmd_new(args):
    """Crea un nuovo file .odx da testo."""
    print_header("ODX NEW — Crea documento")

    title = args.title
    lang = getattr(args, 'lang', 'it') or 'it'
    text_content = getattr(args, 'text', '') or ''
    author = getattr(args, 'author', '') or ''
    output = getattr(args, 'output', None)

    if not text_content and not args.stdin:
        # Testo minimale di default
        text_content = f"{title}\n\nDocumento creato con ODX v0.1."
    elif args.stdin:
        info("Leggo testo da stdin (Ctrl+D per terminare)...")
        text_content = sys.stdin.read()

    if not output:
        # Genera nome file dal titolo
        safe_name = ''.join(c if c.isalnum() or c in ' -_' else '_'
                            for c in title).strip().replace(' ', '_')
        output = f"{safe_name}.odx"

    writer = ODXWriter()
    writer.set_meta(
        title=title,
        lang=lang,
        authors=[{"name": author}] if author else None,
        document_type="other"
    )
    writer.set_text(text_content)
    writer.set_semantic_from_text(text_content, lang=lang)

    stats = writer.save(output)
    success(f"File creato: {output}")
    info(f"Dimensione: {stats['file_size_bytes']:,} byte  —  "
         f"UUID: {stats['uuid']}")


def cmd_ocr(args):
    """Esegue OCR su un'immagine e crea un file .odx."""
    print_header(f"ODX OCR — {Path(args.image).name}")

    src = Path(args.image)
    if not src.exists():
        error(f"Immagine non trovata: {args.image}")
        sys.exit(1)

    lang = getattr(args, 'lang', 'eng') or 'eng'
    output = getattr(args, 'output', None) or str(src.with_suffix('.odx'))

    try:
        from odxlib.ocr.pipeline import OCRPipeline
        pipeline = OCRPipeline(lang=lang)

        step(1, f"OCR in corso su {src.name}...")
        t0 = time.time()
        page_dict = pipeline.process_image(str(src), page=1)
        elapsed = time.time() - t0

        words = page_dict.get('words', [])
        conf  = page_dict.get('overall_confidence', 0)
        text  = page_dict.get('full_text_extracted', '')
        stats = page_dict.get('confidence_stats', {})

        info(f"Parole riconosciute: {len(words)}")
        info(f"Confidence media:    {conf:.0%}")
        info(f"Distribuzione:       "
             f"🟢{stats.get('green',0)} 🟡{stats.get('yellow',0)} "
             f"🟠{stats.get('orange',0)} 🔴{stats.get('red',0)}")
        info(f"Tempo OCR:           {elapsed:.2f}s")

        if page_dict.get('requires_review'):
            warning("Revisione consigliata (confidence < 85%)")

        step(2, "Creazione file .odx...")
        ocr_layer = {
            "odxo_version": "0.1",
            "total_pages": 1,
            "pages": [page_dict]
        }

        writer = ODXWriter()
        writer.set_meta(
            title=src.stem,
            lang=lang,
            page_count=1,
            source_format="image"
        )
        writer.set_text(text or "(testo OCR)")
        writer.set_semantic_from_text(text or src.stem, lang=lang)
        writer.set_ocr_raw(
            json.dumps(ocr_layer, ensure_ascii=False).encode('utf-8')
        )
        write_stats = writer.save(output)

        success(f"File ODX con OCR creato: {output}")
        info(f"Dimensione: {write_stats['file_size_bytes']:,} byte")

    except Exception as e:
        error(f"OCR fallito: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)


def cmd_diff(args):
    """Mostra la cronologia delle versioni di un documento .odx."""
    print_header(f"ODX DIFF — {Path(args.file).name}")

    try:
        reader = ODXReader(args.file)
        history = reader.get_diff_history()
    except Exception as e:
        error(f"Impossibile leggere il file: {e}")
        sys.exit(1)

    if not history:
        info("Nessun layer /diff presente in questo documento.")
        info("Il versioning viene abilitato automaticamente quando si modifica")
        info("il documento con un editor ODX-compatibile.")
        return

    commits = history.get('commits', [])
    info(f"Trovati {len(commits)} commit nel documento")

    print()
    for commit in commits:
        cid = commit.get('id', '?')[:8]
        ts  = commit.get('timestamp', '?')[:19]
        author = commit.get('author_display', 'anonimo')
        msg  = commit.get('message', '(nessun messaggio)')
        patches = commit.get('patches', [])
        parent = commit.get('parent', None)

        parent_str = f"← {parent[:8]}" if parent else "(baseline)"
        print(f"  ● {cid}  {ts}  [{author}]  {parent_str}")
        print(f"    {msg}")
        if patches:
            for p in patches[:3]:
                print(f"    {p.get('op','?'):8} {p.get('layer','?')}: "
                      f"{p.get('path','?')}")
            if len(patches) > 3:
                print(f"    ... e altri {len(patches)-3} cambiamenti")
        print()


# ─────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='odx',
        description='ODX — Open Document eXtended · Tool CLI v0.1',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  odx info documento.odx
  odx validate documento.odx
  odx convert documento.pdf
  odx convert documento.pdf output.odx --lang it
  odx convert documento.odx output.pdf
  odx extract documento.odx
  odx extract documento.odx -o testo.txt
  odx new "Il mio documento" --lang it --author "Marco R."
  odx ocr immagine.png --lang ita
  odx diff documento.odx
        """
    )
    parser.add_argument('--version', action='version', version='ODX CLI 0.1')

    sub = parser.add_subparsers(dest='command', metavar='comando')
    sub.required = True

    # info
    p_info = sub.add_parser('info', help='Mostra metadati e struttura')
    p_info.add_argument('file', help='File .odx')

    # validate
    p_val = sub.add_parser('validate', help='Valida conformità alla specifica')
    p_val.add_argument('file', help='File .odx')

    # extract
    p_ext = sub.add_parser('extract', help='Estrai testo puro')
    p_ext.add_argument('file', help='File .odx')
    p_ext.add_argument('-o', '--output', help='File di output (default: stdout)')

    # convert
    p_conv = sub.add_parser('convert', help='Converti PDF↔ODX')
    p_conv.add_argument('input', help='File sorgente (.pdf o .odx)')
    p_conv.add_argument('output', nargs='?', help='File destinazione (opzionale)')
    p_conv.add_argument('--lang', help='Lingua (es. it, en, fr)')
    p_conv.add_argument('--ocr', action='store_true',
                         help='Esegui OCR sulle immagini trovate nel PDF')

    # new
    p_new = sub.add_parser('new', help='Crea nuovo documento .odx')
    p_new.add_argument('title', help='Titolo del documento')
    p_new.add_argument('--lang', default='it', help='Lingua (default: it)')
    p_new.add_argument('--author', help='Nome autore')
    p_new.add_argument('--text', help='Testo del documento')
    p_new.add_argument('--stdin', action='store_true',
                        help='Leggi testo da stdin')
    p_new.add_argument('-o', '--output', help='Nome file output')

    # ocr
    p_ocr = sub.add_parser('ocr', help='OCR su immagine → .odx')
    p_ocr.add_argument('image', help='Immagine sorgente (PNG, JPG, TIFF)')
    p_ocr.add_argument('--lang', default='eng', help='Lingua OCR (default: eng)')
    p_ocr.add_argument('-o', '--output', help='Nome file .odx output')

    # diff
    p_diff = sub.add_parser('diff', help='Cronologia versioni del documento')
    p_diff.add_argument('file', help='File .odx')

    return parser


def main():
    print_header("ODX — Open Document eXtended · CLI v0.1")

    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        'info':     cmd_info,
        'validate': cmd_validate,
        'extract':  cmd_extract,
        'convert':  cmd_convert,
        'new':      cmd_new,
        'ocr':      cmd_ocr,
        'diff':     cmd_diff,
    }

    handler = dispatch.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
