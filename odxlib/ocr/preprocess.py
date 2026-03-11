"""
odxlib/ocr/preprocess.py — Preprocessing immagini per pipeline OCR

Questo modulo prepara le immagini prima del riconoscimento del testo.
Un buon preprocessing vale quanto un buon engine OCR: Tesseract su
un'immagine mal preprocessata produce errori che nessun modello linguistico
può correggere a valle.

Pipeline di preprocessing per tipo di sorgente:
  - scan_clean:  denoising leggero + binarizzazione Sauvola
  - scan_skewed: deskewing automatico + pipeline clean
  - photo:       CLAHE + dewarping + pipeline scan

Dipendenze: opencv-python-headless (o opencv-python), numpy, Pillow
"""

import cv2
import numpy as np
from PIL import Image
from dataclasses import dataclass, field
from typing import Optional
import math
import io


@dataclass
class PreprocessResult:
    """
    Risultato del preprocessing: immagine elaborata + metadati
    che finiscono nel layer /ocr per documentare cosa è stato fatto.
    """
    image: np.ndarray                  # immagine elaborata (grayscale, uint8)
    image_pil: Image.Image             # stessa immagine come oggetto PIL
    original_width: int
    original_height: int
    deskew_angle_deg: float = 0.0      # angolo di correzione applicato
    binarization_method: str = "none"  # sauvola | otsu | none
    denoising_applied: bool = False
    clahe_applied: bool = False
    estimated_dpi: Optional[int] = None
    preprocessing_log: list = field(default_factory=list)

    def log(self, msg: str):
        self.preprocessing_log.append(msg)


# ─────────────────────────────────────────────────────────────
#  FUNZIONI ATOMICHE DI PREPROCESSING
# ─────────────────────────────────────────────────────────────

def load_image(source) -> np.ndarray:
    """
    Carica un'immagine da path (str), bytes, o ndarray.
    Ritorna sempre un array BGR (OpenCV standard).
    """
    if isinstance(source, np.ndarray):
        return source.copy()
    elif isinstance(source, (str, bytes.__class__)) and isinstance(source, str):
        img = cv2.imread(source, cv2.IMREAD_COLOR)
        if img is None:
            raise FileNotFoundError(f"Impossibile caricare l'immagine: {source}")
        return img
    elif isinstance(source, bytes):
        arr = np.frombuffer(source, np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    elif isinstance(source, Image.Image):
        return cv2.cvtColor(np.array(source.convert("RGB")), cv2.COLOR_RGB2BGR)
    else:
        raise TypeError(f"Tipo sorgente non supportato: {type(source)}")


def to_grayscale(img: np.ndarray) -> np.ndarray:
    """
    Converte in grayscale. Se già grayscale, ritorna invariato.
    Motivo: quasi tutto il processing OCR lavora su grayscale —
    il colore non aggiunge informazione per il riconoscimento del testo
    e triplica il numero di pixel da elaborare.
    """
    if len(img.shape) == 2:
        return img
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def estimate_dpi(img: np.ndarray, known_page_width_mm: float = 210.0) -> int:
    """
    Stima il DPI assumendo che la pagina sia A4 (210mm di larghezza).
    Formula: DPI = (pixel_width / mm_width) * 25.4
    Utile per capire se l'immagine è troppo a bassa risoluzione per OCR.
    Sotto 150 DPI, la qualità OCR degrada significativamente.
    Sopra 300 DPI, i benefici sono marginali ma il tempo di processing aumenta.
    """
    pixel_width = img.shape[1]
    dpi = int((pixel_width / known_page_width_mm) * 25.4)
    return dpi


def denoise(img: np.ndarray, strength: str = "light") -> np.ndarray:
    """
    Rimozione del rumore con Non-Local Means Denoising (NLM).

    NLM è superiore al semplice blur Gaussiano perché preserva i bordi
    dei caratteri mentre rimuove il rumore casuale. Cruciale per scanner
    economici o fotografie con rumore digitale.

    strength:
      "light"  → h=7,  templateWindowSize=7,  searchWindowSize=21
      "medium" → h=10, templateWindowSize=7,  searchWindowSize=21
      "heavy"  → h=15, templateWindowSize=7,  searchWindowSize=21
    """
    params = {
        "light":  (7,  7, 21),
        "medium": (10, 7, 21),
        "heavy":  (15, 7, 21),
    }.get(strength, (7, 7, 21))

    return cv2.fastNlMeansDenoising(img, h=params[0],
                                     templateWindowSize=params[1],
                                     searchWindowSize=params[2])


def apply_clahe(img: np.ndarray,
                clip_limit: float = 2.0,
                tile_size: int = 8) -> np.ndarray:
    """
    CLAHE — Contrast Limited Adaptive Histogram Equalization.

    Corregge l'illuminazione non uniforme tipica delle foto da smartphone:
    angoli scuri, riflessi, ombre. L'equalizzazione adattiva lavora su
    tile locali invece che sull'immagine intera, evitando la
    sovramplificazione del rumore che l'equalizzazione globale produce.

    clip_limit: limite di contrasto (2.0 = default bilanciato)
    tile_size:  dimensione dei tile in pixel (8 = 8x8 pixel per tile)
    """
    clahe = cv2.createCLAHE(clipLimit=clip_limit,
                              tileGridSize=(tile_size, tile_size))
    return clahe.apply(img)


def binarize_sauvola(img: np.ndarray,
                     window_size: int = 31,
                     k: float = 0.2) -> np.ndarray:
    """
    Binarizzazione adattiva metodo Sauvola (1999).

    Superiore alla binarizzazione globale di Otsu su documenti con:
    - sfondo non uniforme (carta ingiallita, ombre)
    - illuminazione variabile
    - testo con contrasto variabile

    La soglia viene calcolata localmente per ogni pixel come:
        T(x,y) = mean(x,y) * (1 + k * (std(x,y)/R - 1))
    dove R = 128 (range dinamico massimo), k = sensibilità.

    Implementazione via threshold adattivo Gaussiano di OpenCV
    (approssima Sauvola con parametri equivalenti):
    """
    # Prima normalizziamo per gestire immagini molto scure o molto chiare
    normalized = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX)

    # Threshold adattivo Gaussiano con window_size e costante C
    # C negativo = soglia più aggressiva per testo chiaro su sfondo scuro
    binary = cv2.adaptiveThreshold(
        normalized,
        maxValue=255,
        adaptiveMethod=cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        thresholdType=cv2.THRESH_BINARY,
        blockSize=window_size,
        C=int(k * 50)   # mappatura approssimata del parametro k
    )
    return binary


