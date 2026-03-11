"""
odxlib — Libreria Python di riferimento per il formato ODX v0.1
Open Document eXtended

Dipendenze (tutte open source, pip installabili):
    pip install zstandard jsonschema lxml

Uso rapido:
    from odxlib import ODXWriter, ODXReader, ODXValidator

    # Creare un documento .odx da testo
    writer = ODXWriter()
    writer.set_meta(title="Il mio documento", lang="it", authors=[{"name": "Marco R."}])
    writer.set_text("Questo è il contenuto del documento.")
    writer.set_semantic_from_text("Questo è il contenuto del documento.")
    writer.save("documento.odx")

    # Leggere un documento .odx
    reader = ODXReader("documento.odx")
    print(reader.get_text())
    print(reader.get_meta())

    # Validare
    v = ODXValidator("documento.odx")
    v.validate()
    v.print_report()
"""

import struct
import json
import uuid
import hashlib
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Importazioni opzionali (non bloccanti se mancano)
try:
    import zstandard as zstd
    ZSTD_AVAILABLE = True
except ImportError:
    ZSTD_AVAILABLE = False
    print("[odxlib] WARNING: zstandard non installato. Uso zlib come fallback.")

try:
    import jsonschema
    JSONSCHEMA_AVAILABLE = True
except ImportError:
    JSONSCHEMA_AVAILABLE = False

try:
    from lxml import etree
    LXML_AVAILABLE = True
except ImportError:
    LXML_AVAILABLE = False
    import xml.etree.ElementTree as etree


# ─────────────────────────────────────────────
#  COSTANTI DEL FORMATO
# ─────────────────────────────────────────────

ODX_MAGIC       = b'\x4F\x44\x58\x21'   # "ODX!"
FORMAT_VERSION  = (0, 1)                  # major=0, minor=1
HEADER_SIZE     = 32                      # byte, fisso
SEGMENT_ENTRY_SIZE = 40                   # byte per voce nella segment table

# Segment IDs
SEG_META     = 0x01
SEG_SEMANTIC = 0x02
SEG_LAYOUT   = 0x03
SEG_TEXT     = 0x04
SEG_ASSETS   = 0x05
SEG_OCR      = 0x06
SEG_DIFF     = 0x07
SEG_SIGN     = 0x08

SEG_NAMES = {
    SEG_META:     "meta",
    SEG_SEMANTIC: "semantic",
    SEG_LAYOUT:   "layout",
    SEG_TEXT:     "text",
    SEG_ASSETS:   "assets",
    SEG_OCR:      "ocr",
    SEG_DIFF:     "diff",
    SEG_SIGN:     "sign",
}

# Flags (bitmask nel file header)
FLAG_HAS_OCR        = 0x0001
FLAG_HAS_DIFF       = 0x0002
FLAG_HAS_SIGNATURES = 0x0004
FLAG_ENCRYPTED      = 0x0008
FLAG_STREAMING_SAFE = 0x0010
FLAG_HAS_HANDWRITING = 0x0020

# Compression codes
COMP_NONE  = 0
COMP_ZSTD  = 1

# Encoding codes
ENC_BINARY = 0
ENC_UTF8   = 1
ENC_JSON   = 2
ENC_XML    = 3


# ─────────────────────────────────────────────
#  FUNZIONI DI COMPRESSIONE
# ─────────────────────────────────────────────

def compress(data: bytes, level: int = 3) -> bytes:
    """
    Comprime dati con Zstandard se disponibile, altrimenti zlib.
    Zstandard è 3-5x più veloce di zlib a parità di ratio.
    """
    if ZSTD_AVAILABLE:
        cctx = zstd.ZstdCompressor(level=level)
        return cctx.compress(data)
    else:
        return zlib.compress(data, level=min(level, 9))


def decompress(data: bytes) -> bytes:
    """
    Decomprime dati. Prova zstd prima, poi zlib.
    """
    if ZSTD_AVAILABLE:
        try:
            dctx = zstd.ZstdDecompressor()
            return dctx.decompress(data)
        except Exception:
            pass
    return zlib.decompress(data)


