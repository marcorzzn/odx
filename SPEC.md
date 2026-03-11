# ODX — Open Document eXtended
## Specifica Formale · Versione 0.1 · Draft
**Data:** 2026-03-10  
**Stato:** BOZZA — non ratificata  
**Licenza specifica:** CC0 1.0 (dominio pubblico)

---

## 1. Introduzione

ODX è un formato aperto per documenti digitali progettato per superare i limiti strutturali del PDF. Un file `.odx` è un documento binario autocontenuto che separa radicalmente la struttura semantica (cosa significa il documento) dal layout visivo (come appare), permettendo rendering adattivo su qualsiasi dispositivo e accessibilità nativa.

### 1.1 Obiettivi di progettazione

| Obiettivo | Metrica target |
|-----------|----------------|
| Peso vs PDF equivalente | -30% minimo, -60% tipico |
| Tempo prima pagina visibile | < 100ms su hardware 2015 |
| Apertura file 500 pagine | Solo pagina 1-2 caricate inizialmente |
| Accessibilità | Nessun file valido senza layer semantico |
| Portabilità | Windows / Linux / macOS / Android / iOS / Web |

### 1.2 Non-obiettivi v0.1

- Supporto DRM (rimandato a v0.5)
- Rendering 3D o contenuti interattivi complessi
- Compatibilità binaria con PDF (solo conversione)

---

## 2. Struttura binaria del file

### 2.1 Layout fisico

```
┌─────────────────────────────────────────────────────┐
│  FILE HEADER  (32 byte fissi)                       │
├─────────────────────────────────────────────────────┤
│  SEGMENT TABLE  (variabile, offset noto dall'header)│
├─────────────────────────────────────────────────────┤
│  SEGMENT 0: /meta     (JSON-LD, zstd compressed)    │
├─────────────────────────────────────────────────────┤
│  SEGMENT 1: /semantic (XML, zstd compressed)        │
├─────────────────────────────────────────────────────┤
│  SEGMENT 2: /layout   (ODXL JSON, zstd compressed)  │
├─────────────────────────────────────────────────────┤
│  SEGMENT 3: /text     (UTF-8 plain, zstd)           │
├─────────────────────────────────────────────────────┤
│  SEGMENT 4: /assets   (sotto-segmenti per asset)    │
├─────────────────────────────────────────────────────┤
│  SEGMENT 5: /ocr      (ODXO JSON, zstd)             │
├─────────────────────────────────────────────────────┤
│  SEGMENT 6: /diff     (JSON Patch log, zstd)        │
├─────────────────────────────────────────────────────┤
│  SEGMENT 7: /sign     (JSON firme, NON compresso)   │
└─────────────────────────────────────────────────────┘
```

**Motivazione layout fisico:** il file header e la segment table sono in testa al file, non in coda come in ZIP. Questo permette seek O(1) a qualsiasi segmento con una sola lettura dell'header. Il segmento /sign non è compresso perché deve essere verificabile senza dipendenze da zstd.

### 2.2 File Header (32 byte)

```
Offset  Size  Tipo    Campo
------  ----  ------  ----------------------------------------
0x00    4B    bytes   MAGIC = 0x4F 0x44 0x58 0x21  ("ODX!")
0x04    2B    uint16  FORMAT_VERSION (major << 8 | minor) = 0x0001
0x06    2B    uint16  FLAGS (bitmask, vedi §2.3)
0x08    8B    uint64  SEGMENT_TABLE_OFFSET (byte offset nel file)
0x10    4B    uint32  SEGMENT_TABLE_SIZE (numero di segmenti)
0x14    4B    uint32  DOCUMENT_UUID_HI (primi 4 byte UUID v4)
0x18    4B    uint32  DOCUMENT_UUID_LO (ultimi 4 byte UUID v4)
0x1C    4B    uint32  HEADER_CHECKSUM (CRC32 dei 28 byte precedenti)
```

### 2.3 FLAGS bitmask

```
Bit 0  (0x0001):  HAS_OCR_LAYER      — layer /ocr presente
Bit 1  (0x0002):  HAS_DIFF_LAYER     — layer /diff presente
Bit 2  (0x0004):  HAS_SIGNATURES     — layer /sign presente
Bit 3  (0x0008):  ENCRYPTED          — uno o più segmenti cifrati
Bit 4  (0x0010):  STREAMING_SAFE     — ottimizzato per streaming (page table first)
Bit 5  (0x0020):  HAS_HANDWRITING    — contiene testo manoscritto nel layer OCR
Bit 6–15:         RESERVED = 0
```

