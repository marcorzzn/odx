"""
odxlib/ocr/pipeline.py — Orchestratore della pipeline OCR per ODX

Questo modulo è il punto di ingresso per tutto l'OCR.
Riceve un'immagine (o un PDF), determina il tipo di sorgente,
sceglie gli engine giusti, e produce un layer /ocr completo
pronto per essere scritto nel file .odx.

Workflow per tipo di sorgente:

  SCAN (testo stampato/scansionato)
    → preprocess_scan()
    → TesseractEngine (engine principale)
    → EasyOCREngine (se disponibile, per consensus)
    → merge_results()
    → ODX OCR layer JSON

  PHOTO (foto da smartphone)
    → preprocess_photo()
    → TesseractEngine o EasyOCREngine
    → ODX OCR layer JSON

  HANDWRITING (testo manoscritto)
    → preprocess_handwriting()
    → TrOCREngine
    → ODX OCR layer JSON

  BORN_DIGITAL (PDF, screenshot)
    → estrazione diretta del testo
    → nessun OCR necessario
    → layer /ocr vuoto o minimale

Uso:
    from odxlib.ocr.pipeline import OCRPipeline

    pipeline = OCRPipeline(lang="ita")
    ocr_layer = pipeline.process_image("scan.png", page=1)
    print(ocr_layer["full_text_extracted"])
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional, Union
import numpy as np
from PIL import Image

# Aggiungi path per import locale
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from odxlib.ocr.preprocess import (
    preprocess_scan, preprocess_photo, preprocess_handwriting,
    detect_source_type, load_image, to_grayscale
)
from odxlib.ocr.engines import (
    TesseractEngine, EasyOCREngine, TrOCREngine,
    merge_results, OCRPageResult
)


# Confidenze soglia per il report visivo ODX (specifica §6.2)
CONF_HIGH   = 0.90   # verde
CONF_MEDIUM = 0.70   # giallo
CONF_LOW    = 0.50   # arancio
# sotto CONF_LOW → rosso


def confidence_color(conf: float) -> str:
    """Mappa confidence → colore per l'editor visivo ODX."""
    if conf >= CONF_HIGH:   return "green"
    if conf >= CONF_MEDIUM: return "yellow"
    if conf >= CONF_LOW:    return "orange"
    return "red"


