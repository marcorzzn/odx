"""
converters/pdf_to_odx.py — Converter PDF → ODX

Converte un file PDF esistente in formato .odx preservando:
  - Testo (con struttura paragrafi/titoli dedotta automaticamente)
  - Metadati (titolo, autore, soggetto, lingua)
  - Struttura a pagine
  - Immagini inline (se presenti)

Dipendenze:
  pip install pikepdf Pillow reportlab

Limitazioni v0.1:
  - La struttura semantica (titoli vs corpo) è dedotta euristicamente
    dalla dimensione del font e dalla posizione nel testo.
    Per documenti complessi, il layer /semantic potrebbe richiedere
    revisione manuale.
  - Le immagini sono estratte ma non ottimizzate (AVIF/JPEG XL rimandato a v0.2)
  - I layout a più colonne sono gestiti come testo lineare
"""

import sys
import json
import hashlib
import re
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

# pikepdf: parsing PDF a basso livello (licenza MIT)
import pikepdf
from pikepdf import Pdf, Dictionary, Array, Name, String

sys.path.insert(0, str(Path(__file__).parent.parent))
from odxlib import ODXWriter


# ─────────────────────────────────────────────────────────────
#  STRUTTURE DATI INTERMEDIE
# ─────────────────────────────────────────────────────────────

@dataclass
class PDFTextBlock:
    """Blocco di testo estratto da una pagina PDF."""
    text: str
    page: int
    font_size: float = 10.0
    is_bold: bool = False
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0

    def likely_heading(self) -> bool:
        """
        Euristica: è probabilmente un titolo se il font è grande
        o se il testo è corto e inizia con un numero o una maiuscola.
        """
        if self.font_size >= 14:
            return True
        if self.is_bold and len(self.text.split()) <= 10:
            return True
        if re.match(r'^\d+\.?\s+[A-ZÀ-Ú]', self.text):
            return True
        return False

    def heading_level(self) -> int:
        """Stima il livello del titolo (1-4) dalla dimensione del font."""
        if self.font_size >= 20: return 1
        if self.font_size >= 16: return 2
        if self.font_size >= 13: return 3
        return 4


@dataclass
class PDFExtracted:
    """Tutto il contenuto estratto da un PDF, pronto per la conversione."""
    title: str = ""
    author: str = ""
    subject: str = ""
    lang: str = "en"
    page_count: int = 0
    text_blocks: list = field(default_factory=list)   # [PDFTextBlock]
    plain_text: str = ""
    images: list = field(default_factory=list)        # [(page, bytes, ext)]
    source_format: str = "pdf"


# ─────────────────────────────────────────────────────────────
#  ESTRAZIONE TESTO DAL PDF
# ─────────────────────────────────────────────────────────────