### 2.4 Segment Table Entry (40 byte per segmento)

```
Offset  Size  Tipo    Campo
------  ----  ------  ----------------------------------------
0x00    8B    uint64  SEGMENT_OFFSET  (byte offset nel file)
0x08    8B    uint64  SEGMENT_SIZE_COMPRESSED
0x10    8B    uint64  SEGMENT_SIZE_UNCOMPRESSED
0x18    4B    uint32  SEGMENT_ID      (enum, vedi §2.5)
0x1C    1B    uint8   COMPRESSION     (0=none, 1=zstd, 2=zstd+dict)
0x1D    1B    uint8   ENCODING        (0=binary, 1=utf8, 2=json, 3=xml)
0x1E    2B    uint16  FLAGS           (0x0001 = encrypted, 0x0002 = optional)
0x20    8B    bytes   SHA256_PARTIAL  (primi 8 byte dell'hash SHA-256 del segmento decompresso)
```

### 2.5 SEGMENT_ID enum

```
0x01  META
0x02  SEMANTIC
0x03  LAYOUT
0x04  TEXT
0x05  ASSETS
0x06  OCR
0x07  DIFF
0x08  SIGN
0x09–0xFF  RESERVED
```

---

## 3. Layer /meta — Metadati

**Encoding:** JSON-LD, UTF-8, compresso zstd  
**Schema:** JSON Schema Draft 2020-12  
**Obbligatorio:** SÌ — un file senza /meta è invalido

