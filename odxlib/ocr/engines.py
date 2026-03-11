"""
odxlib/ocr/engines.py — Engine OCR per il formato ODX

Implementa tre engine in una gerarchia progressiva:

  1. TesseractEngine  — testo stampato/scansionato (veloce, maturo)
  2. EasyOCREngine    — fallback multilingua, scene naturali
  3. TrOCREngine      — testo scritto a mano (modello transformer)

Ogni engine ritorna una lista di ODXWord con tutti i campi
necessari per popolare il layer /ocr del formato ODX.

Le dipendenze sono importate "lazy" (solo quando l'engine viene
effettivamente usato) in modo che il modulo sia importabile anche
se alcune librerie non sono installate.
"""

import json
from dataclasses import dataclass, field, asdict
from typing import Optional
import numpy as np
from PIL import Image


# ─────────────────────────────────────────────────────────────
#  STRUTTURE DATI CONDIVISE
# ─────────────────────────────────────────────────────────────

@dataclass
class BBox:
    """Bounding box di una parola nell'immagine originale."""
    x: int
    y: int
    w: int
    h: int
    page: int = 1

    def area(self) -> int:
        return self.w * self.h

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h, "page": self.page}


@dataclass
class WordAlternative:
    """Trascrizione alternativa con probabilità associata."""
    text: str
    prob: float


@dataclass
class ODXWord:
    """
    Rappresenta una parola riconosciuta da OCR.
    Struttura 1:1 con il JSON nel layer /ocr del formato ODX.
    """
    id: str
    text: str
    confidence: float          # 0.0 – 1.0
    engine: str                # "tesseract" | "easyocr" | "trocr" | "consensus"
    bbox: BBox
    alternatives: list = field(default_factory=list)  # [WordAlternative]
    conflict: bool = False     # True se due engine divergono su questa parola
    engine_results: dict = field(default_factory=dict)
    corrected: bool = False
    correction_source: Optional[str] = None
    line_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "confidence": round(self.confidence, 4),
            "engine": self.engine,
            "bbox": self.bbox.to_dict(),
            "alternatives": [{"text": a.text, "prob": round(a.prob, 4)}
                              for a in self.alternatives],
            "conflict": self.conflict,
            "engine_results": self.engine_results,
            "corrected": self.corrected,
            "correction_source": self.correction_source,
        }


@dataclass
class OCRPageResult:
    """Risultato completo OCR di una singola pagina."""
    page_number: int
    words: list           # [ODXWord]
    full_text: str        # testo concatenato nell'ordine di lettura
    engine_used: str
    overall_confidence: float
    low_confidence_count: int
    requires_review: bool
    source_type: str      # scan | photo | handwriting | born_digital
    preprocessing_log: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "page": self.page_number,
            "engine_used": self.engine_used,
            "source_type": self.source_type,
            "overall_confidence": round(self.overall_confidence, 4),
            "low_confidence_word_count": self.low_confidence_count,
            "requires_review": self.requires_review,
            "words": [w.to_dict() for w in self.words],
            "full_text_extracted": self.full_text,
            "preprocessing_log": self.preprocessing_log,
        }


# ─────────────────────────────────────────────────────────────
#  ENGINE 1: TESSERACT (testo stampato)
# ─────────────────────────────────────────────────────────────