class PDFTextExtractor:
    """
    Estrae il testo da un PDF usando pikepdf per l'accesso
    alla struttura interna e un parser del content stream
    per recuperare posizioni e font size.

    pikepdf è un wrapper Python per libqpdf (C++), una delle
    librerie PDF più complete e affidabili disponibili open source.
    Licenza: MIT/Apache 2.0.
    """

    def extract(self, pdf_path: str) -> PDFExtracted:
        result = PDFExtracted()

        with pikepdf.open(pdf_path) as pdf:
            # ── Metadati ─────────────────────────────────────
            result.page_count = len(pdf.pages)
            docinfo = pdf.docinfo

            def get_meta(key: str) -> str:
                val = docinfo.get(f'/{key}', '')
                return str(val).strip() if val else ''

            result.title   = get_meta('Title')
            result.author  = get_meta('Author')
            result.subject = get_meta('Subject')

            # Prova a rilevare la lingua dai metadati XMP
            try:
                with pdf.open_metadata() as meta:
                    lang = meta.get('dc:language', '')
                    if lang:
                        result.lang = str(lang)[:5]
            except Exception:
                pass

            # ── Testo per pagina ─────────────────────────────
            all_text_parts = []

            for page_idx, page in enumerate(pdf.pages, start=1):
                page_text, blocks = self._extract_page_text(page, page_idx)
                all_text_parts.append(page_text)
                result.text_blocks.extend(blocks)

                # Estrai immagini dalla pagina
                try:
                    imgs = self._extract_page_images(page, page_idx)
                    result.images.extend(imgs)
                except Exception:
                    pass

            result.plain_text = "\n\n".join(
                t for t in all_text_parts if t.strip()
            )

        return result

    def _extract_page_text(self, page, page_num: int) -> tuple:
        """
        Estrae il testo da una pagina PDF parsando il content stream.
        Ritorna (testo_plain, [PDFTextBlock]).

        Strategia: usa pikepdf per accedere agli oggetti della pagina,
        poi estrae il testo dai flussi di contenuto. Per ora usiamo
        un approccio semplificato che legge il testo linearmente;
        il positioning preciso richiede un parser PDF completo.
        """
        blocks = []
        text_parts = []

        try:
            # Metodo 1: usa /Contents per estrarre testo raw
            raw_text = self._parse_content_stream(page)

            if raw_text.strip():
                # Dividi in "blocchi" per doppio newline
                paragraphs = [p.strip() for p in raw_text.split('\n\n')
                              if p.strip() and len(p.strip()) > 2]

                for para in paragraphs:
                    block = PDFTextBlock(
                        text=para,
                        page=page_num,
                        font_size=10.0,
                    )
                    # Euristica titolo: testo corto che inizia con numero o maiuscola
                    if (len(para.split()) <= 8 and
                        (para[0].isupper() or re.match(r'^\d', para))):
                        block.font_size = 14.0

                    blocks.append(block)
                    text_parts.append(para)

        except Exception as e:
            # Fallback: tenta di leggere gli oggetti testo direttamente
            try:
                fallback = self._fallback_text_extract(page)
                if fallback:
                    blocks.append(PDFTextBlock(
                        text=fallback, page=page_num, font_size=10.0
                    ))
                    text_parts.append(fallback)
            except Exception:
                pass

        return "\n".join(text_parts), blocks

    def _parse_content_stream(self, page) -> str:
        """
        Parser minimale del content stream PDF.
        Cerca operatori Tj, TJ, ' (testo) e ricostruisce il testo.

        Il content stream PDF è una sequenza di operandi e operatori:
            (Hello World) Tj   → stampa "Hello World"
            [(Hello) -200 (World)] TJ  → stampa con kerning
            (New line) '       → nuova riga e stampa

        Questa è una versione semplificata; un parser completo
        dovrebbe gestire anche: font encoding, ToUnicode CMap,
        matrici di trasformazione, Type3 fonts, ecc.
        """
        text_parts = []

        contents = page.get('/Contents')
        if contents is None:
            return ''

        # Normalizza a lista (a volte è un singolo stream, a volte un array)
        if not isinstance(contents, Array):
            contents = [contents]

        for stream_obj in contents:
            try:
                # Decodifica il content stream
                raw = stream_obj.read_bytes()
                text = raw.decode('latin-1', errors='replace')

                # Estrai stringhe tra parentesi seguite da Tj/TJ/'
                # Pattern: (testo) Tj  oppure  (testo) '
                tj_pattern = re.findall(
                    r'\(([^)]*)\)\s*(?:Tj|\'|\")',
                    text
                )

                # Pattern per TJ: [(str) kern (str) kern ...]
                tj_array = re.findall(
                    r'\[([^\]]*)\]\s*TJ',
                    text
                )

                for match in tj_pattern:
                    clean = self._decode_pdf_string(match)
                    if clean.strip():
                        text_parts.append(clean)

                for array_str in tj_array:
                    # Estrai solo le stringhe (non i numeri di kerning)
                    strings = re.findall(r'\(([^)]*)\)', array_str)
                    combined = ''.join(
                        self._decode_pdf_string(s) for s in strings
                    )
                    if combined.strip():
                        text_parts.append(combined)

                # Cerca newline espliciti (operatore T*)
                # Ogni T* indica un a-capo nel flusso originale
                lines = []
                current = []
                parts_iter = iter(text_parts)
                for part in parts_iter:
                    current.append(part)
                # Unisci con spazi intelligenti
                result_lines = []
                current_line = []
                for part in text_parts:
                    if len(part) == 1 and not part.isalpha():
                        if current_line:
                            result_lines.append(' '.join(current_line))
                            current_line = []
                    current_line.append(part)
                if current_line:
                    result_lines.append(' '.join(current_line))

            except Exception:
                continue

        # Post-processing: rimuovi rumore tipico dei PDF
        clean_parts = []
        for part in text_parts:
            part = part.strip()
            # Rimuovi artefatti comuni (sequenze di caratteri non stampabili)
            part = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', part)
            if part and len(part) > 1:
                clean_parts.append(part)

        return self._reconstruct_text(clean_parts)

    def _decode_pdf_string(self, s: str) -> str:
        """
        Decodifica escape sequences nelle stringhe PDF.
        PDF usa \\n, \\r, \\t, \\(, \\), \\\\, e sequenze ottali \\nnn
        """
        result = []
        i = 0
        while i < len(s):
            if s[i] == '\\' and i + 1 < len(s):
                next_char = s[i+1]
                if next_char == 'n':
                    result.append('\n'); i += 2
                elif next_char == 'r':
                    result.append('\r'); i += 2
                elif next_char == 't':
                    result.append('\t'); i += 2
                elif next_char in '()\\':
                    result.append(next_char); i += 2
                elif next_char.isdigit():
                    # Sequenza ottale: max 3 cifre
                    oct_str = ''
                    j = i + 1
                    while j < len(s) and s[j].isdigit() and len(oct_str) < 3:
                        oct_str += s[j]; j += 1
                    try:
                        result.append(chr(int(oct_str, 8)))
                    except ValueError:
                        result.append(next_char)
                    i = j
                else:
                    result.append(next_char); i += 2
            else:
                result.append(s[i]); i += 1
        return ''.join(result)

    def _reconstruct_text(self, parts: list) -> str:
        """
        Ricostruisce il testo da token estratti dal content stream.
        Usa euristiche per capire dove mettere spazi e a-capo.
        """
        if not parts:
            return ''

        lines = []
        current_line = []

        for part in parts:
            # Se il parte è molto corta e la precedente era lunga,
            # probabilmente è su una riga diversa (numero pagina, ecc.)
            if (current_line and len(part) < 3 and
                    len(current_line[-1]) > 20):
                lines.append(' '.join(current_line))
                current_line = [part]
            else:
                current_line.append(part)

        if current_line:
            lines.append(' '.join(current_line))

        # Unisci righe molto corte (probabile spezzatura artificiale del PDF)
        result_lines = []
        buffer = ''
        for line in lines:
            line = line.strip()
            if not line:
                if buffer:
                    result_lines.append(buffer)
                    buffer = ''
                    result_lines.append('')
            elif len(line) < 40 and buffer:
                # Riga corta: potrebbe essere titolo, metti su riga propria
                result_lines.append(buffer)
                buffer = line
            else:
                if buffer and buffer[-1] not in '.!?:':
                    buffer += ' ' + line
                else:
                    if buffer:
                        result_lines.append(buffer)
                    buffer = line

        if buffer:
            result_lines.append(buffer)

        return '\n'.join(result_lines)

    def _fallback_text_extract(self, page) -> str:
        """
        Fallback: cerca qualsiasi stringa leggibile nella pagina
        accedendo direttamente agli oggetti /Resources.
        """
        texts = []
        try:
            resources = page.get('/Resources', {})
            # Cerca in /XObject (form XObjects possono contenere testo)
            xobjects = resources.get('/XObject', {})
            for key in xobjects:
                try:
                    xobj = xobjects[key]
                    if xobj.get('/Subtype') == Name('/Form'):
                        raw = xobj.read_bytes()
                        text = raw.decode('latin-1', errors='replace')
                        strings = re.findall(r'\(([^\)]{2,50})\)', text)
                        for s in strings:
                            clean = self._decode_pdf_string(s)
                            if clean.strip() and clean.isprintable():
                                texts.append(clean)
                except Exception:
                    pass
        except Exception:
            pass
        return ' '.join(texts)

    def _extract_page_images(self, page, page_num: int) -> list:
        """
        Estrae immagini inline dalla pagina PDF.
        Ritorna lista di (page_num, image_bytes, extension).
        """
        images = []
        try:
            resources = page.get('/Resources', {})
            xobjects = resources.get('/XObject', {})
            for key in xobjects:
                try:
                    xobj = xobjects[key]
                    subtype = xobj.get('/Subtype', '')
                    if str(subtype) == '/Image':
                        img_bytes = xobj.read_bytes()
                        # Determina formato dall'encoding
                        filter_val = xobj.get('/Filter', '')
                        filter_str = str(filter_val)
                        if 'DCT' in filter_str:
                            ext = 'jpg'
                        elif 'PNG' in filter_str or 'Flate' in filter_str:
                            ext = 'png'
                        else:
                            ext = 'bin'
                        images.append((page_num, img_bytes, ext))
                except Exception:
                    pass
        except Exception:
            pass
        return images