class OCRPipeline:
    """
    Pipeline OCR completa per il formato ODX.

    Inizializzazione:
        pipeline = OCRPipeline(
            lang="ita",          # lingua principale del documento
            use_consensus=True,  # usa sia Tesseract che EasyOCR (più lento, più accurato)
            use_trocr=False,     # abilita TrOCR per handwriting (richiede ~300MB)
            source_type=None,    # None = auto-detect
        )
    """

    def __init__(self,
                 lang: str = "eng",
                 use_consensus: bool = False,
                 use_trocr: bool = False,
                 source_type: Optional[str] = None):

        self.lang = lang
        self.use_consensus = use_consensus
        self.use_trocr = use_trocr
        self.forced_source_type = source_type

        # Mappa lingua ODX/ISO → lingua Tesseract
        # Tesseract usa codici ISO 639-2 (3 lettere)
        lang_map = {
            "it": "ita", "en": "eng", "fr": "fra",
            "de": "deu", "es": "spa", "pt": "por",
            "ru": "rus", "zh": "chi_sim", "ja": "jpn",
            "ar": "ara", "la": "lat",
        }
        tess_lang = lang_map.get(lang[:2], lang)

        # Inizializza gli engine (lazy: i modelli vengono caricati solo quando usati)
        self.tesseract = TesseractEngine(lang=tess_lang)

        if use_consensus:
            # EasyOCR usa codici ISO 639-1 (2 lettere)
            easy_langs = [lang[:2], "en"] if lang[:2] != "en" else ["en"]
            self.easyocr = EasyOCREngine(langs=easy_langs)
        else:
            self.easyocr = None

        if use_trocr:
            self.trocr = TrOCREngine()
        else:
            self.trocr = None

        print(f"[OCRPipeline] Inizializzata — lingua: {lang}")
        print(f"  Tesseract: {'✅' if self.tesseract.available else '❌ non disponibile'}")
        print(f"  EasyOCR:   {'✅ (consensus mode)' if use_consensus else '⏭ (disabilitato)'}")
        print(f"  TrOCR:     {'✅ (handwriting mode)' if use_trocr else '⏭ (disabilitato)'}")

    def process_image(self,
                      source: Union[str, np.ndarray, Image.Image],
                      page: int = 1) -> dict:
        """
        Processa una singola immagine e ritorna il dizionario
        del layer /ocr per quella pagina, pronto per essere
        inserito nel file .odx.

        source: path (str), array numpy, o oggetto PIL Image
        page:   numero di pagina (per i bounding box)
        """
        # Determina tipo di sorgente
        if self.forced_source_type:
            src_type = self.forced_source_type
        elif isinstance(source, str):
            src_type = detect_source_type(source)
        else:
            src_type = "scan"  # default per array in memoria

        print(f"\n[OCRPipeline] Pagina {page} — sorgente rilevata: {src_type}")

        # ── BORN DIGITAL: nessun OCR necessario ──────────────
        if src_type == "born_digital":
            print("  → Testo digitale nativo, OCR non necessario")
            return self._empty_ocr_layer(page, "born_digital")

        # ── HANDWRITING: pipeline TrOCR ───────────────────────
        if src_type == "handwriting":
            if self.trocr and self.trocr.available:
                print("  → Pipeline handwriting (TrOCR)")
                prep = preprocess_handwriting(source)
                result = self.trocr.run(prep.image, page=page)
            else:
                print("  → Handwriting rilevato ma TrOCR non disponibile")
                print("    Fallback a Tesseract (accuratezza ridotta su manoscritto)")
                print("    Per attivare TrOCR: pip install transformers torch")
                prep = preprocess_scan(source)
                result = self.tesseract.run(prep.image, page=page)

            result.preprocessing_log = prep.preprocessing_log
            return self._build_ocr_layer(result, prep)

        # ── PHOTO: preprocessing aggressivo ───────────────────
        if src_type == "photo":
            print("  → Pipeline foto (CLAHE + deskew + Tesseract/EasyOCR)")
            prep = preprocess_photo(source)
        else:
            # SCAN (default)
            print("  → Pipeline scan (deskew + Sauvola + Tesseract)")
            prep = preprocess_scan(source)

        print(f"  Preprocessing: {' | '.join(prep.preprocessing_log)}")
        if abs(prep.deskew_angle_deg) > 0.1:
            print(f"  Deskew: {prep.deskew_angle_deg:+.2f}°")

        # ── OCR TESSERACT ─────────────────────────────────────
        result_tess = self.tesseract.run(prep.image, page=page)
        print(f"  Tesseract: {len(result_tess.words)} parole, "
              f"confidence media {result_tess.overall_confidence:.0%}")

        # ── CONSENSUS con EasyOCR (opzionale) ─────────────────
        if self.easyocr and self.easyocr.available:
            print("  EasyOCR: in esecuzione per consensus...")
            result_easy = self.easyocr.run(prep.image, page=page)
            print(f"  EasyOCR: {len(result_easy.words)} parole, "
                  f"confidence media {result_easy.overall_confidence:.0%}")

            result_final = merge_results(result_tess, result_easy)
            conflicts = sum(1 for w in result_final.words if w.conflict)
            print(f"  Consensus: {conflicts} conflitti rilevati "
                  f"({'⚠️ revisione consigliata' if conflicts > 0 else '✅ nessun conflitto'})")
        else:
            result_final = result_tess

        result_final.source_type = src_type
        result_final.preprocessing_log = prep.preprocessing_log

        return self._build_ocr_layer(result_final, prep)

    def process_pdf_page(self, pdf_path: str, page_num: int) -> dict:
        """
        Estrae e processa una pagina da un file PDF.
        Richiede: pip install pymupdf (fitz) — licenza AGPL
        o pip install pdf2image poppler-utils

        Usa pdf2image come fallback (più compatibile).
        """
        try:
            from pdf2image import convert_from_path
            images = convert_from_path(pdf_path,
                                        dpi=300,
                                        first_page=page_num,
                                        last_page=page_num)
            if not images:
                return self._empty_ocr_layer(page_num, "pdf-extraction-failed")

            pil_img = images[0]
            import numpy as np
            img_array = np.array(pil_img.convert("RGB"))
            return self.process_image(img_array, page=page_num)

        except ImportError:
            print("[OCRPipeline] pdf2image non installato.")
            print("  Installa con: pip install pdf2image")
            print("  E poppler: sudo apt install poppler-utils")
            return self._empty_ocr_layer(page_num, "pdf2image-unavailable")

    def process_document(self, source_path: str) -> dict:
        """
        Processa un documento intero (immagine singola o multi-pagina).
        Ritorna il dizionario completo del layer /ocr pronto per .odx.

        Per documenti multi-pagina (PDF, TIFF multi-frame):
        itera su tutte le pagine.
        """
        path = Path(source_path)
        suffix = path.suffix.lower()

        pages_data = []

        if suffix == ".pdf":
            try:
                import fitz  # PyMuPDF
                doc = fitz.open(source_path)
                print(f"[OCRPipeline] PDF con {doc.page_count} pagine")
                for i, page_obj in enumerate(doc, start=1):
                    mat = fitz.Matrix(300/72, 300/72)  # 300 DPI
                    pix = page_obj.get_pixmap(matrix=mat)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    page_dict = self.process_image(np.array(img), page=i)
                    pages_data.append(page_dict)
            except ImportError:
                pages_data.append(self.process_pdf_page(source_path, 1))
        else:
            # Immagine singola
            page_dict = self.process_image(source_path, page=1)
            pages_data.append(page_dict)

        # Calcola statistiche globali
        all_words = sum(len(p.get("words", [])) for p in pages_data)
        all_confs = [w["confidence"] for p in pages_data
                     for w in p.get("words", [])]
        overall = float(np.mean(all_confs)) if all_confs else 0.0
        low_conf = sum(1 for c in all_confs if c < 0.70)
        full_text = "\n\n".join(
            p.get("full_text_extracted", "") for p in pages_data
        ).strip()

        return {
            "odxo_version": "0.1",
            "source_file": str(path.name),
            "total_pages": len(pages_data),
            "total_words": all_words,
            "overall_confidence": round(overall, 4),
            "low_confidence_word_count": low_conf,
            "requires_review": overall < 0.85 or low_conf > all_words * 0.1,
            "full_text_all_pages": full_text,
            "pages": pages_data,
        }

    # ── Helper privati ──────────────────────────────────────────

    def _build_ocr_layer(self, result: OCRPageResult, prep) -> dict:
        """
        Converte un OCRPageResult nel dizionario JSON
        del layer /ocr del formato ODX.
        """
        page_dict = result.to_dict()

        # Aggiungi informazioni di preprocessing
        page_dict["preprocessing"] = {
            "deskew_angle_deg": round(prep.deskew_angle_deg, 3),
            "binarization": prep.binarization_method,
            "denoising_applied": prep.denoising_applied,
            "clahe_applied": prep.clahe_applied,
            "estimated_dpi": prep.estimated_dpi,
        }

        # Aggiungi colore confidence per ogni parola (per l'editor visivo)
        for word_dict in page_dict.get("words", []):
            word_dict["display_color"] = confidence_color(word_dict["confidence"])

        # Statistiche per tipo di confidence (utile per UI)
        words = page_dict.get("words", [])
        page_dict["confidence_stats"] = {
            "green":  sum(1 for w in words if w["confidence"] >= CONF_HIGH),
            "yellow": sum(1 for w in words if CONF_MEDIUM <= w["confidence"] < CONF_HIGH),
            "orange": sum(1 for w in words if CONF_LOW <= w["confidence"] < CONF_MEDIUM),
            "red":    sum(1 for w in words if w["confidence"] < CONF_LOW),
        }

        return page_dict

    def _empty_ocr_layer(self, page: int, reason: str) -> dict:
        return {
            "page": page,
            "source_type": reason,
            "words": [],
            "full_text_extracted": "",
            "overall_confidence": 1.0,
            "low_confidence_word_count": 0,
            "requires_review": False,
            "preprocessing": {},
            "confidence_stats": {"green": 0, "yellow": 0, "orange": 0, "red": 0},
        }