class TesseractEngine:
    """
    Wrapper per Tesseract OCR 5.x con engine LSTM.

    Tesseract 5 usa un modello LSTM (rete neurale) invece
    del vecchio engine pattern-matching di Tesseract 4.
    Risultato: molto più accurato su font non standard e
    testo con leggera deformazione.

    Requisiti:
        pip install pytesseract
        apt install tesseract-ocr tesseract-ocr-ita  (Linux)
        brew install tesseract tesseract-lang         (macOS)
    """

    def __init__(self, lang: str = "eng"):
        """
        lang: codice lingua Tesseract (es. 'ita', 'eng', 'ita+eng')
        Se la lingua non è installata, Tesseract usa 'eng' come fallback.
        Installa lingue aggiuntive: apt install tesseract-ocr-ita
        """
        try:
            import pytesseract
            self._tess = pytesseract
            self.available = True
        except ImportError:
            self.available = False
            print("[TesseractEngine] WARNING: pytesseract non installato.")
            print("  Installa con: pip install pytesseract")
            return

        # Verifica che il binario tesseract sia disponibile
        try:
            version = pytesseract.get_tesseract_version()
            self.version = str(version)
        except Exception:
            self.available = False
            print("[TesseractEngine] WARNING: binario tesseract non trovato.")
            print("  Su Ubuntu: sudo apt install tesseract-ocr")
            return

        # Verifica lingua richiesta, fallback a 'eng'
        try:
            available_langs = pytesseract.get_languages()
            if lang not in available_langs and '+' not in lang:
                print(f"[TesseractEngine] Lingua '{lang}' non disponibile. "
                      f"Disponibili: {available_langs}")
                print(f"  Fallback a 'eng'. Per installare italiano:")
                print(f"  sudo apt install tesseract-ocr-ita")
                self.lang = "eng"
            else:
                self.lang = lang
        except Exception:
            self.lang = lang

        # Configurazione Tesseract:
        # --oem 3 = engine LSTM (più accurato)
        # --psm 3 = page segmentation automatica (layout completo)
        # --psm 6 = assumere blocco di testo uniforme (più veloce, meno accurato per layout complessi)
        self.config = "--oem 3 --psm 3"

    def run(self, image: np.ndarray, page: int = 1) -> OCRPageResult:
        """
        Esegue OCR su un'immagine preprocessata (grayscale, uint8).
        Ritorna un OCRPageResult con tutte le parole riconosciute.
        """
        if not self.available:
            return self._empty_result(page, "tesseract unavailable")

        pil_img = Image.fromarray(image)

        # image_to_data restituisce un dict con: text, conf, left, top, width, height
        # Output.DICT è più efficiente di Output.STRING per post-processing
        data = self._tess.image_to_data(
            pil_img,
            lang=self.lang,
            config=self.config,
            output_type=self._tess.Output.DICT
        )

        words = []
        word_counter = 0

        for i in range(len(data["text"])):
            text = data["text"][i].strip()
            conf = float(data["conf"][i])

            # Tesseract restituisce conf=-1 per elementi non-parola (righe, paragrafi)
            if not text or conf < 0:
                continue

            # Normalizza confidence in [0, 1]
            confidence = conf / 100.0

            word_counter += 1
            word_id = f"w_{page}_{word_counter:04d}"

            word = ODXWord(
                id=word_id,
                text=text,
                confidence=confidence,
                engine="tesseract",
                bbox=BBox(
                    x=int(data["left"][i]),
                    y=int(data["top"][i]),
                    w=int(data["width"][i]),
                    h=int(data["height"][i]),
                    page=page
                ),
                alternatives=[WordAlternative(text=text, prob=confidence)],
            )
            words.append(word)

        full_text = self._tess.image_to_string(pil_img, lang=self.lang,
                                                config=self.config).strip()

        return self._build_result(words, full_text, page, "tesseract")

    def _build_result(self, words: list, full_text: str,
                       page: int, engine: str) -> OCRPageResult:
        if not words:
            return self._empty_result(page, engine)

        confidences = [w.confidence for w in words]
        overall = float(np.mean(confidences))
        low_conf_count = sum(1 for c in confidences if c < 0.7)

        return OCRPageResult(
            page_number=page,
            words=words,
            full_text=full_text,
            engine_used=engine,
            overall_confidence=overall,
            low_confidence_count=low_conf_count,
            requires_review=(overall < 0.85 or low_conf_count > len(words) * 0.1),
            source_type="scan",
        )

    def _empty_result(self, page: int, reason: str) -> OCRPageResult:
        return OCRPageResult(
            page_number=page, words=[], full_text="",
            engine_used=reason, overall_confidence=0.0,
            low_confidence_count=0, requires_review=True,
            source_type="unknown",
        )


# ─────────────────────────────────────────────────────────────
#  ENGINE 2: EASYOCR (fallback multilingua)
# ─────────────────────────────────────────────────────────────