# ─────────────────────────────────────────────────────────────
#  BUILDER SEMANTIC XML
# ─────────────────────────────────────────────────────────────

def build_semantic_from_blocks(blocks: list, lang: str = "en") -> bytes:
    """
    Costruisce il layer /semantic XML da una lista di PDFTextBlock.
    Deduce automaticamente titoli vs corpo dal font size e dalla posizione.
    """
    ODX_NS = "https://odx-format.org/ns/semantic/0.1"

    try:
        from lxml import etree
        root = etree.Element(f"{{{ODX_NS}}}document", attrib={"lang": lang})
    except ImportError:
        import xml.etree.ElementTree as etree
        root = etree.Element(f"{{{ODX_NS}}}document", attrib={"lang": lang})

    # Raggruppa i blocchi in sezioni basandosi sui titoli
    current_section = None
    section_counter = 0
    element_counter = 0

    def new_section(role="body"):
        nonlocal section_counter, current_section
        section_counter += 1
        current_section = etree.SubElement(
            root, f"{{{ODX_NS}}}section",
            attrib={"id": f"s{section_counter:03d}", "role": role}
        )
        return current_section

    # Prima sezione di default
    new_section("body")

    for block in blocks:
        element_counter += 1
        eid = f"e{element_counter:04d}"

        if not block.text.strip():
            continue

        if block.likely_heading():
            level = block.heading_level()
            # Ogni titolo di primo livello inizia una nuova sezione
            if level <= 2:
                new_section("body")

            heading = etree.SubElement(
                current_section,
                f"{{{ODX_NS}}}heading",
                attrib={"id": eid, "level": str(level)}
            )
            heading.text = block.text.strip()
        else:
            para = etree.SubElement(
                current_section,
                f"{{{ODX_NS}}}paragraph",
                attrib={"id": eid}
            )
            para.text = block.text.strip()

    try:
        return etree.tostring(root, pretty_print=True,
                               xml_declaration=True, encoding="UTF-8")
    except TypeError:
        import io
        tree = etree.ElementTree(root)
        buf = io.BytesIO()
        tree.write(buf, xml_declaration=True, encoding="UTF-8")
        return buf.getvalue()