### 3.1 Schema JSON completo

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://odx-format.org/schemas/meta/v0.1",
  "type": "object",
  "required": ["odx_version", "uuid", "created_at", "lang", "title"],
  "additionalProperties": false,
  "properties": {

    "odx_version": {
      "type": "string",
      "pattern": "^\\d+\\.\\d+$",
      "description": "Versione del formato ODX usata per creare questo file",
      "examples": ["0.1"]
    },

    "uuid": {
      "type": "string",
      "format": "uuid",
      "description": "Identificatore univoco del documento, UUID v4, immutabile"
    },

    "created_at": {
      "type": "string",
      "format": "date-time",
      "description": "Data/ora di creazione originale, ISO 8601 con timezone"
    },

    "modified_at": {
      "type": "string",
      "format": "date-time",
      "description": "Data/ora ultima modifica"
    },

    "lang": {
      "type": "string",
      "pattern": "^[a-z]{2,3}(-[A-Z]{2})?$",
      "description": "Lingua principale del documento, BCP 47",
      "examples": ["it", "en-US", "zh-Hant"]
    },

    "title": {
      "type": "string",
      "minLength": 1,
      "maxLength": 512,
      "description": "Titolo leggibile del documento"
    },

    "authors": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["name"],
        "properties": {
          "name":  { "type": "string" },
          "email": { "type": "string", "format": "email" },
          "orcid": { "type": "string", "pattern": "^\\d{4}-\\d{4}-\\d{4}-\\d{3}[0-9X]$" }
        }
      }
    },

    "description": {
      "type": "string",
      "maxLength": 4096
    },

    "keywords": {
      "type": "array",
      "items": { "type": "string" },
      "maxItems": 64
    },

    "page_count": {
      "type": "integer",
      "minimum": 1
    },

    "word_count": {
      "type": "integer",
      "minimum": 0
    },

    "document_type": {
      "type": "string",
      "enum": [
        "article", "book", "report", "thesis", "letter",
        "form", "invoice", "presentation", "other"
      ]
    },

    "license": {
      "type": "string",
      "description": "SPDX license identifier o URL",
      "examples": ["CC-BY-4.0", "MIT", "https://example.com/license"]
    },

    "source_format": {
      "type": "string",
      "description": "Formato originale se il documento è una conversione",
      "examples": ["pdf", "docx", "epub", "html"]
    },

    "integrity": {
      "type": "object",
      "description": "Hash di integrità per ogni segmento presente",
      "additionalProperties": {
        "type": "string",
        "pattern": "^sha256:[a-f0-9]{64}$"
      },
      "examples": [
        {
          "semantic": "sha256:a3f2...",
          "text": "sha256:b7c1..."
        }
      ]
    },

    "page_index": {
      "type": "array",
      "description": "Indice lazy loading: offset byte per pagina nel segmento /assets",
      "items": {
        "type": "object",
        "required": ["page", "offset", "size"],
        "properties": {
          "page":   { "type": "integer", "minimum": 1 },
          "offset": { "type": "integer", "minimum": 0 },
          "size":   { "type": "integer", "minimum": 1 }
        }
      }
    },

    "custom": {
      "type": "object",
      "description": "Namespace per metadati personalizzati da applicazioni terze"
    }
  }
}
```

---

## 4. Layer /semantic — Struttura Logica

**Encoding:** XML, UTF-8, compresso zstd  
**Standard di riferimento:** ARIA 1.2, EPUB Accessibility 1.1  
**Obbligatorio:** SÌ — un file senza /semantic è invalido per definizione ODX

### 4.1 Namespace XML

```xml
xmlns:odx="https://odx-format.org/ns/semantic/0.1"
xmlns:aria="https://www.w3.org/ns/aria"
```

### 4.2 Elementi supportati

```xml
<odx:document lang="it" page-count="10">

  <odx:section id="s001" role="title-page">
    <odx:heading id="h001" level="1" aria-label="Titolo principale">
      Testo del titolo
    </odx:heading>
    <odx:paragraph id="p001" role="subtitle">Sottotitolo</odx:paragraph>
  </odx:section>

  <odx:section id="s002" role="abstract">
    <odx:paragraph id="p002">Testo dell'abstract...</odx:paragraph>
  </odx:section>

  <odx:section id="s003" role="body">

    <odx:heading id="h002" level="2">Sezione 1</odx:heading>

    <odx:paragraph id="p003">Testo normale con
      <odx:emphasis type="strong">testo in grassetto</odx:emphasis> e
      <odx:link href="https://example.com" aria-label="Link esempio">link</odx:link>.
    </odx:paragraph>

    <odx:figure id="fig001" aria-describedby="cap001">
      <odx:image asset-ref="img_001" alt="Descrizione immagine obbligatoria" />
      <odx:caption id="cap001">Figura 1: didascalia</odx:caption>
    </odx:figure>

    <odx:table id="tab001" aria-label="Tabella dati">
      <odx:thead>
        <odx:row>
          <odx:cell role="columnheader" scope="col">Colonna A</odx:cell>
          <odx:cell role="columnheader" scope="col">Colonna B</odx:cell>
        </odx:row>
      </odx:thead>
      <odx:tbody>
        <odx:row>
          <odx:cell>Dato 1</odx:cell>
          <odx:cell>Dato 2</odx:cell>
        </odx:row>
      </odx:tbody>
    </odx:table>

    <odx:list type="ordered" id="l001">
      <odx:item id="li001">Primo elemento</odx:item>
      <odx:item id="li002">Secondo elemento</odx:item>
    </odx:list>

    <odx:footnote id="fn001" ref="p003">Testo della nota a piè di pagina</odx:footnote>

    <odx:formula id="f001" notation="latex" alt="Integrale da zero a infinito">
      \int_0^\infty e^{-x^2} dx = \frac{\sqrt{\pi}}{2}
    </odx:formula>

  </odx:section>

  <odx:section id="s004" role="bibliography">
    <odx:reference id="ref001" type="article">
      <odx:author>Cognome, Nome</odx:author>
      <odx:year>2024</odx:year>
      <odx:title>Titolo articolo</odx:title>
    </odx:reference>
  </odx:section>