class EasyOCREngine:
    """
    Wrapper per EasyOCR — engine basato su deep learning.

    Vantaggi rispetto a Tesseract:
    - Non richiede installazione di language pack separati
    - Migliore su testo in scene naturali (foto, angolazioni)
    - Supporto nativo per 80+ lingue incluse CJK (cinese, giapponese, coreano)

    Svantaggio: più lento di Tesseract, download modello al primo uso (~100MB).

    Requisiti:
        pip install easyocr
    """

    def __init__(self, langs: list = None):
        """
        langs: lista di codici lingua EasyOCR (es. ['it', 'en'])
        EasyOCR usa codici ISO 639-1 diversi da Tesseract.
        """
        self.langs = langs or ['en']
        self.reader = None
        self.available = False

        try:
            import easyocr
            self._easyocr_module = easyocr
            self.available = True
        except ImportError:
            print("[EasyOCREngine] WARNING: easyocr non installato.")
            print("  Installa con: pip install easyocr")
            print("  Nota: scarica modelli ~100MB al primo utilizzo")

    def _get_reader(self):
        """Lazy initialization: carica il modello solo quando serve."""
        if self.reader is None:
            print(f"[EasyOCREngine] Caricamento modello per lingue: {self.langs}")
            print("  Prima esecuzione: potrebbe richiedere qualche minuto...")
            # gpu=False: CPU inference obbligatoria (requisito ODX)
            self.reader = self._easyocr_module.Reader(
                self.langs,
                gpu=False,
                verbose=False
            )
            print("[EasyOCREngine] Modello pronto.")
        return self.reader

    def run(self, image: np.ndarray, page: int = 1) -> OCRPageResult:
        if not self.available:
            return self._empty_result(page, "easyocr unavailable")

        reader = self._get_reader()

        # EasyOCR ritorna: [([[x1,y1],[x2,y2],[x3,y3],[x4,y4]], text, prob), ...]
        results = reader.readtext(image, detail=1, paragraph=False)

        words = []
        full_text_parts = []
        word_counter = 0

        for (bbox_quad, text, prob) in results:
            text = text.strip()
            if not text:
                continue

            # Converti quadrilatero in bounding rect
            xs = [p[0] for p in bbox_quad]
            ys = [p[1] for p in bbox_quad]
            x = int(min(xs))
            y = int(min(ys))
            w = int(max(xs) - x)
            h = int(max(ys) - y)

            word_counter += 1
            word_id = f"w_{page}_{word_counter:04d}_ez"

            word = ODXWord(
                id=word_id,
                text=text,
                confidence=float(prob),
                engine="easyocr",
                bbox=BBox(x=x, y=y, w=w, h=h, page=page),
                alternatives=[WordAlternative(text=text, prob=float(prob))],
            )
            words.append(word)
            full_text_parts.append(text)

        full_text = " ".join(full_text_parts)
        return self._build_result(words, full_text, page, "easyocr")

    def _build_result(self, words, full_text, page, engine):
        if not words:
            return self._empty_result(page, engine)
        confidences = [w.confidence for w in words]
        overall = float(np.mean(confidences))
        low_conf_count = sum(1 for c in confidences if c < 0.7)
        return OCRPageResult(
            page_number=page, words=words, full_text=full_text,
            engine_used=engine, overall_confidence=overall,
            low_confidence_count=low_conf_count,
            requires_review=(overall < 0.80),
            source_type="photo",
        )

    def _empty_result(self, page, reason):
        return OCRPageResult(
            page_number=page, words=[], full_text="",
            engine_used=reason, overall_confidence=0.0,
            low_confidence_count=0, requires_review=True,
            source_type="unknown",
        )


# ─────────────────────────────────────────────────────────────
#  ENGINE 3: TrOCR (testo scritto a mano)
# ─────────────────────────────────────────────────────────────