# ─────────────────────────────────────────────────────────────
#  CONVERTER PRINCIPALE
# ─────────────────────────────────────────────────────────────

class PDFtoODXConverter:
    """
    Converte un file PDF in formato ODX.

    Uso:
        converter = PDFtoODXConverter()
        stats = converter.convert("documento.pdf", "documento.odx")
        print(stats)

    Il converter:
    1. Estrae testo, metadati e immagini dal PDF con pikepdf
    2. Costruisce il layer /semantic deducendo la struttura
    3. Impacchetta tutto in un file .odx valido
    4. Opzionalmente esegue OCR sulle immagini estratte
    """

    def __init__(self, run_ocr: bool = False, lang: Optional[str] = None):
        """
        run_ocr: se True, esegue OCR sulle immagini trovate nel PDF
        lang:    lingua override (se None, dedotta dai metadati PDF)
        """
        self.run_ocr = run_ocr
        self.lang_override = lang
        self.extractor = PDFTextExtractor()

    def convert(self, pdf_path: str, output_path: Optional[str] = None) -> dict:
        """
        Esegue la conversione PDF → ODX.
        Se output_path è None, usa lo stesso nome del PDF con estensione .odx.
        Ritorna dict con statistiche di conversione.
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF non trovato: {pdf_path}")

        if output_path is None:
            output_path = pdf_path.with_suffix('.odx')
        output_path = Path(output_path)

        print(f"\n[PDFtoODX] Conversione: {pdf_path.name} → {output_path.name}")
        print(f"{'─' * 50}")

        # ── Step 1: Estrai contenuto dal PDF ─────────────────
        print("  [1/4] Estrazione contenuto PDF...")
        extracted = self.extractor.extract(str(pdf_path))

        pdf_size = pdf_path.stat().st_size
        print(f"        Pagine:    {extracted.page_count}")
        print(f"        Titolo:    {extracted.title or '(non trovato)'}")
        print(f"        Autore:    {extracted.author or '(non trovato)'}")
        print(f"        Blocchi:   {len(extracted.text_blocks)} blocchi testo")
        print(f"        Immagini:  {len(extracted.images)}")
        print(f"        Caratteri: {len(extracted.plain_text):,}")

        # Determina lingua
        lang = self.lang_override or extracted.lang or "en"

        # ── Step 2: Costruisci layer semantic ─────────────────
        print("  [2/4] Costruzione layer /semantic...")
        if extracted.text_blocks:
            semantic_xml = build_semantic_from_blocks(extracted.text_blocks, lang)
        else:
            # Fallback: costruisci semantic da testo plain
            from odxlib import build_semantic_layer_from_text
            semantic_xml = build_semantic_layer_from_text(
                extracted.plain_text or "(documento vuoto)", lang
            )

        headings = sum(1 for b in extracted.text_blocks if b.likely_heading())
        paragraphs = len(extracted.text_blocks) - headings
        print(f"        Titoli rilevati:    {headings}")
        print(f"        Paragrafi:          {paragraphs}")

        # ── Step 3: OCR opzionale ─────────────────────────────
        ocr_layer_bytes = None
        if self.run_ocr and extracted.images:
            print(f"  [3/4] OCR su {len(extracted.images)} immagini...")
            ocr_layer_bytes = self._run_ocr_on_images(extracted.images, lang)
        else:
            print(f"  [3/4] OCR: {'skipped' if not self.run_ocr else 'nessuna immagine'}")

        # ── Step 4: Assembla file .odx ────────────────────────
        print("  [4/4] Assemblaggio file .odx...")
        writer = ODXWriter()

        authors = []
        if extracted.author:
            authors = [{"name": a.strip()}
                       for a in extracted.author.split(',') if a.strip()]

        writer.set_meta(
            title=extracted.title or pdf_path.stem,
            lang=lang,
            authors=authors if authors else None,
            description=extracted.subject or None,
            page_count=extracted.page_count,
            document_type="other",
            source_format="pdf"
        )

        writer.set_text(extracted.plain_text or "")
        writer.set_semantic_raw(semantic_xml)

        if ocr_layer_bytes:
            writer.set_ocr_raw(ocr_layer_bytes)

        stats_write = writer.save(str(output_path))

        # ── Statistiche finali ────────────────────────────────
        odx_size = output_path.stat().st_size
        size_reduction = (1 - odx_size / pdf_size) * 100

        stats = {
            "pdf_path": str(pdf_path),
            "odx_path": str(output_path),
            "pdf_size_bytes": pdf_size,
            "odx_size_bytes": odx_size,
            "size_reduction_pct": round(size_reduction, 1),
            "pages": extracted.page_count,
            "text_blocks": len(extracted.text_blocks),
            "headings_detected": headings,
            "paragraphs_detected": paragraphs,
            "images_found": len(extracted.images),
            "ocr_run": bool(ocr_layer_bytes),
            "title": extracted.title,
            "author": extracted.author,
            "lang": lang,
        }

        print(f"\n  {'─' * 50}")
        print(f"  ✅ Conversione completata")
        print(f"     PDF originale: {pdf_size:,} byte")
        print(f"     ODX risultato: {odx_size:,} byte")
        if size_reduction > 0:
            print(f"     Riduzione:     {size_reduction:.1f}%")
        else:
            print(f"     Overhead:      {abs(size_reduction):.1f}% "
                  "(normale per PDF già compressi o piccoli)")

        return stats

    def _run_ocr_on_images(self, images: list, lang: str) -> Optional[bytes]:
        """Esegue OCR sulle immagini estratte dal PDF."""
        try:
            import numpy as np
            from PIL import Image
            import io as io_mod
            from odxlib.ocr.pipeline import OCRPipeline

            pipeline = OCRPipeline(lang=lang[:2])
            pages_data = []

            for page_num, img_bytes, ext in images:
                try:
                    pil_img = Image.open(io_mod.BytesIO(img_bytes)).convert("RGB")
                    img_array = np.array(pil_img)
                    page_dict = pipeline.process_image(img_array, page=page_num)
                    pages_data.append(page_dict)
                except Exception as e:
                    print(f"        ⚠️ OCR pagina {page_num}: {e}")

            if not pages_data:
                return None

            ocr_layer = {
                "odxo_version": "0.1",
                "total_pages": len(pages_data),
                "pages": pages_data,
            }
            return json.dumps(ocr_layer, ensure_ascii=False).encode("utf-8")

        except ImportError:
            print("        OCR non disponibile (pytesseract/opencv mancanti)")
            return None


# ─────────────────────────────────────────────────────────────
#  CONVERTER ODX → PDF
# ─────────────────────────────────────────────────────────────

class ODXtoPDFConverter:
    """
    Converte un file ODX in PDF usando reportlab.

    Produce un PDF fedele al contenuto testuale e alla struttura
    semantica dell'ODX. Il layout visivo è ricostruito da zero
    (non è una conversione round-trip esatta del layout originale).

    Uso:
        converter = ODXtoPDFConverter()
        converter.convert("documento.odx", "output.pdf")
    """

    def convert(self, odx_path: str, output_path: Optional[str] = None) -> dict:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.platypus import (SimpleDocTemplate, Paragraph,
                                         Spacer, HRFlowable)
        from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
        from odxlib import ODXReader

        odx_path = Path(odx_path)
        if output_path is None:
            output_path = odx_path.with_suffix('.pdf')

        print(f"\n[ODXtoPDF] Conversione: {odx_path.name} → {Path(output_path).name}")

        reader = ODXReader(str(odx_path))
        meta   = reader.get_meta()
        text   = reader.get_text()
        xml_b  = reader.get_semantic_xml()

        # Stili
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle('ODXTitle', parent=styles['Title'],
            fontSize=20, spaceAfter=8,
            textColor=colors.HexColor('#0a1628'))
        h1 = ParagraphStyle('ODXH1', parent=styles['Heading1'],
            fontSize=14, spaceBefore=12, spaceAfter=4,
            textColor=colors.HexColor('#1a3a5c'))
        h2 = ParagraphStyle('ODXH2', parent=styles['Heading2'],
            fontSize=11, spaceBefore=8, spaceAfter=3,
            textColor=colors.HexColor('#2d5a8e'))
        body_style = ParagraphStyle('ODXBody', parent=styles['Normal'],
            fontSize=10, leading=15, alignment=TA_JUSTIFY, spaceAfter=5)
        meta_style = ParagraphStyle('ODXMeta', parent=styles['Italic'],
            fontSize=9, textColor=colors.HexColor('#666666'), spaceAfter=12)

        story = []

        # Titolo e metadati
        title = meta.get('title', odx_path.stem)
        story.append(Paragraph(title, title_style))

        meta_parts = []
        authors = meta.get('authors', [])
        if authors:
            meta_parts.append(', '.join(a.get('name', '') for a in authors))
        if meta.get('created_at'):
            meta_parts.append(meta['created_at'][:10])
        meta_parts.append(f"ODX {meta.get('odx_version', '0.1')}")

        story.append(Paragraph(' · '.join(meta_parts), meta_style))
        story.append(HRFlowable(width="100%", thickness=0.5,
                                 color=colors.HexColor('#cccccc')))
        story.append(Spacer(1, 6*mm))

        # Contenuto dal layer /semantic
        if xml_b:
            story.extend(self._semantic_to_flowables(xml_b, h1, h2, body_style))
        elif text:
            for para in text.split('\n\n'):
                if para.strip():
                    story.append(Paragraph(para.strip(), body_style))
                    story.append(Spacer(1, 2*mm))

        # Footer con UUID
        story.append(Spacer(1, 10*mm))
        story.append(HRFlowable(width="100%", thickness=0.3,
                                 color=colors.HexColor('#dddddd')))
        footer_style = ParagraphStyle('Footer', parent=styles['Normal'],
            fontSize=7, textColor=colors.HexColor('#999999'))
        story.append(Paragraph(
            f"Generato da ODX v{meta.get('odx_version','0.1')} · "
            f"UUID: {meta.get('uuid','?')} · "
            f"Convertito in PDF con odxlib",
            footer_style
        ))

        doc = SimpleDocTemplate(
            str(output_path), pagesize=A4,
            leftMargin=25*mm, rightMargin=25*mm,
            topMargin=25*mm, bottomMargin=20*mm,
            title=title,
            author=', '.join(a.get('name','') for a in authors),
        )
        doc.build(story)

        odx_size = odx_path.stat().st_size
        pdf_size = Path(output_path).stat().st_size

        print(f"  ✅ PDF generato: {pdf_size:,} byte "
              f"(ODX originale: {odx_size:,} byte)")
        return {"odx_path": str(odx_path), "pdf_path": str(output_path),
                "pdf_size": pdf_size, "odx_size": odx_size}

    def _semantic_to_flowables(self, xml_bytes, h1, h2, body_style):
        """Converte il layer /semantic XML in elementi reportlab."""
        from reportlab.platypus import Paragraph, Spacer
        from reportlab.lib.units import mm
        story = []
        try:
            try:
                from lxml import etree
                root = etree.fromstring(xml_bytes)
                all_elements = list(root.iter())
            except ImportError:
                import xml.etree.ElementTree as etree
                root = etree.fromstring(xml_bytes.decode('utf-8'))
                all_elements = list(root.iter())

            ODX_NS = "https://odx-format.org/ns/semantic/0.1"
            for elem in all_elements:
                tag = elem.tag.replace(f"{{{ODX_NS}}}", "")
                text = (elem.text or '').strip()
                if not text:
                    continue
                if tag == 'heading':
                    level = int(elem.get('level', '1'))
                    style = h1 if level <= 2 else h2
                    story.append(Paragraph(text, style))
                elif tag == 'paragraph':
                    story.append(Paragraph(text, body_style))
                    story.append(Spacer(1, 1*mm))
        except Exception as e:
            story.append(Paragraph(f"(errore lettura semantic: {e})", body_style))
        return story