def sha256_hex(data: bytes) -> str:
    """Calcola SHA-256 e restituisce stringa hex con prefisso."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ─────────────────────────────────────────────
#  STRUTTURE BINARIE
# ─────────────────────────────────────────────

def encode_header(flags: int, seg_table_offset: int,
                  seg_table_size: int, doc_uuid: str) -> bytes:
    """
    Costruisce i 32 byte dell'header ODX.

    Layout:
        4B  magic
        2B  version (major<<8 | minor)
        2B  flags
        8B  segment_table_offset (uint64 LE)
        4B  segment_table_size   (uint32 LE)
        4B  uuid_hi              (uint32 LE)
        4B  uuid_lo              (uint32 LE)
        4B  header_checksum      (CRC32 dei 28 byte precedenti)
    """
    version = (FORMAT_VERSION[0] << 8) | FORMAT_VERSION[1]
    uuid_bytes = uuid.UUID(doc_uuid).bytes
    uuid_hi = struct.unpack('>I', uuid_bytes[0:4])[0]
    uuid_lo = struct.unpack('>I', uuid_bytes[12:16])[0]

    header_body = struct.pack(
        '<4sHHQIII',
        ODX_MAGIC,
        version,
        flags,
        seg_table_offset,
        seg_table_size,
        uuid_hi,
        uuid_lo
    )
    checksum = zlib.crc32(header_body) & 0xFFFFFFFF
    return header_body + struct.pack('<I', checksum)


def decode_header(data: bytes) -> dict:
    """
    Decodifica i 32 byte dell'header. Verifica magic e checksum.
    Ritorna un dict con i campi dell'header.
    Solleva ValueError se il file non è un ODX valido.
    """
    if len(data) < HEADER_SIZE:
        raise ValueError("File troppo corto per essere un ODX valido.")

    magic, version, flags, seg_offset, seg_count, uuid_hi, uuid_lo = \
        struct.unpack('<4sHHQIII', data[:28])
    checksum = struct.unpack('<I', data[28:32])[0]

    if magic != ODX_MAGIC:
        raise ValueError(
            f"Magic bytes non validi: {magic.hex()} (atteso: {ODX_MAGIC.hex()}). "
            "Questo file non è un documento ODX."
        )

    expected_crc = zlib.crc32(data[:28]) & 0xFFFFFFFF
    if checksum != expected_crc:
        raise ValueError(
            f"Checksum header non valido: {checksum:#010x} "
            f"(atteso: {expected_crc:#010x}). File corrotto."
        )

    major = (version >> 8) & 0xFF
    minor = version & 0xFF

    return {
        "version_major": major,
        "version_minor": minor,
        "version_str": f"{major}.{minor}",
        "flags": flags,
        "flag_has_ocr": bool(flags & FLAG_HAS_OCR),
        "flag_has_diff": bool(flags & FLAG_HAS_DIFF),
        "flag_has_signatures": bool(flags & FLAG_HAS_SIGNATURES),
        "flag_encrypted": bool(flags & FLAG_ENCRYPTED),
        "segment_table_offset": seg_offset,
        "segment_count": seg_count,
        "uuid_hi": uuid_hi,
        "uuid_lo": uuid_lo,
    }


def encode_segment_entry(seg_offset: int, size_compressed: int,
                          size_uncompressed: int, seg_id: int,
                          compression: int, encoding: int,
                          flags: int, data_hash: bytes) -> bytes:
    """
    Costruisce una voce da 40 byte della segment table.
    data_hash: primi 8 byte del SHA-256 del contenuto decompresso.
    """
    return struct.pack(
        '<QQQIBBH8s',
        seg_offset,
        size_compressed,
        size_uncompressed,
        seg_id,
        compression,
        encoding,
        flags,
        data_hash[:8]
    )


def decode_segment_entry(data: bytes, offset: int = 0) -> dict:
    """Decodifica una voce da 40 byte della segment table."""
    seg_offset, size_comp, size_uncomp, seg_id, comp, enc, flags, partial_hash = \
        struct.unpack_from('<QQQIBBH8s', data, offset)
    return {
        "offset": seg_offset,
        "size_compressed": size_comp,
        "size_uncompressed": size_uncomp,
        "segment_id": seg_id,
        "segment_name": SEG_NAMES.get(seg_id, f"unknown_{seg_id:#04x}"),
        "compression": comp,
        "encoding": enc,
        "flags": flags,
        "partial_hash": partial_hash.hex(),
        "is_optional": bool(flags & 0x0002),
        "is_encrypted": bool(flags & 0x0001),
    }


# ─────────────────────────────────────────────
#  GENERATORI DI LAYER
# ─────────────────────────────────────────────

def build_meta_layer(title: str, lang: str, doc_uuid: str,
                     authors: Optional[list] = None,
                     description: Optional[str] = None,
                     page_count: Optional[int] = None,
                     document_type: str = "other",
                     source_format: Optional[str] = None) -> bytes:
    """
    Costruisce il layer /meta come JSON UTF-8.
    Questo è il layer obbligatorio che identifica il documento.
    """
    now = datetime.now(timezone.utc).isoformat()

    meta = {
        "odx_version": f"{FORMAT_VERSION[0]}.{FORMAT_VERSION[1]}",
        "uuid": doc_uuid,
        "created_at": now,
        "modified_at": now,
        "lang": lang,
        "title": title,
        "document_type": document_type,
        "integrity": {}
    }

    if authors:
        meta["authors"] = authors
    if description:
        meta["description"] = description
    if page_count is not None:
        meta["page_count"] = page_count
    if source_format:
        meta["source_format"] = source_format

    return json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")


def build_semantic_layer_from_text(text: str, lang: str = "it") -> bytes:
    """
    Costruisce un layer /semantic minimale da testo puro.
    Ogni paragrafo separato da doppio newline diventa un <odx:paragraph>.
    Per documenti complessi, il layer semantic andrebbe costruito manualmente
    o tramite pipeline NLP.
    """
    ODX_NS = "https://odx-format.org/ns/semantic/0.1"

    root = etree.Element(
        f"{{{ODX_NS}}}document",
        attrib={"lang": lang}
    )

    body = etree.SubElement(root, f"{{{ODX_NS}}}section",
                             attrib={"id": "s001", "role": "body"})

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    if not paragraphs:
        paragraphs = [text.strip()] if text.strip() else ["(documento vuoto)"]

    for i, para_text in enumerate(paragraphs, start=1):
        para = etree.SubElement(body, f"{{{ODX_NS}}}paragraph",
                                 attrib={"id": f"p{i:04d}"})
        para.text = para_text

    if LXML_AVAILABLE:
        return etree.tostring(root, pretty_print=True,
                               xml_declaration=True, encoding="UTF-8")
    else:
        # Fallback senza lxml
        tree = etree.ElementTree(root)
        import io
        buf = io.BytesIO()
        tree.write(buf, xml_declaration=True, encoding="UTF-8")
        return buf.getvalue()


def build_text_layer(text: str) -> bytes:
    """
    Layer /text: testo puro UTF-8, completamente indicizzabile.
    Nessuna formattazione, solo contenuto testuale.
    """
    return text.encode("utf-8")


# ─────────────────────────────────────────────
#  ODX WRITER
# ─────────────────────────────────────────────

class ODXWriter:
    """
    Scrive un file .odx a partire dai layer forniti.

    Esempio:
        writer = ODXWriter()
        writer.set_meta(title="Test", lang="it")
        writer.set_text("Testo del documento.")
        writer.set_semantic_from_text("Testo del documento.")
        writer.save("output.odx")
    """

    def __init__(self):
        self._doc_uuid = str(uuid.uuid4())
        self._segments = {}   # seg_id -> bytes (raw, non compresso)
        self._flags = FLAG_STREAMING_SAFE
        self._meta_params = {}

    def set_meta(self, title: str, lang: str = "it",
                 authors: Optional[list] = None,
                 description: Optional[str] = None,
                 page_count: Optional[int] = None,
                 document_type: str = "other",
                 source_format: Optional[str] = None):
        """Imposta i metadati del documento."""
        self._meta_params = {
            "title": title, "lang": lang, "doc_uuid": self._doc_uuid,
            "authors": authors, "description": description,
            "page_count": page_count, "document_type": document_type,
            "source_format": source_format
        }
        raw = build_meta_layer(**self._meta_params)
        self._segments[SEG_META] = raw

    def set_text(self, text: str):
        """Imposta il testo puro (layer /text)."""
        self._segments[SEG_TEXT] = build_text_layer(text)

    def set_semantic_from_text(self, text: str, lang: str = "it"):
        """
        Genera automaticamente il layer /semantic da testo puro.
        Per documenti complessi usa set_semantic_raw() con XML pre-costruito.
        """
        self._segments[SEG_SEMANTIC] = build_semantic_layer_from_text(text, lang)

    def set_semantic_raw(self, xml_bytes: bytes):
        """Imposta il layer /semantic da XML già costruito."""
        self._segments[SEG_SEMANTIC] = xml_bytes

    def set_layout_raw(self, json_bytes: bytes):
        """Imposta il layer /layout da JSON ODXL già costruito."""
        self._segments[SEG_LAYOUT] = json_bytes

    def set_ocr_raw(self, json_bytes: bytes):
        """Imposta il layer /ocr da JSON ODXO già costruito."""
        self._segments[SEG_OCR] = json_bytes
        self._flags |= FLAG_HAS_OCR

    def set_diff_raw(self, json_bytes: bytes):
        """Imposta il layer /diff."""
        self._segments[SEG_DIFF] = json_bytes
        self._flags |= FLAG_HAS_DIFF

    def _get_encoding(self, seg_id: int) -> int:
        """Inferisce il tipo di encoding dal segment ID."""
        if seg_id == SEG_SEMANTIC:
            return ENC_XML
        elif seg_id in (SEG_META, SEG_LAYOUT, SEG_OCR, SEG_DIFF, SEG_SIGN):
            return ENC_JSON
        elif seg_id == SEG_TEXT:
            return ENC_UTF8
        else:
            return ENC_BINARY

    def save(self, path: str) -> dict:
        """
        Assembla e scrive il file .odx.
        Ritorna un dict con statistiche: dimensioni originali, compresse, ratio.
        """
        if SEG_META not in self._segments:
            raise ValueError(
                "Il layer /meta è obbligatorio. Chiama set_meta() prima di save()."
            )
        if SEG_SEMANTIC not in self._segments:
            raise ValueError(
                "Il layer /semantic è obbligatorio. "
                "Chiama set_semantic_from_text() o set_semantic_raw()."
            )

        # Comprime tutti i segmenti e raccoglie metadati
        seg_order = [SEG_META, SEG_SEMANTIC, SEG_LAYOUT, SEG_TEXT,
                     SEG_ASSETS, SEG_OCR, SEG_DIFF, SEG_SIGN]

        present_segs = [(sid, self._segments[sid])
                        for sid in seg_order if sid in self._segments]

        # Calcola gli hash per l'integrity nel meta
        integrity = {}
        for seg_id, raw_data in present_segs:
            name = SEG_NAMES[seg_id]
            integrity[name] = sha256_hex(raw_data)

        # Aggiorna il meta con gli hash di tutti gli ALTRI layer.
        # Il meta non può contenere il proprio hash (bootstrap problem):
        # includi l'hash del meta sarebbe come chiedere a un documento
        # di certificare se stesso prima di essere scritto.
        # La verifica del meta usa il partial_hash nella segment table.
        integrity_others = {k: v for k, v in integrity.items() if k != "meta"}

        if SEG_META in self._segments:
            meta_obj = json.loads(self._segments[SEG_META].decode("utf-8"))
            meta_obj["integrity"] = integrity_others
            self._segments[SEG_META] = json.dumps(
                meta_obj, ensure_ascii=False, indent=2
            ).encode("utf-8")
            # Aggiorna la lista present_segs con il meta aggiornato
            present_segs = [(sid, self._segments[sid])
                            for sid in seg_order if sid in self._segments]

        # Comprimi segmenti
        compressed_segs = []
        for seg_id, raw_data in present_segs:
            # /sign non viene compresso (deve essere verificabile senza zstd)
            if seg_id == SEG_SIGN:
                comp_data = raw_data
                comp_type = COMP_NONE
            else:
                comp_data = compress(raw_data)
                comp_type = COMP_ZSTD if ZSTD_AVAILABLE else COMP_ZSTD

            partial_hash = hashlib.sha256(raw_data).digest()[:8]
            encoding = self._get_encoding(seg_id)

            compressed_segs.append({
                "seg_id": seg_id,
                "raw": raw_data,
                "compressed": comp_data,
                "compression": comp_type,
                "encoding": encoding,
                "partial_hash": partial_hash,
            })

        # Calcola offset: header + segment_table + segmenti dati
        seg_table_offset = HEADER_SIZE
        seg_table_total_bytes = len(compressed_segs) * SEGMENT_ENTRY_SIZE
        data_start = seg_table_offset + seg_table_total_bytes

        # Calcola offset di ogni segmento
        current_offset = data_start
        for seg in compressed_segs:
            seg["file_offset"] = current_offset
            current_offset += len(seg["compressed"])

        # Costruisce segment table
        seg_table_bytes = b""
        for seg in compressed_segs:
            entry = encode_segment_entry(
                seg_offset=seg["file_offset"],
                size_compressed=len(seg["compressed"]),
                size_uncompressed=len(seg["raw"]),
                seg_id=seg["seg_id"],
                compression=seg["compression"],
                encoding=seg["encoding"],
                flags=0x0000,
                data_hash=seg["partial_hash"]
            )
            seg_table_bytes += entry

        # Costruisce header
        header_bytes = encode_header(
            flags=self._flags,
            seg_table_offset=seg_table_offset,
            seg_table_size=len(compressed_segs),
            doc_uuid=self._doc_uuid
        )

        # Scrive il file
        output_path = Path(path)
        with open(output_path, "wb") as f:
            f.write(header_bytes)
            f.write(seg_table_bytes)
            for seg in compressed_segs:
                f.write(seg["compressed"])

        # Statistiche
        total_raw = sum(len(s["raw"]) for s in compressed_segs)
        total_comp = sum(len(s["compressed"]) for s in compressed_segs)
        file_size = output_path.stat().st_size
        ratio = (1 - total_comp / total_raw) * 100 if total_raw > 0 else 0

        stats = {
            "path": str(output_path),
            "file_size_bytes": file_size,
            "segments_count": len(compressed_segs),
            "total_uncompressed_bytes": total_raw,
            "total_compressed_bytes": total_comp,
            "compression_ratio_pct": round(ratio, 1),
            "uuid": self._doc_uuid,
        }

        print(f"[ODXWriter] File scritto: {output_path}")
        print(f"  Dimensione: {file_size:,} byte")
        print(f"  Segmenti:   {len(compressed_segs)}")
        print(f"  Compressione: {ratio:.1f}% riduzione")
        print(f"  UUID: {self._doc_uuid}")

        return stats


# ─────────────────────────────────────────────
#  ODX READER
# ─────────────────────────────────────────────

class ODXReader:
    """
    Legge un file .odx e fornisce accesso ai layer.

    Esempio:
        reader = ODXReader("documento.odx")
        print(reader.get_text())
        print(reader.get_meta())
        xml = reader.get_semantic_xml()
    """

    def __init__(self, path: str):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"File non trovato: {path}")

        with open(self.path, "rb") as f:
            self._raw = f.read()

        # Decode header
        self._header = decode_header(self._raw[:HEADER_SIZE])

        # Decode segment table
        tbl_offset = self._header["segment_table_offset"]
        tbl_count  = self._header["segment_count"]
        self._segments_index = {}

        for i in range(tbl_count):
            entry_offset = tbl_offset + i * SEGMENT_ENTRY_SIZE
            entry = decode_segment_entry(self._raw, entry_offset)
            self._segments_index[entry["segment_id"]] = entry

    def _read_segment_raw(self, seg_id: int) -> Optional[bytes]:
        """
        Legge e decomprime un segmento. Lazy loading nativo:
        legge solo i byte necessari usando lo slice dell'array.
        """
        if seg_id not in self._segments_index:
            return None

        entry = self._segments_index[seg_id]
        start = entry["offset"]
        end   = start + entry["size_compressed"]
        raw_compressed = self._raw[start:end]

        if entry["compression"] == COMP_NONE:
            return raw_compressed
        else:
            return decompress(raw_compressed)

    def get_meta(self) -> dict:
        """Ritorna i metadati come dizionario Python."""
        data = self._read_segment_raw(SEG_META)
        if not data:
            raise ValueError("Layer /meta non trovato nel file.")
        return json.loads(data.decode("utf-8"))

    def get_text(self) -> str:
        """Ritorna il testo puro del documento."""
        data = self._read_segment_raw(SEG_TEXT)
        if not data:
            # Fallback: prova ad estrarre testo dal layer semantic
            return self._extract_text_from_semantic()
        return data.decode("utf-8")

    def _extract_text_from_semantic(self) -> str:
        """Estrae il testo dal layer /semantic come fallback."""
        data = self._read_segment_raw(SEG_SEMANTIC)
        if not data:
            return ""
        try:
            if LXML_AVAILABLE:
                root = etree.fromstring(data)
                return " ".join(root.itertext())
            else:
                root = etree.fromstring(data.decode("utf-8"))
                return " ".join(root.itertext())
        except Exception:
            return data.decode("utf-8", errors="replace")

    def get_semantic_xml(self) -> Optional[bytes]:
        """Ritorna il layer /semantic come bytes XML."""
        return self._read_segment_raw(SEG_SEMANTIC)

    def get_layout(self) -> Optional[dict]:
        """Ritorna il layer /layout come dizionario Python."""
        data = self._read_segment_raw(SEG_LAYOUT)
        if not data:
            return None
        return json.loads(data.decode("utf-8"))

    def get_ocr(self) -> Optional[dict]:
        """Ritorna il layer /ocr come dizionario Python."""
        data = self._read_segment_raw(SEG_OCR)
        if not data:
            return None
        return json.loads(data.decode("utf-8"))

    def get_diff_history(self) -> Optional[dict]:
        """Ritorna la cronologia delle modifiche."""
        data = self._read_segment_raw(SEG_DIFF)
        if not data:
            return None
        return json.loads(data.decode("utf-8"))

    def has_segment(self, seg_id: int) -> bool:
        """Controlla se un determinato segmento è presente."""
        return seg_id in self._segments_index

    def get_info(self) -> dict:
        """Ritorna un report riassuntivo del file."""
        meta = self.get_meta()
        segs = {SEG_NAMES[sid]: idx for sid, idx in self._segments_index.items()}

        return {
            "path": str(self.path),
            "file_size_bytes": len(self._raw),
            "odx_version": self._header["version_str"],
            "uuid": meta.get("uuid"),
            "title": meta.get("title"),
            "lang": meta.get("lang"),
            "created_at": meta.get("created_at"),
            "authors": meta.get("authors", []),
            "segments_present": list(segs.keys()),
            "has_ocr": self._header.get("flag_has_ocr", False),
            "has_diff": self._header.get("flag_has_diff", False),
            "has_signatures": self._header.get("flag_has_signatures", False),
        }

    def verify_integrity(self) -> dict:
        """
        Verifica l'integrità di ogni segmento confrontando
        gli hash nel meta con i dati effettivi.
        """
        meta = self.get_meta()
        integrity = meta.get("integrity", {})
        results = {}

        for seg_id, entry in self._segments_index.items():
            name = SEG_NAMES.get(seg_id, f"unknown_{seg_id}")
            raw = self._read_segment_raw(seg_id)
            if raw is None:
                results[name] = {"status": "MISSING", "ok": False}
                continue

            actual_hash = sha256_hex(raw)
            expected_hash = integrity.get(name)

            # Il meta non contiene il proprio hash (bootstrap problem):
            # usa il partial_hash nella segment table come verifica leggera.
            if name == "meta":
                partial = entry.get("partial_hash", "")
                actual_partial = hashlib.sha256(raw).digest()[:8].hex()
                if partial and partial != actual_partial:
                    results[name] = {"status": "PARTIAL_HASH_MISMATCH", "ok": False}
                else:
                    results[name] = {"status": "OK (partial hash)", "ok": True}
                continue

            if expected_hash is None:
                results[name] = {"status": "NO_HASH_IN_META", "ok": True}
            elif actual_hash == expected_hash:
                results[name] = {"status": "OK", "ok": True}
            else:
                results[name] = {
                    "status": "HASH_MISMATCH",
                    "ok": False,
                    "expected": expected_hash,
                    "actual": actual_hash
                }

        all_ok = all(r["ok"] for r in results.values())
        return {"segments": results, "all_ok": all_ok}


# ─────────────────────────────────────────────
#  ODX VALIDATOR
# ─────────────────────────────────────────────

class ODXValidator:
    """
    Valida un file .odx secondo le regole della specifica §9.
    Un file invalido non è un documento ODX — è un file corrotto
    o non conforme.

    Esempio:
        v = ODXValidator("documento.odx")
        results = v.validate()
        v.print_report()
    """

    def __init__(self, path: str):
        self.path = path
        self._errors = []
        self._warnings = []
        self._infos = []

    def _err(self, code: str, msg: str):
        self._errors.append(f"[{code}] ERRORE: {msg}")

    def _warn(self, code: str, msg: str):
        self._warnings.append(f"[{code}] AVVISO: {msg}")

    def _info(self, code: str, msg: str):
        self._infos.append(f"[{code}] INFO: {msg}")

    def validate(self) -> dict:
        """Esegue tutte le regole di validazione. Ritorna il report."""
        self._errors = []
        self._warnings = []
        self._infos = []

        # V-001: Leggi il file e verifica magic bytes
        try:
            reader = ODXReader(self.path)
        except ValueError as e:
            self._err("V-001", str(e))
            return self._build_report()
        except FileNotFoundError:
            self._err("V-000", f"File non trovato: {self.path}")
            return self._build_report()

        # V-002: Versione formato riconosciuta
        version = reader._header["version_str"]
        if version not in ("0.1",):
            self._err("V-002",
                f"Versione formato non riconosciuta: {version}. "
                "Questa implementazione supporta: 0.1"
            )

        # V-003: Layer /meta presente
        if not reader.has_segment(SEG_META):
            self._err("V-003", "Layer /meta obbligatorio non trovato.")
        else:
            try:
                meta = reader.get_meta()

                # V-005: lang presente
                if not meta.get("lang"):
                    self._err("V-005",
                        "meta.lang obbligatorio non presente. "
                        "Ogni documento ODX deve dichiarare la sua lingua (BCP 47)."
                    )

                # Controlla campi obbligatori
                for field in ("odx_version", "uuid", "created_at", "title"):
                    if not meta.get(field):
                        self._err("V-003",
                            f"Campo obbligatorio meta.{field} mancante."
                        )

            except Exception as e:
                self._err("V-003", f"Layer /meta non leggibile: {e}")

        # V-004: Layer /semantic presente
        if not reader.has_segment(SEG_SEMANTIC):
            self._err("V-004",
                "Layer /semantic obbligatorio non trovato. "
                "Ogni documento ODX deve avere una struttura semantica."
            )
        else:
            xml_data = reader.get_semantic_xml()
            if xml_data:
                self._validate_semantic_xml(xml_data)

        # V-008: page_count coerente
        if reader.has_segment(SEG_META) and reader.has_segment(SEG_ASSETS):
            meta = reader.get_meta()
            # (in questa implementazione base non verifichiamo gli assets)
            self._info("V-008", "Verifica page_count vs assets: N/A in v0.1 base")

        # V-009: Flag OCR coerente con presenza segmento
        if reader._header.get("flag_has_ocr") and not reader.has_segment(SEG_OCR):
            self._warn("V-009",
                "Flag HAS_OCR_LAYER attivo ma segmento /ocr non trovato."
            )

        # V-010: Integrità hash
        try:
            integrity = reader.verify_integrity()
            for seg_name, result in integrity["segments"].items():
                if not result["ok"]:
                    if result["status"] == "HASH_MISMATCH":
                        self._warn("V-010",
                            f"Hash integrità non corrispondente per segmento /{seg_name}. "
                            "Il segmento potrebbe essere stato modificato."
                        )
        except Exception as e:
            self._warn("V-010", f"Impossibile verificare integrità: {e}")

        return self._build_report()

    def _validate_semantic_xml(self, xml_data: bytes):
        """Valida la struttura XML del layer /semantic."""
        try:
            if LXML_AVAILABLE:
                root = etree.fromstring(xml_data)
                ODX_NS = "https://odx-format.org/ns/semantic/0.1"

                # V-006: Ogni immagine ha alt text
                images = root.findall(f".//{{{ODX_NS}}}image")
                for img in images:
                    alt = img.get("alt", "").strip()
                    if not alt:
                        self._err("V-006",
                            f"Immagine con asset-ref='{img.get('asset-ref', '?')}' "
                            "senza attributo alt. Obbligatorio per accessibilità."
                        )

                # V-007: Ogni figure ha caption o aria-label
                figures = root.findall(f".//{{{ODX_NS}}}figure")
                for fig in figures:
                    has_caption = fig.find(f"{{{ODX_NS}}}caption") is not None
                    has_aria = fig.get("aria-label") or fig.get("aria-describedby")
                    if not (has_caption or has_aria):
                        self._warn("V-007",
                            f"Figure id='{fig.get('id', '?')}' senza caption "
                            "né aria-label. Aggiungere per accessibilità completa."
                        )
            else:
                self._info("V-004",
                    "lxml non disponibile: validazione XML parziale. "
                    "Installa lxml per controlli completi: pip install lxml"
                )
        except Exception as e:
            self._err("V-004", f"Layer /semantic XML non valido: {e}")

    def _build_report(self) -> dict:
        is_valid = len(self._errors) == 0
        return {
            "valid": is_valid,
            "errors": self._errors,
            "warnings": self._warnings,
            "infos": self._infos,
            "summary": (
                f"{'✅ VALIDO' if is_valid else '❌ NON VALIDO'} — "
                f"{len(self._errors)} errori, "
                f"{len(self._warnings)} avvisi, "
                f"{len(self._infos)} info"
            )
        }

    def print_report(self):
        """Stampa il report di validazione in modo leggibile."""
        report = self.validate()
        print(f"\n{'='*60}")
        print(f"  VALIDAZIONE ODX: {self.path}")
        print(f"{'='*60}")
        print(f"  {report['summary']}")
        print()

        if report["errors"]:
            print("  ERRORI CRITICI:")
            for e in report["errors"]:
                print(f"    {e}")

        if report["warnings"]:
            print("  AVVISI:")
            for w in report["warnings"]:
                print(f"    {w}")

        if report["infos"]:
            print("  INFO:")
            for i in report["infos"]:
                print(f"    {i}")

        print(f"{'='*60}\n")
        return report["valid"]