def binarize_otsu(img: np.ndarray) -> np.ndarray:
    """
    Binarizzazione di Otsu — fallback per immagini uniformi.
    Più veloce di Sauvola, buona per scanner di qualità su carta bianca.
    """
    _, binary = cv2.threshold(img, 0, 255,
                               cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


def deskew(img: np.ndarray, max_angle: float = 15.0) -> tuple[np.ndarray, float]:
    """
    Correzione automatica dell'inclinazione della pagina (deskewing).

    Algoritmo: Hough Line Transform per rilevare le linee di testo,
    calcola l'angolo dominante, applica rotazione affine.

    Fallback: se Hough fallisce, usa l'analisi della distribuzione
    dei pixel scuri (metodo della proiezione orizzontale).

    Ritorna (immagine_corretta, angolo_applicato_gradi).
    max_angle: inclinazioni oltre questa soglia non vengono corrette
               (probabilmente non è un errore di scan ma un layout intenzionale)
    """
    # Lavoriamo su una copia binarizzata per rilevare le linee
    _, binary = cv2.threshold(img, 0, 255,
                               cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # Dilatazione orizzontale: "fondi" i caratteri in linee orizzontali
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (30, 1))
    dilated = cv2.dilate(binary, kernel)

    # Rileva contorni delle "linee" di testo fuse
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL,
                                    cv2.CHAIN_APPROX_SIMPLE)

    angles = []
    for cnt in contours:
        if cv2.contourArea(cnt) < 100:  # ignora contorni microscopici
            continue
        rect = cv2.minAreaRect(cnt)
        angle = rect[-1]

        # cv2.minAreaRect restituisce angoli in [-90, 0)
        # Normalizziamo a [-45, 45]
        if angle < -45:
            angle += 90

        if abs(angle) < max_angle:
            angles.append(angle)

    if not angles:
        return img, 0.0

    # Angolo mediano (più robusto della media contro outlier)
    correction_angle = float(np.median(angles))

    if abs(correction_angle) < 0.1:  # inclinazione trascurabile
        return img, 0.0

    # Applica rotazione affine con interpolazione cubica
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), correction_angle, 1.0)

    # Determina il colore di riempimento: bianco per documenti chiari
    bg_color = int(np.percentile(img, 95))  # 95° percentile ≈ colore sfondo
    rotated = cv2.warpAffine(img, M, (w, h),
                              flags=cv2.INTER_CUBIC,
                              borderMode=cv2.BORDER_CONSTANT,
                              borderValue=bg_color)
    return rotated, correction_angle