</odx:document>
```

### 4.3 Ruoli section validi

```
title-page, abstract, toc, body, chapter, appendix,
bibliography, index, glossary, acknowledgments, preface,
cover, colophon, other
```

---

## 5. Layer /layout — Layout Adattivo (formato ODXL)

**Encoding:** JSON, compresso zstd  
**Filosofia:** vincoli dichiarativi, non coordinate assolute  
**Obbligatorio:** NO per documenti text-only, SÌ se il documento ha immagini o layout non lineare

### 5.1 Schema ODXL

```json
{
  "odxl_version": "0.1",
  "page_size": {
    "width": "210mm",
    "height": "297mm",
    "format": "A4"
  },
  "margins": {
    "top": "25mm", "bottom": "25mm",
    "inner": "25mm", "outer": "20mm"
  },
  "columns": {
    "default": 1,
    "breakpoints": [
      { "min_width": "800px", "columns": 2, "gap": "8mm" }
    ]
  },
  "typography": {
    "base_font_size": "11pt",
    "line_height": 1.6,
    "scale_on_mobile": 1.1
  },
  "elements": [
    {
      "semantic_ref": "h001",
      "font_asset": "font_heading",
      "font_size": "24pt",
      "font_weight": 700,
      "margin_top": "0",
      "margin_bottom": "6mm",
      "priority": "high",
      "page_break_before": false
    },
    {
      "semantic_ref": "fig001",
      "width": "100%",
      "max_width": "140mm",
      "align": "center",
      "float": "none",
      "priority": "medium",
      "mobile_behavior": "inline",
      "anchor": "p003"
    },
    {
      "semantic_ref": "fn001",
      "desktop_behavior": "footnote",
      "mobile_behavior": "tooltip",
      "priority": "low"
    }
  ]
}
```

### 5.2 Priority system

| Priority | Desktop | Mobile (< 600px) |
|----------|---------|-------------------|
| `high`   | Sempre visibile, mai collassato | Sempre visibile |
| `medium` | Visibile | Visibile, può essere ridimensionato |
| `low`    | Visibile | Collassato, espandibile su tap |

### 5.3 mobile_behavior valori validi

```
inline     — mostrato inline nel flusso del testo
tooltip    — accessibile tramite tap/click
collapsed  — nascosto con "mostra" button
scrollable — tabella/figura in scroll orizzontale con header fisso
hidden     — non mostrato su mobile (solo per decorativi puri)
```

---

## 6. Layer /ocr — OCR Nativo (formato ODXO)

**Encoding:** JSON, compresso zstd  
**Obbligatorio:** NO — presente solo se il documento ha immagini con testo

### 6.1 Schema ODXO

```json
{
  "odxo_version": "0.1",
  "pages": [
    {
      "page": 1,
      "image_asset_ref": "scan_p001",
      "image_width_px": 2480,
      "image_height_px": 3508,
      "source_type": "scan",
      "preprocessing": {
        "deskew_angle_deg": 0.43,
        "binarization": "sauvola",
        "denoising": "nlmeans",
        "clahe_applied": false
      },
      "engines_used": ["tesseract-5.3", "easyocr-1.7"],
      "language_detected": "it",
      "script_detected": "latin",
      "has_handwriting": false,
      "words": [
        {
          "id": "w_1_001",
          "text": "Introduzione",
          "confidence": 0.98,
          "engine": "tesseract",
          "alternatives": [
            { "text": "Introduzione", "prob": 0.98 },
            { "text": "Introduzíone", "prob": 0.01 }
          ],
          "bbox": {
            "x": 180, "y": 240,
            "w": 312, "h": 38,
            "page": 1
          },
          "corrected": false,
          "correction_source": null
        },
        {
          "id": "w_1_002",
          "text": "Questo",
          "confidence": 0.61,
          "engine": "consensus",
          "conflict": true,
          "engine_results": {
            "tesseract": { "text": "Questo", "conf": 0.61 },
            "easyocr":   { "text": "Quasto", "conf": 0.55 }
          },
          "alternatives": [
            { "text": "Questo", "prob": 0.61 },
            { "text": "Quasto", "prob": 0.55 }
          ],
          "bbox": { "x": 180, "y": 290, "w": 98, "h": 32, "page": 1 },
          "corrected": false
        }
      ],
      "lines": [
        {
          "id": "line_1_001",
          "word_ids": ["w_1_001"],
          "bbox": { "x": 180, "y": 240, "w": 312, "h": 38 },
          "type": "printed"
        }
      ],
      "handwriting_segments": []
    }
  ],
  "full_text_extracted": "Introduzione\nQuesto documento...",
  "overall_confidence": 0.91,
  "low_confidence_word_count": 3,
  "requires_review": true
}
```

### 6.2 Gradi di confidence — codice colore standard

```
0.90 – 1.00  →  verde   — alta fiducia, testo confermato
0.70 – 0.89  →  giallo  — fiducia media, suggerito controllo
0.50 – 0.69  →  arancio — fiducia bassa, probabile errore
0.00 – 0.49  →  rosso   — fiducia molto bassa, correzione obbligatoria
```

### 6.3 source_type valori

```
scan        — documento scansionato (scanner flatbed o ADF)
photo       — fotografia con smartphone o fotocamera
handwriting — testo scritto a mano (attiva TrOCR pipeline)
born_digital — documento nato digitale (OCR non necessario, layer vuoto)
mixed       — pagina con sia testo stampato che manoscritto
```

---

## 7. Layer /diff — Versioning Documentale

**Encoding:** JSON, compresso zstd  
**Standard patch:** JSON Patch RFC 6902  
**Obbligatorio:** NO

### 7.1 Schema

```json
{
  "diff_version": "0.1",
  "baseline_commit": "c0000000",
  "commits": [
    {
      "id": "c0000000",
      "timestamp": "2026-03-10T10:00:00Z",
      "author_hash": "sha256:a3f2...",
      "author_display": "Marco R.",
      "message": "Creazione documento",
      "parent": null,
      "patches": []
    },
    {
      "id": "c0000001",
      "timestamp": "2026-03-10T14:22:00Z",
      "author_hash": "sha256:a3f2...",
      "author_display": "Marco R.",
      "message": "Corretta sezione introduzione",
      "parent": "c0000000",
      "patches": [
        {
          "layer": "semantic",
          "op": "replace",
          "path": "/document/section[@id='s002']/paragraph[@id='p002']",
          "value": "Testo corretto dell'abstract."
        },
        {
          "layer": "text",
          "op": "replace",
          "path": "/paragraphs/p002",
          "value": "Testo corretto dell'abstract."
        }
      ],
      "size_bytes": 128
    }
  ]
}
```

---

## 8. Layer /sign — Firme Digitali

**Encoding:** JSON, **non compresso** (deve essere verificabile senza zstd)  
**Algoritmo firma:** Ed25519 (via PyNaCl / libsodium)  
**Hash integrità:** SHA-256 per segmento  
**Obbligatorio:** NO

### 8.1 Schema

```json
{
  "sign_version": "0.1",
  "signatures": [
    {
      "id": "sig001",
      "signer": "Marco Razzano",
      "timestamp": "2026-03-10T12:00:00Z",
      "algorithm": "Ed25519",
      "public_key_b64": "base64encodedpublickey==",
      "signed_layers": ["meta", "semantic", "text"],
      "layer_hashes": {
        "meta":     "sha256:a3f2c1...",
        "semantic": "sha256:b7d4e2...",
        "text":     "sha256:c9f8a3..."
      },
      "signature_b64": "base64encodedsignature=="
    }
  ]
}
```

---

## 9. Regole di validazione

Un file `.odx` è **valido** se e solo se:

```
REGOLA V-001 [CRITICA]:  Magic bytes = 0x4F445821
REGOLA V-002 [CRITICA]:  FORMAT_VERSION riconosciuta dal parser
REGOLA V-003 [CRITICA]:  Segmento /meta presente e conforme allo schema §3.1
REGOLA V-004 [CRITICA]:  Segmento /semantic presente e conforme allo schema §4
REGOLA V-005 [CRITICA]:  meta.lang presente e valido BCP 47
REGOLA V-006 [CRITICA]:  Ogni <odx:image> ha attributo alt non vuoto
REGOLA V-007 [CRITICA]:  Ogni <odx:figure> ha <odx:caption> o aria-label
REGOLA V-008 [AVVISO]:   meta.page_count corrisponde al numero di pagine negli assets
REGOLA V-009 [AVVISO]:   Se FLAGS.HAS_OCR_LAYER=1, segmento /ocr deve essere presente
REGOLA V-010 [AVVISO]:   SHA-256 in meta.integrity corrisponde agli hash dei segmenti
REGOLA V-011 [INFO]:     word_count in meta corrisponde al conteggio in /text
```

---

## 10. Compatibilità e conversione

### 10.1 Relazione con formati esistenti

| Formato | Relazione con ODX |
|---------|-------------------|
| PDF     | Conversione bidirezionale; ODX è successore concettuale |
| ePub    | Conversione bidirezionale; ODX aggiunge OCR e layout fisso |
| HTML    | ODX → HTML per pubblicazione web; HTML → ODX con perdita layout |
| DOCX    | Conversione ODX → DOCX per editing; DOCX → ODX per distribuzione |
| Markdown | Subset testuale; conversione senza perdita se no immagini |

### 10.2 MIME type proposto

```
application/vnd.odx
```

### 10.3 Estensioni file

```
.odx   — documento ODX standard
.odxt  — documento ODX solo testo (senza assets, ultra-leggero)
.odxs  — specifica ODX (schema, non documento)
```

---

## 11. Implementazione di riferimento — struttura repository

```
odx/
├── README.md
├── SPEC.md                    ← questo documento
├── CONTRIBUTING.md
├── LICENSE                    ← Apache 2.0
├── .github/
│   └── workflows/
│       ├── test.yml           ← pytest su push
│       └── validate-spec.yml  ← validazione schema JSON
├── odxlib/                    ← libreria Python core
│   ├── __init__.py
│   ├── reader.py              ← lettura file .odx
│   ├── writer.py              ← scrittura file .odx
│   ├── validator.py           ← validazione regole §9
│   └── layers/
│       ├── meta.py
│       ├── semantic.py
│       ├── layout.py
│       ├── text.py
│       ├── assets.py
│       ├── ocr.py
│       ├── diff.py
│       └── sign.py
├── odxlib/ocr/
│   ├── pipeline.py            ← orchestratore OCR
│   ├── preprocess.py          ← OpenCV preprocessing
│   ├── engines/
│   │   ├── tesseract.py
│   │   ├── easyocr_engine.py
│   │   └── trocr_engine.py    ← handwriting
│   └── postprocess.py         ← symspell correction
├── odxlib/compress/
│   └── zstd_wrapper.py
├── odxlib/crypto/
│   └── signing.py
├── odx-cli/
│   └── main.py                ← typer CLI
├── converters/
│   ├── pdf_to_odx.py
│   ├── odx_to_pdf.py
│   ├── epub_to_odx.py
│   └── html_to_odx.py
├── odx-renderer/
│   └── render_html.py         ← ODX → HTML responsivo
├── tests/
│   ├── test_reader.py
│   ├── test_writer.py
│   ├── test_validator.py
│   ├── test_ocr.py
│   └── fixtures/              ← documenti .odx di test
└── docs/
    ├── getting-started.md
    ├── spec-rationale.md      ← perché ogni scelta
    └── api-reference.md