class TrOCREngine:
    """
    Wrapper per TrOCR — modello transformer per Handwritten Text Recognition.

    TrOCR (Microsoft Research, 2021, licenza MIT) è un modello
    encoder-decoder che combina:
    - ViT (Vision Transformer) come encoder visivo
    - RoBERTa come decoder linguistico

    Questo approccio è enormemente superiore a Tesseract su
    testo manoscritto perché il modello ha un prior linguistico
    che lo aiuta a disambiguare caratteri ambigui.

    Modelli disponibili (tutti gratuiti su HuggingFace):
      microsoft/trocr-base-handwritten    → 334MB, buona accuratezza
      microsoft/trocr-small-handwritten   → 109MB, più veloce, meno accurato
      microsoft/trocr-large-handwritten   → 1.3GB, massima accuratezza

    CPU inference: ~2-8 sec per riga di testo su CPU moderna.

    Requisiti:
        pip install transformers torch Pillow
        # Al primo uso scarica il modello (~334MB da HuggingFace)
    """

    def __init__(self, model_name: str = "microsoft/trocr-small-handwritten"):
        self.model_name = model_name
        self.processor = None
        self.model = None
        self.available = False

        try:
            from transformers import TrOCRProcessor, VisionEncoderDecoderModel
            self._TrOCRProcessor = TrOCRProcessor
            self._VisionEncoderDecoderModel = VisionEncoderDecoderModel
            self.available = True
        except ImportError:
            print("[TrOCREngine] WARNING: transformers non installato.")
            print("  Installa con: pip install transformers torch")
            print("  Nota: scarica modello ~109-334MB al primo utilizzo")

    def _load_model(self):
        """Lazy loading del modello — pesante, lo carichiamo solo quando serve."""
        if self.processor is None:
            print(f"[TrOCREngine] Caricamento modello: {self.model_name}")
            print("  Prima esecuzione: download ~109-334MB da HuggingFace...")
            self.processor = self._TrOCRProcessor.from_pretrained(self.model_name)
            self.model = self._VisionEncoderDecoderModel.from_pretrained(
                self.model_name
            )
            self.model.eval()   # modalità inferenza (no dropout, no gradients)
            print("[TrOCREngine] Modello pronto.")

    def recognize_line(self, line_image: Image.Image) -> tuple[str, float]:
        """
        Riconosce il testo in una singola riga di testo manoscritto.
        Ritorna (testo_riconosciuto, confidence_approssimata).

        TrOCR non restituisce confidence per parola come Tesseract.
        Usiamo la lunghezza della sequenza generata come proxy:
        sequenze molto corte su immagini lunghe = bassa confidence.
        """
        if not self.available:
            return "", 0.0

        self._load_model()

        try:
            import torch

            # Normalizza dimensioni: TrOCR è addestrato su patch 384x384
            if line_image.mode != "RGB":
                line_image = line_image.convert("RGB")

            pixel_values = self.processor(
                images=line_image,
                return_tensors="pt"
            ).pixel_values

            with torch.no_grad():
                generated_ids = self.model.generate(pixel_values)

            text = self.processor.batch_decode(
                generated_ids,
                skip_special_tokens=True
            )[0].strip()

            # Confidence euristica: rapporto tra lunghezza testo generato
            # e dimensione dell'immagine in input
            img_w = line_image.width
            expected_chars = img_w / 20  # ~20px per carattere a 300DPI
            actual_chars = len(text)
            ratio = min(actual_chars / max(expected_chars, 1), 1.0)
            confidence = 0.5 + 0.4 * ratio  # range [0.5, 0.9]

            return text, confidence

        except Exception as e:
            print(f"[TrOCREngine] Errore riconoscimento: {e}")
            return "", 0.0

    def segment_lines(self, image: np.ndarray) -> list:
        """
        Segmenta l'immagine in righe di testo prima di passarle a TrOCR.
        TrOCR lavora su singole righe, non su pagine intere.

        Algoritmo: proiezione orizzontale dei pixel scuri.
        Trova i "gap" bianchi tra le righe e taglia l'immagine.
        """
        # Binarizza per la segmentazione
        _, binary = cv2.threshold(image, 0, 255,
                                   cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        # Proiezione orizzontale: somma dei pixel scuri per riga
        row_sums = np.sum(binary, axis=1)
        threshold = np.max(row_sums) * 0.05  # 5% del massimo = soglia gap

        in_text = False
        start = 0
        lines = []
        padding = 5  # pixel di padding sopra/sotto ogni riga

        for y, val in enumerate(row_sums):
            if not in_text and val > threshold:
                in_text = True
                start = max(0, y - padding)
            elif in_text and val <= threshold:
                in_text = False
                end = min(image.shape[0], y + padding)
                if end - start > 10:  # ignora righe troppo sottili (< 10px)
                    lines.append(image[start:end, :])

        if in_text:  # ultima riga arriva a fine immagine
            lines.append(image[start:, :])

        return lines

    def run(self, image: np.ndarray, page: int = 1) -> OCRPageResult:
        """
        Esegue HTR (Handwritten Text Recognition) su un'immagine intera.
        Pipeline: segmentazione in righe → riconoscimento per riga → merge.
        """
        import cv2  # già importato globalmente, ma esplicito per chiarezza

        lines = self.segment_lines(image)
        if not lines:
            return self._empty_result(page, "trocr-no-lines-detected")

        words = []
        full_text_lines = []
        word_counter = 0
        y_offset = 0

        for line_idx, line_arr in enumerate(lines):
            line_pil = Image.fromarray(line_arr).convert("RGB")
            text, conf = self.recognize_line(line_pil)

            if not text:
                y_offset += line_arr.shape[0]
                continue

            full_text_lines.append(text)

            # Crea una ODXWord per ogni "parola" nella riga riconosciuta
            # (TrOCR non dà bbox per parola, solo per riga intera)
            line_words = text.split()
            line_h = line_arr.shape[0]
            line_w = line_arr.shape[1]
            word_w = line_w // max(len(line_words), 1)

            for word_idx, word_text in enumerate(line_words):
                word_counter += 1
                word = ODXWord(
                    id=f"w_{page}_{word_counter:04d}_trocr",
                    text=word_text,
                    confidence=conf,
                    engine="trocr",
                    bbox=BBox(
                        x=word_idx * word_w,
                        y=y_offset,
                        w=word_w,
                        h=line_h,
                        page=page
                    ),
                    alternatives=[WordAlternative(text=word_text, prob=conf)],
                )
                words.append(word)

            y_offset += line_h

        full_text = "\n".join(full_text_lines)
        confidences = [w.confidence for w in words] if words else [0.0]
        overall = float(np.mean(confidences))

        return OCRPageResult(
            page_number=page,
            words=words,
            full_text=full_text,
            engine_used="trocr",
            overall_confidence=overall,
            low_confidence_count=sum(1 for c in confidences if c < 0.7),
            requires_review=(overall < 0.75),
            source_type="handwriting",
        )

    def _empty_result(self, page, reason):
        return OCRPageResult(
            page_number=page, words=[], full_text="",
            engine_used=reason, overall_confidence=0.0,
            low_confidence_count=0, requires_review=True,
            source_type="handwriting",
        )


# ─────────────────────────────────────────────────────────────
#  CONSENSUS ENGINE — merge multi-engine
# ─────────────────────────────────────────────────────────────

def merge_results(result_a: OCRPageResult,
                  result_b: OCRPageResult,
                  conflict_threshold: float = 0.15) -> OCRPageResult:
    """
    Fonde i risultati di due engine OCR usando una strategia di consensus.

    Per ogni coppia di parole con bounding box sovrapposta:
    - Se i testi coincidono: confidence = media pesata
    - Se divergono: marca come 'conflict=True', tieni il più confident
                    e popola 'engine_results' con entrambe le versioni

    Questo è il "word-level diff" descritto nella specifica ODX:
    le zone di conflitto vengono evidenziate nell'editor visivo
    per la revisione umana.

    conflict_threshold: se le confidence differiscono di più di questo
                        valore, viene marcato come conflitto anche se
                        i testi coincidono.
    """
    if not result_a.words:
        return result_b
    if not result_b.words:
        return result_a

    # Strategia semplificata per v0.1:
    # Usa result_a (Tesseract) come baseline e arricchisce con result_b (EasyOCR)
    # dove la confidence di EasyOCR è significativamente più alta.
    # Un matching geometrico preciso richiede un algoritmo Hungarian/IOU
    # che verrà implementato nella v0.2.

    merged_words = []
    all_words_b = {w.text.lower(): w for w in result_b.words}

    for word_a in result_a.words:
        matching_b = all_words_b.get(word_a.text.lower())

        if matching_b is None:
            # Nessuna corrispondenza in B: usa A così com'è
            merged_words.append(word_a)
            continue

        conf_diff = abs(word_a.confidence - matching_b.confidence)
        texts_match = word_a.text.lower() == matching_b.text.lower()

        if texts_match and conf_diff <= conflict_threshold:
            # Accordo: confidence media pesata (Tesseract più affidabile su stampato)
            merged_conf = 0.6 * word_a.confidence + 0.4 * matching_b.confidence
            word_a.confidence = merged_conf
            word_a.engine = "consensus"
            merged_words.append(word_a)
        else:
            # Conflitto o divergenza significativa
            winner = word_a if word_a.confidence >= matching_b.confidence else matching_b
            winner.conflict = not texts_match
            winner.engine = "consensus"
            winner.engine_results = {
                result_a.engine_used: {
                    "text": word_a.text,
                    "conf": word_a.confidence
                },
                result_b.engine_used: {
                    "text": matching_b.text,
                    "conf": matching_b.confidence
                }
            }
            # Aggiungi l'alternativa dell'engine perdente
            loser = matching_b if winner is word_a else word_a
            winner.alternatives.append(
                WordAlternative(text=loser.text, prob=loser.confidence)
            )
            merged_words.append(winner)

    confidences = [w.confidence for w in merged_words]
    overall = float(np.mean(confidences)) if confidences else 0.0
    low_conf = sum(1 for c in confidences if c < 0.7)
    conflicts = sum(1 for w in merged_words if w.conflict)

    # Testo da engine A (più strutturato per documenti stampati)
    full_text = result_a.full_text or result_b.full_text

    return OCRPageResult(
        page_number=result_a.page_number,
        words=merged_words,
        full_text=full_text,
        engine_used=f"consensus({result_a.engine_used}+{result_b.engine_used})",
        overall_confidence=overall,
        low_confidence_count=low_conf,
        requires_review=(overall < 0.85 or conflicts > 0),
        source_type=result_a.source_type,
    )