def upscale_if_needed(img: np.ndarray,
                       target_dpi: int = 300,
                       current_dpi: Optional[int] = None) -> tuple[np.ndarray, bool]:
    """
    Upscale l'immagine se il DPI stimato è troppo basso per OCR affidabile.

    Tesseract funziona meglio a 300 DPI. Sotto 150 DPI i caratteri
    hanno troppo pochi pixel per essere riconosciuti correttamente.
    Sopra 600 DPI il processing è lento senza benefici aggiuntivi.

    Usa interpolazione INTER_CUBIC che produce bordi più nitidi
    rispetto a INTER_LINEAR per le lettere.
    """
    if current_dpi is None:
        return img, False

    if current_dpi >= 200:  # già abbastanza buono
        return img, False

    scale = target_dpi / current_dpi
    if scale > 4.0:
        scale = 4.0  # evita upscale eccessivi che degradano la qualità

    h, w = img.shape[:2]
    new_w = int(w * scale)
    new_h = int(h * scale)

    upscaled = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
    return upscaled, True


def remove_borders(img: np.ndarray, margin: int = 10) -> np.ndarray:
    """
    Rimuove i bordi neri o artefatti ai margini tipici delle scansioni.
    Trova il bounding box del contenuto significativo e ritaglia.
    """
    _, binary = cv2.threshold(img, 0, 255,
                               cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    coords = cv2.findNonZero(binary)
    if coords is None:
        return img

    x, y, w, h = cv2.boundingRect(coords)
    x = max(0, x - margin)
    y = max(0, y - margin)
    w = min(img.shape[1] - x, w + 2 * margin)
    h = min(img.shape[0] - y, h + 2 * margin)

    return img[y:y+h, x:x+w]


# ─────────────────────────────────────────────────────────────
#  PIPELINE COMPLETE PER TIPO DI SORGENTE
# ─────────────────────────────────────────────────────────────

def preprocess_scan(source, aggressive: bool = False) -> PreprocessResult:
    """
    Pipeline per documenti scansionati (scanner flatbed/ADF).
    Assume buona risoluzione (150+ DPI) e poca distorsione geometrica.

    Passi:
      1. Grayscale
      2. Stima DPI
      3. Deskewing automatico
      4. Denoising leggero
      5. Binarizzazione Sauvola
    """
    raw = load_image(source)
    h, w = raw.shape[:2]
    result = PreprocessResult(
        image=raw, image_pil=Image.fromarray(raw),
        original_width=w, original_height=h
    )

    # Step 1: Grayscale
    gray = to_grayscale(raw)
    result.log("grayscale conversion")

    # Step 2: DPI
    dpi = estimate_dpi(gray)
    result.estimated_dpi = dpi
    result.log(f"estimated DPI: {dpi}")

    # Step 3: Upscale se DPI troppo basso
    gray, upscaled = upscale_if_needed(gray, current_dpi=dpi)
    if upscaled:
        result.log(f"upscaled to ~300 DPI (was {dpi})")

    # Step 4: Deskew
    gray, angle = deskew(gray)
    result.deskew_angle_deg = angle
    if abs(angle) > 0.1:
        result.log(f"deskewed: {angle:.2f}°")
    else:
        result.log("deskew: no correction needed")

    # Step 5: Denoising
    strength = "medium" if aggressive else "light"
    gray = denoise(gray, strength=strength)
    result.denoising_applied = True
    result.log(f"denoising: {strength}")

    # Step 6: Binarizzazione Sauvola
    binary = binarize_sauvola(gray)
    result.binarization_method = "sauvola"
    result.log("binarization: sauvola adaptive")

    result.image = binary
    result.image_pil = Image.fromarray(binary)
    return result


def preprocess_photo(source) -> PreprocessResult:
    """
    Pipeline per foto da smartphone.
    Gestisce: illuminazione non uniforme, leggera prospettiva,
    rumore digitale, JPEG artifacts.

    Passi aggiuntivi rispetto a preprocess_scan:
      - CLAHE per correzione illuminazione
      - Sharpening per compensare il blur della fotocamera
    """
    raw = load_image(source)
    h, w = raw.shape[:2]
    result = PreprocessResult(
        image=raw, image_pil=Image.fromarray(raw),
        original_width=w, original_height=h
    )

    # Step 1: Grayscale
    gray = to_grayscale(raw)
    result.log("grayscale conversion")

    # Step 2: CLAHE per illuminazione non uniforme
    gray = apply_clahe(gray, clip_limit=2.5, tile_size=8)
    result.clahe_applied = True
    result.log("CLAHE equalization applied")

    # Step 3: Sharpening con kernel unsharp mask
    blurred = cv2.GaussianBlur(gray, (0, 0), 2.0)
    gray = cv2.addWeighted(gray, 1.5, blurred, -0.5, 0)
    result.log("unsharp mask sharpening")

    # Step 4: Deskew
    gray, angle = deskew(gray)
    result.deskew_angle_deg = angle
    result.log(f"deskewed: {angle:.2f}°" if abs(angle) > 0.1 else "deskew: minimal")

    # Step 5: Denoising medio (foto hanno più rumore degli scanner)
    gray = denoise(gray, strength="medium")
    result.denoising_applied = True
    result.log("denoising: medium")

    # Step 6: Binarizzazione Sauvola (più robusta con illuminazione variabile)
    binary = binarize_sauvola(gray, window_size=41, k=0.15)
    result.binarization_method = "sauvola"
    result.log("binarization: sauvola (photo-tuned)")

    result.image = binary
    result.image_pil = Image.fromarray(binary)
    return result


def preprocess_handwriting(source) -> PreprocessResult:
    """
    Pipeline per testo scritto a mano.
    Non binarizza — TrOCR lavora meglio su immagine grayscale.
    Si concentra su: contrasto, deskew gentile, riduzione rumore.
    """
    raw = load_image(source)
    h, w = raw.shape[:2]
    result = PreprocessResult(
        image=raw, image_pil=Image.fromarray(raw),
        original_width=w, original_height=h
    )

    gray = to_grayscale(raw)
    result.log("grayscale conversion")

    # CLAHE delicato per handwriting
    gray = apply_clahe(gray, clip_limit=1.5, tile_size=16)
    result.clahe_applied = True
    result.log("CLAHE (gentle) applied")

    # Denoising leggero (non vogliamo perdere tratti sottili della scrittura)
    gray = denoise(gray, strength="light")
    result.denoising_applied = True
    result.log("denoising: light (preserve stroke detail)")

    # Deskew gentile con soglia più alta (la scrittura a mano è naturalmente variabile)
    gray, angle = deskew(gray, max_angle=8.0)
    result.deskew_angle_deg = angle
    result.log(f"deskewed: {angle:.2f}°" if abs(angle) > 0.2 else "deskew: minimal")

    # Per handwriting NON binarizziamo: manteniamo grayscale
    # TrOCR è addestrato su immagini grayscale/colore, non binarie
    result.binarization_method = "none (grayscale for TrOCR)"
    result.log("no binarization: grayscale preserved for TrOCR")

    result.image = gray
    result.image_pil = Image.fromarray(gray)
    return result


def detect_source_type(img_path: str) -> str:
    """
    Euristica per rilevare automaticamente il tipo di sorgente
    analizzando le caratteristiche statistiche dell'immagine.

    Ritorna: 'scan' | 'photo' | 'born_digital'
    """
    img = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return 'scan'

    # Calcola statistiche
    mean_val  = np.mean(img)
    std_val   = np.std(img)
    hist      = cv2.calcHist([img], [0], None, [256], [0, 256]).flatten()

    # Immagini "born digital" (screenshot, PDF renderizzato):
    # bimodali con picco molto marcato vicino a 255 (sfondo bianco)
    white_peak = float(hist[240:256].sum()) / float(hist.sum())
    if white_peak > 0.6 and std_val < 60:
        return 'born_digital'

    # Foto da smartphone:
    # gradiente di illuminazione, valore medio intermedio, std alta
    dpi_est = estimate_dpi(img)
    if dpi_est < 150 or std_val > 80:
        return 'photo'

    return 'scan'