```

---

## Appendice A — Scelte architetturali critiche e motivazioni

**A1. Zstandard invece di DEFLATE (ZIP/gzip)**  
Zstandard comprime fino a 3-5x più velocemente di gzip a ratio comparabile, e ha supporto per dizionari pre-addestrati. Per JSON strutturato ripetitivo (come i layer /semantic e /ocr), un dizionario addestrato su documenti tipici può migliorare il ratio del 20-30% aggiuntivo. Licenza BSD, supportato in Python con `zstandard` (pip).

**A2. Ed25519 invece di RSA per le firme**  
Chiavi 32 byte, firme 64 byte, sicurezza equivalente a RSA-3072, operazioni 10-100x più veloci. È lo standard attuale per firme digitali in nuovi protocolli (SSH, TLS 1.3, WireGuard).

**A3. XML per /semantic invece di JSON**  
Il layer semantico è profondamente gerarchico e richiede namespace, attributi ID, e query XPath/XQuery per l'accesso efficiente. JSON non ha un equivalente maturo di XPath. Libreria: `lxml` (BSD, molto performante).

**A4. JSON per /meta, /layout, /ocr, /diff invece di XML**  
Questi layer hanno struttura più piatta o array-heavy dove JSON è più leggibile, più veloce da parsare, e meglio supportato in JavaScript per il renderer web.

**A5. Separazione fisica in segmenti invece di directory ZIP**  
Permette aggiornamento parziale di un singolo layer senza riscrivere l'intero file. Critico per la pipeline OCR: dopo correzione di una parola, si aggiorna solo il segmento /ocr.

---

*Fine specifica ODX v0.1 — Draft*  
*Prossimo passo: Fase 2 — Implementazione parser Python (odxlib)*
